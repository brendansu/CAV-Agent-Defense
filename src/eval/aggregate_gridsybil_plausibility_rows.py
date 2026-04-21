"""Aggregate GridSybil plausibility per-row logs (jsonl/jsonl.gz) across shards.

Typical usage:
python -m src.eval.aggregate_gridsybil_plausibility_rows ^
  --input_glob "outputs/eval_logs/plausibility_100k/*.jsonl.gz" ^
  --run_id 20260417-120000_100k ^
  --model_tag lora ^
  --split test ^
  --merged_out_jsonl outputs/eval_integration_plausibility/rows_20260417_lora_test.jsonl ^
  --overall_out_json outputs/eval_integration_plausibility/overall_rows_20260417_lora_test.json ^
  --bucket_out_csv outputs/eval_integration_plausibility/buckets_20260417_lora_test.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, TextIO, Tuple


DEFAULT_ANALYSIS_KEYS: Tuple[str, ...] = (
    "run_stem",
    "gold_label",
    "error_type",
    "parse_ok",
    "prompt_too_long",
)

DEFAULT_ACTUAL_FEATURE_KEYS_OLD_EVAL: Tuple[str, ...] = (
    # D
    "msg_has_prev_same_pseudo",
    "msg_dt_prev_same_pseudo",
    "msg_valid_history_features",
    # E-final-v1
    "msg_catch_art",
    "msg_catch_freq",
    "msg_catch_int_min_neighbor",
    "msg_catch_int_n_violations",
    "msg_catch_mgtd",
    "msg_catch_mgts",
    "msg_catch_mgtsv",
    "msg_catch_mgtsvm",
    "msg_catch_phc",
    # F-v1
    "ctx_n_neighbors",
    "ctx_dist_min",
    "ctx_dist_mean",
    "ctx_n_close_5m",
    "ctx_n_close_10m",
    "ctx_speed_diff_mean",
    "ctx_head_diff_mean_deg",
    "ctx_n_speed_diff_lt_0p5",
    "ctx_n_head_diff_lt_5deg",
    "ctx_n_triplet_similar",
    "ctx_triplet_ratio",
)

DEFAULT_BUCKET_KEYS: Tuple[str, ...] = (
    *DEFAULT_ANALYSIS_KEYS,
    *DEFAULT_ACTUAL_FEATURE_KEYS_OLD_EVAL,
)

BUCKET_PRESETS: Dict[str, Tuple[str, ...]] = {
    "old_eval_actual": DEFAULT_BUCKET_KEYS,
    "analysis_only": DEFAULT_ANALYSIS_KEYS,
}

BINARY_BUCKET_KEYS = {
    "gold_label",
    "parse_ok",
    "prompt_too_long",
    "msg_has_prev_same_pseudo",
    "msg_valid_history_features",
}

COUNT_BUCKET_SPECS: Dict[str, List[Tuple[int, str]]] = {
    "ctx_n_neighbors": [(0, "0"), (1, "1"), (3, "2-3"), (7, "4-7")],
    "msg_catch_int_n_violations": [(0, "0"), (1, "1"), (3, "2-3"), (7, "4-7")],
    "ctx_n_close_5m": [(0, "0"), (1, "1"), (3, "2-3"), (7, "4-7")],
    "ctx_n_close_10m": [(0, "0"), (1, "1"), (3, "2-3"), (7, "4-7")],
    "ctx_n_speed_diff_lt_0p5": [(0, "0"), (1, "1"), (3, "2-3"), (7, "4-7")],
    "ctx_n_head_diff_lt_5deg": [(0, "0"), (1, "1"), (3, "2-3"), (7, "4-7")],
    "ctx_n_triplet_similar": [(0, "0"), (1, "1"), (3, "2-3"), (7, "4-7")],
}

SCORE_BUCKET_KEYS = {
    "msg_catch_art",
    "msg_catch_freq",
    "msg_catch_int_min_neighbor",
    "msg_catch_mgtd",
    "msg_catch_mgts",
    "msg_catch_mgtsv",
    "msg_catch_mgtsvm",
    "msg_catch_phc",
    "ctx_triplet_ratio",
}

POS_CONT_BUCKET_GT1_EDGES: Dict[str, List[float]] = {
    "msg_dt_prev_same_pseudo": [2.0, 5.0, 10.0],
    "ctx_dist_min": [5.0, 10.0, 20.0, 50.0],
    "ctx_dist_mean": [5.0, 10.0, 20.0, 50.0],
    "ctx_speed_diff_mean": [2.0, 5.0, 10.0],
    "ctx_head_diff_mean_deg": [5.0, 10.0, 30.0, 90.0, 180.0],
}


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def binary_metrics(tp: int, fp: int, fn: int, tn: int) -> Dict[str, float]:
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    f1 = safe_div(2 * precision * recall, precision + recall)
    accuracy = safe_div(tp + tn, tp + fp + fn + tn)
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Aggregate GridSybil plausibility per-row logs and bucket stats."
    )
    p.add_argument(
        "--input_glob",
        type=str,
        default=None,
        help="Glob for row log files (.jsonl / .jsonl.gz).",
    )
    p.add_argument(
        "--input_paths_file",
        type=str,
        default=None,
        help="Optional text file: one row-log path per line.",
    )
    p.add_argument("--run_id", type=str, default=None)
    p.add_argument("--model_tag", type=str, default=None, choices=["base", "lora"])
    p.add_argument("--split", type=str, default=None, choices=["train", "val", "test"])
    p.add_argument("--merged_out_jsonl", type=str, default=None)
    p.add_argument("--merged_out_parquet", type=str, default=None)
    p.add_argument("--overall_out_json", type=str, required=True)
    p.add_argument("--bucket_out_csv", type=str, required=True)
    p.add_argument("--bucket_out_json", type=str, default=None)
    p.add_argument(
        "--bucket_preset",
        type=str,
        default="old_eval_actual",
        choices=["old_eval_actual", "analysis_only"],
        help=(
            "Bucket preset. old_eval_actual=analysis keys + old eval D/E/F features. "
            "analysis_only=run/meta diagnostics only."
        ),
    )
    p.add_argument(
        "--bucket_keys",
        type=str,
        nargs="+",
        default=None,
        help="Optional explicit bucket keys. Overrides --bucket_preset.",
    )
    p.add_argument(
        "--metrics_json_glob",
        type=str,
        default=None,
        help="Optional glob for slurm logs that contain METRICS_JSON lines (for alignment check).",
    )
    p.add_argument(
        "--alignment_tol",
        type=float,
        default=1e-12,
        help="Numeric tolerance for metric alignment checks.",
    )
    return p.parse_args()


def read_lines(path: Path) -> Iterable[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line
    else:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                yield line


def resolve_input_paths(input_glob: str | None, input_paths_file: str | None) -> List[Path]:
    paths: List[Path] = []
    if input_glob:
        paths.extend(Path(p) for p in glob.glob(input_glob))
    if input_paths_file:
        src = Path(input_paths_file)
        if not src.exists():
            raise FileNotFoundError(f"input_paths_file not found: {src}")
        for line in src.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            paths.append(Path(s))
    unique = sorted({str(p): p for p in paths}.values(), key=lambda p: str(p))
    if not unique:
        raise ValueError("No input files found. Provide --input_glob and/or --input_paths_file.")
    missing = [str(p) for p in unique if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Some input files do not exist: {missing[:5]}")
    return unique


def maybe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def int01(v: Any) -> int:
    try:
        return 1 if int(v) == 1 else 0
    except Exception:
        return 0


def run_stem_from_episode_id(episode_id: Any) -> str | None:
    if episode_id is None:
        return None
    s = str(episode_id).strip()
    if not s:
        return None
    return s.replace("/", "_")


def dedupe_keep_order(keys: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for k in keys:
        if k not in seen:
            out.append(k)
            seen.add(k)
    return out


def resolved_bucket_keys(args: argparse.Namespace) -> List[str]:
    if args.bucket_keys:
        return dedupe_keep_order([str(k) for k in args.bucket_keys])
    return dedupe_keep_order(list(BUCKET_PRESETS[args.bucket_preset]))


def maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _is_close(x: float, y: float, eps: float = 1e-12) -> bool:
    return abs(x - y) <= eps


def bucket_binary(value: Any) -> str:
    if value is None:
        return "null"
    iv = maybe_int(value)
    if iv is None:
        return "null"
    if iv == 0:
        return "0"
    if iv == 1:
        return "1"
    return "other"


def bucket_count(value: Any, spec: List[Tuple[int, str]]) -> str:
    iv = maybe_int(value)
    if iv is None:
        return "null"
    if iv < 0:
        return "<0"
    for upper, label in spec:
        if iv <= upper:
            return label
    return "8+"


def bucket_score_01(value: Any) -> str:
    x = maybe_float(value)
    if x is None:
        return "null"
    if x < 0:
        return "<0"
    if x > 1:
        return ">1"
    if _is_close(x, 0.0):
        return "0"
    if _is_close(x, 1.0):
        return "1"
    if x <= 0.2:
        return "(0,0.2]"
    if x <= 0.5:
        return "(0.2,0.5]"
    if x <= 0.8:
        return "(0.5,0.8]"
    if x <= 0.95:
        return "(0.8,0.95]"
    return "(0.95,1)"


def bucket_positive_continuous(value: Any, gt1_edges: List[float]) -> str:
    x = maybe_float(value)
    if x is None:
        return "null"
    if x < 0:
        return "<0"
    if _is_close(x, 0.0):
        return "0"
    if _is_close(x, 1.0):
        return "1"
    if 0 < x < 1:
        if x <= 0.2:
            return "(0,0.2]"
        if x <= 0.5:
            return "(0.2,0.5]"
        if x <= 0.8:
            return "(0.5,0.8]"
        return "(0.8,1)"
    prev = 1.0
    for edge in gt1_edges:
        if x <= edge:
            return f"({prev:g},{edge:g}]"
        prev = edge
    return f">{gt1_edges[-1]:g}" if gt1_edges else ">1"


def bucket_value(row: Dict[str, Any], key: str) -> str:
    value = row.get(key)
    if key in ("run_stem", "error_type"):
        return "null" if value is None else str(value)
    if key in BINARY_BUCKET_KEYS:
        return bucket_binary(value)
    if key in COUNT_BUCKET_SPECS:
        return bucket_count(value, COUNT_BUCKET_SPECS[key])
    if key in SCORE_BUCKET_KEYS:
        return bucket_score_01(value)
    if key in POS_CONT_BUCKET_GT1_EDGES:
        return bucket_positive_continuous(value, POS_CONT_BUCKET_GT1_EDGES[key])
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return bucket_positive_continuous(value, [2.0, 5.0, 10.0, 20.0])
    return str(value)


def filter_row(row: Dict[str, Any], run_id: str | None, model_tag: str | None, split: str | None) -> bool:
    if run_id is not None and str(row.get("run_id")) != run_id:
        return False
    if model_tag is not None and str(row.get("model_tag")) != model_tag:
        return False
    if split is not None and str(row.get("split")) != split:
        return False
    return True


@dataclass
class Agg:
    n_rows_total: int = 0
    parse_ok_rows: int = 0
    prompt_too_long_rows: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def update(self, row: Dict[str, Any]) -> None:
        self.n_rows_total += 1
        self.parse_ok_rows += int01(row.get("parse_ok"))
        self.prompt_too_long_rows += int01(row.get("prompt_too_long"))
        self.tp += int01(row.get("tp"))
        self.fp += int01(row.get("fp"))
        self.fn += int01(row.get("fn"))
        self.tn += int01(row.get("tn"))

    def to_summary(self) -> Dict[str, Any]:
        counted = self.tp + self.fp + self.fn + self.tn
        return {
            "n_rows_total": self.n_rows_total,
            "n_rows_counted_eval": counted,
            "parse_ok_rows": self.parse_ok_rows,
            "prompt_too_long_rows": self.prompt_too_long_rows,
            "parse_ok_rate": safe_div(self.parse_ok_rows, self.n_rows_total),
            "prompt_too_long_rate": safe_div(self.prompt_too_long_rows, self.n_rows_total),
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            **binary_metrics(self.tp, self.fp, self.fn, self.tn),
        }


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def parse_metrics_json_rows(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in read_lines(path):
        if not line.startswith("METRICS_JSON:"):
            continue
        payload = line.split("METRICS_JSON:", 1)[1].strip()
        out.append(json.loads(payload))
    return out


def compute_metrics_json_alignment(
    args: argparse.Namespace,
    overall: Dict[str, Any],
) -> Dict[str, Any]:
    if not args.metrics_json_glob:
        return {"enabled": False}
    paths = [Path(p) for p in sorted(glob.glob(args.metrics_json_glob))]
    if not paths:
        return {
            "enabled": True,
            "ok": False,
            "reason": f"No files matched metrics_json_glob={args.metrics_json_glob!r}",
        }

    agg = Agg()
    rows_used = 0
    for path in paths:
        for m in parse_metrics_json_rows(path):
            if args.model_tag is not None and str(m.get("model_tag")) != args.model_tag:
                continue
            if args.split is not None and str(m.get("split")) != args.split:
                continue
            rid = m.get("run_id")
            if args.run_id is not None and rid is not None and str(rid) != args.run_id:
                continue
            agg.n_rows_total += int(m.get("n", 0))
            agg.parse_ok_rows += int(m.get("parse_ok", 0))
            agg.prompt_too_long_rows += int(m.get("prompt_too_long", 0))
            agg.tp += int(m.get("tp", 0))
            agg.fp += int(m.get("fp", 0))
            agg.fn += int(m.get("fn", 0))
            agg.tn += int(m.get("tn", 0))
            rows_used += 1

    if rows_used == 0:
        return {
            "enabled": True,
            "ok": False,
            "reason": "No METRICS_JSON rows matched the target filters.",
            "metrics_json_files": len(paths),
        }

    ref = agg.to_summary()
    keys_exact = ("n_rows_total", "parse_ok_rows", "prompt_too_long_rows", "tp", "fp", "fn", "tn")
    keys_float = ("accuracy", "precision", "recall", "f1")
    exact_ok = all(int(overall[k]) == int(ref[k]) for k in keys_exact)
    float_ok = all(abs(float(overall[k]) - float(ref[k])) <= float(args.alignment_tol) for k in keys_float)
    return {
        "enabled": True,
        "ok": bool(exact_ok and float_ok),
        "metrics_json_glob": args.metrics_json_glob,
        "metrics_json_files": len(paths),
        "metrics_json_rows_used": rows_used,
        "check_exact_keys": {k: {"rows": int(overall[k]), "metrics_json": int(ref[k])} for k in keys_exact},
        "check_float_keys": {k: {"rows": float(overall[k]), "metrics_json": float(ref[k])} for k in keys_float},
        "alignment_tol": float(args.alignment_tol),
    }


def maybe_open_text_writer(path_str: str | None) -> TextIO | None:
    if path_str is None:
        return None
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8")


def normalize_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(raw)
    if row.get("run_stem") is None:
        row["run_stem"] = run_stem_from_episode_id(row.get("episode_id"))
    if row.get("gold_label") is None and row.get("label") is not None:
        row["gold_label"] = maybe_int(row.get("label"))
    row["pred_is_attack"] = int(maybe_int(row.get("predicted_label")) == 1)
    row["is_counted_eval_row"] = int(
        int01(row.get("tp")) + int01(row.get("fp")) + int01(row.get("fn")) + int01(row.get("tn")) > 0
    )
    return row


def maybe_write_parquet(path_str: str | None, rows: Sequence[Dict[str, Any]]) -> str | None:
    if path_str is None:
        return None
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd  # type: ignore

        pd.DataFrame(list(rows)).to_parquet(path, index=False)
        return str(path)
    except Exception:
        try:
            import pyarrow as pa  # type: ignore
            import pyarrow.parquet as pq  # type: ignore

            table = pa.Table.from_pylist(list(rows))
            pq.write_table(table, path)
            return str(path)
        except Exception as e:
            print(f"[warn] parquet output skipped: {e}", flush=True)
            return None


def main() -> None:
    args = parse_args()
    bucket_keys = resolved_bucket_keys(args)
    input_paths = resolve_input_paths(args.input_glob, args.input_paths_file)

    merged_fh = maybe_open_text_writer(args.merged_out_jsonl)
    rows_for_parquet: List[Dict[str, Any]] = []
    agg = Agg()
    bucket_aggs: Dict[Tuple[str, str], Agg] = defaultdict(Agg)
    shard_indices: set[int] = set()
    num_shards_values: set[int] = set()
    read_rows = 0
    kept_rows = 0

    try:
        for path in input_paths:
            for line in read_lines(path):
                s = line.strip()
                if not s:
                    continue
                read_rows += 1
                raw = json.loads(s)
                if not isinstance(raw, dict):
                    continue
                if not filter_row(raw, run_id=args.run_id, model_tag=args.model_tag, split=args.split):
                    continue
                row = normalize_row(raw)
                kept_rows += 1
                agg.update(row)

                si = maybe_int(row.get("shard_index"))
                ns = maybe_int(row.get("num_shards"))
                if si is not None:
                    shard_indices.add(si)
                if ns is not None:
                    num_shards_values.add(ns)

                for key in bucket_keys:
                    bucket_aggs[(key, bucket_value(row, key))].update(row)

                if merged_fh is not None:
                    merged_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                if args.merged_out_parquet:
                    rows_for_parquet.append(row)
    finally:
        if merged_fh is not None:
            merged_fh.flush()
            merged_fh.close()

    if kept_rows == 0:
        raise ValueError("No rows matched filters from input row logs.")

    overall = agg.to_summary()
    expected_num_shards = min(num_shards_values) if num_shards_values else None
    seen = sorted(shard_indices)
    shard_coverage_ok = (
        expected_num_shards is None or seen == list(range(expected_num_shards))
    )

    bucket_rows: List[Dict[str, Any]] = []
    for (key, val), b_agg in sorted(bucket_aggs.items(), key=lambda x: (x[0][0], x[0][1])):
        s = b_agg.to_summary()
        bucket_rows.append(
            {
                "bucket_key": key,
                "bucket_value": val,
                "n_rows_total": s["n_rows_total"],
                "n_rows_counted_eval": s["n_rows_counted_eval"],
                "parse_ok_rows": s["parse_ok_rows"],
                "prompt_too_long_rows": s["prompt_too_long_rows"],
                "tp": s["tp"],
                "fp": s["fp"],
                "fn": s["fn"],
                "tn": s["tn"],
                "accuracy": s["accuracy"],
                "precision": s["precision"],
                "recall": s["recall"],
                "f1": s["f1"],
            }
        )

    write_csv(Path(args.bucket_out_csv), bucket_rows)
    if args.bucket_out_json:
        write_json(Path(args.bucket_out_json), {"rows": bucket_rows})

    parquet_path = maybe_write_parquet(args.merged_out_parquet, rows_for_parquet)
    alignment = compute_metrics_json_alignment(args=args, overall=overall)

    summary = {
        "run_id_filter": args.run_id,
        "model_tag_filter": args.model_tag,
        "split_filter": args.split,
        "input_glob": args.input_glob,
        "input_paths_file": args.input_paths_file,
        "input_files_used": [str(p) for p in input_paths],
        "input_files_count": len(input_paths),
        "rows_read": read_rows,
        "rows_kept": kept_rows,
        "bucket_preset": args.bucket_preset,
        "bucket_keys": list(bucket_keys),
        "bucket_rows": len(bucket_rows),
        "num_shards_values_seen": sorted(num_shards_values),
        "seen_shard_indices": seen,
        "shard_coverage_ok": shard_coverage_ok,
        "merged_out_jsonl": args.merged_out_jsonl,
        "merged_out_parquet": parquet_path,
        "bucket_out_csv": str(Path(args.bucket_out_csv)),
        "bucket_out_json": args.bucket_out_json,
        "metrics_alignment": alignment,
        **overall,
    }
    write_json(Path(args.overall_out_json), summary)

    print("=== GridSybil Plausibility Row Aggregation ===", flush=True)
    print(
        json.dumps(
            {
                "rows_kept": kept_rows,
                "accuracy": summary["accuracy"],
                "precision": summary["precision"],
                "recall": summary["recall"],
                "f1": summary["f1"],
                "prompt_too_long_rows": summary["prompt_too_long_rows"],
                "shard_coverage_ok": summary["shard_coverage_ok"],
                "metrics_alignment_ok": summary["metrics_alignment"].get("ok", None),
                "overall_out_json": str(Path(args.overall_out_json)),
                "bucket_out_csv": str(Path(args.bucket_out_csv)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
