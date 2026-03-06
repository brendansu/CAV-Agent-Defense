from __future__ import annotations

"""
Utility script to build a smaller debug JSONL dataset from the full phase1 JSONL.

Example usage (from repo root):

  python src/data/build_debug_sample.py ^
    --source_dir data/processed/jsonl/phase1_binary ^
    --out_dir data/processed/jsonl/phase1_binary_debug ^
    --max_train 50000 ^
    --max_val 5000 ^
    --max_test 5000

This will:
- Read train/val/test.jsonl from source_dir
- Write at most max_* lines into the corresponding files in out_dir

The goal is fast local iteration; sampling策略简单采用“按行顺序截断”，
若需更随机的采样，可后续扩展为按概率抽样或按 run_id 过滤。
"""

import argparse
from pathlib import Path


def copy_head_lines(src: Path, dst: Path, max_lines: int) -> int:
    """
    Copy at most `max_lines` lines from src to dst.
    Returns the number of lines actually written.
    """
    if max_lines <= 0:
        return 0
    if not src.exists():
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            fout.write(line)
            written += 1
            if written >= max_lines:
                break
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--source_dir",
        type=str,
        default="data/processed/jsonl/phase1_binary",
        help="Directory containing full train/val/test.jsonl",
    )
    ap.add_argument(
        "--out_dir",
        type=str,
        default="data/processed/jsonl/phase1_binary_debug",
        help="Directory to write debug train/val/test.jsonl",
    )
    ap.add_argument(
        "--max_train",
        type=int,
        default=50000,
        help="Maximum number of train samples to keep in debug set.",
    )
    ap.add_argument(
        "--max_val",
        type=int,
        default=5000,
        help="Maximum number of val samples to keep in debug set.",
    )
    ap.add_argument(
        "--max_test",
        type=int,
        default=5000,
        help="Maximum number of test samples to keep in debug set.",
    )
    args = ap.parse_args()

    source_dir = Path(args.source_dir)
    out_dir = Path(args.out_dir)

    print(f"Building debug samples from {source_dir} -> {out_dir}")

    train_src = source_dir / "train.jsonl"
    val_src = source_dir / "val.jsonl"
    test_src = source_dir / "test.jsonl"

    train_dst = out_dir / "train.jsonl"
    val_dst = out_dir / "val.jsonl"
    test_dst = out_dir / "test.jsonl"

    n_train = copy_head_lines(train_src, train_dst, args.max_train)
    n_val = copy_head_lines(val_src, val_dst, args.max_val)
    n_test = copy_head_lines(test_src, test_dst, args.max_test)

    print(f"Debug train: wrote {n_train} lines -> {train_dst}")
    print(f"Debug val:   wrote {n_val} lines -> {val_dst}")
    print(f"Debug test:  wrote {n_test} lines -> {test_dst}")


if __name__ == "__main__":
    main()

