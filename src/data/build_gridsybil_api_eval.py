from __future__ import annotations

"""
Build evaluation episode ID lists for GPT API zero/few-shot experiments.

This script:
- Reads a GridSybil pseudo-ident JSONL split (typically test.jsonl)
- Uses eval_meta.num_candidates / eval_meta.num_true_attackers and
  input.meta.traffic_regime to define episode-level buckets
- Performs approximately 1/10 stratified sampling over these buckets
- Writes a plain-text file with one episode id per line

Typical usage (from repo root):

    python -m src.data.build_gridsybil_api_eval ^
        --jsonl_path data/processed/jsonl/gridsybil_pseudo_ident_balanced_a/test.jsonl ^
        --output_ids configs/eval_sets/gridsybil_test_5000_ids.txt ^
        --target_episodes 5000 ^
        --seed 42
"""

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build episode id list for GPT API eval via stratified sampling over "
            "traffic_regime, num_candidates and num_true_attackers."
        )
    )
    p.add_argument(
        "--jsonl_path",
        type=str,
        required=True,
        help="Path to test.jsonl (episode-level GridSybil pseudo-ident data).",
    )
    p.add_argument(
        "--output_ids",
        type=str,
        required=True,
        help="Output path for txt file with one episode id per line.",
    )
    p.add_argument(
        "--target_episodes",
        type=int,
        default=5000,
        help="Approximate number of episodes to sample (default 5000).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for sampling.",
    )
    return p.parse_args()


def candidate_bin_from_num(num_candidates: int) -> str:
    """Bucket for num_candidates (episode-level), mirroring candidate_bin style."""
    n = int(num_candidates)
    if n <= 8:
        return "less than 9"
    if n <= 16:
        return "9 to 16"
    if n <= 24:
        return "17 to 24"
    if n <= 32:
        return "25 to 32"
    return "more than 32"


def attacker_bin_from_num(num_true_attackers: int) -> str:
    """Bucket for num_true_attackers, mirroring visible_attacker_bin style."""
    n = int(num_true_attackers)
    if n <= 1:
        return "0 to 1"
    if n <= 4:
        return "2 to 4"
    if n <= 8:
        return "5 to 8"
    if n <= 14:
        return "9 to 14"
    return "more than 14"


def load_episode_rows(jsonl_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {jsonl_path}:{line_no}: {exc}") from exc
    return rows


def stratified_sample_episode_ids(
    rows: List[Dict[str, Any]],
    target_episodes: int,
    seed: int,
) -> Tuple[List[str], Dict[Tuple[str, str, str], int]]:
    """
    Group by (traffic_regime, cand_bin, attack_bin) and sample approximately
    target_episodes episodes in total, proportionally to bucket sizes.
    """
    # Build bucket -> [episode_ids]
    buckets: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
    for ex in rows:
        ep_id = str(ex.get("id"))
        if not ep_id:
            continue

        inp = ex.get("input", {})
        meta = inp.get("meta", {}) if isinstance(inp, dict) else {}
        regime = str(meta.get("traffic_regime", "null"))

        eval_meta = ex.get("eval_meta", {}) if isinstance(ex, dict) else {}
        num_cand = int(eval_meta.get("num_candidates", 0))
        num_true = int(eval_meta.get("num_true_attackers", 0))

        cand_bin = candidate_bin_from_num(num_cand)
        attack_bin = attacker_bin_from_num(num_true)

        key = (regime, cand_bin, attack_bin)
        buckets[key].append(ep_id)

    # Count total episodes and per-bucket counts
    bucket_counts: Dict[Tuple[str, str, str], int] = {}
    bucket_sample_counts: Dict[Tuple[str, str, str], int] = {}

    for key, ids in buckets.items():
        # Ensure unique ids per bucket (defensive; typically already unique)
        bucket_counts[key] = len(sorted(set(ids)))

    total_episodes = sum(bucket_counts.values())
    if total_episodes == 0:
        raise ValueError("No episodes found in JSONL; cannot sample.")

    rng = random.Random(seed)
    target = max(1, target_episodes)

    # First pass: compute ideal fractional quotas per bucket
    quotas: Dict[Tuple[str, str, str], float] = {}
    for key, count in bucket_counts.items():
        quotas[key] = target * (count / total_episodes)

    # Second pass: assign integer sample sizes with floor and remainder
    int_samples: Dict[Tuple[str, str, str], int] = {}
    remainders: List[Tuple[float, Tuple[str, str, str]]] = []
    for key, q in quotas.items():
        base = min(bucket_counts[key], int(math.floor(q)))
        int_samples[key] = base
        remainders.append((q - base, key))

    # Distribute remaining episodes by largest fractional remainder
    used = sum(int_samples.values())
    remaining = max(0, target - used)
    remainders.sort(reverse=True, key=lambda x: x[0])
    for frac, key in remainders:
        if remaining <= 0:
            break
        if int_samples[key] < bucket_counts[key]:
            int_samples[key] += 1
            remaining -= 1

    # Now sample within each bucket
    sampled_ids: List[str] = []
    for key, ids in buckets.items():
        unique_ids = sorted(set(ids))
        k = min(int_samples.get(key, 0), len(unique_ids))
        bucket_sample_counts[key] = k
        if k <= 0:
            continue
        rng.shuffle(unique_ids)
        sampled_ids.extend(unique_ids[:k])

    # Shuffle overall list for good measure
    rng.shuffle(sampled_ids)

    # Deduplicate globally while preserving order
    seen: set[str] = set()
    dedup_ids: List[str] = []
    for ep_id in sampled_ids:
        if ep_id in seen:
            continue
        seen.add(ep_id)
        dedup_ids.append(ep_id)

    return dedup_ids, bucket_counts, bucket_sample_counts


def write_ids(path: Path, ids: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ep_id in ids:
            f.write(f"{ep_id}\n")


def main() -> None:
    args = parse_args()
    jsonl_path = Path(args.jsonl_path)
    out_path = Path(args.output_ids)

    rows = load_episode_rows(jsonl_path)
    print(f"Loaded {len(rows)} episodes from {jsonl_path}", flush=True)

    sampled_ids, bucket_counts, bucket_sample_counts = stratified_sample_episode_ids(
        rows=rows,
        target_episodes=args.target_episodes,
        seed=args.seed,
    )

    print(
        f"Stratified sample: target={args.target_episodes}, "
        f"sampled={len(sampled_ids)} episodes",
        flush=True,
    )
    print("Bucket episode counts (traffic_regime, cand_bin, attack_bin -> count):", flush=True)
    for key, count in sorted(bucket_counts.items()):
        regime, cand_bin, attack_bin = key
        print(
            f"  regime={regime}, cand={cand_bin}, attack={attack_bin} -> episodes={count}, sampled={bucket_sample_counts[key]}",
            flush=True,
        )

    write_ids(out_path, sampled_ids)
    print(f"Wrote sampled episode ids to {out_path}", flush=True)


if __name__ == "__main__":
    main()

