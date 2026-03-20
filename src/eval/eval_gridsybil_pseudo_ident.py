"""Evaluate GridSybil pseudo-entity identification (generative JSON array).

Uses the same prompt + entity truncation as training (`build_pseudo_ident_prompt`).
Gold labels for metrics are `visible_output_ids`; the visible candidate universe is
`visible_candidate_ids` (for micro TP/FP/FN/TN per pseudo).

Typical usage (repo root):

    # Smoke on val (default), base model, small sample
    python -m src.eval.eval_gridsybil_pseudo_ident ^
        --config configs/qwen2.5_1.5b_gridsybil_pseudo_ident.yaml

    # LoRA checkpoint
    python -m src.eval.eval_gridsybil_pseudo_ident ^
        --config configs/qwen2.5_1.5b_gridsybil_pseudo_ident.yaml ^
        --mode lora ^
        --lora_dir outputs/qwen2.5-1.5b-gridsybil-pseudo-ident
"""

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

from ..training.dataset_gridsybil_pseudo_ident import load_jsonl_rows
from ..training.dataset_sanity_gridsybil_pseudo_ident import extract_json_array
from ..training.gridsybil_pseudo_ident_utils import build_pseudo_ident_prompt

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
    reserve_answer_tokens: int
    entity_sort_policy: str
    simulate_budget_cutoff: bool
    max_new_tokens: int
    load_in_4bit: bool
    show_examples: int
    print_predictions: int
    device: str | None
    num_shards: int
    shard_index: int
    seed: int


def load_yaml_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"Config {path} must be a mapping.")
    return cfg


def parse_args() -> EvalConfig:
    p = argparse.ArgumentParser(
        description="Evaluate GridSybil pseudo-ident (base and/or LoRA)."
    )
    p.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_gridsybil_pseudo_ident.yaml",
        help="Training YAML (model_name, jsonl_dir, max_seq_len, reserve_answer_tokens, ...).",
    )
    p.add_argument(
        "--split",
        type=str,
        default="val",
        choices=["train", "val", "test"],
        help="JSONL split (default val to limit test exposure during smoke).",
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
        default=64,
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
    args = p.parse_args()

    cfg_path = Path(args.config)
    y = load_yaml_config(cfg_path)

    jsonl_dir = Path(y.get("jsonl_dir", "data/processed/jsonl/gridsybil_pseudo_ident_balanced_a"))
    if not jsonl_dir.is_absolute():
        jsonl_dir = Path.cwd() / jsonl_dir

    max_seq_len = int(y.get("max_seq_len", 4096))
    reserve = int(y.get("reserve_answer_tokens", 256))
    max_new = int(args.max_new_tokens) if args.max_new_tokens is not None else reserve

    if args.mode in ("lora", "both") and args.lora_dir is None:
        raise ValueError("--lora_dir is required for mode=lora or both.")

    if args.num_shards <= 0:
        raise ValueError("--num_shards must be positive.")
    if not (0 <= args.shard_index < args.num_shards):
        raise ValueError("shard_index must be in [0, num_shards).")

    max_samples = None if args.max_samples == 0 else args.max_samples

    return EvalConfig(
        model_name=str(y.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct")),
        jsonl_dir=jsonl_dir,
        split=args.split,
        mode=args.mode,  # type: ignore[arg-type]
        lora_dir=Path(args.lora_dir) if args.lora_dir else None,
        max_samples=max_samples,
        max_seq_len=max_seq_len,
        reserve_answer_tokens=reserve,
        entity_sort_policy=str(y.get("entity_sort_policy", "msgs_lifetime_distance")),
        simulate_budget_cutoff=bool(y.get("simulate_budget_cutoff", True)),
        max_new_tokens=max_new,
        load_in_4bit=not args.no_4bit,
        show_examples=max(0, args.show_examples),
        print_predictions=max(0, args.print_predictions),
        device=args.device,
        num_shards=args.num_shards,
        shard_index=args.shard_index,
        seed=args.seed,
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


def _norm_id(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def parse_prediction_ids(raw_gen: str) -> Tuple[List[str], bool]:
    """Return (list of pseudo ids, parse_ok). On failure returns ([], False)."""
    try:
        arr = extract_json_array(raw_gen)
    except (ValueError, json.JSONDecodeError):
        return [], False
    out: List[str] = []
    seen: Set[str] = set()
    for x in arr:
        s = _norm_id(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out, True


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


def micro_counts_for_episode(
    candidates: List[str],
    gold_pos: Set[str],
    pred_visible: Set[str],
) -> Tuple[int, int, int, int]:
    """TP/FP/FN/TN over visible candidates (attacker = positive)."""
    tp = fp = fn = tn = 0
    vis = list(dict.fromkeys(candidates))
    for c in vis:
        t = 1 if c in gold_pos else 0
        p = 1 if c in pred_visible else 0
        if t == 1 and p == 1:
            tp += 1
        elif t == 0 and p == 1:
            fp += 1
        elif t == 1 and p == 0:
            fn += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def aggregate_metrics(
    tp: int, fp: int, fn: int, tn: int
) -> Dict[str, float]:
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    tot = tp + tn + fp + fn
    acc = (tp + tn) / tot if tot > 0 else 0.0
    return {
        "micro_accuracy": acc,
        "micro_precision": prec,
        "micro_recall": rec,
        "micro_f1": f1,
    }


def run_eval(
    model: PreTrainedModel,
    tokenizer: PreTrainedTokenizerBase,
    rows: List[Dict[str, Any]],
    cfg: EvalConfig,
    model_tag: str,
) -> None:
    device = next(model.parameters()).device

    total_tp = total_fp = total_fn = total_tn = 0
    exact_match = 0
    parse_ok_n = 0
    prompt_too_long = 0
    pred_oob_n = 0
    n = len(rows)

    examples: List[Dict[str, Any]] = []

    print(
        f"Evaluating {model_tag} on {n} samples (split={cfg.split})...",
        flush=True,
    )
    t0 = time.time()

    for i, ex in enumerate(rows):
        pb = build_pseudo_ident_prompt(
            sample=ex,
            tokenizer=tokenizer,
            simulate_budget_cutoff=cfg.simulate_budget_cutoff,
            total_budget=cfg.max_seq_len,
            reserve_answer_tokens=cfg.reserve_answer_tokens,
            entity_sort_policy=cfg.entity_sort_policy,
        )
        prompt_text = pb.prompt_text
        visible_candidates = list(pb.visible_candidate_ids)
        gold_visible = [_norm_id(x) for x in pb.visible_output_ids if _norm_id(x)]
        gold_set = set(gold_visible)
        visible_set = set(visible_candidates)

        enc = tokenizer(
            prompt_text,
            return_tensors="pt",
            add_special_tokens=False,
            truncation=False,
        )
        prompt_len = enc["input_ids"].shape[1]
        if prompt_len > cfg.max_seq_len:
            prompt_too_long += 1
            print(
                f"  [skip] id={ex.get('id')} prompt_len={prompt_len} > max_seq_len={cfg.max_seq_len}",
                flush=True,
            )
            continue

        enc = {k: v.to(device) for k, v in enc.items()}
        attn = enc.get("attention_mask")
        if attn is None:
            enc["attention_mask"] = torch.ones_like(enc["input_ids"])

        with torch.no_grad():
            gen_ids = model.generate(
                **enc,
                max_new_tokens=cfg.max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )

        input_len = enc["input_ids"].shape[1]
        gen_text = tokenizer.decode(
            gen_ids[0][input_len:],
            skip_special_tokens=True,
        )

        pred_list, p_ok = parse_prediction_ids(gen_text)
        if p_ok:
            parse_ok_n += 1
        pred_set_all = set(pred_list)
        pred_visible = {x for x in pred_list if x in visible_set}
        oob = [x for x in pred_list if x not in visible_set]
        if oob:
            pred_oob_n += 1

        tp, fp, fn, tn = micro_counts_for_episode(visible_candidates, gold_set, pred_visible)
        total_tp += tp
        total_fp += fp
        total_fn += fn
        total_tn += tn

        if gold_set == pred_visible:
            exact_match += 1

        if cfg.print_predictions > 0 and i < cfg.print_predictions:
            print(
                f"  [{i}] id={ex.get('id')} parse_ok={p_ok} "
                f"n_candidates={len(visible_candidates)} n_gold_visible={len(gold_set)} "
                f"truncated={pb.is_truncated_entities}",
                flush=True,
            )
            print(f"      gold_visible={gold_visible}", flush=True)
            print(f"      pred_visible={sorted(pred_visible)}", flush=True)
            if oob:
                print(f"      pred_oob={oob}", flush=True)
            print(f"      raw_gen={gen_text[:500]!r}", flush=True)

        if cfg.show_examples > 0 and len(examples) < cfg.show_examples:
            if gold_set != pred_visible or not p_ok or oob:
                examples.append(
                    {
                        "id": ex.get("id"),
                        "gold": gold_visible,
                        "pred": sorted(pred_visible),
                        "parse_ok": p_ok,
                        "oob": oob,
                        "raw": gen_text[:800],
                        "truncated": pb.is_truncated_entities,
                    }
                )

        if (i + 1) % 50 == 0 or (i + 1) == n:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(
                f"  progress {i + 1}/{n} ({rate:.2f} samples/s)",
                flush=True,
            )

    metrics = aggregate_metrics(total_tp, total_fp, total_fn, total_tn)
    elapsed = time.time() - t0

    print(f"\n=== Results ({model_tag}) ===", flush=True)
    print(f"split={cfg.split} samples_attempted={n} wall_s={elapsed:.1f}", flush=True)
    print(
        f"prompt_too_long_skips={prompt_too_long} parse_ok={parse_ok_n}/{n} "
        f"episodes_with_oob_pred={pred_oob_n}",
        flush=True,
    )
    print(
        f"micro_pseudo_level: TP={total_tp} FP={total_fp} FN={total_fn} TN={total_tn}",
        flush=True,
    )
    print(
        f"micro_accuracy={metrics['micro_accuracy']:.4f} "
        f"micro_precision={metrics['micro_precision']:.4f} "
        f"micro_recall={metrics['micro_recall']:.4f} "
        f"micro_f1={metrics['micro_f1']:.4f}",
        flush=True,
    )
    print(
        f"episode_exact_match={exact_match}/{n} "
        f"({exact_match / max(1, n):.4f})",
        flush=True,
    )

    summary = {
        "model_tag": model_tag,
        "split": cfg.split,
        "n": n,
        "prompt_too_long": prompt_too_long,
        "parse_ok": parse_ok_n,
        "pred_oob_episodes": pred_oob_n,
        "tp": total_tp,
        "fp": total_fp,
        "fn": total_fn,
        "tn": total_tn,
        "micro_accuracy": metrics["micro_accuracy"],
        "micro_precision": metrics["micro_precision"],
        "micro_recall": metrics["micro_recall"],
        "micro_f1": metrics["micro_f1"],
        "episode_exact_match": exact_match,
    }
    print("METRICS_JSON:", json.dumps(summary, ensure_ascii=False), flush=True)

    if examples and cfg.show_examples > 0:
        print("\n=== Example mismatches / parse issues (up to N) ===", flush=True)
        for j, e in enumerate(examples[: cfg.show_examples], start=1):
            print(f"\n--- Example {j} ---", flush=True)
            print(json.dumps(e, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    cfg = parse_args()

    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True

    split_path = cfg.jsonl_dir / f"{cfg.split}.jsonl"
    if not split_path.exists():
        raise FileNotFoundError(f"Missing split file: {split_path}")

    all_rows = load_jsonl_rows(split_path)
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
                "jsonl_dir": str(cfg.jsonl_dir),
                "split": cfg.split,
                "mode": cfg.mode,
                "max_samples": cfg.max_samples,
                "shard": f"{cfg.shard_index + 1}/{cfg.num_shards}",
                "max_seq_len": cfg.max_seq_len,
                "reserve_answer_tokens": cfg.reserve_answer_tokens,
                "max_new_tokens": cfg.max_new_tokens,
                "entity_sort_policy": cfg.entity_sort_policy,
                "simulate_budget_cutoff": cfg.simulate_budget_cutoff,
                "seed": cfg.seed,
            },
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )
    print(
        f"Loaded {len(all_rows)} rows from {split_path}; "
        f"evaluating {len(rows)} after sample+shard.",
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
