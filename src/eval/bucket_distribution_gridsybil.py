from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def read_text_lines(path: Path) -> Iterable[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line
    else:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                yield line


def percentile_linear(sorted_vals: List[int], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if q <= 0:
        return float(sorted_vals[0])
    if q >= 1:
        return float(sorted_vals[-1])
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def to_int(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def make_hist_rows(counts: Dict[int, int], total: int, key_name: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for k in sorted(counts.keys()):
        c = counts[k]
        rows.append(
            {
                key_name: k,
                "episodes": c,
                "episode_share": c / total if total > 0 else 0.0,
            }
        )
    return rows


def collect_episode_level(
    pseudo_pattern: str,
) -> Tuple[Dict[str, Dict[str, int]], List[Path]]:
    paths = [Path(p) for p in sorted(glob.glob(pseudo_pattern))]
    if not paths:
        raise FileNotFoundError(f"No pseudo fact files matched pattern: {pseudo_pattern}")

    episodes: Dict[str, Dict[str, int]] = {}
    for p in paths:
        for line in read_text_lines(p):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            episode_id = str(row.get("episode_id"))
            if episode_id in episodes:
                continue
            episodes[episode_id] = {
                "n_visible_candidates": to_int(row.get("n_visible_candidates", 0)),
                "n_visible_attackers": to_int(row.get("n_visible_attackers", 0)),
            }
    return episodes, paths


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build episode-level distribution summaries from GridSybil pseudo_fact "
            "files (JSONL/JSONL.GZ)."
        )
    )
    p.add_argument("--run_id", type=str, required=True)
    p.add_argument("--model_tag", type=str, required=True, choices=["base", "lora"])
    p.add_argument("--split", type=str, required=True, choices=["val", "test"])
    p.add_argument(
        "--pseudo_pattern",
        type=str,
        default=None,
        help="Glob for pseudo fact files. If omitted, inferred from run_id/model_tag/split.",
    )
    p.add_argument(
        "--output_dir",
        type=str,
        default="outputs/eval_distribution",
        help="Directory for generated distribution CSV/JSON.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    pseudo_pattern = (
        args.pseudo_pattern
        or f"outputs/eval_logs/pseudo_fact_{args.run_id}_{args.model_tag}_{args.split}_shard*.json*"
    )
    episodes, used_files = collect_episode_level(pseudo_pattern)
    n_episodes = len(episodes)
    if n_episodes == 0:
        raise ValueError("No episode rows were collected from matched pseudo files.")

    cand_counts: Dict[int, int] = {}
    att_counts: Dict[int, int] = {}
    cand_vals: List[int] = []
    att_vals: List[int] = []

    for e in episodes.values():
        c = e["n_visible_candidates"]
        a = e["n_visible_attackers"]
        cand_vals.append(c)
        att_vals.append(a)
        cand_counts[c] = cand_counts.get(c, 0) + 1
        att_counts[a] = att_counts.get(a, 0) + 1

    cand_vals.sort()
    att_vals.sort()

    cand_quantiles = {
        "q25": percentile_linear(cand_vals, 0.25),
        "q50": percentile_linear(cand_vals, 0.50),
        "q75": percentile_linear(cand_vals, 0.75),
        "q90": percentile_linear(cand_vals, 0.90),
        "q95": percentile_linear(cand_vals, 0.95),
    }
    att_quantiles = {
        "q25": percentile_linear(att_vals, 0.25),
        "q50": percentile_linear(att_vals, 0.50),
        "q75": percentile_linear(att_vals, 0.75),
        "q90": percentile_linear(att_vals, 0.90),
        "q95": percentile_linear(att_vals, 0.95),
    }

    cand_rows = make_hist_rows(cand_counts, n_episodes, "n_visible_candidates")
    att_rows = make_hist_rows(att_counts, n_episodes, "n_visible_attackers")

    out_dir = Path(args.output_dir)
    stem = f"{args.run_id}_{args.model_tag}_{args.split}"
    cand_csv = out_dir / f"hist_candidates_{stem}.csv"
    att_csv = out_dir / f"hist_attackers_{stem}.csv"
    summary_json = out_dir / f"distribution_summary_{stem}.json"

    write_csv(cand_csv, cand_rows)
    write_csv(att_csv, att_rows)
    summary = {
        "run_id": args.run_id,
        "model_tag": args.model_tag,
        "split": args.split,
        "pseudo_pattern": pseudo_pattern,
        "pseudo_files_used": [str(p) for p in used_files],
        "n_episodes": n_episodes,
        "n_visible_candidates_quantiles": cand_quantiles,
        "n_visible_attackers_quantiles": att_quantiles,
        "hist_candidates_csv": str(cand_csv),
        "hist_attackers_csv": str(att_csv),
    }
    write_json(summary_json, summary)

    print("=== GridSybil Episode Distribution ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
