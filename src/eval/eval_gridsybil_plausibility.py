"""Evaluate GridSybil message-level plausibility binary classification (0/1 generation)."""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Set, Tuple

import torch
import yaml
from peft import PeftModel
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    PreTrainedModel,
    PreTrainedTokenizerBase,
)

from ..training.dataset_gridsybil_plausibility import load_parquet_rows
from ..training.gridsybil_plausibility_utils import (
    PROMPT_VARIANTS,
    build_plausibility_prompt,
    parse_prediction_label,
)

EvalMode = Literal["base", "lora", "both"]


@dataclass
class EvalConfig:
    model_name: str
    parquet_dir: Path
    split: str
    mode: EvalMode
    lora_dir: Path | None
    max_samples: int | None
    max_seq_len: int
    reserve_answer_tokens: int
    simulate_budget_cutoff: bool
    max_new_tokens: int
    load_in_4bit: bool
    show_examples: int
    print_predictions: int
    device: str | None
    num_shards: int
    shard_index: int
    seed: int
    run_id: str
    prompt_variant: str
    prompt_exclude_columns: Set[str]
    prompt_include_prefixes: List[str]
    prompt_include_columns: List[str]
    feature_name_style: str
    progress_every_samples: int


def load_yaml_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"Config {path} must be a mapping.")
    return cfg


def parse_args() -> EvalConfig:
    p = argparse.ArgumentParser(description="Evaluate GridSybil plausibility (base and/or LoRA).")
    p.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_gridsybil_plausibility.yaml",
        help="Training YAML (model_name, parquet_dir, max_seq_len, reserve_answer_tokens, ...).",
    )
    p.add_argument(
        "--split",
        type=str,
        default="val",
        choices=["train", "val", "test"],
        help="Parquet split (default val).",
    )
    p.add_argument(
        "--mode",
        type=str,
        default="base",
        choices=["base", "lora", "both"],
    )
    p.add_argument("--lora_dir", type=str, default=None)
    p.add_argument(
        "--max_samples",
        type=int,
        default=1024,
        help="Max examples from split (after sharding). 0 = all.",
    )
    p.add_argument(
        "--max_new_tokens",
        type=int,
        default=None,
        help="Override generation budget; default = reserve_answer_tokens from config.",
    )
    p.add_argument("--no_4bit", action="store_true")
    p.add_argument("--show_examples", type=int, default=5)
    p.add_argument("--print_predictions", type=int, default=8)
    p.add_argument("--device", type=str, default=None)
    p.add_argument("--num_shards", type=int, default=1)
    p.add_argument("--shard_index", type=int, default=0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--run_id",
        type=str,
        default=None,
        help="Run identifier used for shard metric integration.",
    )
    p.add_argument(
        "--prompt_variant",
        type=str,
        default=None,
        help="Override YAML prompt_variant. If unset, read from --config.",
    )
    p.add_argument(
        "--progress_every_samples",
        type=int,
        default=5000,
        help="Print progress every N evaluated samples (0 disables periodic progress).",
    )
    args = p.parse_args()

    cfg_path = Path(args.config)
    y = load_yaml_config(cfg_path)

    parquet_dir = Path(y.get("parquet_dir", "data/processed/plausibility_messages_split"))
    if not parquet_dir.is_absolute():
        parquet_dir = Path.cwd() / parquet_dir

    max_seq_len = int(y.get("max_seq_len", 4096))
    reserve = int(y.get("reserve_answer_tokens", 32))
    max_new = int(args.max_new_tokens) if args.max_new_tokens is not None else reserve
    prompt_variant = (
        str(args.prompt_variant).strip().lower()
        if args.prompt_variant is not None
        else str(y.get("prompt_variant", "default")).strip().lower()
    )
    if prompt_variant not in PROMPT_VARIANTS:
        raise ValueError(
            f"Invalid prompt_variant={prompt_variant!r}; expected one of {list(PROMPT_VARIANTS)}"
        )
    prompt_exclude_columns = {str(x) for x in y.get("prompt_exclude_columns", [])}
    prompt_include_prefixes = [str(x) for x in y.get("prompt_include_prefixes", [])]
    prompt_include_columns = [str(x) for x in y.get("prompt_include_columns", [])]
    feature_name_style = str(y.get("feature_name_style", "descriptive")).strip().lower()

    if args.mode in ("lora", "both") and args.lora_dir is None:
        raise ValueError("--lora_dir is required for mode=lora or both.")
    if args.num_shards <= 0:
        raise ValueError("--num_shards must be positive.")
    if not (0 <= args.shard_index < args.num_shards):
        raise ValueError("shard_index must be in [0, num_shards).")

    max_samples = None if args.max_samples == 0 else args.max_samples
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    return EvalConfig(
        model_name=str(y.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct")),
        parquet_dir=parquet_dir,
        split=args.split,
        mode=args.mode,  # type: ignore[arg-type]
        lora_dir=Path(args.lora_dir) if args.lora_dir else None,
        max_samples=max_samples,
        max_seq_len=max_seq_len,
        reserve_answer_tokens=reserve,
        simulate_budget_cutoff=bool(y.get("simulate_budget_cutoff", True)),
        max_new_tokens=max_new,
        load_in_4bit=not args.no_4bit,
        show_examples=max(0, args.show_examples),
        print_predictions=max(0, args.print_predictions),
        device=args.device,
        num_shards=args.num_shards,
        shard_index=args.shard_index,
        seed=args.seed,
        run_id=run_id,
        prompt_variant=prompt_variant,
        prompt_exclude_columns=prompt_exclude_columns,
        prompt_include_prefixes=prompt_include_prefixes,
        prompt_include_columns=prompt_include_columns,
        feature_name_style=feature_name_style,
        progress_every_samples=max(0, int(args.progress_every_samples)),
    )


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
    if load_in_4bit:
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            quantization_config=bnb_config,
            device_map="auto" if device is None else None,
        )
        if device is not None:
            model.to(device)
    else:
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            device_map="auto" if device is None else None,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        )
        if device is not None:
            model.to(device)
    model.eval()
    return model


def wrap_with_lora(
    base_model: PreTrainedModel,
    lora_dir: Path,
    device: str | None,
) -> PreTrainedModel:
    model = PeftModel.from_pretrained(base_model, str(lora_dir))
    if device is not None:
        model.to(device)
    model.eval()
    return model


def shard_and_limit_rows(
    rows: List[Dict[str, Any]],
    max_samples: int | None,
    num_shards: int,
    shard_index: int,
    seed: int,
) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    n = len(rows)
    limit = n if max_samples is None else min(max_samples, n)
    idxs = list(range(n))
    rng.shuffle(idxs)
    idxs = idxs[:limit]

    if num_shards > 1:
        shard_size = (len(idxs) + num_shards - 1) // num_shards
        start = shard_index * shard_size
        end = min(start + shard_size, len(idxs))
        if start >= end:
            return []
        idxs = idxs[start:end]
    return [rows[i] for i in idxs]


def aggregate_metrics(tp: int, fp: int, fn: int, tn: int) -> Dict[str, float]:
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    tot = tp + tn + fp + fn
    acc = (tp + tn) / tot if tot > 0 else 0.0
    return {
        "accuracy": acc,
        "precision": prec,
        "recall": rec,
        "f1": f1,
    }


def run_eval(
    model: PreTrainedModel,
    tokenizer: PreTrainedTokenizerBase,
    rows: List[Dict[str, Any]],
    cfg: EvalConfig,
    model_tag: str,
) -> None:
    device = next(model.parameters()).device
    tp = fp = fn = tn = 0
    parse_ok_n = 0
    prompt_too_long = 0
    n = len(rows)
    examples: List[Dict[str, Any]] = []

    print(f"Evaluating {model_tag} on {n} samples (split={cfg.split})...", flush=True)
    t0 = time.time()

    for i, ex in enumerate(rows):
        gold = int(ex.get("label", 0))
        pb = build_plausibility_prompt(
            sample=ex,
            tokenizer=tokenizer,
            simulate_budget_cutoff=cfg.simulate_budget_cutoff,
            total_budget=cfg.max_seq_len,
            reserve_answer_tokens=cfg.reserve_answer_tokens,
            prompt_variant=cfg.prompt_variant,
            feature_name_style=cfg.feature_name_style,
            include_prefixes=cfg.prompt_include_prefixes,
            include_columns=cfg.prompt_include_columns,
            exclude_columns=cfg.prompt_exclude_columns,
        )
        enc = tokenizer(
            pb.prompt_text,
            return_tensors="pt",
            add_special_tokens=False,
            truncation=False,
        )
        prompt_len = enc["input_ids"].shape[1]
        if prompt_len > cfg.max_seq_len:
            prompt_too_long += 1
            continue
        enc = {k: v.to(device) for k, v in enc.items()}
        if "attention_mask" not in enc:
            enc["attention_mask"] = torch.ones_like(enc["input_ids"])

        with torch.no_grad():
            gen_ids = model.generate(
                **enc,
                max_new_tokens=cfg.max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )
        input_len = enc["input_ids"].shape[1]
        gen_text = tokenizer.decode(gen_ids[0][input_len:], skip_special_tokens=True)
        pred, ok = parse_prediction_label(gen_text)
        if ok:
            parse_ok_n += 1
        pred = 0 if pred is None else int(pred)

        if gold == 1 and pred == 1:
            tp += 1
        elif gold == 0 and pred == 1:
            fp += 1
        elif gold == 1 and pred == 0:
            fn += 1
        else:
            tn += 1

        if i < cfg.print_predictions:
            print(
                f"[{i + 1}] gold={gold} pred={pred} parse_ok={ok} raw={gen_text!r}",
                flush=True,
            )
        if gold != pred and len(examples) < cfg.show_examples:
            examples.append(
                {
                    "idx": i,
                    "gold": gold,
                    "pred": pred,
                    "parse_ok": ok,
                    "raw_generation": gen_text,
                    "episode_id": ex.get("episode_id"),
                    "message_id": ex.get("message_id"),
                    "sender_id": ex.get("sender_id"),
                }
            )
        if (
            (cfg.progress_every_samples > 0 and (i + 1) % cfg.progress_every_samples == 0)
            or (i + 1) == n
        ):
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0.0
            remaining = max(0, n - (i + 1))
            eta_s = (remaining / rate) if rate > 0 else float("inf")
            eta_text = f"{eta_s:.1f}s" if eta_s != float("inf") else "inf"
            print(
                f"  progress {i + 1}/{n} "
                f"elapsed={elapsed:.1f}s rate={rate:.2f} samples/s eta={eta_text}",
                flush=True,
            )

    metrics = aggregate_metrics(tp, fp, fn, tn)
    elapsed = time.time() - t0
    print(f"\n=== Results ({model_tag}) ===", flush=True)
    print(f"split={cfg.split} samples_attempted={n} wall_s={elapsed:.1f}", flush=True)
    print(
        f"prompt_too_long_skips={prompt_too_long} parse_ok={parse_ok_n}/{n}",
        flush=True,
    )
    print(f"binary_counts: TP={tp} FP={fp} FN={fn} TN={tn}", flush=True)
    print(
        f"accuracy={metrics['accuracy']:.4f} precision={metrics['precision']:.4f} "
        f"recall={metrics['recall']:.4f} f1={metrics['f1']:.4f}",
        flush=True,
    )
    print(
        "METRICS_JSON:",
        json.dumps(
            {
                "run_id": cfg.run_id,
                "model_tag": model_tag,
                "split": cfg.split,
                "num_shards": cfg.num_shards,
                "shard_index": cfg.shard_index,
                "n": n,
                "prompt_too_long": prompt_too_long,
                "parse_ok": parse_ok_n,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
                **metrics,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if examples and cfg.show_examples > 0:
        print("\n=== Example mismatches (up to N) ===", flush=True)
        for j, e in enumerate(examples[: cfg.show_examples], start=1):
            print(f"\n--- Example {j} ---", flush=True)
            print(json.dumps(e, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    cfg = parse_args()
    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True

    split_path = cfg.parquet_dir / f"{cfg.split}.parquet"
    if not split_path.exists():
        raise FileNotFoundError(f"Missing split file: {split_path}")

    all_rows = load_parquet_rows(split_path)
    rows = shard_and_limit_rows(
        all_rows,
        cfg.max_samples,
        cfg.num_shards,
        cfg.shard_index,
        cfg.seed,
    )

    print("Eval config:", flush=True)
    print(
        json.dumps(
            {
                "model_name": cfg.model_name,
                "parquet_dir": str(cfg.parquet_dir),
                "split": cfg.split,
                "mode": cfg.mode,
                "max_samples": cfg.max_samples,
                "shard": f"{cfg.shard_index + 1}/{cfg.num_shards}",
                "max_seq_len": cfg.max_seq_len,
                "reserve_answer_tokens": cfg.reserve_answer_tokens,
                "max_new_tokens": cfg.max_new_tokens,
                "simulate_budget_cutoff": cfg.simulate_budget_cutoff,
                "seed": cfg.seed,
                "run_id": cfg.run_id,
                "prompt_variant": cfg.prompt_variant,
                "prompt_exclude_columns": sorted(cfg.prompt_exclude_columns),
                "prompt_include_prefixes": list(cfg.prompt_include_prefixes),
                "prompt_include_columns": sorted(cfg.prompt_include_columns),
                "feature_name_style": cfg.feature_name_style,
            },
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )
    print(
        f"Loaded {len(all_rows)} rows from {split_path}; evaluating {len(rows)} after sample+shard.",
        flush=True,
    )

    tokenizer_src = (
        str(cfg.lora_dir)
        if cfg.mode in ("lora", "both") and cfg.lora_dir is not None
        else cfg.model_name
    )
    tokenizer = setup_tokenizer(tokenizer_src)

    if cfg.mode in ("base", "both"):
        print("\n--- Base model ---", flush=True)
        base = load_base_model(cfg.model_name, cfg.load_in_4bit, cfg.device)
        run_eval(base, tokenizer, rows, cfg, "base")
        del base
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    if cfg.mode in ("lora", "both"):
        assert cfg.lora_dir is not None
        print("\n--- LoRA ---", flush=True)
        base2 = load_base_model(cfg.model_name, cfg.load_in_4bit, cfg.device)
        lora_model = wrap_with_lora(base2, cfg.lora_dir, cfg.device)
        run_eval(lora_model, tokenizer, rows, cfg, "lora")
        del lora_model
        del base2
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
