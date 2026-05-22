from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import yaml
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import GaussianNB
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from xgboost import XGBClassifier


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train/eval tabular baselines using YAML-defined plausibility features."
    )
    parser.add_argument("--config", type=Path, required=True, help="Path to LLM YAML config.")
    parser.add_argument(
        "--model",
        type=str,
        default="both",
        choices=[
            "lr",
            "gnb",
            "rf",
            "xgb",
            "lgbm",
            "mlp",
            "svm",
            "both",
            "all",
            "all_wo_svm",
        ],
        help="Which baseline to run.",
    )
    parser.add_argument(
        "--eval_split",
        type=str,
        default="test",
        choices=["val", "test"],
        help="Which split to evaluate on.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output_json", type=Path, default=None, help="Optional output JSON path.")
    parser.add_argument("--run_id", type=str, default="")
    return parser.parse_args()


def _safe_div(num: float, den: float) -> float:
    return float(num / den) if den else 0.0


def aggregate_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, Any]:
    yt = y_true.astype(int)
    yp = y_pred.astype(int)
    tp = int(np.sum((yt == 1) & (yp == 1)))
    fp = int(np.sum((yt == 0) & (yp == 1)))
    fn = int(np.sum((yt == 1) & (yp == 0)))
    tn = int(np.sum((yt == 0) & (yp == 0)))
    prec = _safe_div(tp, tp + fp)
    rec = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * prec * rec, prec + rec)
    acc = _safe_div(tp + tn, tp + fp + fn + tn)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "accuracy": acc,
        "precision": prec,
        "recall": rec,
        "f1": f1,
    }


def load_config(path: Path) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"Invalid config YAML: {path}")
    return cfg


def resolve_parquet_dir(cfg: Dict[str, Any], config_path: Path) -> Path:
    raw = str(cfg.get("parquet_dir", "")).strip()
    if not raw:
        raise ValueError("Missing parquet_dir in config.")
    p = Path(raw)
    if p.is_absolute():
        return p
    return (config_path.parent / p).resolve()


def load_split_df(parquet_dir: Path, split: str, limit: int) -> pd.DataFrame:
    path = parquet_dir / f"{split}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing split parquet: {path}")
    df = pd.read_parquet(path)
    if limit > 0 and len(df) > limit:
        df = df.sample(n=limit, random_state=42).reset_index(drop=True)
    return df


def validate_features(df_map: Dict[str, pd.DataFrame], feature_cols: List[str]) -> None:
    missing: Dict[str, List[str]] = {}
    for split, df in df_map.items():
        miss = [c for c in feature_cols if c not in df.columns]
        if miss:
            missing[split] = miss
    if missing:
        msg = "; ".join(
            f"{split}: {', '.join(cols[:15])}{' ...' if len(cols) > 15 else ''}"
            for split, cols in missing.items()
        )
        raise ValueError(
            "Feature columns from YAML prompt_include_columns missing in parquet splits. "
            f"Details -> {msg}"
        )


def build_xy(df: pd.DataFrame, feature_cols: List[str]) -> Tuple[np.ndarray, np.ndarray]:
    if "label" not in df.columns:
        raise ValueError("label column not found in split parquet.")
    x_df = df[feature_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    x = x_df.to_numpy(dtype=np.float32)
    y = df["label"].astype(int).to_numpy()
    return x, y


def train_xgb(x_train: np.ndarray, y_train: np.ndarray, seed: int) -> XGBClassifier:
    model = XGBClassifier(
        n_estimators=600,
        learning_rate=0.05,
        max_depth=6,
        min_child_weight=1.0,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="binary:logistic",
        eval_metric="logloss",
        tree_method="hist",
        random_state=seed,
        n_jobs=8,
    )
    model.fit(x_train, y_train)
    return model


def train_mlp(x_train: np.ndarray, y_train: np.ndarray, seed: int) -> MLPClassifier:
    model = MLPClassifier(
        hidden_layer_sizes=(256, 128),
        activation="relu",
        alpha=1e-4,
        learning_rate_init=1e-3,
        batch_size=512,
        max_iter=200,
        early_stopping=True,
        validation_fraction=0.1,
        random_state=seed,
    )
    model.fit(x_train, y_train)
    return model


def train_lr(x_train: np.ndarray, y_train: np.ndarray, seed: int) -> Any:
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            solver="lbfgs",
            max_iter=2000,
            random_state=seed,
            n_jobs=8,
        ),
    )
    model.fit(x_train, y_train)
    return model


def train_gnb(x_train: np.ndarray, y_train: np.ndarray, seed: int) -> Any:
    _ = seed  # Keep signature consistent.
    model = GaussianNB()
    model.fit(x_train, y_train)
    return model


def train_rf(x_train: np.ndarray, y_train: np.ndarray, seed: int) -> Any:
    model = RandomForestClassifier(
        n_estimators=500,
        max_depth=None,
        min_samples_leaf=1,
        random_state=seed,
        n_jobs=8,
    )
    model.fit(x_train, y_train)
    return model


def train_lgbm(x_train: np.ndarray, y_train: np.ndarray, seed: int) -> Any:
    try:
        from lightgbm import LGBMClassifier
    except Exception as exc:
        raise RuntimeError(
            "lightgbm is required for --model lgbm/all but is not installed. "
            "Install with: pip install lightgbm"
        ) from exc

    model = LGBMClassifier(
        n_estimators=700,
        learning_rate=0.05,
        num_leaves=31,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="binary",
        random_state=seed,
        n_jobs=8,
    )
    model.fit(x_train, y_train)
    return model


def train_svm(x_train: np.ndarray, y_train: np.ndarray, seed: int) -> Any:
    # SVM benefits from standardized feature scales.
    model = make_pipeline(
        StandardScaler(),
        SVC(
            C=1.0,
            kernel="rbf",
            gamma="scale",
            probability=False,
            random_state=seed,
        ),
    )
    model.fit(x_train, y_train)
    return model


def evaluate_model(model: Any, x_eval: np.ndarray, y_eval: np.ndarray) -> Dict[str, Any]:
    t0 = time.time()
    y_pred = model.predict(x_eval)
    wall_s = float(time.time() - t0)
    metrics = aggregate_metrics(y_eval, y_pred)
    n = int(len(y_eval))
    metrics.update(
        {
            "n": n,
            "wall_s": wall_s,
            "latency_e2e_ms": _safe_div(1000.0 * wall_s, n),
            "throughput_sps": _safe_div(n, wall_s),
        }
    )
    return metrics


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    parquet_dir = resolve_parquet_dir(cfg, args.config)
    feature_cols = list(cfg.get("prompt_include_columns", []) or [])
    if not feature_cols:
        raise ValueError("prompt_include_columns is empty in config; cannot build tabular baseline.")

    max_train_rows = int(cfg.get("max_train_rows", 0) or 0)
    max_val_rows = int(cfg.get("max_val_rows", 0) or 0)
    max_test_rows = int(cfg.get("max_test_rows", 0) or 0)
    limits = {"train": max_train_rows, "val": max_val_rows, "test": max_test_rows}

    train_df = load_split_df(parquet_dir, "train", limits["train"])
    val_df = load_split_df(parquet_dir, "val", limits["val"])
    test_df = load_split_df(parquet_dir, "test", limits["test"])
    split_map = {"train": train_df, "val": val_df, "test": test_df}
    validate_features(split_map, feature_cols)

    x_train, y_train = build_xy(train_df, feature_cols)
    x_eval, y_eval = build_xy(split_map[args.eval_split], feature_cols)

    run_id = args.run_id.strip() or time.strftime("%Y%m%d-%H%M%S")
    if args.model == "both":
        models = ["xgb", "mlp"]
    elif args.model == "all":
        models = ["lr", "gnb", "rf", "xgb", "lgbm", "mlp", "svm"]
    elif args.model == "all_wo_svm":
        models = ["lr", "gnb", "rf", "xgb", "lgbm", "mlp"]
    else:
        models = [args.model]
    all_results: Dict[str, Any] = {
        "run_id": run_id,
        "config": str(args.config),
        "parquet_dir": str(parquet_dir),
        "eval_split": args.eval_split,
        "feature_count": len(feature_cols),
        "models": {},
    }

    for model_name in models:
        print(f"[baseline] training {model_name} ...", flush=True)
        if model_name == "lr":
            model = train_lr(x_train, y_train, seed=args.seed)
        elif model_name == "gnb":
            model = train_gnb(x_train, y_train, seed=args.seed)
        elif model_name == "rf":
            model = train_rf(x_train, y_train, seed=args.seed)
        elif model_name == "xgb":
            model = train_xgb(x_train, y_train, seed=args.seed)
        elif model_name == "lgbm":
            model = train_lgbm(x_train, y_train, seed=args.seed)
        elif model_name == "mlp":
            model = train_mlp(x_train, y_train, seed=args.seed)
        else:
            model = train_svm(x_train, y_train, seed=args.seed)
        result = evaluate_model(model, x_eval, y_eval)
        result.update({"model_tag": model_name, "split": args.eval_split})
        all_results["models"][model_name] = result
        print("METRICS_JSON:", json.dumps(result, ensure_ascii=False), flush=True)

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[baseline] wrote summary: {args.output_json}", flush=True)


if __name__ == "__main__":
    main()
