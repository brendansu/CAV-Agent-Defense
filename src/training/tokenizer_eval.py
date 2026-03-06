"""Check prompt tokenization and truncation before training.

Reads the same YAML as train_lora_qwen_phase1_binary.py, uses the same tokenizer
and prompt building (dataset_phase1.build_phase1_prompt), and checks a few
samples to see token counts and whether truncation would cut off the "Answer:"
part. Run from repo root:

    python -m src.training.tokenizer_eval --config configs/qwen2.5_1.5b_phase1_binary_debug.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

import yaml
from transformers import AutoTokenizer

from .dataset_phase1 import (
    Phase1DatasetConfig,
    build_phase1_prompt,
    load_raw_phase1_splits,
)


def load_config(path: str) -> Dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"Config file {cfg_path} did not contain a mapping.")
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check prompt length and truncation with current tokenizer and dataset."
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_phase1_binary_debug.yaml",
        help="Path to YAML config (same as training script).",
    )
    parser.add_argument(
        "--num_samples",
        type=int,
        default=8,
        help="Number of samples to check (from train split).",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="train",
        choices=["train", "val", "test"],
        help="Which split to sample from.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    model_name = cfg.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct")
    jsonl_dir = Path(cfg.get("jsonl_dir", "data/processed/jsonl/phase1_binary_debug"))
    max_seq_len = int(cfg.get("max_seq_len", 512))

    print("=" * 60)
    print("Tokenizer / prompt length check (same logic as training)")
    print("=" * 60)
    print(f"Config:        {args.config}")
    print(f"model_name:    {model_name}")
    print(f"jsonl_dir:     {jsonl_dir}")
    print(f"max_seq_len:   {max_seq_len}")
    print(f"split:         {args.split}")
    print(f"num_samples:   {args.num_samples}")
    print()

    if not jsonl_dir.exists():
        raise FileNotFoundError(f"jsonl_dir not found: {jsonl_dir}")

    # Same tokenizer setup as train_lora_qwen_phase1_binary.py
    print("Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    print("Tokenizer loaded.")
    print()

    # Load raw data (same as training)
    config = Phase1DatasetConfig(jsonl_dir=jsonl_dir, max_seq_len=max_seq_len)
    raw_splits = load_raw_phase1_splits(config)
    ds = raw_splits[args.split]
    n_total = len(ds)
    n_check = min(args.num_samples, n_total)
    print(f"Loaded split '{args.split}' with {n_total} samples; checking first {n_check}.")
    print()

    truncated_count = 0
    max_tokens_seen = 0
    tail_ok_count = 0  # prompt tail contains "Answer:"

    for i in range(n_check):
        example = ds[i]
        prompt = build_phase1_prompt(example)

        encoded = tokenizer(
            prompt,
            truncation=True,
            max_length=max_seq_len,
        )
        n_tokens = len(encoded["input_ids"])
        truncated = n_tokens >= max_seq_len
        if truncated:
            truncated_count += 1
        if n_tokens > max_tokens_seen:
            max_tokens_seen = n_tokens

        # Check if the prompt string ends with "Answer:" (so model sees the instruction)
        prompt_ends_with_answer = "Answer:" in prompt[-120:]
        if prompt_ends_with_answer:
            tail_ok_count += 1

        # Decode what the model would actually see (after truncation)
        decoded = tokenizer.decode(encoded["input_ids"], skip_special_tokens=True)
        decoded_ends_with_answer = "Answer:" in decoded[-120:]

        print("-" * 60)
        print(f"Sample {i} (label={example.get('output', '?')})")
        print(f"  chars:     {len(prompt)}")
        print(f"  tokens:    {n_tokens} (max_seq_len={max_seq_len})")
        print(f"  truncated: {truncated}")
        print(f"  prompt ends with 'Answer:': {prompt_ends_with_answer}")
        print(f"  decoded (model sees) ends with 'Answer:': {decoded_ends_with_answer}")
        print("  prompt_tail (last 380 chars):")
        print("  | " + prompt[-380:].replace("\n", "\n  | "))
        if truncated:
            print("  decoded_tail (what model actually sees, last 380 chars):")
            print("  | " + decoded[-380:].replace("\n", "\n  | "))
        print()

    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Samples checked:     {n_check}")
    print(f"Truncated:           {truncated_count} / {n_check}")
    print(f"Max token count:     {max_tokens_seen}")
    print(f"Prompt tail had 'Answer:' (full text): {tail_ok_count} / {n_check}")
    if truncated_count > 0:
        print()
        print("Recommendation: increase max_seq_len in YAML so that token count stays below max_seq_len")
        print(f"                (e.g. try max_seq_len >= {max_tokens_seen + 64} or 1024).")
    else:
        print()
        print("No truncation in checked samples; current max_seq_len is sufficient for these.")
    print()


if __name__ == "__main__":
    main()
