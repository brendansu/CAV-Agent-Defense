from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from transformers import AutoTokenizer


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
        help="Budget reserved for answer tokens when --simulate_budget_cutoff is enabled.",
    )
    ap.add_argument(
        "--entity_sort_policy",
        type=str,
        default="msgs_lifetime_distance",
        choices=["none", "msgs_lifetime_distance", "distance_msgs_lifetime"],
        help="Policy to prioritize pseudo entities when truncating.",
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


def _fmt(x: Any, d: int = 2) -> str:
    try:
        if x is None:
            return "-"
        return f"{float(x):.{d}f}"
    except Exception:
        return str(x)


def build_entity_line(ent: Dict[str, Any]) -> str:
    pid = ent.get("pseudo_local_id", "?")
    gid = ent.get("group_id", "?")
    num_msgs = ent.get("num_msgs", "-")
    avg_speed = _fmt(ent.get("avg_speed"))
    speed_std = _fmt(ent.get("speed_std"))
    dist_ego = _fmt(ent.get("distance_to_ego"))
    lifetime = _fmt(ent.get("lifetime_fraction"))
    msg_rate = _fmt(ent.get("msg_rate"))
    ap = ent.get("avg_pos")
    if isinstance(ap, (list, tuple)) and len(ap) >= 2:
        px = _fmt(ap[0])
        py = _fmt(ap[1])
    elif isinstance(ap, (list, tuple)) and len(ap) == 1:
        px = _fmt(ap[0])
        py = "-"
    else:
        px = py = "-"
    hd = _fmt(ent.get("avg_heading"))
    hd_std = _fmt(ent.get("heading_std"))
    return (
        f"- {pid}: g={gid} msgs={num_msgs} life={lifetime} rate={msg_rate} "
        f"spd={avg_speed} spd_std={speed_std} dist={dist_ego} "
        f"px={px} py={py} hd={hd} hd_std={hd_std}"
    )


def sort_entities(entities: List[Dict[str, Any]], policy: str) -> List[Dict[str, Any]]:
    if policy == "none":
        return list(entities)

    def _safe_float(v: Any, fallback: float) -> float:
        try:
            if v is None:
                return fallback
            return float(v)
        except Exception:
            return fallback

    if policy == "msgs_lifetime_distance":
        return sorted(
            entities,
            key=lambda e: (
                -_safe_float(e.get("num_msgs"), 0.0),
                -_safe_float(e.get("lifetime_fraction"), 0.0),
                _safe_float(e.get("distance_to_ego"), 1e9),
            ),
        )

    # distance_msgs_lifetime
    return sorted(
        entities,
        key=lambda e: (
            _safe_float(e.get("distance_to_ego"), 1e9),
            -_safe_float(e.get("num_msgs"), 0.0),
            -_safe_float(e.get("lifetime_fraction"), 0.0),
        ),
    )


def build_probe_prompt(
    sample: Dict[str, Any],
    include_output: bool,
    tokenizer: Any,
    simulate_budget_cutoff: bool,
    total_budget: int,
    reserve_answer_tokens: int,
    entity_sort_policy: str,
) -> Tuple[str, Dict[str, Any]]:
    instruction = sample.get("instruction", "Identify attacker pseudo local IDs.")
    inp = sample.get("input", {})
    meta = inp.get("meta", {})
    ego = inp.get("ego", {})
    region = inp.get("region", {})
    entities = inp.get("pseudo_entities", [])
    entities = entities if isinstance(entities, list) else []
    candidates = inp.get("candidate_pseudo_local_ids", [])

    role_lines: List[str] = [
        "You are an onboard CAV intrusion-detection model.",
        "",
        "Task:",
        "Identify which pseudo local IDs are attacker-controlled in this episode.",
        "",
        "Output rules:",
        "- Return ONLY one JSON array of pseudo local IDs.",
        "- Predict only from the listed candidate pseudo entities.",
        '- Use ascending order, for example ["p1","p4"].',
        "- Do not repeat IDs.",
        "- If none are attacker-controlled, return [].",
        "- Do not output explanations or extra text.",
        "",
        "Episode summary:",
    ]
    summary_lines: List[str] = []
    summary_lines.append(f"- traffic_regime: {meta.get('traffic_regime', 'unknown')}")
    win = meta.get("window", {})
    summary_lines.append(
        f"- window_sec: {_fmt(win.get('duration'))}"
    )
    summary_lines.append(
        f"- ego: disp={_fmt(ego.get('displacement'))}, avg_spd={_fmt(ego.get('avg_speed'))}, spd_std={_fmt(ego.get('speed_std'))}"
    )
    summary_lines.append(
        f"- region: bsms={region.get('num_bsms', '-')}, pseudos={region.get('num_unique_pseudos', '-')}, "
        f"senders={region.get('num_unique_senders', '-')}, slow_frac={_fmt(region.get('slow_msg_fraction'))}"
    )
    summary_lines.append(f"- candidate_count: {len(candidates)}")

    footer_lines: List[str] = []
    footer_lines.append("")
    footer_lines.append("Candidate pseudo entities:")
    footer_lines.append("")
    if include_output:
        footer_lines.append(f"Answer: {sample.get('output', '[]')}")
    else:
        footer_lines.append("Answer:")

    sorted_entities = sort_entities(entities, entity_sort_policy)
    total_entities = len(sorted_entities)
    kept_entities = 0
    truncated = False

    def build_fixed_text(shown_count: int, is_truncated: bool) -> str:
        truncation_suffix = " (truncated)" if is_truncated else ""
        dynamic_summary = list(summary_lines)
        dynamic_summary.append(
            f"- pseudo_entities_shown: {shown_count}/{total_entities}{truncation_suffix}"
        )
        return "\n".join(role_lines + dynamic_summary)

    fixed_text_no_entities = "\n".join(role_lines + summary_lines + [f"- pseudo_entities_shown: 0/{total_entities}"])
    fixed_with_footer_text = "\n".join([fixed_text_no_entities, *footer_lines])
    fixed_tokens = len(tokenizer(fixed_with_footer_text, truncation=False)["input_ids"])
    entity_lines = [build_entity_line(ent) for ent in sorted_entities]
    entity_line_tokens = [
        len(tokenizer(line, truncation=False)["input_ids"]) for line in entity_lines
    ]

    if not simulate_budget_cutoff:
        body_lines = list(entity_lines)
        kept_entities = total_entities
        fixed_text = build_fixed_text(shown_count=kept_entities, is_truncated=False)
        prompt = "\n".join([fixed_text] + body_lines + [*footer_lines])
        attacker_full_ids = sample.get("output_ids", [])
        visible_candidates = {ent.get("pseudo_local_id") for ent in sorted_entities}
        visible_attackers = [pid for pid in attacker_full_ids if pid in visible_candidates]
        return prompt, {
            "fixed_tokens": fixed_tokens,
            "entity_line_tokens": entity_line_tokens,
            "entities_total": total_entities,
            "entities_kept": total_entities,
            "is_truncated_entities": False,
            "entity_budget": None,
            "true_attackers_full": len(attacker_full_ids),
            "true_attackers_visible": len(visible_attackers),
            "hidden_attacker": False,
        }

    entity_budget = max(0, total_budget - fixed_tokens - reserve_answer_tokens)

    body_lines: List[str] = []
    kept_entity_token_sum = 0

    for line, line_tokens in zip(entity_lines, entity_line_tokens):
        if kept_entity_token_sum + line_tokens <= entity_budget:
            body_lines.append(line)
            kept_entity_token_sum += line_tokens
            kept_entities += 1
        else:
            truncated = True
            break

    if kept_entities == 0 and total_entities > 0:
        body_lines = [entity_lines[0]]
        kept_entity_token_sum = entity_line_tokens[0]
        kept_entities = 1
        truncated = total_entities > 1

    fixed_text = build_fixed_text(shown_count=kept_entities, is_truncated=truncated)
    current_text = "\n".join([fixed_text] + body_lines + [*footer_lines])
    current_tokens = len(tokenizer(current_text, truncation=False)["input_ids"])
    attacker_full_ids = sample.get("output_ids", [])
    visible_candidates = set()
    for ent in sorted_entities[:kept_entities]:
        pid = ent.get("pseudo_local_id")
        if pid is not None:
            visible_candidates.add(pid)
    visible_attackers = [pid for pid in attacker_full_ids if pid in visible_candidates]

    return current_text, {
        "fixed_tokens": fixed_tokens,
        "entity_line_tokens": entity_line_tokens,
        "entities_total": total_entities,
        "entities_kept": kept_entities,
        "is_truncated_entities": truncated,
        "entity_budget": entity_budget,
        "tokens_after_budget_build": current_tokens,
        "true_attackers_full": len(attacker_full_ids),
        "true_attackers_visible": len(visible_attackers),
        "hidden_attacker": len(visible_attackers) < len(attacker_full_ids),
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
        f"| include_output={args.include_output} | simulate_budget_cutoff={args.simulate_budget_cutoff}",
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
            fixed_tokens_mean = fixed_stats["mean"]
            entity_line_tokens_p90 = entity_line_stats["p90"]
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

