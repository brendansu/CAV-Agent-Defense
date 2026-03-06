"""LoRA fine-tuning script for Phase 1 binary attack detection using Qwen2.5-1.5B.

This script:
- Loads phase1_binary JSONL datasets (train/val) via `training.dataset_phase1`
- Builds a Qwen2.5-1.5B model with 4-bit QLoRA
- Runs causal LM fine-tuning with HuggingFace Trainer

Typical usage (from repo root):

    python src/training/train_lora_qwen_phase1_binary.py ^
        --config configs/qwen2.5_1.5b_phase1_binary.yaml
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict

import torch
import torch.nn.functional as F
import yaml
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)
from peft import LoraConfig, get_peft_model

from .dataset_phase1 import load_phase1_binary_datasets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_phase1_binary.yaml",
        help="Path to YAML config file.",
    )
    # 可选：允许从命令行覆盖输出目录等
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


def setup_tokenizer_and_model(
    model_name: str,
    lora_r: int,
    lora_alpha: int,
    lora_dropout: float,
    target_modules: list[str],
) -> tuple[AutoTokenizer, torch.nn.Module]:
    """Load 4-bit QLoRA model and tokenizer."""
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    # 确保有 pad_token
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )

    base_model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        device_map="auto",
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
    # 可选：打印可训练参数比例
    trainable, total = 0, 0
    for _, p in model.named_parameters():
        numel = p.numel()
        total += numel
        if p.requires_grad:
            trainable += numel
    print(
        f"Trainable params: {trainable} / {total} "
        f"({100 * trainable / max(1,total):.2f}%)"
    )

    return tokenizer, model


def build_training_arguments(
    config: Dict[str, Any],
    train_dataset_size: int,
) -> TrainingArguments:
    """
    根据配置和 train 数据集大小构造 TrainingArguments。
    这里使用 warmup_steps 代替 warmup_ratio，以避免弃用警告。
    """
    output_dir = config.get("output_dir", "outputs/qwen2.5-1.5b-phase1-binary")

    per_device_train_batch_size = int(config.get("per_device_train_batch_size", 1))
    gradient_accumulation_steps = int(config.get("gradient_accumulation_steps", 8))
    num_train_epochs = float(config.get("num_train_epochs", 1.0))
    warmup_ratio = float(config.get("warmup_ratio", 0.03))

    # 单机单卡场景下，global_batch_size = per_device_train_batch_size
    steps_per_epoch = math.ceil(
        train_dataset_size / (per_device_train_batch_size * gradient_accumulation_steps)
    )
    total_training_steps = int(steps_per_epoch * num_train_epochs)
    warmup_steps = int(total_training_steps * warmup_ratio)
    print(
        f"Estimated steps_per_epoch={steps_per_epoch}, "
        f"total_training_steps={total_training_steps}, "
        f"warmup_steps={warmup_steps}"
    )

    # ⬇️ 从 YAML 读取 eval / save 相关配置
    eval_strategy = config.get("eval_strategy", "no")
    eval_steps = int(config.get("eval_steps", 500))
    save_strategy = config.get("save_strategy", "steps")
    save_steps = int(config.get("save_steps", 500))

    args = TrainingArguments(
        output_dir=output_dir,
        per_device_train_batch_size=per_device_train_batch_size,
        per_device_eval_batch_size=int(config.get("per_device_eval_batch_size", 1)),
        gradient_accumulation_steps=gradient_accumulation_steps,
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
        load_best_model_at_end=True,          # 可选
        metric_for_best_model="eval_loss",    # 可选
        greater_is_better=False,              # 可选
    )
    return args


class CausalLMTrainer(Trainer):
    """
    自定义 Trainer：支持按类别加权的 answer loss。
    - BENIGN：全序列 loss（鼓励有逻辑地“抄写”一致轨迹）。
    - ATTACK：仅对答案 token 算 loss 并乘 answer_loss_weight_attack（不鼓励学抄攻击内容）。
    """

    def __init__(self, answer_loss_weight_attack: float = 0.0, **kwargs):
        super().__init__(**kwargs)
        # 0 或未设置时沿用默认全序列 loss
        self._answer_loss_weight_attack = answer_loss_weight_attack

    def compute_loss(
        self,
        model,
        inputs,
        return_outputs: bool = False,
        num_items_in_batch: int | None = None,
        **kwargs
    ):
        labels = inputs.pop("labels", None)
        if labels is None:
            labels = inputs["input_ids"].clone()

        is_attack = inputs.pop("is_attack", None)
        length = inputs.pop("length", None)
        use_weighted = (
            self._answer_loss_weight_attack > 0
            and is_attack is not None
            and length is not None
        )

        if not use_weighted:
            outputs = model(**inputs, labels=labels)
            loss = outputs.loss
            if return_outputs:
                return loss, outputs
            return loss

        # 加权 loss：BENIGN 全序列，ATTACK 仅答案位置
        outputs = model(**inputs)
        logits = outputs.logits  # (B, L, V)
        B, L, V = logits.shape
        device = logits.device
        if labels.dim() == 1:
            labels = labels.unsqueeze(0)
        is_attack = is_attack.to(device)
        length = length.to(device)

        per_sample_losses = []
        for i in range(B):
            li = length[i].item()
            if li < 2:
                li = 2
            if is_attack[i].item() == 0:
                # BENIGN: 全序列 causal LM loss（logits[:li-1] 预测 labels[1:li]）
                logits_i = logits[i, : li - 1].reshape(-1, V)
                labels_i = labels[i, 1:li].reshape(-1)
                loss_i = F.cross_entropy(logits_i, labels_i)
            else:
                # ATTACK: 仅答案位置，并加权
                loss_i = F.cross_entropy(
                    logits[i, li - 2].unsqueeze(0),
                    labels[i, li - 1].unsqueeze(0),
                ) * self._answer_loss_weight_attack
            per_sample_losses.append(loss_i)
        loss = torch.stack(per_sample_losses).mean()
        if return_outputs:
            return loss, outputs
        return loss


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    # 允许从命令行覆盖输出目录
    if args.output_dir is not None:
        config["output_dir"] = args.output_dir

    model_name = config.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct")
    jsonl_dir = config.get("jsonl_dir", "data/processed/jsonl/phase1_binary")
    max_seq_len = int(config.get("max_seq_len", 512))

    lora_r = int(config.get("lora_r", 16))
    lora_alpha = int(config.get("lora_alpha", 32))
    lora_dropout = float(config.get("lora_dropout", 0.05))
    target_modules = config.get(
        "target_modules",
        ["q_proj", "k_proj", "v_proj", "o_proj"],
    )

    print("Loaded config:")
    print(json.dumps(config, indent=2))

    # 一些 CUDA 性能/数值设置
    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True

    # 初始化 tokenizer & LoRA 模型
    tokenizer, model = setup_tokenizer_and_model(
        model_name=model_name,
        lora_r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        target_modules=target_modules,
    )

    # 加载并 tokenization Phase 1 二分类数据集
    train_ds, val_ds = load_phase1_binary_datasets(
        jsonl_dir=jsonl_dir,
        tokenizer=tokenizer,
        max_seq_len=max_seq_len,
    )

    print(f"Train dataset size: {len(train_ds)}")
    print(f"Val dataset size:   {len(val_ds)}")

    # 可选：仅使用 val 子集进行训练时的周期性 eval，以减少 eval 时间
    max_eval_samples = int(config.get("max_eval_samples", 0))
    if max_eval_samples > 0 and len(val_ds) > max_eval_samples:
        eval_ds = val_ds.select(range(max_eval_samples))
        print(f"Eval subset size:   {len(eval_ds)} (max_eval_samples={max_eval_samples})")
    else:
        eval_ds = val_ds
        print("Eval subset size:   full validation set")

    # 构建 TrainingArguments 和 Trainer
    training_args = build_training_arguments(config, train_dataset_size=len(train_ds))
    answer_loss_weight_attack = float(config.get("answer_loss_weight_attack", 0.0))
    trainer = CausalLMTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        answer_loss_weight_attack=answer_loss_weight_attack,
    )

    # 训练
    trainer.train()
    trainer.save_model(training_args.output_dir)
    tokenizer.save_pretrained(training_args.output_dir)
    print(f"Training complete. Model saved to {training_args.output_dir}")


if __name__ == "__main__":
    main()

