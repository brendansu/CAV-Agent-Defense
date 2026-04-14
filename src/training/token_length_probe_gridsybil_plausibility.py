from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import yaml
from transformers import AutoTokenizer

from .dataset_gridsybil_plausibility import load_parquet_rows
from .gridsybil_plausibility_utils import (
    GridSybilPlausibilityPrompt,
    PROMPT_VARIANTS,
    answer_suffix_text_from_label,
    build_plausibility_prompt,
)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Probe token-length distribution for GridSybil plausibility parquet splits "
            "using the current prompt template and whitelist."
        )
    )
    ap.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_gridsybil_plausibility_50k.yaml",
        help="YAML config used by train/eval scripts.",
    )
    ap.add_argument(
        "--split",
        type=str,
        default="all",
        choices=["train", "val", "test", "all"],
        help="Which split to analyze.",
    )
    ap.add_argument(
        "--max_samples",
        type=int,
        default=5000,
        help="Max samples per split to analyze (0 means all).",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed used when sampling records.",
    )
    ap.add_argument(
        "--topk_longest",
        type=int,
        default=5,
        help="Print top-k longest samples for each split.",
    )
    ap.add_argument(
        "--include_output",
        action="store_true",
        help="Append ground-truth output label to prompt for training-length approximation.",
    )
    ap.add_argument(
        "--simulate_budget_cutoff",
        action="store_true",
        help=(
            "Override YAML and enable budget-aware prompt construction "
            "(truncate features to fit total_budget)."
        ),
    )
    ap.add_argument(
        "--no_simulate_budget_cutoff",
        action="store_true",
        help="Override YAML and disable budget-aware prompt construction.",
    )
    ap.add_argument(
        "--total_budget",
        type=int,
        default=None,
        help="Override total token budget; default comes from YAML max_seq_len.",
    )
    ap.add_argument(
        "--reserve_answer_tokens",
        type=int,
        default=None,
        help="Override reserve_answer_tokens from YAML.",
    )
    ap.add_argument(
        "--prompt_variant",
        type=str,
        default=None,
        choices=list(PROMPT_VARIANTS),
        help="Override YAML prompt_variant.",
    )
    ap.add_argument(
        "--show_prompt_examples",
        type=int,
        default=0,
        help="Print the first N prompt examples per split.",
    )
    return ap.parse_args()


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"Config {path} must contain a mapping.")
    return obj


def maybe_sample_records(
    records: List[Dict[str, Any]],
    max_samples: int,
    seed: int,
) -> List[Dict[str, Any]]:
    if max_samples <= 0 or len(records) <= max_samples:
        return records
    rng = random.Random(seed)
    idx = list(range(len(records)))
    rng.shuffle(idx)
    chosen = idx[:max_samples]
    chosen.sort()
    return [records[i] for i in chosen]


def percentile(sorted_values: List[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if p <= 0:
        return float(sorted_values[0])
    if p >= 100:
        return float(sorted_values[-1])
    k = (len(sorted_values) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def summarize_lengths(lengths: List[int]) -> Dict[str, float]:
    if not lengths:
        return {
            "n": 0,
            "mean": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
            "gt512": 0.0,
            "gt768": 0.0,
            "gt1024": 0.0,
            "gt1536": 0.0,
            "gt2048": 0.0,
        }
    sv = sorted(lengths)
    n = len(lengths)
    mean = sum(lengths) / n
    return {
        "n": n,
        "mean": mean,
        "p50": percentile(sv, 50),
        "p90": percentile(sv, 90),
        "p95": percentile(sv, 95),
        "p99": percentile(sv, 99),
        "max": float(sv[-1]),
        "gt512": sum(1 for x in lengths if x > 512) / n,
        "gt768": sum(1 for x in lengths if x > 768) / n,
        "gt1024": sum(1 for x in lengths if x > 1024) / n,
        "gt1536": sum(1 for x in lengths if x > 1536) / n,
        "gt2048": sum(1 for x in lengths if x > 2048) / n,
    }


def summarize_numeric(values: List[float]) -> Dict[str, float]:
    if not values:
        return {
            "n": 0,
            "mean": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
    sv = sorted(values)
    n = len(values)
    mean = sum(values) / n
    return {
        "n": n,
        "mean": mean,
        "p50": percentile(sv, 50),
        "p90": percentile(sv, 90),
        "p95": percentile(sv, 95),
        "max": float(sv[-1]),
    }


def print_summary(prefix: str, stats: Dict[str, float]) -> None:
    print(
        f"{prefix} n={int(stats['n'])} "
        f"mean={stats['mean']:.1f} p50={stats['p50']:.1f} p90={stats['p90']:.1f} "
        f"p95={stats['p95']:.1f} p99={stats['p99']:.1f} max={stats['max']:.0f}",
        flush=True,
    )
    print(
        f"{prefix} overflow ratio: >512={stats['gt512']:.3%} >768={stats['gt768']:.3%} "
        f">1024={stats['gt1024']:.3%} >1536={stats['gt1536']:.3%} >2048={stats['gt2048']:.3%}",
        flush=True,
    )


def build_probe_prompt(
    sample: Dict[str, Any],
    include_output: bool,
    tokenizer: Any,
    simulate_budget_cutoff: bool,
    total_budget: int,
    reserve_answer_tokens: int,
    prompt_variant: str,
    feature_name_style: str,
    include_prefixes: List[str],
    include_columns: List[str],
    exclude_columns: List[str],
) -> Tuple[str, Dict[str, Any]]:
    prompt_build: GridSybilPlausibilityPrompt = build_plausibility_prompt(
        sample=sample,
        tokenizer=tokenizer,
        simulate_budget_cutoff=simulate_budget_cutoff,
        total_budget=total_budget,
        reserve_answer_tokens=reserve_answer_tokens,
        prompt_variant=prompt_variant,
        feature_name_style=feature_name_style,
        include_prefixes=include_prefixes,
        include_columns=set(include_columns),
        exclude_columns=set(exclude_columns),
    )
    prompt = prompt_build.prompt_text
    if include_output:
        prompt = prompt + answer_suffix_text_from_label(int(sample.get("label", 0)))

    return prompt, {
        "fixed_tokens": prompt_build.fixed_tokens,
        "feature_line_tokens": prompt_build.feature_line_tokens,
        "features_total": prompt_build.features_total,
        "features_kept": prompt_build.features_kept,
        "is_truncated_features": prompt_build.is_truncated_features,
        "feature_budget": prompt_build.feature_budget,
        "tokens_after_budget_build": len(
            tokenizer(prompt, truncation=False, add_special_tokens=False)["input_ids"]
        ),
        "visible_feature_keys": list(prompt_build.visible_feature_keys),
        "label": int(sample.get("label", 0)),
    }


def load_split_records(parquet_dir: Path, split: str) -> List[Dict[str, Any]]:
    path = parquet_dir / f"{split}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Split file not found: {path}")
    return list(load_parquet_rows(path))


def main() -> None:
    args = parse_args()
    cfg_path = Path(args.config)
    cfg = load_yaml(cfg_path)

    parquet_dir = Path(cfg.get("parquet_dir", "data/processed/plausibility_messages_split"))
    if not parquet_dir.is_absolute():
        parquet_dir = Path.cwd() / parquet_dir
    if not parquet_dir.exists():
        raise FileNotFoundError(f"parquet_dir not found: {parquet_dir}")

    model_name = str(cfg.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct"))
    prompt_variant = str(cfg.get("prompt_variant", "default")).strip().lower()
    if args.prompt_variant is not None:
        prompt_variant = str(args.prompt_variant).strip().lower()
    feature_name_style = str(cfg.get("feature_name_style", "descriptive")).strip().lower()
    include_prefixes = [str(x) for x in cfg.get("prompt_include_prefixes", [])]
    include_columns = [str(x) for x in cfg.get("prompt_include_columns", [])]
    exclude_columns = [str(x) for x in cfg.get("prompt_exclude_columns", [])]

    simulate_budget_cutoff = bool(cfg.get("simulate_budget_cutoff", True))
    if args.simulate_budget_cutoff:
        simulate_budget_cutoff = True
    if args.no_simulate_budget_cutoff:
        simulate_budget_cutoff = False

    total_budget = int(args.total_budget) if args.total_budget is not None else int(cfg.get("max_seq_len", 4096))
    reserve_answer_tokens = (
        int(args.reserve_answer_tokens)
        if args.reserve_answer_tokens is not None
        else int(cfg.get("reserve_answer_tokens", 32))
    )

    split_names = ["train", "val", "test"] if args.split == "all" else [args.split]
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    print(
        f"Tokenizer={model_name} split={args.split} max_samples={args.max_samples} "
        f"include_output={args.include_output} simulate_budget_cutoff={simulate_budget_cutoff} "
        f"total_budget={total_budget} reserve_answer_tokens={reserve_answer_tokens}",
        flush=True,
    )
    print(
        f"Prompt settings: variant={prompt_variant} feature_name_style={feature_name_style} "
        f"features_whitelisted={len(include_columns)}",
        flush=True,
    )

    for split_name in split_names:
        raw_records = load_split_records(parquet_dir, split_name)
        records = maybe_sample_records(raw_records, args.max_samples, args.seed)
        print(
            f"\n=== {split_name} === raw={len(raw_records)} analyzed={len(records)}",
            flush=True,
        )

        lengths: List[int] = []
        lengths_y0: List[int] = []
        lengths_y1: List[int] = []
        longest: List[Tuple[int, str, int, int, int]] = []  # len, episode, msg, label, kept
        n_truncated = 0
        sum_features_total = 0
        sum_features_kept = 0
        fixed_tokens_all: List[float] = []
        feature_line_tokens_all: List[float] = []
        answer_tokens_all: List[float] = []
        by_label: Dict[str, List[int]] = defaultdict(list)

        for sample in records:
            prompt, prompt_meta = build_probe_prompt(
                sample=sample,
                include_output=args.include_output,
                tokenizer=tokenizer,
                simulate_budget_cutoff=simulate_budget_cutoff,
                total_budget=total_budget,
                reserve_answer_tokens=reserve_answer_tokens,
                prompt_variant=prompt_variant,
                feature_name_style=feature_name_style,
                include_prefixes=include_prefixes,
                include_columns=include_columns,
                exclude_columns=exclude_columns,
            )
            n_tokens = len(
                tokenizer(prompt, truncation=False, add_special_tokens=False)["input_ids"]
            )
            lengths.append(n_tokens)

            label = int(sample.get("label", 0))
            by_label[str(label)].append(n_tokens)
            if label == 0:
                lengths_y0.append(n_tokens)
            else:
                lengths_y1.append(n_tokens)

            sid = str(sample.get("episode_id", "UNKNOWN_EPISODE"))
            mid = int(sample.get("message_id", -1))
            longest.append(
                (
                    n_tokens,
                    sid,
                    mid,
                    label,
                    int(prompt_meta.get("features_kept", 0)),
                )
            )

            if bool(prompt_meta.get("is_truncated_features", False)):
                n_truncated += 1
            sum_features_total += int(prompt_meta.get("features_total", 0))
            sum_features_kept += int(prompt_meta.get("features_kept", 0))
            fixed_tokens_all.append(float(prompt_meta.get("fixed_tokens", 0.0)))
            feature_line_tokens_all.extend(
                float(x) for x in prompt_meta.get("feature_line_tokens", [])
            )
            answer_tokens_all.append(
                float(
                    len(
                        tokenizer(
                            answer_suffix_text_from_label(label),
                            truncation=False,
                            add_special_tokens=False,
                        )["input_ids"]
                    )
                )
            )

            if args.show_prompt_examples > 0 and len(longest) <= args.show_prompt_examples:
                print(f"\n[prompt example {len(longest)}]", flush=True)
                print(prompt, flush=True)
                if args.include_output:
                    print("[end prompt with output]", flush=True)
                else:
                    print("[end prompt]", flush=True)

        stats = summarize_lengths(lengths)
        print_summary("[overall]", stats)
        print_summary("[label=0]", summarize_lengths(lengths_y0))
        print_summary("[label=1]", summarize_lengths(lengths_y1))

        if len(records) > 0:
            trunc_ratio = n_truncated / len(records)
            avg_kept = sum_features_kept / len(records)
            avg_total = sum_features_total / len(records)
            keep_ratio = sum_features_kept / max(1, sum_features_total)
            print(
                f"[feature budget] truncated_ratio={trunc_ratio:.3%} "
                f"avg_kept={avg_kept:.2f} avg_total={avg_total:.2f} keep_ratio={keep_ratio:.3%}",
                flush=True,
            )
            fixed_stats = summarize_numeric(fixed_tokens_all)
            line_stats = summarize_numeric(feature_line_tokens_all)
            answer_stats = summarize_numeric(answer_tokens_all)
            fixed_tokens_mean = fixed_stats["mean"]
            line_tokens_p90 = line_stats["p90"]
            estimated_max_features_by_p90 = (
                int(
                    max(
                        0.0,
                        (total_budget - fixed_tokens_mean - reserve_answer_tokens)
                        / max(1.0, line_tokens_p90),
                    )
                )
                if simulate_budget_cutoff
                else 0
            )
            print(
                f"[budget estimate] fixed_tokens_mean={fixed_tokens_mean:.1f} "
                f"feature_line_tokens_p90={line_tokens_p90:.1f} "
                f"estimated_max_features_by_p90={estimated_max_features_by_p90}",
                flush=True,
            )
            print(
                f"[answer tokens] mean={answer_stats['mean']:.1f} p50={answer_stats['p50']:.1f} "
                f"p90={answer_stats['p90']:.1f} p95={answer_stats['p95']:.1f} "
                f"max={answer_stats['max']:.0f}",
                flush=True,
            )

        longest.sort(key=lambda t: t[0], reverse=True)
        if args.topk_longest > 0:
            print(f"[top {args.topk_longest} longest]", flush=True)
            for i, (n_tokens, sid, mid, label, kept) in enumerate(
                longest[: args.topk_longest], start=1
            ):
                print(
                    f"  {i}. tokens={n_tokens} label={label} features_kept={kept} "
                    f"episode_id={sid} message_id={mid}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
