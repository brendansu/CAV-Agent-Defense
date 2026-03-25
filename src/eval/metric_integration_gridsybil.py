from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def micro_from_counts(tp: int, fp: int, fn: int, tn: int) -> Dict[str, float]:
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    f1 = safe_div(2 * precision * recall, precision + recall)
    accuracy = safe_div(tp + tn, tp + fp + fn + tn)
    return {
        "micro_accuracy": accuracy,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
    }


def candidate_bin(n: int) -> str:
    if n <= 8:
        return "less than 9"
    if n <= 16:
        return "9 to 16"
    if n <= 24:
        return "17 to 24"
    if n <= 32:
        return "25 to 32"
    return "more than 32"


def visible_attacker_bin(n: int) -> str:
    if n <= 1:
        return "0 to 1"
    if n == 4:
        return "2 to 4"
    if n <= 8:
        return "5 to 8"
    if n <= 14:
        return "9 to 14"
    return "more than 14"


def read_text_lines(path: Path) -> Iterable[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line
    else:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                yield line


def find_metrics_json(path: Path) -> Dict[str, Any] | None:
    for line in read_text_lines(path):
        if line.startswith("METRICS_JSON:"):
            payload = line.split("METRICS_JSON:", 1)[1].strip()
            return json.loads(payload)
    return None


@dataclass
class OverallAgg:
    n: int = 0
    prompt_too_long: int = 0
    parse_ok: int = 0
    pred_oob_episodes: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    episode_exact_match: int = 0
    pseudo_fact_rows: int = 0

    def update(self, row: Dict[str, Any]) -> None:
        self.n += int(row.get("n", 0))
        self.prompt_too_long += int(row.get("prompt_too_long", 0))
        self.parse_ok += int(row.get("parse_ok", 0))
        self.pred_oob_episodes += int(row.get("pred_oob_episodes", 0))
        self.tp += int(row.get("tp", 0))
        self.fp += int(row.get("fp", 0))
        self.fn += int(row.get("fn", 0))
        self.tn += int(row.get("tn", 0))
        self.episode_exact_match += int(row.get("episode_exact_match", 0))
        self.pseudo_fact_rows += int(row.get("pseudo_fact_rows", 0))

    def to_summary(self) -> Dict[str, Any]:
        micro = micro_from_counts(self.tp, self.fp, self.fn, self.tn)
        return {
            "n": self.n,
            "prompt_too_long": self.prompt_too_long,
            "parse_ok": self.parse_ok,
            "pred_oob_episodes": self.pred_oob_episodes,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "episode_exact_match": self.episode_exact_match,
            "pseudo_fact_rows": self.pseudo_fact_rows,
            "parse_ok_rate": safe_div(self.parse_ok, self.n),
            "oob_episode_rate": safe_div(self.pred_oob_episodes, self.n),
            "skip_rate": safe_div(self.prompt_too_long, self.n),
            "episode_exact_match_rate": safe_div(self.episode_exact_match, self.n),
            **micro,
        }


@dataclass
class BucketAgg:
    rows: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    parse_ok_sum: int = 0
    oob_sum: int = 0
    episode_ids: set[str] = None  # type: ignore[assignment]
    episode_exact_sum: int = 0
    episode_parse_sum: int = 0
    episode_oob_sum: int = 0

    def __post_init__(self) -> None:
        self.episode_ids = set()

    def update(self, row: Dict[str, Any]) -> None:
        self.rows += 1
        self.tp += int(row.get("tp", 0))
        self.fp += int(row.get("fp", 0))
        self.fn += int(row.get("fn", 0))
        self.tn += int(row.get("tn", 0))
        self.parse_ok_sum += int(row.get("parse_ok", 0))
        self.oob_sum += int(row.get("has_oob_pred", 0))

        episode_id = str(row.get("episode_id"))
        if episode_id not in self.episode_ids:
            self.episode_ids.add(episode_id)
            self.episode_exact_sum += int(row.get("is_exact_match", 0))
            self.episode_parse_sum += int(row.get("parse_ok", 0))
            self.episode_oob_sum += int(row.get("has_oob_pred", 0))

    def to_row(self, bucket_name: str, bucket_value: str) -> Dict[str, Any]:
        micro = micro_from_counts(self.tp, self.fp, self.fn, self.tn)
        n_ep = len(self.episode_ids)
        return {
            "bucket_name": bucket_name,
            "bucket_value": bucket_value,
            "rows": self.rows,
            "episodes": n_ep,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "parse_ok_rate": safe_div(self.parse_ok_sum, self.rows),
            "oob_row_rate": safe_div(self.oob_sum, self.rows),
            "episode_exact_match_rate": safe_div(self.episode_exact_sum, n_ep),
            "episode_parse_ok_rate": safe_div(self.episode_parse_sum, n_ep),
            "episode_oob_rate": safe_div(self.episode_oob_sum, n_ep),
            **micro,
        }


def aggregate_from_slurm(slurm_pattern: str) -> Tuple[Dict[str, Any], List[Path]]:
    paths = [Path(p) for p in sorted(glob.glob(slurm_pattern))]
    if not paths:
        raise FileNotFoundError(f"No slurm files matched pattern: {slurm_pattern}")

    agg = OverallAgg()
    used_paths: List[Path] = []
    for p in paths:
        m = find_metrics_json(p)
        if m is None:
            continue
        agg.update(m)
        used_paths.append(p)

    if not used_paths:
        raise ValueError(f"Matched files but found no METRICS_JSON lines: {slurm_pattern}")

    summary = agg.to_summary()
    summary["shard_files_found"] = len(paths)
    summary["shard_files_used"] = len(used_paths)
    return summary, used_paths


def aggregate_buckets_from_pseudo(pseudo_pattern: str) -> Tuple[List[Dict[str, Any]], List[Path]]:
    paths = [Path(p) for p in sorted(glob.glob(pseudo_pattern))]
    if not paths:
        raise FileNotFoundError(f"No pseudo fact files matched pattern: {pseudo_pattern}")

    traffic: Dict[str, BucketAgg] = {}
    cand: Dict[str, BucketAgg] = {}
    vis_att: Dict[str, BucketAgg] = {}

    for p in paths:
        for line in read_text_lines(p):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)

            traffic_key = str(row.get("traffic_regime", "null"))
            cand_key = candidate_bin(int(row.get("n_visible_candidates", 0)))
            vis_key = visible_attacker_bin(int(row.get("n_visible_attackers", 0)))

            traffic.setdefault(traffic_key, BucketAgg()).update(row)
            cand.setdefault(cand_key, BucketAgg()).update(row)
            vis_att.setdefault(vis_key, BucketAgg()).update(row)

    out_rows: List[Dict[str, Any]] = []
    for k, v in sorted(traffic.items()):
        out_rows.append(v.to_row("traffic_regime", k))
    for k, v in sorted(cand.items()):
        out_rows.append(v.to_row("n_visible_candidates_bin", k))
    for k, v in sorted(vis_att.items()):
        out_rows.append(v.to_row("n_visible_attackers_bin", k))
    return out_rows, paths


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write("")
        return
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Integrate GridSybil eval outputs for one (run_id, model_tag, split): "
            "slurm METRICS_JSON aggregation + pseudo_fact bucket aggregation."
        )
    )
    p.add_argument("--run_id", type=str, required=True)
    p.add_argument("--model_tag", type=str, required=True, choices=["base", "lora"])
    p.add_argument("--split", type=str, required=True, choices=["val", "test"])
    p.add_argument(
        "--slurm_pattern",
        type=str,
        default=None,
        help="Glob for slurm .out files. If omitted, inferred from run_id/model_tag.",
    )
    p.add_argument(
        "--pseudo_pattern",
        type=str,
        default=None,
        help="Glob for pseudo fact files. If omitted, inferred from run_id/model_tag/split.",
    )
    p.add_argument(
        "--output_dir",
        type=str,
        default="outputs/eval_integration",
        help="Directory for overall JSON and bucket CSV.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    run_id = args.run_id
    model_tag = args.model_tag
    split = args.split

    slurm_pattern = args.slurm_pattern or f"slurm/eval_gridsybil_{model_tag}_*.out"
    pseudo_pattern = (
        args.pseudo_pattern
        or f"outputs/eval_logs/pseudo_fact_{run_id}_{model_tag}_{split}_shard*.json*"
    )

    overall, slurm_used = aggregate_from_slurm(slurm_pattern)
    buckets, pseudo_used = aggregate_buckets_from_pseudo(pseudo_pattern)

    overall.update(
        {
            "run_id": run_id,
            "model_tag": model_tag,
            "split": split,
            "slurm_pattern": slurm_pattern,
            "pseudo_pattern": pseudo_pattern,
            "slurm_files_used": [str(p) for p in slurm_used],
            "pseudo_files_used": [str(p) for p in pseudo_used],
        }
    )

    out_dir = Path(args.output_dir)
    overall_path = out_dir / f"overall_{run_id}_{model_tag}_{split}.json"
    bucket_path = out_dir / f"bucket_{run_id}_{model_tag}_{split}.csv"
    write_json(overall_path, overall)
    write_csv(bucket_path, buckets)

    print("=== GridSybil Integrated Metrics ===")
    print(
        json.dumps(
            {
                "run_id": run_id,
                "model_tag": model_tag,
                "split": split,
                "micro_accuracy": overall["micro_accuracy"],
                "micro_precision": overall["micro_precision"],
                "micro_recall": overall["micro_recall"],
                "micro_f1": overall["micro_f1"],
                "episode_exact_match_rate": overall["episode_exact_match_rate"],
                "parse_ok_rate": overall["parse_ok_rate"],
                "oob_episode_rate": overall["oob_episode_rate"],
                "skip_rate": overall["skip_rate"],
                "overall_json": str(overall_path),
                "bucket_csv": str(bucket_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
