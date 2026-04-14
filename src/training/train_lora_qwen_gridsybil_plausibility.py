"""LoRA fine-tuning script for GridSybil message-level plausibility classification.

This script:
- Loads train/val/test parquet splits via `training.dataset_gridsybil_plausibility`
- Builds a Qwen2.5 model with 4-bit QLoRA
- Runs causal-LM style SFT where target token is binary label text (benign/attack)
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Dict

import torch
import torch.nn.functional as F
import yaml
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

from .dataset_gridsybil_plausibility import load_gridsybil_plausibility_datasets
from .gridsybil_plausibility_utils import PROMPT_VARIANTS


class WeightedLabelTrainer(Trainer):
    """Apply per-example class weights on supervised target tokens."""

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        labels = inputs.pop("labels")
        sample_weights = inputs.pop("label_weight", None)
        # Keep label for logging/statistics but do not feed to model.
        _ = inputs.pop("label", None)

        outputs = model(**inputs)
        logits = outputs.get("logits")
        if logits is None:
            loss = outputs["loss"] if isinstance(outputs, dict) else outputs.loss
            return (loss, outputs) if return_outputs else loss

        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()
        vocab_size = shift_logits.size(-1)
        token_loss = F.cross_entropy(
            shift_logits.view(-1, vocab_size),
            shift_labels.view(-1),
            reduction="none",
            ignore_index=-100,
        ).view(shift_labels.size())
        token_mask = (shift_labels != -100).float()
        per_sample_den = token_mask.sum(dim=1).clamp_min(1.0)
        per_sample_loss = (token_loss * token_mask).sum(dim=1) / per_sample_den

        if sample_weights is None:
            loss = per_sample_loss.mean()
        else:
            w = sample_weights.float().to(per_sample_loss.device)
            loss = (per_sample_loss * w).sum() / w.sum().clamp_min(1e-8)

        return (loss, outputs) if return_outputs else loss


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_gridsybil_plausibility.yaml",
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
        "outputs/qwen2.5-1.5b-gridsybil-plausibility",
    )
    per_device_train_batch_size = int(config.get("per_device_train_batch_size", 1))
    gradient_accumulation_steps = int(config.get("gradient_accumulation_steps", 8))
    num_train_epochs = float(config.get("num_train_epochs", 1.0))
    warmup_ratio = float(config.get("warmup_ratio", 0.03))
    max_steps = int(config.get("max_steps", -1))
    world_size = _distributed_world_size()
    denom = per_device_train_batch_size * gradient_accumulation_steps * max(1, world_size)
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
                f"total_training_steps={total_training_steps}, warmup_steps={warmup_steps}",
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

    label1 = int(sum(ds["label"]))
    label0 = int(n - label1)
    trunc_ratio = _mean("is_truncated_features")
    prompt_len_mean = _mean("prompt_length")
    target_len_mean = _mean("target_length")
    features_total_mean = _mean("features_total")
    features_kept_mean = _mean("features_kept")
    print(
        f"[{name}] n={n} label0={label0} label1={label1} "
        f"prompt_len_mean={prompt_len_mean:.1f} target_len_mean={target_len_mean:.1f} "
        f"features_total_mean={features_total_mean:.1f} features_kept_mean={features_kept_mean:.1f} "
        f"truncated_ratio={trunc_ratio:.3%}",
        flush=True,
    )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.output_dir is not None:
        config["output_dir"] = args.output_dir

    model_name = config.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct")
    parquet_dir = config.get(
        "parquet_dir",
        "data/processed/plausibility_messages_split",
    )
    max_seq_len = int(config.get("max_seq_len", 4096))
    reserve_answer_tokens = int(config.get("reserve_answer_tokens", 32))
    simulate_budget_cutoff = bool(config.get("simulate_budget_cutoff", True))
    add_eos_token = bool(config.get("add_eos_token", True))
    prompt_variant = str(config.get("prompt_variant", "default")).strip().lower()
    if prompt_variant not in PROMPT_VARIANTS:
        raise ValueError(
            f"Invalid prompt_variant={prompt_variant!r}; expected one of {list(PROMPT_VARIANTS)}"
        )
    prompt_exclude_columns = [str(x) for x in config.get("prompt_exclude_columns", [])]
    prompt_include_prefixes = [str(x) for x in config.get("prompt_include_prefixes", [])]
    prompt_include_columns = [str(x) for x in config.get("prompt_include_columns", [])]
    feature_name_style = str(config.get("feature_name_style", "descriptive")).strip().lower()
    attack_sample_weight = float(config.get("attack_sample_weight", 1.0))
    benign_sample_weight = float(config.get("benign_sample_weight", 1.0))
    max_train_rows = int(config.get("max_train_rows", 0))
    max_val_rows = int(config.get("max_val_rows", 0))
    max_test_rows = int(config.get("max_test_rows", 0))
    subsample_seed = int(config.get("subsample_seed", 42))

    lora_r = int(config.get("lora_r", 16))
    lora_alpha = int(config.get("lora_alpha", 32))
    lora_dropout = float(config.get("lora_dropout", 0.05))
    target_modules = config.get("target_modules", ["q_proj", "k_proj", "v_proj", "o_proj"])

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

    train_ds, val_ds, test_ds = load_gridsybil_plausibility_datasets(
        parquet_dir=parquet_dir,
        tokenizer=tokenizer,
        max_seq_len=max_seq_len,
        reserve_answer_tokens=reserve_answer_tokens,
        simulate_budget_cutoff=simulate_budget_cutoff,
        add_eos_token=add_eos_token,
        prompt_variant=prompt_variant,
        prompt_exclude_columns=prompt_exclude_columns,
        prompt_include_prefixes=prompt_include_prefixes,
        prompt_include_columns=prompt_include_columns,
        feature_name_style=feature_name_style,
        attack_sample_weight=attack_sample_weight,
        benign_sample_weight=benign_sample_weight,
        max_train_rows=max_train_rows,
        max_val_rows=max_val_rows,
        max_test_rows=max_test_rows,
        subsample_seed=subsample_seed,
    )

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
    trainer = WeightedLabelTrainer(
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
