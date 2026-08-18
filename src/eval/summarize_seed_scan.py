"""
Summarize the dataset-sub-sampling seed-scan results (LLM foundation models +
tabular ML baselines) into one table per (attack, seed).

Inputs
------
- slurm/seed_scan_ml/*.json
    One file per (attack, model_group, seed), model_group in
    {all_wo_svm, svm}. Each JSON's "models" dict already has final
    accuracy/precision/recall/f1/throughput_sps per model -- no aggregation
    needed, just merge the two model_group files per (attack, seed).
- slurm/seed_scan_llm/<attack>/log_*.out
    8-shard sharded eval logs, one set of 8 per (model, seed). Parsed by
    matching known line prefixes (NOT fixed line numbers -- the gridsybil
    eval script echoes its env vars in a different order than
    datareplay/dosdisruptive, so LORA_DIR/SHARD_INDEX land on different line
    numbers depending on attack):
      - "LORA_DIR=..."                    -> encodes model label + attack + seed
      - "SHARD_INDEX=..."                 -> which of the 8 shards
      - "  progress 6250/6250 elapsed=Xs" -> wall time for this shard
      - "binary_counts: TP=.. FP=.. FN=.. TN=.."
      - "METRICS_JSON: {...}"             -> per-shard accuracy/precision/recall/f1
    The 8 shards per (model, seed) are combined by summing tp/fp/fn/tn and
    elapsed seconds (not averaging per-shard metrics), then recomputing
    accuracy/precision/recall/f1 from the summed confusion matrix and
    throughput from summed n / summed elapsed_s.

Output
------
- outputs/seed_scan_summary/<attack>_seed<N>.csv  (30 files, one per attack x seed)
- outputs/seed_scan_summary/all_results.csv       (all 30 tables stacked, for filtering)

Usage
-----
    python -m src.eval.summarize_seed_scan
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ATTACKS = ["gridsybil", "datareplay", "dosdisruptive"]
SEEDS = [2, 18, 25, 37, 45, 57, 65, 75, 80, 98]

# LORA_DIR basenames look like "<model_label>-<attack_suffix>-seed<N>".
LLM_MODEL_LABELS: List[Tuple[str, str]] = [
    ("qwen2.5-7b", "Qwen2.5-7B"),
    ("llama3.1-8b", "Llama3.1-8B"),
    ("mistral-7b", "Mistral-7B"),
    ("gemma2-9b", "Gemma2-9B"),
]
ATTACK_SUFFIXES = {
    "gridsybil": "gridsybil-plausibility-recentk-autoq-t1",
    "datareplay": "datareplay-plausibility-recentk-autoq",
    "dosdisruptive": "dosdisruptive-plausibility-recentk-autoq-classic",
}

ML_MODEL_LABELS = {
    "lr": "Logistic Regression",
    "gnb": "Gaussian Naive Bayes",
    "rf": "Random Forest",
    "xgb": "XGBoost",
    "lgbm": "LightGBM",
    "mlp": "MLP",
    "svm": "SVM",
}

CSV_COLUMNS = [
    "attack",
    "seed",
    "model",
    "accuracy",
    "precision",
    "recall",
    "f1",
    "throughput_samples_per_s",
    "latency_ms_per_sample",
    "n_samples",
]


def _safe_div(num: float, den: float) -> float:
    return float(num / den) if den else 0.0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ml_dir", type=str, default="slurm/seed_scan_ml")
    ap.add_argument("--llm_dir", type=str, default="slurm/seed_scan_llm")
    ap.add_argument("--out_dir", type=str, default="outputs/seed_scan_summary")
    return ap.parse_args()


def parse_llm_lora_dir(text: str, source_attack: str, source_file: str) -> Tuple[str, str, int]:
    m = re.search(r"^LORA_DIR=(.*)$", text, re.M)
    if not m:
        raise ValueError(f"No LORA_DIR line found in {source_file}")
    basename = m.group(1).strip().rstrip("/").rsplit("/", 1)[-1]

    model_label = None
    for prefix, display in LLM_MODEL_LABELS:
        if basename.startswith(prefix + "-"):
            model_label = display
            rest = basename[len(prefix) + 1 :]
            break
    if model_label is None:
        raise ValueError(f"Unrecognized model prefix in LORA_DIR basename '{basename}' ({source_file})")

    suffix = ATTACK_SUFFIXES[source_attack]
    seed_match = re.search(rf"^{re.escape(suffix)}-seed(\d+)$", rest)
    if not seed_match:
        raise ValueError(
            f"LORA_DIR basename '{basename}' did not match expected attack suffix "
            f"'{suffix}-seed<N>' for attack={source_attack} ({source_file})"
        )
    seed = int(seed_match.group(1))
    return model_label, source_attack, seed


def parse_llm_shard(path: Path, attack: str) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")

    model_label, parsed_attack, seed = parse_llm_lora_dir(text, attack, str(path))

    shard_m = re.search(r"^SHARD_INDEX=(\d+)$", text, re.M)
    if not shard_m:
        raise ValueError(f"No SHARD_INDEX line found in {path}")
    shard_index = int(shard_m.group(1))

    # Multiple "progress N/total elapsed=..." lines are printed as the shard runs
    # (one per PROGRESS_EVERY_SAMPLES); take the LAST one (the completion line,
    # where done == total), not just the first match found in the file.
    progress_matches = list(re.finditer(r"^\s*progress (\d+)/(\d+) elapsed=([0-9.]+)s", text, re.M))
    if not progress_matches:
        raise ValueError(f"No 'progress .../... elapsed=' line found in {path}")
    last = progress_matches[-1]
    done, total = int(last.group(1)), int(last.group(2))
    if done != total:
        raise ValueError(
            f"Last progress line in {path} is not a completion line (done={done} != total={total})"
        )
    elapsed_s = float(last.group(3))

    counts_m = re.search(r"^binary_counts: TP=(\d+) FP=(\d+) FN=(\d+) TN=(\d+)", text, re.M)
    if not counts_m:
        raise ValueError(f"No binary_counts line found in {path}")
    tp, fp, fn, tn = (int(x) for x in counts_m.groups())

    metrics_m = re.search(r"^METRICS_JSON: (.*)$", text, re.M)
    if not metrics_m:
        raise ValueError(f"No METRICS_JSON line found in {path}")
    metrics = json.loads(metrics_m.group(1))
    n = int(metrics["n"])

    return {
        "model": model_label,
        "attack": parsed_attack,
        "seed": seed,
        "shard_index": shard_index,
        "elapsed_s": elapsed_s,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "n": n,
    }


def aggregate_llm(llm_dir: Path) -> Dict[Tuple[str, int, str], Dict[str, Any]]:
    """Returns {(attack, seed, model): row_dict}."""
    shards: Dict[Tuple[str, int, str], Dict[int, Dict[str, Any]]] = defaultdict(dict)

    for attack in ATTACKS:
        files = sorted(glob.glob(str(llm_dir / attack / "*.out")))
        for f in files:
            row = parse_llm_shard(Path(f), attack)
            key = (row["attack"], row["seed"], row["model"])
            shards[key][row["shard_index"]] = row

    out: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for key, shard_map in shards.items():
        missing = sorted(set(range(8)) - set(shard_map.keys()))
        if missing:
            print(f"[warn] {key}: missing shards {missing}, skipping this combo")
            continue
        tp = sum(s["tp"] for s in shard_map.values())
        fp = sum(s["fp"] for s in shard_map.values())
        fn = sum(s["fn"] for s in shard_map.values())
        tn = sum(s["tn"] for s in shard_map.values())
        n = sum(s["n"] for s in shard_map.values())
        elapsed_s = sum(s["elapsed_s"] for s in shard_map.values())

        accuracy = _safe_div(tp + tn, tp + fp + fn + tn)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _safe_div(2 * precision * recall, precision + recall)
        throughput_sps = _safe_div(n, elapsed_s)
        latency_ms = 1000.0 * _safe_div(elapsed_s, n)

        out[key] = {
            "accuracy": accuracy,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "throughput_samples_per_s": throughput_sps,
            "latency_ms_per_sample": latency_ms,
            "n_samples": n,
        }
    return out


def aggregate_ml(ml_dir: Path) -> Dict[Tuple[str, int, str], Dict[str, Any]]:
    """Returns {(attack, seed, model_display_name): row_dict}."""
    out: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    files = sorted(glob.glob(str(ml_dir / "*.json")))
    name_re = re.compile(r"_(datareplay|dosdisruptive|gridsybil)_(all_wo_svm|svm)_seed(\d+)\.json$")

    for f in files:
        m = name_re.search(f)
        if not m:
            print(f"[warn] unparseable ml filename, skipping: {f}")
            continue
        attack, _group, seed = m.group(1), m.group(2), int(m.group(3))
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        for model_key, mdata in data["models"].items():
            display = ML_MODEL_LABELS.get(model_key, model_key)
            key = (attack, seed, display)
            out[key] = {
                "accuracy": float(mdata["accuracy"]),
                "precision": float(mdata["precision"]),
                "recall": float(mdata["recall"]),
                "f1": float(mdata["f1"]),
                "throughput_samples_per_s": float(mdata["throughput_sps"]),
                "latency_ms_per_sample": float(mdata["latency_e2e_ms"]),
                "n_samples": int(mdata["n"]),
            }
    return out


def main() -> None:
    args = parse_args()
    ml_dir = Path(args.ml_dir)
    llm_dir = Path(args.llm_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    llm_rows = aggregate_llm(llm_dir)
    ml_rows = aggregate_ml(ml_dir)

    print(f"[info] llm combos parsed: {len(llm_rows)} (expected {len(ATTACKS) * len(SEEDS) * 4})")
    print(f"[info] ml combos parsed: {len(ml_rows)} (expected {len(ATTACKS) * len(SEEDS) * 7})")

    all_rows: List[Dict[str, Any]] = []
    model_order = [d for _, d in LLM_MODEL_LABELS] + list(ML_MODEL_LABELS.values())

    for attack in ATTACKS:
        for seed in SEEDS:
            table_rows = []
            for model in model_order:
                row = llm_rows.get((attack, seed, model)) or ml_rows.get((attack, seed, model))
                if row is None:
                    print(f"[warn] missing result for attack={attack} seed={seed} model={model}")
                    continue
                full_row = {"attack": attack, "seed": seed, "model": model, **row}
                table_rows.append(full_row)
                all_rows.append(full_row)

            out_path = out_dir / f"{attack}_seed{seed}.csv"
            with out_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                for r in table_rows:
                    writer.writerow(r)

    combined_path = out_dir / "all_results.csv"
    with combined_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in all_rows:
            writer.writerow(r)

    print(f"[done] wrote {len(ATTACKS) * len(SEEDS)} per-(attack,seed) tables + all_results.csv to {out_dir}")


if __name__ == "__main__":
    main()
