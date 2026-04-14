"""Integrate shard-level eval metrics for GridSybil plausibility classification.

Typical usage:
python -m src.eval.metric_integration_gridsybil_plausibility ^
  --run_id 20260414_plausibility_lora_test ^
  --model_tag lora ^
  --split test ^
  --slurm_pattern "slurm/eval_plausibility_lora_test_*.out" ^
  --output_dir outputs/eval_integration_plausibility
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


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


def read_lines(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            yield line


def parse_metrics_json_lines(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in read_lines(path):
        if line.startswith("METRICS_JSON:"):
            payload = line.split("METRICS_JSON:", 1)[1].strip()
            out.append(json.loads(payload))
    return out


@dataclass
class Agg:
    n: int = 0
    parse_ok: int = 0
    prompt_too_long: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def update(self, m: Dict[str, Any]) -> None:
        self.n += int(m.get("n", 0))
        self.parse_ok += int(m.get("parse_ok", 0))
        self.prompt_too_long += int(m.get("prompt_too_long", 0))
        self.tp += int(m.get("tp", 0))
        self.fp += int(m.get("fp", 0))
        self.fn += int(m.get("fn", 0))
        self.tn += int(m.get("tn", 0))

    def summary(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "parse_ok": self.parse_ok,
            "prompt_too_long": self.prompt_too_long,
            "parse_ok_rate": safe_div(self.parse_ok, self.n),
            "skip_rate": safe_div(self.prompt_too_long, self.n),
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Integrate shard eval metrics (METRICS_JSON) for plausibility binary classification."
    )
    p.add_argument("--run_id", type=str, required=True)
    p.add_argument("--model_tag", type=str, required=True, choices=["base", "lora"])
    p.add_argument("--split", type=str, required=True, choices=["train", "val", "test"])
    p.add_argument(
        "--slurm_pattern",
        type=str,
        required=True,
        help="Glob for slurm outputs that contain METRICS_JSON lines.",
    )
    p.add_argument(
        "--output_dir",
        type=str,
        default="outputs/eval_integration",
    )
    return p.parse_args()


def _matches_target(m: Dict[str, Any], run_id: str, model_tag: str, split: str) -> bool:
    if str(m.get("model_tag", "")) != model_tag:
        return False
    if str(m.get("split", "")) != split:
        return False
    # run_id may be absent in old logs; keep them only when absent or matched.
    v = m.get("run_id", None)
    if v is None:
        return True
    return str(v) == run_id


def main() -> None:
    args = parse_args()
    paths = [Path(p) for p in sorted(glob.glob(args.slurm_pattern))]
    if not paths:
        raise FileNotFoundError(f"No slurm files matched pattern: {args.slurm_pattern}")

    shard_rows: List[Dict[str, Any]] = []
    agg = Agg()

    for p in paths:
        lines = parse_metrics_json_lines(p)
        if not lines:
            continue
        for m in lines:
            if not _matches_target(m, args.run_id, args.model_tag, args.split):
                continue
            agg.update(m)
            shard_rows.append(
                {
                    "file": str(p),
                    "run_id": m.get("run_id"),
                    "model_tag": m.get("model_tag"),
                    "split": m.get("split"),
                    "num_shards": m.get("num_shards"),
                    "shard_index": m.get("shard_index"),
                    "n": m.get("n"),
                    "parse_ok": m.get("parse_ok"),
                    "prompt_too_long": m.get("prompt_too_long"),
                    "tp": m.get("tp"),
                    "fp": m.get("fp"),
                    "fn": m.get("fn"),
                    "tn": m.get("tn"),
                }
            )

    if not shard_rows:
        raise ValueError(
            "Matched slurm files but found no METRICS_JSON rows matching "
            f"run_id={args.run_id}, model_tag={args.model_tag}, split={args.split}"
        )

    seen_shards = sorted(
        {
            int(r["shard_index"])
            for r in shard_rows
            if r.get("shard_index") is not None
        }
    )
    expected_num_shards_values = sorted(
        {
            int(r["num_shards"])
            for r in shard_rows
            if r.get("num_shards") is not None
        }
    )
    expected_num_shards = expected_num_shards_values[0] if expected_num_shards_values else None
    shard_coverage_ok = (
        expected_num_shards is None
        or seen_shards == list(range(expected_num_shards))
    )

    summary = {
        "run_id": args.run_id,
        "model_tag": args.model_tag,
        "split": args.split,
        "slurm_pattern": args.slurm_pattern,
        "slurm_files_found": len(paths),
        "metrics_rows_used": len(shard_rows),
        "expected_num_shards_values": expected_num_shards_values,
        "seen_shard_indices": seen_shards,
        "shard_coverage_ok": shard_coverage_ok,
        **agg.summary(),
    }

    out_dir = Path(args.output_dir)
    summary_path = out_dir / f"overall_plausibility_{args.run_id}_{args.model_tag}_{args.split}.json"
    shard_csv_path = out_dir / f"shards_plausibility_{args.run_id}_{args.model_tag}_{args.split}.csv"
    write_json(summary_path, summary)
    write_csv(shard_csv_path, shard_rows)

    print("=== Plausibility Integrated Metrics ===")
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "model_tag": args.model_tag,
                "split": args.split,
                "accuracy": summary["accuracy"],
                "precision": summary["precision"],
                "recall": summary["recall"],
                "f1": summary["f1"],
                "parse_ok_rate": summary["parse_ok_rate"],
                "skip_rate": summary["skip_rate"],
                "shard_coverage_ok": summary["shard_coverage_ok"],
                "overall_json": str(summary_path),
                "shard_csv": str(shard_csv_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
