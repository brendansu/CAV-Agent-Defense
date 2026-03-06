import argparse
import glob
import json
import os
from typing import Dict


def aggregate_from_pattern(pattern: str) -> Dict[str, float]:
    agg = dict(tp=0, fp=0, fn=0, correct=0, n_used=0)

    paths = sorted(glob.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"No files matched pattern: {pattern!r}")

    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("METRICS_JSON:"):
                    data = json.loads(line.split("METRICS_JSON:", 1)[1].strip())
                    agg["tp"] += data["tp"]
                    agg["fp"] += data["fp"]
                    agg["fn"] += data["fn"]
                    agg["correct"] += data["correct"]
                    agg["n_used"] += data["n_used"]

    n = agg["n_used"]
    if n == 0:
        return {
            "n_used": 0,
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1_attack": 0.0,
            "tp": 0,
            "fp": 0,
            "fn": 0,
            "correct": 0,
        }

    tp = agg["tp"]
    fp = agg["fp"]
    fn = agg["fn"]
    correct = agg["correct"]

    acc = correct / n
    precision = tp / (tp + fp) if tp + fp > 0 else 0.0
    recall = tp / (tp + fn) if tp + fn > 0 else 0.0
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)

    return {
        "n_used": n,
        "accuracy": acc,
        "precision": precision,
        "recall": recall,
        "f1_attack": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "correct": correct,
    }


def build_pattern(slurm_dir: str, dataset_id: str, model_id: str) -> str:
    prefix = f"eval_{dataset_id}_{model_id}_"
    return os.path.join(slurm_dir, f"{prefix}*_*.out")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate sharded eval metrics (METRICS_JSON) from Slurm .out files "
            "into a single global accuracy/F1(ATTACK)."
        )
    )
    parser.add_argument(
        "--dataset_id",
        type=str,
        required=True,
        help="Dataset identifier used in Slurm job name/prefix, e.g. '1of8', '100k', '1m'.",
    )
    parser.add_argument(
        "--model_id",
        type=str,
        required=True,
        help="Model identifier used in Slurm job name/prefix, e.g. 'base', 'lora05ep', 'lora2ep'.",
    )
    default_slurm_dir = os.path.join(
        "/scratch",
        os.environ.get("USER", ""),
        "veremi_agent",
        "slurm",
    )
    parser.add_argument(
        "--slurm_dir",
        type=str,
        default=default_slurm_dir,
        help=f"Directory containing Slurm .out files (default: {default_slurm_dir}).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pattern = build_pattern(args.slurm_dir, args.dataset_id, args.model_id)
    print(f"Using pattern: {pattern}")

    metrics = aggregate_from_pattern(pattern)

    print(f"Global used samples: {metrics['n_used']}")
    print(f"Global accuracy: {metrics['accuracy']:.6f}")
    print(f"Global precision(ATTACK): {metrics['precision']:.6f}")
    print(f"Global recall(ATTACK): {metrics['recall']:.6f}")
    print(f"Global F1(ATTACK): {metrics['f1_attack']:.6f}")
    print(
        f"Global counts: TP={metrics['tp']} FP={metrics['fp']} "
        f"FN={metrics['fn']} correct={metrics['correct']}"
    )


if __name__ == "__main__":
    main()
