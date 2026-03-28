"""LoRA fine-tuning script for GridSybil pseudo-entity identification using Qwen2.5-1.5B.

This script:
- Loads GridSybil pseudo-ident JSONL datasets via `training.dataset_gridsybil_pseudo_ident`
- Builds a Qwen2.5-1.5B model with 4-bit QLoRA
- Runs causal LM fine-tuning with HuggingFace Trainer

Typical usage (from repo root):

    python -m src.training.train_lora_qwen_gridsybil_pseudo_ident ^
        --config configs/qwen2.5_1.5b_gridsybil_pseudo_ident.yaml

Single-node multi-GPU (DDP; one 4-bit replica per GPU):

    torchrun --standalone --nproc_per_node=4 -m src.training.train_lora_qwen_gridsybil_pseudo_ident ^
        --config configs/qwen2.5_1.5b_gridsybil_pseudo_ident_hpc.yaml
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Dict

import torch
import yaml
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

from .dataset_gridsybil_pseudo_ident import (
    load_gridsybil_pseudo_ident_datasets,
    maybe_resample_low_attacker_rows,
    summarize_attacker_bucket_distribution,
)
from .gridsybil_pseudo_ident_utils import PROMPT_VARIANTS



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_gridsybil_pseudo_ident.yaml",
        help="Path to YAML config file.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=None,
        help="Override output_dir in config (optional).",
    )
    return parser.parse_args()


def load_config(path: str) -> Dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"Config file {cfg_path} did not contain a mapping.")
    return cfg


def _distributed_world_size() -> int:
    return int(os.environ.get("WORLD_SIZE", "1"))


def _local_rank() -> int:
    # torchrun / torch.distributed.launch set LOCAL_RANK per process
    return int(os.environ.get("LOCAL_RANK", "0"))


def setup_tokenizer_and_model(
    model_name: str,
    lora_r: int,
    lora_alpha: int,
    lora_dropout: float,
    target_modules: list[str],
) -> tuple[AutoTokenizer, torch.nn.Module]:
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )

    local_rank = _local_rank()
    world_size = _distributed_world_size()
    if torch.cuda.is_available():
        torch.cuda.set_device(local_rank)
        # One full model per DDP process; avoid device_map="auto" spanning all GPUs.
        device_map: dict[str, int] | str = {"": local_rank}
    else:
        device_map = "cpu"

    if local_rank == 0:
        print(
            f"QLoRA load: device_map={device_map!r} LOCAL_RANK={local_rank} "
            f"WORLD_SIZE={world_size}",
            flush=True,
        )

    base_model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        device_map=device_map,
    )

    lora_config = LoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        target_modules=target_modules,
        lora_dropout=lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
    )

    model = get_peft_model(base_model, lora_config)
    trainable, total = 0, 0
    for _, p in model.named_parameters():
        numel = p.numel()
        total += numel
        if p.requires_grad:
            trainable += numel
    if _local_rank() == 0:
        print(
            f"Trainable params: {trainable} / {total} "
            f"({100 * trainable / max(1, total):.2f}%)",
            flush=True,
        )
    return tokenizer, model


def build_training_arguments(
    config: Dict[str, Any],
    train_dataset_size: int,
) -> TrainingArguments:
    output_dir = config.get(
        "output_dir",
        "outputs/qwen2.5-1.5b-gridsybil-pseudo-ident",
    )

    per_device_train_batch_size = int(config.get("per_device_train_batch_size", 1))
    gradient_accumulation_steps = int(config.get("gradient_accumulation_steps", 8))
    num_train_epochs = float(config.get("num_train_epochs", 1.0))
    warmup_ratio = float(config.get("warmup_ratio", 0.03))
    max_steps = int(config.get("max_steps", -1))
    world_size = _distributed_world_size()
    denom = (
        per_device_train_batch_size
        * gradient_accumulation_steps
        * max(1, world_size)
    )
    steps_per_epoch = math.ceil(train_dataset_size / denom)

    if max_steps > 0:
        total_training_steps = max_steps
        warmup_steps = int(total_training_steps * warmup_ratio)
        if _local_rank() == 0:
            print(
                f"Using max_steps={max_steps} (overrides num_train_epochs). "
                f"steps_per_epoch≈{steps_per_epoch} (WORLD_SIZE={world_size}), "
                f"warmup_steps={warmup_steps}",
                flush=True,
            )
    else:
        total_training_steps = int(steps_per_epoch * num_train_epochs)
        warmup_steps = int(total_training_steps * warmup_ratio)
        if _local_rank() == 0:
            print(
                f"Estimated steps_per_epoch≈{steps_per_epoch} (WORLD_SIZE={world_size}), "
                f"total_training_steps={total_training_steps}, "
                f"warmup_steps={warmup_steps}",
                flush=True,
            )

    eval_strategy = config.get("eval_strategy", "steps")
    eval_steps = int(config.get("eval_steps", 500))
    save_strategy = config.get("save_strategy", "steps")
    save_steps = int(config.get("save_steps", 500))

    return TrainingArguments(
        output_dir=output_dir,
        per_device_train_batch_size=per_device_train_batch_size,
        per_device_eval_batch_size=int(config.get("per_device_eval_batch_size", 1)),
        gradient_accumulation_steps=gradient_accumulation_steps,
        max_steps=max_steps,
        num_train_epochs=num_train_epochs,
        learning_rate=float(config.get("learning_rate", 1e-4)),
        lr_scheduler_type=config.get("lr_scheduler_type", "cosine"),
        warmup_steps=warmup_steps,
        logging_steps=int(config.get("logging_steps", 50)),
        save_total_limit=int(config.get("save_total_limit", 2)),
        fp16=True,
        bf16=False,
        gradient_checkpointing=True,
        report_to=config.get("report_to", []),
        eval_strategy=eval_strategy,
        eval_steps=eval_steps,
        save_strategy=save_strategy,
        save_steps=save_steps,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
    )


def summarize_dataset(ds, name: str) -> None:
    n = len(ds)
    if n == 0:
        print(f"[{name}] empty dataset", flush=True)
        return

    def _mean(col: str) -> float:
        values = ds[col]
        return float(sum(values) / max(1, len(values)))

    hidden_ratio = _mean("hidden_attacker")
    trunc_ratio = _mean("is_truncated_entities")
    prompt_len_mean = _mean("prompt_length")
    target_len_mean = _mean("target_length")
    visible_attackers_mean = _mean("n_visible_attackers")
    visible_candidates_mean = _mean("n_visible_candidates")
    hidden_attackers_mean = _mean("n_attackers_hidden")
    print(
        f"[{name}] n={n} prompt_len_mean={prompt_len_mean:.1f} "
        f"target_len_mean={target_len_mean:.1f} "
        f"visible_candidates_mean={visible_candidates_mean:.2f} "
        f"visible_attackers_mean={visible_attackers_mean:.2f} "
        f"hidden_attackers_mean={hidden_attackers_mean:.2f} "
        f"truncated_ratio={trunc_ratio:.3%} hidden_attacker_ratio={hidden_ratio:.3%}",
        flush=True,
    )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    if args.output_dir is not None:
        config["output_dir"] = args.output_dir

    model_name = config.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct")
    jsonl_dir = config.get(
        "jsonl_dir",
        "data/processed/jsonl/gridsybil_pseudo_ident_balanced_a",
    )
    max_seq_len = int(config.get("max_seq_len", 4096))
    reserve_answer_tokens = int(config.get("reserve_answer_tokens", 256))
    entity_sort_policy = str(
        config.get("entity_sort_policy", "msgs_lifetime_distance")
    )
    simulate_budget_cutoff = bool(config.get("simulate_budget_cutoff", True))
    add_eos_token = bool(config.get("add_eos_token", True))
    prompt_variant = str(config.get("prompt_variant", "default")).strip().lower()
    if prompt_variant not in PROMPT_VARIANTS:
        raise ValueError(
            f"Invalid prompt_variant={prompt_variant!r}; expected one of {list(PROMPT_VARIANTS)}"
        )

    lora_r = int(config.get("lora_r", 16))
    lora_alpha = int(config.get("lora_alpha", 32))
    lora_dropout = float(config.get("lora_dropout", 0.05))
    target_modules = config.get(
        "target_modules",
        ["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    resample_low_attacker_enabled = bool(
        config.get("resample_low_attacker_enabled", False)
    )
    resample_seed = int(config.get("resample_seed", 42))
    resample_target_size_multiplier = float(
        config.get("resample_target_size_multiplier", 1.0)
    )
    default_bucket_weights = {
        "0_to_1": 3.0,
        "2_to_4": 2.0,
        "5_to_8": 1.0,
        "9_to_14": 1.0,
        "more_than_14": 1.0,
    }
    resample_bucket_weights_raw = config.get(
        "resample_bucket_weights",
        default_bucket_weights,
    )
    if not isinstance(resample_bucket_weights_raw, dict):
        raise ValueError("resample_bucket_weights must be a mapping in config.")
    resample_bucket_weights: Dict[str, float] = {}
    for k, v in resample_bucket_weights_raw.items():
        resample_bucket_weights[str(k)] = float(v)

    if _local_rank() == 0:
        print("Loaded config:", flush=True)
        print(json.dumps(config, indent=2), flush=True)

    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True

    tokenizer, model = setup_tokenizer_and_model(
        model_name=model_name,
        lora_r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        target_modules=target_modules,
    )

    train_ds, val_ds, test_ds = load_gridsybil_pseudo_ident_datasets(
        jsonl_dir=jsonl_dir,
        tokenizer=tokenizer,
        max_seq_len=max_seq_len,
        reserve_answer_tokens=reserve_answer_tokens,
        entity_sort_policy=entity_sort_policy,
        simulate_budget_cutoff=simulate_budget_cutoff,
        add_eos_token=add_eos_token,
        prompt_variant=prompt_variant,
    )

    if resample_low_attacker_enabled:
        if not hasattr(train_ds, "rows"):
            raise TypeError(
                "Resampling requires list-backed train dataset with .rows attribute."
            )
        before_counts = summarize_attacker_bucket_distribution(train_ds.rows)
        resampled_rows = maybe_resample_low_attacker_rows(
            train_rows=train_ds.rows,
            enabled=True,
            bucket_weights=resample_bucket_weights,
            size_multiplier=resample_target_size_multiplier,
            seed=resample_seed,
        )
        train_ds = train_ds.__class__(resampled_rows)
        after_counts = summarize_attacker_bucket_distribution(train_ds.rows)
        if _local_rank() == 0:
            print(
                f"[resample] enabled={resample_low_attacker_enabled} "
                f"seed={resample_seed} "
                f"size_multiplier={resample_target_size_multiplier}",
                flush=True,
            )
            print(
                f"[resample] bucket_weights={json.dumps(resample_bucket_weights, ensure_ascii=False)}",
                flush=True,
            )
            print(f"[resample] train bucket before: {before_counts}", flush=True)
            print(f"[resample] train bucket after : {after_counts}", flush=True)
    elif _local_rank() == 0:
        print("[resample] enabled=False", flush=True)

    if _local_rank() == 0:
        print(f"Train dataset size: {len(train_ds)}", flush=True)
        print(f"Val dataset size:   {len(val_ds)}", flush=True)
        print(f"Test dataset size:  {len(test_ds)}", flush=True)
        summarize_dataset(train_ds, "train")
        summarize_dataset(val_ds, "val")
        summarize_dataset(test_ds, "test")

    max_eval_samples = int(config.get("max_eval_samples", 0))
    if max_eval_samples > 0 and len(val_ds) > max_eval_samples:
        eval_ds = val_ds.select(range(max_eval_samples))
        if _local_rank() == 0:
            print(
                f"Eval subset size:   {len(eval_ds)} (max_eval_samples={max_eval_samples})",
                flush=True,
            )
    else:
        eval_ds = val_ds
        if _local_rank() == 0:
            print("Eval subset size:   full validation set", flush=True)

    training_args = build_training_arguments(config, train_dataset_size=len(train_ds))
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
    )

    trainer.train()
    if _local_rank() == 0:
        trainer.save_model(training_args.output_dir)
        tokenizer.save_pretrained(training_args.output_dir)
        print(
            f"Training complete. Model saved to {training_args.output_dir}",
            flush=True,
        )


if __name__ == "__main__":
    main()
