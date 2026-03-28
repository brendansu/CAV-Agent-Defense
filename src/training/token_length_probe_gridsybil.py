from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from transformers import AutoTokenizer

from .gridsybil_pseudo_ident_utils import (
    GridSybilPseudoIdentPrompt,
    answer_suffix_text_from_ids,
    build_pseudo_ident_prompt,
    count_answer_footer_tokens_from_ids,
)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Probe token-length distribution for GridSybil pseudo-ident JSONL splits "
            "using a lightweight prompt template."
        )
    )
    ap.add_argument(
        "--jsonl_dir",
        type=str,
        required=True,
        help="Directory containing train/val/test.jsonl",
    )
    ap.add_argument(
        "--model_name",
        type=str,
        default="Qwen/Qwen2.5-1.5B-Instruct",
        help="Tokenizer model name/path.",
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
        help="Random seed used when sampling lines.",
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
        help="Append ground-truth output to prompt for training-length approximation.",
    )
    ap.add_argument(
        "--simulate_budget_cutoff",
        action="store_true",
        help=(
            "Enable budget-aware prompt construction: keep fixed sections intact and "
            "truncate pseudo_entities to fit token budget."
        ),
    )
    ap.add_argument(
        "--total_budget",
        type=int,
        default=4096,
        help=(
            "Total prompt token budget when --simulate_budget_cutoff is enabled "
            "(default matches common 4k context)."
        ),
    )
    ap.add_argument(
        "--reserve_answer_tokens",
        type=int,
        default=96,
        help=(
            "Budget reserved for answer tokens when --simulate_budget_cutoff is enabled. "
            "Compare with printed [answer footer tokens] max/mean vs ground-truth output."
        ),
    )
    ap.add_argument(
        "--entity_sort_policy",
        type=str,
        default="msgs_lifetime_distance",
        choices=["none", "msgs_lifetime_distance", "distance_msgs_lifetime"],
        help="Policy to prioritize pseudo entities when truncating.",
    )
    ap.add_argument(
        "--prompt_variant",
        type=str,
        default="default",
        choices=["default", "strict_empty"],
        help="Prompt instruction variant (must match training/eval YAML).",
    )
    return ap.parse_args()


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {exc}") from exc


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


def build_probe_prompt(
    sample: Dict[str, Any],
    include_output: bool,
    tokenizer: Any,
    simulate_budget_cutoff: bool,
    total_budget: int,
    reserve_answer_tokens: int,
    entity_sort_policy: str,
    prompt_variant: str = "default",
) -> Tuple[str, Dict[str, Any]]:
    prompt_build: GridSybilPseudoIdentPrompt = build_pseudo_ident_prompt(
        sample=sample,
        tokenizer=tokenizer,
        simulate_budget_cutoff=simulate_budget_cutoff,
        total_budget=total_budget,
        reserve_answer_tokens=reserve_answer_tokens,
        entity_sort_policy=entity_sort_policy,
        prompt_variant=prompt_variant,
    )
    prompt = prompt_build.prompt_text
    if include_output:
        prompt = prompt + answer_suffix_text_from_ids(prompt_build.visible_output_ids)

    return prompt, {
        "fixed_tokens": prompt_build.fixed_tokens,
        "entity_line_tokens": prompt_build.entity_line_tokens,
        "entities_total": prompt_build.entities_total,
        "entities_kept": prompt_build.entities_kept,
        "is_truncated_entities": prompt_build.is_truncated_entities,
        "entity_budget": prompt_build.entity_budget,
        "tokens_after_budget_build": len(tokenizer(prompt, truncation=False)["input_ids"]),
        "true_attackers_full": len(sample.get("output_ids", [])),
        "true_attackers_visible": len(prompt_build.visible_output_ids),
        "hidden_attacker": len(prompt_build.visible_output_ids) < len(sample.get("output_ids", [])),
        "visible_output_ids": list(prompt_build.visible_output_ids),
    }


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
            "gt1024": 0.0,
            "gt1536": 0.0,
            "gt2048": 0.0,
            "gt3072": 0.0,
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
        "gt1024": sum(1 for x in lengths if x > 1024) / n,
        "gt1536": sum(1 for x in lengths if x > 1536) / n,
        "gt2048": sum(1 for x in lengths if x > 2048) / n,
        "gt3072": sum(1 for x in lengths if x > 3072) / n,
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


def candidate_bucket(num_candidates: int) -> str:
    if num_candidates <= 10:
        return "<=10"
    if num_candidates <= 20:
        return "11-20"
    if num_candidates <= 40:
        return "21-40"
    return ">40"


def print_summary(prefix: str, stats: Dict[str, float]) -> None:
    print(
        f"{prefix} n={int(stats['n'])} "
        f"mean={stats['mean']:.1f} p50={stats['p50']:.1f} p90={stats['p90']:.1f} "
        f"p95={stats['p95']:.1f} p99={stats['p99']:.1f} max={stats['max']:.0f}",
        flush=True,
    )
    print(
        f"{prefix} overflow ratio: >1024={stats['gt1024']:.3%} >1536={stats['gt1536']:.3%} "
        f">2048={stats['gt2048']:.3%} >3072={stats['gt3072']:.3%}",
        flush=True,
    )


def load_split_records(jsonl_dir: Path, split: str) -> List[Dict[str, Any]]:
    path = jsonl_dir / f"{split}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"Split file not found: {path}")
    return list(iter_jsonl(path))


def main() -> None:
    args = parse_args()
    jsonl_dir = Path(args.jsonl_dir)
    if not jsonl_dir.exists():
        raise FileNotFoundError(f"jsonl_dir not found: {jsonl_dir}")

    split_names = ["train", "val", "test"] if args.split == "all" else [args.split]
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    print(
        f"Tokenizer: {args.model_name} | split={args.split} | max_samples={args.max_samples} "
        f"| include_output={args.include_output} | simulate_budget_cutoff={args.simulate_budget_cutoff} "
        f"| prompt_variant={args.prompt_variant}",
        flush=True,
    )
    if args.simulate_budget_cutoff:
        print(
            f"Budget settings: total_budget={args.total_budget}, "
            f"reserve_answer_tokens={args.reserve_answer_tokens}, policy={args.entity_sort_policy} "
            f"(entity budget uses measured fixed_tokens, not a separate fixed_budget flag)",
            flush=True,
        )

    for split_name in split_names:
        raw_records = load_split_records(jsonl_dir, split_name)
        records = maybe_sample_records(raw_records, args.max_samples, args.seed)
        print(
            f"\n=== {split_name} === raw={len(raw_records)} analyzed={len(records)}",
            flush=True,
        )

        lengths: List[int] = []
        longest: List[Tuple[int, str, int, int]] = []  # (len, id, candidates, true)
        by_bucket: Dict[str, List[int]] = defaultdict(list)
        bucket_truncated: Dict[str, int] = defaultdict(int)
        n_truncated = 0
        sum_entities_total = 0
        sum_entities_kept = 0
        fixed_tokens_all: List[float] = []
        entity_line_tokens_all: List[float] = []
        answer_footer_tokens_all: List[float] = []
        true_attackers_full_all: List[float] = []
        true_attackers_visible_all: List[float] = []
        attacker_visibility_all: List[float] = []
        n_hidden_attacker = 0

        for sample in records:
            prompt, prompt_meta = build_probe_prompt(
                sample=sample,
                include_output=args.include_output,
                tokenizer=tokenizer,
                simulate_budget_cutoff=args.simulate_budget_cutoff,
                total_budget=args.total_budget,
                reserve_answer_tokens=args.reserve_answer_tokens,
                entity_sort_policy=args.entity_sort_policy,
                prompt_variant=args.prompt_variant,
            )
            n_tokens = len(tokenizer(prompt, truncation=False)["input_ids"])
            lengths.append(n_tokens)

            eval_meta = sample.get("eval_meta", {})
            cands = int(eval_meta.get("num_candidates", 0))
            trues = int(eval_meta.get("num_true_attackers", 0))
            sid = str(sample.get("id", "UNKNOWN_ID"))
            longest.append((n_tokens, sid, cands, trues))
            b = candidate_bucket(cands)
            by_bucket[b].append(n_tokens)
            is_trunc = bool(prompt_meta.get("is_truncated_entities", False))
            if is_trunc:
                n_truncated += 1
                bucket_truncated[b] += 1
            sum_entities_total += int(prompt_meta.get("entities_total", 0))
            sum_entities_kept += int(prompt_meta.get("entities_kept", 0))
            fixed_tokens_all.append(float(prompt_meta.get("fixed_tokens", 0.0)))
            entity_line_tokens_all.extend(
                float(x) for x in prompt_meta.get("entity_line_tokens", [])
            )
            answer_footer_tokens_all.append(
                float(
                    count_answer_footer_tokens_from_ids(
                        tokenizer,
                        list(prompt_meta.get("visible_output_ids", [])),
                    )
                )
            )
            n_true_full = float(prompt_meta.get("true_attackers_full", 0.0))
            n_true_visible = float(prompt_meta.get("true_attackers_visible", 0.0))
            true_attackers_full_all.append(n_true_full)
            true_attackers_visible_all.append(n_true_visible)
            if n_true_full > 0:
                attacker_visibility_all.append(n_true_visible / n_true_full)
            else:
                attacker_visibility_all.append(1.0)
            if bool(prompt_meta.get("hidden_attacker", False)):
                n_hidden_attacker += 1

        stats = summarize_lengths(lengths)
        print_summary("[overall]", stats)
        if len(records) > 0:
            trunc_ratio = n_truncated / len(records)
            avg_kept = sum_entities_kept / len(records)
            avg_total = sum_entities_total / len(records)
            keep_ratio = (sum_entities_kept / max(1, sum_entities_total))
            print(
                f"[entity budget] truncated_ratio={trunc_ratio:.3%} "
                f"avg_kept={avg_kept:.2f} avg_total={avg_total:.2f} keep_ratio={keep_ratio:.3%}",
                flush=True,
            )
            fixed_stats = summarize_numeric(fixed_tokens_all)
            entity_line_stats = summarize_numeric(entity_line_tokens_all)
            answer_stats = summarize_numeric(answer_footer_tokens_all)
            fixed_tokens_mean = fixed_stats["mean"]
            entity_line_tokens_p90 = entity_line_stats["p90"]
            answer_mean = answer_stats["mean"]
            answer_max = answer_stats["max"]
            answer_p90 = answer_stats["p90"]
            answer_p95 = answer_stats["p95"]
            n_gt_reserve = sum(1 for x in answer_footer_tokens_all if x > args.reserve_answer_tokens)
            frac_gt_reserve = n_gt_reserve / len(answer_footer_tokens_all)
            estimated_max_entities_by_p90 = (
                int(
                    max(
                        0.0,
                        (args.total_budget - fixed_tokens_mean - args.reserve_answer_tokens)
                        / max(1.0, entity_line_tokens_p90),
                    )
                )
                if args.simulate_budget_cutoff
                else 0
            )
            print(
                f"[budget estimate] fixed_tokens_mean={fixed_tokens_mean:.1f} "
                f"entity_line_tokens_p90={entity_line_tokens_p90:.1f} "
                f"estimated_max_entities_by_p90={estimated_max_entities_by_p90}",
                flush=True,
            )
            print(
                f"[answer footer tokens] mean={answer_mean:.1f} p50={answer_stats['p50']:.1f} "
                f"p90={answer_p90:.1f} p95={answer_p95:.1f} max={answer_max:.0f} "
                f"(standalone line 'Answer: '+visible attacker ids after input truncation; "
                f"reserve_answer_tokens={args.reserve_answer_tokens} "
                f"samples_gt_reserve={n_gt_reserve}/{len(records)}={frac_gt_reserve:.3%})",
                flush=True,
            )
            avg_true_full = sum(true_attackers_full_all) / len(true_attackers_full_all)
            avg_true_visible = sum(true_attackers_visible_all) / len(true_attackers_visible_all)
            avg_attacker_visibility_fraction = (
                sum(attacker_visibility_all) / len(attacker_visibility_all)
            )
            hidden_attacker_ratio = n_hidden_attacker / len(records)
            print(
                f"[label visibility] avg_true_attackers_full={avg_true_full:.2f} "
                f"avg_true_attackers_visible={avg_true_visible:.2f} "
                f"avg_attacker_visibility_fraction={avg_attacker_visibility_fraction:.3%} "
                f"hidden_attacker_ratio={hidden_attacker_ratio:.3%}",
                flush=True,
            )

        print("[by num_candidates]", flush=True)
        for bucket in ["<=10", "11-20", "21-40", ">40"]:
            bstats = summarize_lengths(by_bucket.get(bucket, []))
            print_summary(f"  [{bucket}]", bstats)
            n_bucket = len(by_bucket.get(bucket, []))
            if n_bucket > 0:
                btr = bucket_truncated.get(bucket, 0) / n_bucket
                print(
                    f"  [{bucket}] truncated_ratio={btr:.3%}",
                    flush=True,
                )

        longest.sort(key=lambda t: t[0], reverse=True)
        k = max(0, args.topk_longest)
        if k > 0:
            print(f"[top {k} longest]", flush=True)
            for i, (n_tokens, sid, cands, trues) in enumerate(longest[:k], start=1):
                print(
                    f"  {i}. tokens={n_tokens} candidates={cands} true_attackers={trues} id={sid}",
                    flush=True,
                )


if __name__ == "__main__":
    main()

