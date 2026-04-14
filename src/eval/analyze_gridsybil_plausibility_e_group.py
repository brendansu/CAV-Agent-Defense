from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd


E_FEATURES: List[str] = [
    "msg_catch_art",
    "msg_catch_maxs",
    "msg_catch_mgtd",
    "msg_catch_mgts",
    "msg_catch_mgtsv",
    "msg_catch_mgtsvm",
    "msg_catch_freq",
    "msg_catch_phc",
    "msg_catch_saw",
    "msg_catch_rad",
    "msg_catch_int_self",
    "msg_catch_int_min_neighbor",
    "msg_catch_int_n_violations",
    "msg_catch_map",
]


@dataclass
class LabelStats:
    n: int
    non_null_n: int
    null_rate: float
    eq1_rate_non_null: float
    eq0_rate_non_null: float
    mean: float | None
    std: float | None
    min: float | None
    q01: float | None
    q05: float | None
    q10: float | None
    q25: float | None
    q50: float | None
    q75: float | None
    q90: float | None
    q95: float | None
    q99: float | None
    max: float | None
    leq_095_rate_non_null: float
    leq_090_rate_non_null: float
    leq_080_rate_non_null: float
    leq_050_rate_non_null: float


def _safe_float(v: Any) -> float | None:
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _quantile(s: pd.Series, q: float) -> float | None:
    if len(s) == 0:
        return None
    return _safe_float(s.quantile(q))


def compute_label_stats(s: pd.Series) -> LabelStats:
    n = int(len(s))
    sn = s.dropna()
    non_null_n = int(len(sn))
    null_rate = float((n - non_null_n) / n) if n > 0 else 0.0
    if non_null_n == 0:
        return LabelStats(
            n=n,
            non_null_n=non_null_n,
            null_rate=null_rate,
            eq1_rate_non_null=0.0,
            eq0_rate_non_null=0.0,
            mean=None,
            std=None,
            min=None,
            q01=None,
            q05=None,
            q10=None,
            q25=None,
            q50=None,
            q75=None,
            q90=None,
            q95=None,
            q99=None,
            max=None,
            leq_095_rate_non_null=0.0,
            leq_090_rate_non_null=0.0,
            leq_080_rate_non_null=0.0,
            leq_050_rate_non_null=0.0,
        )

    eq1 = float((sn == 1.0).mean())
    eq0 = float((sn == 0.0).mean())
    return LabelStats(
        n=n,
        non_null_n=non_null_n,
        null_rate=null_rate,
        eq1_rate_non_null=eq1,
        eq0_rate_non_null=eq0,
        mean=_safe_float(sn.mean()),
        std=_safe_float(sn.std()),
        min=_safe_float(sn.min()),
        q01=_quantile(sn, 0.01),
        q05=_quantile(sn, 0.05),
        q10=_quantile(sn, 0.10),
        q25=_quantile(sn, 0.25),
        q50=_quantile(sn, 0.50),
        q75=_quantile(sn, 0.75),
        q90=_quantile(sn, 0.90),
        q95=_quantile(sn, 0.95),
        q99=_quantile(sn, 0.99),
        max=_safe_float(sn.max()),
        leq_095_rate_non_null=float((sn <= 0.95).mean()),
        leq_090_rate_non_null=float((sn <= 0.90).mean()),
        leq_080_rate_non_null=float((sn <= 0.80).mean()),
        leq_050_rate_non_null=float((sn <= 0.50).mean()),
    )


def recommendation(global_stats: LabelStats, s0: LabelStats, s1: LabelStats) -> str:
    tail_gap_090 = s1.leq_090_rate_non_null - s0.leq_090_rate_non_null
    tail_gap_080 = s1.leq_080_rate_non_null - s0.leq_080_rate_non_null
    if global_stats.non_null_n == 0:
        return "drop_candidate_all_null"
    if global_stats.null_rate >= 0.95:
        return "drop_candidate_extremely_sparse"
    if global_stats.eq1_rate_non_null >= 0.98 and abs(tail_gap_090) < 0.01:
        return "drop_candidate_near_constant_1"
    if abs(tail_gap_080) >= 0.03 or abs(tail_gap_090) >= 0.05:
        return "keep_candidate_tail_separation"
    if abs((s1.mean or 0.0) - (s0.mean or 0.0)) >= 0.02:
        return "keep_candidate_mean_gap"
    return "review_candidate_weak_signal"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyze E-group (msg_catch_*) feature distributions by label."
    )
    p.add_argument(
        "--parquet",
        type=str,
        default="data/processed/plausibility_messages_split/train.parquet",
        help="Path to split parquet (recommend train split).",
    )
    p.add_argument(
        "--label-col",
        type=str,
        default="label",
        help="Binary label column name (0/1).",
    )
    p.add_argument(
        "--sample-rows",
        type=int,
        default=0,
        help="Optional random sample size for faster smoke. 0 = full parquet.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for optional sampling.",
    )
    p.add_argument(
        "--output-dir",
        type=str,
        default="outputs/feature_audit",
        help="Directory for analysis outputs.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    parquet_path = Path(args.parquet)
    if not parquet_path.exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet_path}")

    cols = [args.label_col, *E_FEATURES]
    print(f"[load] reading columns from {parquet_path}")
    df = pd.read_parquet(parquet_path, columns=cols)
    print(f"[load] rows={len(df)}")
    if args.sample_rows > 0 and args.sample_rows < len(df):
        df = df.sample(n=args.sample_rows, random_state=args.seed).reset_index(drop=True)
        print(f"[sample] sampled rows={len(df)} seed={args.seed}")

    if args.label_col not in df.columns:
        raise ValueError(f"Missing label column: {args.label_col}")
    labels = set(pd.Series(df[args.label_col]).dropna().astype(int).unique().tolist())
    if not labels.issubset({0, 1}):
        raise ValueError(f"Label values must be subset of {{0,1}}, got={sorted(labels)}")

    rows: List[Dict[str, Any]] = []
    details: Dict[str, Any] = {
        "parquet": str(parquet_path),
        "label_col": args.label_col,
        "rows_used": int(len(df)),
        "sample_rows": int(args.sample_rows),
        "seed": int(args.seed),
        "features": {},
    }

    df0 = df[df[args.label_col] == 0]
    df1 = df[df[args.label_col] == 1]
    print(f"[label] n0={len(df0)} n1={len(df1)}")

    for feat in E_FEATURES:
        s = pd.to_numeric(df[feat], errors="coerce")
        s0 = pd.to_numeric(df0[feat], errors="coerce")
        s1 = pd.to_numeric(df1[feat], errors="coerce")

        g = compute_label_stats(s)
        st0 = compute_label_stats(s0)
        st1 = compute_label_stats(s1)
        rec = recommendation(g, st0, st1)

        row = {
            "feature": feat,
            "recommendation": rec,
            "global_null_rate": g.null_rate,
            "global_eq1_rate_non_null": g.eq1_rate_non_null,
            "global_eq0_rate_non_null": g.eq0_rate_non_null,
            "global_mean": g.mean,
            "global_q50": g.q50,
            "global_q90": g.q90,
            "global_q99": g.q99,
            "n0": st0.n,
            "n1": st1.n,
            "null_rate_0": st0.null_rate,
            "null_rate_1": st1.null_rate,
            "mean_0": st0.mean,
            "mean_1": st1.mean,
            "mean_gap_1_minus_0": (st1.mean or 0.0) - (st0.mean or 0.0),
            "leq_090_rate_0": st0.leq_090_rate_non_null,
            "leq_090_rate_1": st1.leq_090_rate_non_null,
            "leq_090_gap_1_minus_0": st1.leq_090_rate_non_null - st0.leq_090_rate_non_null,
            "leq_080_rate_0": st0.leq_080_rate_non_null,
            "leq_080_rate_1": st1.leq_080_rate_non_null,
            "leq_080_gap_1_minus_0": st1.leq_080_rate_non_null - st0.leq_080_rate_non_null,
            "non_null_n_global": g.non_null_n,
        }
        rows.append(row)
        details["features"][feat] = {
            "recommendation": rec,
            "global": g.__dict__,
            "label0": st0.__dict__,
            "label1": st1.__dict__,
        }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = parquet_path.stem
    csv_path = out_dir / f"plausibility_e_group_summary_{stem}.csv"
    json_path = out_dir / f"plausibility_e_group_details_{stem}.json"
    pd.DataFrame(rows).sort_values(by=["recommendation", "feature"]).to_csv(
        csv_path, index=False
    )
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False, indent=2)

    print(f"[done] summary csv: {csv_path}")
    print(f"[done] details json: {json_path}")
    print("[top] quick view:")
    quick = (
        pd.DataFrame(rows)
        .loc[:, ["feature", "recommendation", "global_null_rate", "global_eq1_rate_non_null", "leq_090_gap_1_minus_0", "leq_080_gap_1_minus_0"]]
        .sort_values(by=["recommendation", "feature"])
    )
    print(quick.to_string(index=False))


if __name__ == "__main__":
    main()
