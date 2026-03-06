from __future__ import annotations

"""
Utility script to build a stratified subset of the Phase 1 binary JSONL
(train/val/test), mainly for faster experiments and scaling studies.

Goal:
- Starting from a large Phase 1 JSONL (e.g. full train/val/test),
  create a smaller, stratified subset suitable for quicker training/eval.

Stratification strategy (coarse):
- Group samples by (label, attacker_type), where:
  - label       = JSONL field `output` ("BENIGN" or "ATTACK")
  - attacker_type = JSONL field `input.attacker_type`
    (None / NaN grouped into a single bucket)
- For each group with `n` samples, keep

      k = max(1, floor(n * keep_fraction))

  samples, selected uniformly at random (without replacement).
- This approximately preserves:
  - global BENIGN/ATTACK proportion
  - attacker_type distribution within each label
  while reducing the overall size by roughly `keep_fraction` globally.

Typical usage (from repo root):

    python -m src.data.build_phase1_subset \\
        --input  data/processed/jsonl/phase1_binary/train.jsonl \\
        --output data/processed/jsonl/phase1_binary_1of8/train.jsonl \\
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


# (label, attacker_type_bucket)
# e.g. ("BENIGN", "NONE"), ("ATTACK", "type_1")
GroupKey = Tuple[str, str]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Build a stratified subset of Phase1 binary JSONL by sampling roughly "
            "keep_fraction per (label, attacker_type) group."
        )
    )
    ap.add_argument(
        "--input",
        type=str,
        required=True,
        help="Path to input JSONL (e.g. data/processed/jsonl/phase1_binary/test.jsonl).",
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
    - Build mapping from (label, attacker_type_bucket) -> list of line indices.

    label  : obj['output'] ("BENIGN"/"ATTACK"/other)
    atk_type_bucket: derived from obj['input']['attacker_type'], with None/NaN mapped
                     to a single bucket ("NONE").
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

            # Label from top-level 'output' field (e.g. "BENIGN" / "ATTACK")
            label_raw = obj.get("output", "UNKNOWN")
            label = str(label_raw)

            # Attacker type from input.attacker_type; None/NaN grouped into "NONE"
            inp = obj.get("input", {})
            atk_raw = inp.get("attacker_type", None)
            if atk_raw is None:
                atk_bucket = "NONE"
            else:
                # attacker_type typically numeric; fall back to string if not
                try:
                    atk_bucket = f"type_{int(atk_raw)}"
                except (TypeError, ValueError):
                    atk_bucket = str(atk_raw)

            key: GroupKey = (label, atk_bucket)
            groups[key].append(idx)

    print(f"[index] Done. Total lines seen: {total_lines:,}")
    print(f"[index] Number of (label, attacker_type) groups: {len(groups):,}")
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

