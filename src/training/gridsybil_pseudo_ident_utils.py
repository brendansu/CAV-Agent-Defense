from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

PROMPT_VARIANTS: Tuple[str, ...] = ("default", "strict_empty")


def role_lines_for_prompt_variant(prompt_variant: str) -> List[str]:
    """Instruction + output-rules block (through 'Episode summary:') for a prompt variant."""
    v = str(prompt_variant).strip().lower()
    if v == "default":
        return [
            "You are an onboard CAV intrusion-detection model.",
            "",
            "Task:",
            "Identify which pseudo local IDs are attacker-controlled in this episode.",
            "",
            "Output rules:",
            "- Return ONLY one JSON array of pseudo local IDs.",
            "- Predict only from the pseudo IDs shown in the entity lines below.",
            '- Use ascending order, for example ["p1","p4"].',
            "- Do not repeat IDs.",
            "- If none are attacker-controlled, return [].",
            "- Do not output explanations or extra text.",
            "",
            "Episode summary:",
        ]
    if v == "strict_empty":
        return [
            "You are an onboard CAV intrusion-detection model.",
            "",
            "Task:",
            "Identify which pseudo local IDs are attacker-controlled in this episode.",
            "",
            "Output rules:",
            "- Return ONLY one JSON array of pseudo local IDs.",
            "- Every ID in your answer must appear in the entity lines below; never invent IDs.",
            '- Use ascending order, for example ["p1","p4"].',
            "- Do not repeat IDs.",
            "- If evidence is weak, ambiguous, or insufficient, return []. Prefer false negatives over false positives.",
            "- If none are attacker-controlled, return [].",
            "- Do not output explanations or extra text.",
            "",
            "Episode summary:",
        ]
    raise ValueError(
        f"Unknown prompt_variant={prompt_variant!r}; expected one of {list(PROMPT_VARIANTS)}"
    )


@dataclass
class GridSybilPseudoIdentPrompt:
    prompt_text: str
    fixed_tokens: int
    entity_line_tokens: List[int]
    entities_total: int
    entities_kept: int
    is_truncated_entities: bool
    entity_budget: int | None
    tokens_after_budget_build: int
    visible_candidate_ids: List[str]
    visible_output_ids: List[str]


def fmt_scalar(x: Any, d: int = 2) -> str:
    try:
        if x is None:
            return "-"
        return f"{float(x):.{d}f}"
    except Exception:
        return str(x)


def format_output_ids(ids: List[str]) -> str:
    return json.dumps(list(ids), ensure_ascii=False)


def answer_footer_text_from_ids(ids: List[str]) -> str:
    return f"Answer: {format_output_ids(ids)}"


def answer_suffix_text_from_ids(ids: List[str]) -> str:
    return f" {format_output_ids(ids)}"


def count_answer_footer_tokens_from_ids(tokenizer: Any, ids: List[str]) -> int:
    text = answer_footer_text_from_ids(ids)
    return len(tokenizer(text, truncation=False)["input_ids"])


def build_entity_line(ent: Dict[str, Any]) -> str:
    pid = ent.get("pseudo_local_id", "?")
    gid = ent.get("group_id", "?")
    num_msgs = ent.get("num_msgs", "-")
    avg_speed = fmt_scalar(ent.get("avg_speed"))
    speed_std = fmt_scalar(ent.get("speed_std"))
    dist_ego = fmt_scalar(ent.get("distance_to_ego"))
    lifetime = fmt_scalar(ent.get("lifetime_fraction"))
    msg_rate = fmt_scalar(ent.get("msg_rate"))
    ap = ent.get("avg_pos")
    if isinstance(ap, (list, tuple)) and len(ap) >= 2:
        px = fmt_scalar(ap[0])
        py = fmt_scalar(ap[1])
    elif isinstance(ap, (list, tuple)) and len(ap) == 1:
        px = fmt_scalar(ap[0])
        py = "-"
    else:
        px = py = "-"
    hd = fmt_scalar(ent.get("avg_heading"))
    hd_std = fmt_scalar(ent.get("heading_std"))
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

    return sorted(
        entities,
        key=lambda e: (
            _safe_float(e.get("distance_to_ego"), 1e9),
            -_safe_float(e.get("num_msgs"), 0.0),
            -_safe_float(e.get("lifetime_fraction"), 0.0),
        ),
    )


def build_pseudo_ident_prompt(
    sample: Dict[str, Any],
    tokenizer: Any,
    simulate_budget_cutoff: bool,
    total_budget: int,
    reserve_answer_tokens: int,
    entity_sort_policy: str,
    prompt_variant: str = "default",
) -> GridSybilPseudoIdentPrompt:
    inp = sample.get("input", {})
    meta = inp.get("meta", {})
    ego = inp.get("ego", {})
    region = inp.get("region", {})
    entities = inp.get("pseudo_entities", [])
    entities = entities if isinstance(entities, list) else []
    candidates = inp.get("candidate_pseudo_local_ids", [])
    attacker_full_ids = list(sample.get("output_ids", []))

    role_lines = role_lines_for_prompt_variant(prompt_variant)
    summary_lines: List[str] = []
    summary_lines.append(f"- traffic_regime: {meta.get('traffic_regime', 'unknown')}")
    win = meta.get("window", {})
    summary_lines.append(f"- window_sec: {fmt_scalar(win.get('duration'))}")
    summary_lines.append(
        f"- ego: disp={fmt_scalar(ego.get('displacement'))}, "
        f"avg_spd={fmt_scalar(ego.get('avg_speed'))}, "
        f"spd_std={fmt_scalar(ego.get('speed_std'))}"
    )
    summary_lines.append(
        f"- region: bsms={region.get('num_bsms', '-')}, "
        f"pseudos={region.get('num_unique_pseudos', '-')}, "
        f"slow_frac={fmt_scalar(region.get('slow_msg_fraction'))}"
    )
    summary_lines.append(f"- candidate_count: {len(candidates)}")

    sorted_entities = sort_entities(entities, entity_sort_policy)
    total_entities = len(sorted_entities)

    def build_fixed_text(shown_count: int, is_truncated: bool) -> str:
        truncation_suffix = " (truncated)" if is_truncated else ""
        dynamic_summary = list(summary_lines)
        dynamic_summary.append(
            f"- pseudo_entities_shown: {shown_count}/{total_entities}{truncation_suffix}"
        )
        return "\n".join(role_lines + dynamic_summary)

    footer_lines: List[str] = ["", "Answer:"]
    fixed_text_no_entities = "\n".join(
        role_lines + summary_lines + [f"- pseudo_entities_shown: 0/{total_entities}"]
    )
    fixed_with_footer_text = "\n".join([fixed_text_no_entities, *footer_lines])
    fixed_tokens = len(tokenizer(fixed_with_footer_text, truncation=False)["input_ids"])

    entity_lines = [build_entity_line(ent) for ent in sorted_entities]
    entity_line_tokens = [
        len(tokenizer(line, truncation=False)["input_ids"]) for line in entity_lines
    ]

    kept_entities = 0
    truncated = False
    body_lines: List[str] = []
    visible_candidate_ids: List[str] = []

    if not simulate_budget_cutoff:
        kept_entities = total_entities
        body_lines = list(entity_lines)
        for ent in sorted_entities:
            pid = ent.get("pseudo_local_id")
            if pid is not None:
                visible_candidate_ids.append(pid)
        fixed_text = build_fixed_text(shown_count=kept_entities, is_truncated=False)
        prompt_text = "\n".join([fixed_text] + body_lines + [*footer_lines])
        visible_set = set(visible_candidate_ids)
        visible_output_ids = [pid for pid in attacker_full_ids if pid in visible_set]
        tokens_after_budget_build = len(
            tokenizer(prompt_text, truncation=False)["input_ids"]
        )
        return GridSybilPseudoIdentPrompt(
            prompt_text=prompt_text,
            fixed_tokens=fixed_tokens,
            entity_line_tokens=entity_line_tokens,
            entities_total=total_entities,
            entities_kept=kept_entities,
            is_truncated_entities=False,
            entity_budget=None,
            tokens_after_budget_build=tokens_after_budget_build,
            visible_candidate_ids=visible_candidate_ids,
            visible_output_ids=visible_output_ids,
        )

    entity_budget = max(0, total_budget - fixed_tokens - reserve_answer_tokens)
    kept_entity_token_sum = 0
    for ent, line, line_tokens in zip(sorted_entities, entity_lines, entity_line_tokens):
        if kept_entity_token_sum + line_tokens <= entity_budget:
            body_lines.append(line)
            kept_entity_token_sum += line_tokens
            kept_entities += 1
            pid = ent.get("pseudo_local_id")
            if pid is not None:
                visible_candidate_ids.append(pid)
        else:
            truncated = True
            break

    if kept_entities == 0 and total_entities > 0:
        first_ent = sorted_entities[0]
        first_pid = first_ent.get("pseudo_local_id")
        body_lines = [entity_lines[0]]
        kept_entity_token_sum = entity_line_tokens[0]
        kept_entities = 1
        visible_candidate_ids = [first_pid] if first_pid is not None else []
        truncated = total_entities > 1

    fixed_text = build_fixed_text(shown_count=kept_entities, is_truncated=truncated)
    prompt_text = "\n".join([fixed_text] + body_lines + [*footer_lines])
    visible_set = set(visible_candidate_ids)
    visible_output_ids = [pid for pid in attacker_full_ids if pid in visible_set]
    tokens_after_budget_build = len(tokenizer(prompt_text, truncation=False)["input_ids"])
    return GridSybilPseudoIdentPrompt(
        prompt_text=prompt_text,
        fixed_tokens=fixed_tokens,
        entity_line_tokens=entity_line_tokens,
        entities_total=total_entities,
        entities_kept=kept_entities,
        is_truncated_entities=truncated,
        entity_budget=entity_budget,
        tokens_after_budget_build=tokens_after_budget_build,
        visible_candidate_ids=visible_candidate_ids,
        visible_output_ids=visible_output_ids,
    )
