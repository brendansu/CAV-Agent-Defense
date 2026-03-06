from __future__ import annotations

"""
Utility script to build a stratified subset of the Phase 1 binary TEST JSONL.

Goal:
- Starting from a large test JSONL (e.g. ~2.77M windows, ~8GB),
  create a smaller, stratified subset suitable for faster evaluation runs.

Stratification strategy:
- Group samples by (run_id, receiver_id, sender_id).
- For each group with `n` samples, keep

      k = max(1, floor(n * keep_fraction))

  samples, selected uniformly at random (without replacement).
- This preserves coverage across:
  - all runs,
  - all (receiver, sender) pairs within each run,
  while reducing the overall size by roughly `keep_fraction` globally.

Typical usage (from repo root):

    python -m src.data.build_test_subset \\
        --input data/processed/jsonl/phase1_binary/test.jsonl \\
        --output data/processed/jsonl/phase1_binary/test_subset_1of8.jsonl \\
        --keep_fraction 0.125 \\
        --seed 42

This script is I/O bound and should be run once (locally or on HPC login/DTN),
not inside a Slurm GPU job.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, List, Tuple

import numpy as np


GroupKey = Tuple[str, int, int]  # (run_id, receiver_id, sender_id)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build a stratified subset of Phase1 binary TEST JSONL "
        "by sampling roughly keep_fraction per (run_id, receiver_id, sender_id) group."
    )
    ap.add_argument(
        "--input",
        type=str,
        required=True,
        help="Path to input TEST JSONL (e.g. data/processed/jsonl/phase1_binary/test.jsonl).",
    )
    ap.add_argument(
        "--output",
        type=str,
        required=True,
        help="Path to output subset JSONL.",
    )
    ap.add_argument(
        "--keep_fraction",
        type=float,
        default=0.125,
        help="Approximate fraction of samples to keep per group (default: 0.125 = 1/8).",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible sampling.",
    )
    return ap.parse_args()


def build_group_index(input_path: Path) -> Tuple[int, DefaultDict[GroupKey, List[int]]]:
    """
    First pass over the JSONL:
    - Count total number of lines.
    - Build mapping from (run_id, receiver_id, sender_id) -> list of line indices.
    """
    groups: DefaultDict[GroupKey, List[int]] = defaultdict(list)
    total_lines = 0

    with input_path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            total_lines += 1
            if total_lines % 1_000_000 == 0:
                print(f"[index] processed {total_lines:,} lines ...")
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                # Log and skip malformed lines
                print(f"[warning] JSON decode error at line {idx}: {e}")
                continue

            inp = obj.get("input", {})
            run_id = str(inp.get("run_id"))
            receiver_id = int(inp.get("receiver_id"))
            sender_id = int(inp.get("sender_id"))
            key: GroupKey = (run_id, receiver_id, sender_id)
            groups[key].append(idx)

    print(f"[index] Done. Total lines seen: {total_lines:,}")
    print(f"[index] Number of (run_id, receiver_id, sender_id) groups: {len(groups):,}")
    return total_lines, groups


def choose_line_indices(
    total_lines: int,
    groups: DefaultDict[GroupKey, List[int]],
    keep_fraction: float,
    seed: int,
) -> List[bool]:
    """
    For each group, choose k = max(1, floor(n * keep_fraction)) indices uniformly at random.
    Returns a boolean mask of length total_lines indicating which lines to keep.
    """
    if not (0.0 < keep_fraction <= 1.0):
        raise ValueError(f"keep_fraction must be in (0, 1], got {keep_fraction}")

    rng = np.random.default_rng(seed)
    keep_mask: List[bool] = [False] * total_lines

    total_kept = 0
    for key, indices in groups.items():
        n = len(indices)
        if n == 0:
            continue
        k = int(n * keep_fraction)
        if k <= 0:
            k = 1  # ensure every group keeps at least one window

        if k >= n:
            chosen = indices
        else:
            chosen = list(rng.choice(indices, size=k, replace=False))

        for idx in chosen:
            if 0 <= idx < total_lines:
                keep_mask[idx] = True
        total_kept += len(chosen)

    print(f"[sample] keep_fraction={keep_fraction:.4f}, seed={seed}")
    print(f"[sample] Total groups: {len(groups):,}")
    print(f"[sample] Total lines kept: {total_kept:,} (≈ {total_kept / max(1, total_lines):.3%} of all lines)")
    return keep_mask


def write_subset(input_path: Path, output_path: Path, keep_mask: List[bool]) -> None:
    """
    Second pass over the JSONL:
    - Write out only those lines where keep_mask[line_index] is True.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0

    with input_path.open("r", encoding="utf-8") as f_in, output_path.open(
        "w", encoding="utf-8"
    ) as f_out:
        for idx, line in enumerate(f_in):
            if idx < len(keep_mask) and keep_mask[idx]:
                f_out.write(line)
                n_written += 1
                if n_written % 100_000 == 0:
                    print(f"[write] written {n_written:,} lines ...")

    print(f"[write] Done. Wrote {n_written:,} lines to {output_path}")


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(f"Input JSONL not found: {input_path}")

    print(f"Input JSONL:  {input_path}")
    print(f"Output JSONL: {output_path}")
    print(f"keep_fraction={args.keep_fraction}, seed={args.seed}")

    total_lines, groups = build_group_index(input_path)
    keep_mask = choose_line_indices(
        total_lines=total_lines,
        groups=groups,
        keep_fraction=args.keep_fraction,
        seed=args.seed,
    )
    write_subset(input_path, output_path, keep_mask)


if __name__ == "__main__":
    main()

