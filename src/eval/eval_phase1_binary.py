from __future__ import annotations

"""
Evaluation script for Phase 1 binary attack detection on VeReMi (LLM-based).

Goals:
- Evaluate Qwen2.5-1.5B (base) and/or a LoRA‑fine‑tuned adapter on the Phase 1
  JSONL splits (typically the debug test set).
- Reuse the same prompt structure as training, but without revealing the label.
- Compute simple message‑level metrics (accuracy, F1 with ATTACK as positive).
- Optionally print a few example predictions for manual inspection.

Typical usage (from repo root, with src on PYTHONPATH):

    # 1) Evaluate base model on debug test set (first 1000 samples)
    python -m src.eval.eval_phase1_binary ^
        --jsonl_dir data/processed/jsonl/phase1_binary_debug ^
        --split test ^
        --mode base ^
        --max_samples 1000

    # 2) Evaluate LoRA‑fine‑tuned model (same dataset)
    python -m src.eval.eval_phase1_binary ^
        --jsonl_dir data/processed/jsonl/phase1_binary_debug ^
        --split test ^
        --mode lora ^
        --lora_dir outputs/qwen2.5-1.5b-phase1-binary-debug ^
        --max_samples 1000

    # 3) Run both base and LoRA sequentially in one script run
    python -m src.eval.eval_phase1_binary ^
        --jsonl_dir data/processed/jsonl/phase1_binary_debug ^
        --split test ^
        --mode both ^
        --lora_dir outputs/qwen2.5-1.5b-phase1-binary-debug ^
        --max_samples 1000
"""

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Tuple
import subprocess

import torch
from datasets import Dataset, load_dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    PreTrainedModel,
    PreTrainedTokenizerBase,
)

from ..training.dataset_phase1 import build_phase1_prompt

EvalMode = Literal["base", "lora", "both"]


@dataclass
class EvalConfig:
    model_name: str
    jsonl_dir: Path
    split: str
    mode: EvalMode
    lora_dir: Path | None
    max_samples: int | None
    max_seq_len: int
    max_new_tokens: int
    load_in_4bit: bool
    show_examples: int
    print_predictions: int
    device: str | None
    num_shards: int
    shard_index: int


def parse_args() -> EvalConfig:
    parser = argparse.ArgumentParser(
        description="Evaluate Phase 1 binary detection on VeReMi JSONL splits."
    )
    parser.add_argument(
        "--model_name",
        type=str,
        default="Qwen/Qwen2.5-1.5B-Instruct",
        help="Base model name or path (same as training).",
    )
    parser.add_argument(
        "--jsonl_dir",
        type=str,
        default="data/processed/jsonl/phase1_binary_debug",
        help="Directory containing train/val/test.jsonl.",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        choices=["train", "val", "test"],
        help="Which split to evaluate on.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="base",
        choices=["base", "lora", "both"],
        help="Evaluate base model, LoRA‑fine‑tuned model, or both sequentially.",
    )
    parser.add_argument(
        "--lora_dir",
        type=str,
        default=None,
        help="Directory containing LoRA adapter weights (required for mode=lora/both).",
    )
    parser.add_argument(
        "--max_samples",
        type=int,
        default=1000,
        help="Maximum number of samples from the split to evaluate (None = all).",
    )
    parser.add_argument(
        "--max_seq_len",
        type=int,
        default=512,
        help="Maximum sequence length for tokenization (should match training).",
    )
    parser.add_argument(
        "--max_new_tokens",
        type=int,
        default=12,
        help="Maximum number of tokens to generate for the answer (default 12 so model can output BENIGN/ATTACK).",
    )
    parser.add_argument(
        "--no_4bit",
        action="store_true",
        help="Disable 4‑bit quantization and load model in full precision fp16.",
    )
    parser.add_argument(
        "--show_examples",
        type=int,
        default=5,
        help="Number of example predictions to print (misclassified if possible).",
    )
    parser.add_argument(
        "--print_predictions",
        type=int,
        default=10,
        help="Print model output (true, pred, raw gen) for the first N samples (0 to disable).",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Torch device override, e.g. 'cuda:0' or 'cpu'. If None, use HF device_map.",
    )
    parser.add_argument(
        "--num_shards",
        type=int,
        default=1,
        help="Total number of shards to split evaluation into (for running multiple jobs in parallel).",
    )
    parser.add_argument(
        "--shard_index",
        type=int,
        default=0,
        help="Index of this shard in [0, num_shards-1].",
    )

    args = parser.parse_args()

    jsonl_dir = Path(args.jsonl_dir)
    if not jsonl_dir.exists():
        raise FileNotFoundError(f"jsonl_dir not found: {jsonl_dir}")

    if args.num_shards <= 0:
        raise ValueError(f"--num_shards must be positive, got {args.num_shards}")
    if not (0 <= args.shard_index < args.num_shards):
        raise ValueError(
            f"--shard_index must be in [0, num_shards-1], got {args.shard_index} with num_shards={args.num_shards}"
        )

    if args.mode in ("lora", "both") and args.lora_dir is None:
        raise ValueError("--lora_dir is required when mode is 'lora' or 'both'.")

    lora_dir = Path(args.lora_dir) if args.lora_dir is not None else None

    cfg = EvalConfig(
        model_name=args.model_name,
        jsonl_dir=jsonl_dir,
        split=args.split,
        mode=args.mode,  # type: ignore[arg-type]
        lora_dir=lora_dir,
        max_samples=args.max_samples if args.max_samples and args.max_samples > 0 else None,
        max_seq_len=args.max_seq_len,
        max_new_tokens=args.max_new_tokens,
        load_in_4bit=not args.no_4bit,
        show_examples=max(0, args.show_examples),
        print_predictions=max(0, args.print_predictions),
        device=args.device,
        num_shards=args.num_shards,
        shard_index=args.shard_index,
    )
    return cfg

def get_gpu_util():
    try:
        result = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"]
        )
        return int(result.decode().strip().split("\n")[0])
    except:
        return -1

def load_raw_split(jsonl_dir: Path, split: str) -> Dataset:
    split_file = jsonl_dir / f"{split}.jsonl"
    if not split_file.exists():
        raise FileNotFoundError(f"Split file not found: {split_file}")
    ds = load_dataset("json", data_files=str(split_file))["train"]
    return ds


def build_eval_prompt(example: dict) -> str:
    """
    Build an evaluation prompt by reusing the training prompt template and
    stripping the final 'Answer: <LABEL>' line so the model must predict it.
    """
    full = build_phase1_prompt(example)
    lines = full.splitlines()
    if not lines:
        return full

    # Expect the last line to be "Answer: <LABEL>" in the training template.
    # For evaluation, we remove that line and optionally append a blank "Answer:".
    if lines[-1].lstrip().startswith("Answer:"):
        lines = lines[:-1]
    lines.append("Answer:")
    return "\n".join(lines)


def prepare_prompts_and_labels(
    ds: Dataset,
    max_samples: int | None = None,
    num_shards: int = 1,
    shard_index: int = 0,
) -> Tuple[List[str], List[str]]:
    prompts: List[str] = []
    labels: List[str] = []

    n = len(ds)
    total_limit = n if max_samples is None else min(max_samples, n)

    if num_shards <= 1:
        indices = range(total_limit)
    else:
        shard_size = (total_limit + num_shards - 1) // num_shards  # ceil
        start = shard_index * shard_size
        end = min(start + shard_size, total_limit)
        if start >= end:
            # This shard has no data
            return [], []
        indices = range(start, end)

    for i in indices:
        ex = ds[i]
        prompt = build_eval_prompt(ex)
        label = str(ex.get("output", "BENIGN"))
        prompts.append(prompt)
        labels.append(label)

    return prompts, labels


def normalize_label(text: str) -> str:
    """
    Map raw generation text to one of {BENIGN, ATTACK, UNKNOWN}.
    """
    s = text.strip().upper()
    idx_attack = s.find("ATTACK")
    idx_benign = s.find("BENIGN")

    if idx_attack == -1 and idx_benign == -1:
        return "UNKNOWN"
    if idx_attack == -1:
        return "BENIGN"
    if idx_benign == -1:
        return "ATTACK"
    # Both present: take whichever appears first.
    return "ATTACK" if idx_attack < idx_benign else "BENIGN"


def label_to_int(label: str) -> int:
    """
    Encode labels as integers for metric computation.
    ATTACK is treated as positive class (1), BENIGN as 0.
    """
    label = label.strip().upper()
    if label == "ATTACK":
        return 1
    if label == "BENIGN":
        return 0
    raise ValueError(f"Unexpected label: {label!r}")


def compute_binary_metrics(y_true: Iterable[int], y_pred: Iterable[int]) -> Dict[str, float]:
    """
    Compute accuracy and F1 (ATTACK as positive) without external dependencies.

    Returns both derived metrics and raw counts so that sharded evaluations can be
    aggregated into a single global metric by summing counts across shards.
    """
    y_true = list(y_true)
    y_pred = list(y_pred)
    assert len(y_true) == len(y_pred)
    n = len(y_true)
    if n == 0:
        return {
            "accuracy": 0.0,
            "f1_attack": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "n_used": 0,
            "correct": 0,
            "tp": 0,
            "fp": 0,
            "fn": 0,
        }

    correct = sum(1 for t, p in zip(y_true, y_pred) if t == p)
    acc = correct / n

    tp = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    if precision + recall == 0.0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return {
        "accuracy": acc,
        "f1_attack": f1,
        "precision": precision,
        "recall": recall,
        "n_used": n,
        "correct": correct,
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def setup_tokenizer(model_name_or_dir: str) -> PreTrainedTokenizerBase:
    tokenizer = AutoTokenizer.from_pretrained(model_name_or_dir)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    return tokenizer


def load_base_model(
    model_name: str,
    load_in_4bit: bool,
    device: str | None,
) -> PreTrainedModel:
    print(
        f"[DEBUG] load_base_model: model_name={model_name}, load_in_4bit={load_in_4bit}, device={device}",
        flush=True,
    )
    start_time = time.time()
    if load_in_4bit:
        print("[DEBUG] constructing BitsAndBytesConfig for 4-bit quantization", flush=True)
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
        print("[DEBUG] before AutoModelForCausalLM.from_pretrained (4bit)", flush=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            quantization_config=bnb_config,
            device_map="auto" if device is None else None,
        )
        if device is not None:
            model.to(device)
    else:
        print("[DEBUG] before AutoModelForCausalLM.from_pretrained (no_4bit)", flush=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            device_map="auto" if device is None else None,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        )
        if device is not None:
            model.to(device)

    elapsed = time.time() - start_time
    print(f"[DEBUG] finished load_base_model in {elapsed:.1f} seconds", flush=True)
    model.eval()
    return model


def wrap_with_lora(
    base_model: PreTrainedModel,
    lora_dir: Path,
    device: str | None,
) -> PreTrainedModel:
    from peft import PeftModel

    model = PeftModel.from_pretrained(base_model, str(lora_dir))
    if device is not None:
        model.to(device)
    model.eval()
    return model


def run_eval_for_model(
    model: PreTrainedModel,
    tokenizer: PreTrainedTokenizerBase,
    prompts: List[str],
    labels: List[str],
    max_seq_len: int,
    max_new_tokens: int,
    show_examples: int,
    print_predictions: int,
    model_tag: str,
    num_shards: int = 1,
    shard_index: int = 0,
) -> None:
    assert len(prompts) == len(labels)
    device = next(model.parameters()).device

    y_true_int: List[int] = []
    y_pred_int: List[int] = []
    example_buffer: List[Dict[str, str]] = []

    total = len(prompts)
    print(f"Evaluating {model_tag} on {total} samples...")
    if num_shards > 1:
        print(f"Shard {shard_index + 1}/{num_shards}")
    start_time = time.time()

    for i, (prompt, true_label) in enumerate(zip(prompts, labels)):
        enc = tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=max_seq_len,
        )
        enc = {k: v.to(device) for k, v in enc.items()}

        with torch.no_grad():
            gen_ids = model.generate(
                **enc,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )

        input_len = enc["input_ids"].shape[1]
        gen_text = tokenizer.decode(
            gen_ids[0][input_len:],
            skip_special_tokens=True,
        )
        pred_label = normalize_label(gen_text)

        if print_predictions > 0 and i < print_predictions:
            print(f"  [{i}] true={true_label!r} pred={pred_label!r} output={gen_text!r}")

        try:
            y_t = label_to_int(true_label)
        except ValueError:
            # Skip unknown ground-truth labels
            continue

        if pred_label == "UNKNOWN":
            # For now, treat UNKNOWN as BENIGN=0 (or skip).
            # Here we choose to skip UNKNOWN predictions.
            continue

        y_p = label_to_int(pred_label)
        y_true_int.append(y_t)
        y_pred_int.append(y_p)

        # Collect a few illustrative examples (prefer misclassifications).
        if len(example_buffer) < show_examples:
            if y_t != y_p or show_examples > 0:
                example_buffer.append(
                    {
                        "prompt": prompt,
                        "true": true_label,
                        "pred": pred_label,
                        "gen": gen_text.strip(),
                    }
                )

        if (i + 1) % 5000 == 0 or (i + 1) == total:
            elapsed = time.time() - start_time
            processed = i + 1
            rate = processed / elapsed if elapsed > 0 else 0
            eta = max((total - processed) / rate / 3600, 0) if rate > 0 else 0
            gpu_util = get_gpu_util()
            elapsed_h = elapsed / 3600

            print(f"Processed {processed}/{total} | {rate:.2f} samples/s | GPU {gpu_util}% | elapsed {elapsed_h:.2f} h | ETA {eta:.2f} h")
        
        if (i + 1) % 10000 == 0:  # add intermediate metrics print every 10000 samples (walltime fallback measure)
            metrics_int = compute_binary_metrics(y_true_int, y_pred_int)
            n_used_int = len(y_true_int)

            print(f"\n=== Results for {model_tag} at {i + 1} samples ===")
            print(f"Used samples (after skipping UNKNOWN/invalid): {n_used_int}")
            print(f"Accuracy:  {metrics_int['accuracy']:.4f}")
            print(f"F1 (ATTACK): {metrics_int['f1_attack']:.4f}")

    metrics = compute_binary_metrics(y_true_int, y_pred_int)
    n_used = metrics["n_used"]

    print(f"\n=== Results for {model_tag} ===")
    if num_shards > 1:
        print(f"Shard {shard_index + 1}/{num_shards}")
    print(f"Used samples (after skipping UNKNOWN/invalid): {n_used}")
    print(f"Accuracy:  {metrics['accuracy']:.4f}")
    print(f"F1 (ATTACK): {metrics['f1_attack']:.4f}")
    print(f"Precision (ATTACK): {metrics['precision']:.4f}")
    print(f"Recall    (ATTACK): {metrics['recall']:.4f}")
    print(
        f"Counts: TP={metrics['tp']} FP={metrics['fp']} "
        f"FN={metrics['fn']} correct={metrics['correct']}"
    )

    summary = {
        "model_tag": model_tag,
        "num_shards": num_shards,
        "shard_index": shard_index,
        "n_used": n_used,
        "correct": metrics["correct"],
        "tp": metrics["tp"],
        "fp": metrics["fp"],
        "fn": metrics["fn"],
    }
    print("METRICS_JSON:", json.dumps(summary))

    if show_examples > 0 and example_buffer:
        print("\n=== Example predictions (first few) ===")
        for idx, ex in enumerate(example_buffer[:show_examples], start=1):
            print(f"\n--- Example {idx} ---")
            print("True label:", ex["true"])
            print("Pred label:", ex["pred"])
            print("Raw generation:", repr(ex["gen"]))
            print("\nPrompt:")
            print(ex["prompt"])


def main() -> None:
    cfg = parse_args()

    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True

    ds = load_raw_split(cfg.jsonl_dir, cfg.split)
    prompts, labels = prepare_prompts_and_labels(
        ds,
        cfg.max_samples,
        num_shards=cfg.num_shards,
        shard_index=cfg.shard_index,
    )
    print(
        f"Loaded split '{cfg.split}' from {cfg.jsonl_dir} with {len(ds)} total samples; "
        f"shard {cfg.shard_index + 1}/{cfg.num_shards} has {len(prompts)} samples.",
        flush=True,
    )

    # Base tokenizer: either from LoRA dir (if evaluating only LoRA) or from model_name.
    tokenizer_src = (
        str(cfg.lora_dir) if cfg.mode in ("lora", "both") and cfg.lora_dir is not None else cfg.model_name
    )
    tokenizer = setup_tokenizer(tokenizer_src)

    # 1) Evaluate base model (if requested)
    if cfg.mode in ("base", "both"):
        print("\n--- Loading base model ---")
        base_model = load_base_model(
            model_name=cfg.model_name,
            load_in_4bit=cfg.load_in_4bit,
            device=cfg.device,
        )
        run_eval_for_model(
            model=base_model,
            tokenizer=tokenizer,
            prompts=prompts,
            labels=labels,
            max_seq_len=cfg.max_seq_len,
            max_new_tokens=cfg.max_new_tokens,
            show_examples=cfg.show_examples,
            print_predictions=cfg.print_predictions,
            model_tag="base",
            num_shards=cfg.num_shards,
            shard_index=cfg.shard_index,
        )
        del base_model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    # 2) Evaluate LoRA‑fine‑tuned model (if requested)
    if cfg.mode in ("lora", "both"):
        assert cfg.lora_dir is not None
        print("\n--- Loading base model for LoRA ---")
        base_model_for_lora = load_base_model(
            model_name=cfg.model_name,
            load_in_4bit=cfg.load_in_4bit,
            device=cfg.device,
        )
        print(f"--- Wrapping base model with LoRA from {cfg.lora_dir} ---")
        lora_model = wrap_with_lora(
            base_model=base_model_for_lora,
            lora_dir=cfg.lora_dir,
            device=cfg.device,
        )
        run_eval_for_model(
            model=lora_model,
            tokenizer=tokenizer,
            prompts=prompts,
            labels=labels,
            max_seq_len=cfg.max_seq_len,
            max_new_tokens=cfg.max_new_tokens,
            show_examples=cfg.show_examples,
            print_predictions=cfg.print_predictions,
            model_tag="lora",
            num_shards=cfg.num_shards,
            shard_index=cfg.shard_index,
        )
        del lora_model
        del base_model_for_lora
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


if __name__ == "__main__":
    main()

