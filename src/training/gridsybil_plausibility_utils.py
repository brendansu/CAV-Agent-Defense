from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Set, Tuple

import numpy as np

PROMPT_VARIANTS: Tuple[str, ...] = (
    "default",
    "traffic_neutral",
    "traffic_neutral_recentk",
    "replay_neutral_recentk",
    "traffic_benign_prior",
    "traffic_compact",
)

DEFAULT_FEATURE_INCLUDE_PREFIXES: Tuple[str, ...] = (
    "msg_catch_",
    "ctx_recentk_",
    "ctx_",
    "msg_valid_history_features",
    "msg_has_prev_same_pseudo",
    "msg_dt_prev_same_pseudo",
    "avail_prev_same_pseudo",
    "avail_neighbor_context",
)

DEFAULT_FEATURE_WHITELIST: Tuple[str, ...] = (
    # D: history/availability
    "msg_has_prev_same_pseudo",
    "msg_dt_prev_same_pseudo",
    "msg_valid_history_features",
    # E-final-v1
    "msg_catch_art",
    "msg_catch_freq",
    "msg_catch_int_min_neighbor",
    "msg_catch_int_n_violations",
    "msg_catch_mgtd",
    "msg_catch_mgts",
    "msg_catch_mgtsv",
    "msg_catch_mgtsvm",
    "msg_catch_phc",
    # F-v1
    "ctx_n_neighbors",
    "ctx_dist_min",
    "ctx_dist_mean",
    "ctx_n_close_5m",
    "ctx_n_close_10m",
    "ctx_speed_diff_mean",
    "ctx_head_diff_mean_deg",
    "ctx_n_speed_diff_lt_0p5",
    "ctx_n_head_diff_lt_5deg",
    "ctx_n_triplet_similar",
    "ctx_triplet_ratio",
)

FEATURE_ALIASES: Dict[str, str] = {
    "msg_has_prev_same_pseudo": "previous message from same pseudonym exists",
    "msg_dt_prev_same_pseudo": "time since previous message from same pseudonym (s)",
    "msg_valid_history_features": "same-pseudonym history features valid",
    "msg_catch_art": "plausibility range consistency (ART)",
    "msg_catch_maxs": "plausibility max speed consistency",
    "msg_catch_mgtd": "plausibility movement-gradient displacement consistency",
    "msg_catch_mgts": "plausibility movement-gradient speed consistency",
    "msg_catch_mgtsv": "plausibility movement-gradient speed variation",
    "msg_catch_mgtsvm": "plausibility movement-gradient margin",
    "msg_catch_freq": "plausibility beacon-frequency consistency",
    "msg_catch_phc": "plausibility heading-change consistency",
    "msg_catch_saw": "plausibility spatial-alignment window",
    "msg_catch_rad": "plausibility receiver absolute distance",
    "msg_catch_int_self": "plausibility overlap self-intersection ratio",
    "msg_catch_int_min_neighbor": "plausibility overlap min-neighbor ratio",
    "msg_catch_int_n_violations": "plausibility overlap violation count",
    "ctx_n_neighbors": "context neighbor count",
    "ctx_dist_min": "context neighbor min distance",
    "ctx_dist_mean": "context neighbor mean distance",
    "ctx_n_close_5m": "context neighbors within 5 m",
    "ctx_n_close_10m": "context neighbors within 10 m",
    "ctx_speed_diff_min": "context neighbor min speed diff",
    "ctx_speed_diff_mean": "context neighbor mean speed diff",
    "ctx_head_diff_min_deg": "context neighbor min heading diff (deg)",
    "ctx_head_diff_mean_deg": "context neighbor mean heading diff (deg)",
    "ctx_n_speed_diff_lt_0p5": "context neighbors with speed diff < 0.5 m/s",
    "ctx_n_head_diff_lt_5deg": "context neighbors with heading diff < 5 deg",
    "ctx_n_triplet_similar": "context similar-triplet count",
    "ctx_triplet_ratio": "context similar-triplet ratio",
    # Recent-K v1 (sender prior messages, time-capped and count-capped)
    "ctx_recentk_hist_count": "sender recent-window message count",
    "ctx_recentk_hist_span_sec": "sender recent-window time span (s)",
    "ctx_recentk_last_msg_catch_mgtsv": "recent-window last MGTSV score",
    "ctx_recentk_mean_msg_catch_mgtsv": "recent-window mean MGTSV score",
    "ctx_recentk_min_msg_catch_mgtsv": "recent-window min MGTSV score",
    # Legacy aliases retained for backward compatibility; values now map to selected autoq thresholds.
    "ctx_recentk_count_msg_catch_mgtsv_le_0p8": "legacy alias: recent-window count MGTSV at autoq threshold (mapped to T1)",
    "ctx_recentk_count_msg_catch_mgtsv_le_0p95": "legacy alias: recent-window count MGTSV at autoq threshold (mapped to T2)",
    "ctx_recentk_last_msg_catch_int_min_neighbor": "recent-window last overlap min-neighbor ratio",
    "ctx_recentk_mean_msg_catch_int_min_neighbor": "recent-window mean overlap min-neighbor ratio",
    "ctx_recentk_min_msg_catch_int_min_neighbor": "recent-window min overlap min-neighbor ratio",
    "ctx_recentk_count_msg_catch_int_min_neighbor_le_0p8": "legacy alias: recent-window count overlap min-neighbor at autoq threshold (mapped to T1)",
    "ctx_recentk_last_ctx_triplet_ratio": "recent-window last similar-triplet ratio",
    "ctx_recentk_mean_ctx_triplet_ratio": "recent-window mean similar-triplet ratio",
    "ctx_recentk_count_ctx_triplet_ratio_gt_0": "recent-window count triplet ratio > 0 (structural non-zero threshold)",
    "ctx_recentk_frac_ctx_triplet_ratio_gt_0": "recent-window fraction triplet ratio > 0 (structural non-zero threshold)",
    "ctx_recentk_last_ctx_speed_diff_mean": "recent-window last mean neighbor speed diff",
    "ctx_recentk_mean_ctx_speed_diff_mean": "recent-window mean of mean neighbor speed diff",
    "ctx_recentk_min_ctx_speed_diff_mean": "recent-window min mean neighbor speed diff",
    "ctx_recentk_count_ctx_speed_diff_mean_lt_5": "legacy alias: recent-window count mean speed diff at autoq threshold (mapped to T2)",
    "ctx_recentk_count_ctx_speed_diff_mean_lt_0p2": "legacy alias: recent-window count mean speed diff at autoq threshold (mapped to T1)",
    "ctx_recentk_frac_ctx_speed_diff_mean_lt_5": "legacy alias: recent-window fraction mean speed diff at autoq threshold (mapped to T2)",
    "ctx_recentk_last_ctx_head_diff_mean_deg": "recent-window last mean heading diff (deg)",
    "ctx_recentk_mean_ctx_head_diff_mean_deg": "recent-window mean mean heading diff (deg)",
    "ctx_recentk_min_ctx_head_diff_mean_deg": "recent-window min mean heading diff (deg)",
    "ctx_recentk_count_ctx_head_diff_mean_deg_lt_1": "legacy alias: recent-window count mean heading diff at autoq threshold (mapped to T1)",
    "ctx_recentk_count_ctx_head_diff_mean_deg_lt_5": "legacy alias: recent-window count mean heading diff at autoq threshold (mapped to T2)",
    "ctx_recentk_frac_ctx_head_diff_mean_deg_lt_5": "legacy alias: recent-window fraction mean heading diff at autoq threshold (mapped to T2)",
    # AutoQ explicit threshold summaries.
    "ctx_recentk_count_msg_catch_mgtsv_t1": "recent-window count MGTSV hits selected autoq T1 threshold",
    "ctx_recentk_frac_msg_catch_mgtsv_t1": "recent-window fraction MGTSV hits selected autoq T1 threshold",
    "ctx_recentk_count_msg_catch_mgtsv_t2": "recent-window count MGTSV hits selected autoq T2 threshold",
    "ctx_recentk_frac_msg_catch_mgtsv_t2": "recent-window fraction MGTSV hits selected autoq T2 threshold",
    "ctx_recentk_count_msg_catch_int_min_neighbor_t1": "recent-window count overlap min-neighbor hits selected autoq T1 threshold",
    "ctx_recentk_frac_msg_catch_int_min_neighbor_t1": "recent-window fraction overlap min-neighbor hits selected autoq T1 threshold",
    "ctx_recentk_count_msg_catch_int_min_neighbor_t2": "recent-window count overlap min-neighbor hits selected autoq T2 threshold",
    "ctx_recentk_frac_msg_catch_int_min_neighbor_t2": "recent-window fraction overlap min-neighbor hits selected autoq T2 threshold",
    "ctx_recentk_count_ctx_triplet_ratio_t1": "recent-window count triplet ratio hits selected autoq T1 threshold (often structural > 0)",
    "ctx_recentk_frac_ctx_triplet_ratio_t1": "recent-window fraction triplet ratio hits selected autoq T1 threshold (often structural > 0)",
    "ctx_recentk_count_ctx_triplet_ratio_t2": "recent-window count triplet ratio hits selected autoq T2 threshold",
    "ctx_recentk_frac_ctx_triplet_ratio_t2": "recent-window fraction triplet ratio hits selected autoq T2 threshold",
    "ctx_recentk_count_ctx_speed_diff_mean_t1": "recent-window count mean speed diff hits selected autoq T1 threshold",
    "ctx_recentk_frac_ctx_speed_diff_mean_t1": "recent-window fraction mean speed diff hits selected autoq T1 threshold",
    "ctx_recentk_count_ctx_speed_diff_mean_t2": "recent-window count mean speed diff hits selected autoq T2 threshold",
    "ctx_recentk_frac_ctx_speed_diff_mean_t2": "recent-window fraction mean speed diff hits selected autoq T2 threshold",
    "ctx_recentk_count_ctx_head_diff_mean_deg_t1": "recent-window count mean heading diff hits selected autoq T1 threshold",
    "ctx_recentk_frac_ctx_head_diff_mean_deg_t1": "recent-window fraction mean heading diff hits selected autoq T1 threshold",
    "ctx_recentk_count_ctx_head_diff_mean_deg_t2": "recent-window count mean heading diff hits selected autoq T2 threshold",
    "ctx_recentk_frac_ctx_head_diff_mean_deg_t2": "recent-window fraction mean heading diff hits selected autoq T2 threshold",
}


@dataclass(frozen=True)
class PromptVariantSpec:
    intro_lines: Tuple[str, ...]
    group_hints: Dict[str, str]


_TRAFFIC_GROUP_HINTS: Dict[str, str] = {
    "History and timing": (
        "These features show whether the same pseudonym has recent prior messages and "
        "whether short-history timing is available and consistent."
    ),
    "Plausibility checks": (
        "Traffic sybil messages may show multiple plausibility inconsistencies, such as "
        "unrealistic motion, overlap conflicts, or abnormal heading/frequency patterns."
    ),
    "Neighbor context": (
        "Traffic sybil behavior may appear as multiple nearby senders with unusually similar "
        "motion, heading, or local clustering patterns."
    ),
    # Recent-K sub-groups (ctx_recentk_*); finer hints than a single umbrella block.
    "Sender recent: window": (
        "How much sender-only history is available inside the recent window: message count and "
        "time span (window is time-capped and count-capped)."
    ),
    "Sender recent: MGTSV": (
        "Movement-gradient speed variation (MGTSV) over the sender's prior messages in the window; "
        "threshold counts summarize how often scores were very low."
    ),
    "Sender recent: overlap": (
        "Neighbor overlap plausibility (min-neighbor ratio) over recent sender messages; "
        "tracks last/mean/min and how often the ratio fell below a tight cutoff."
    ),
    "Sender recent: triplets": (
        "Similar-motion triplet ratio over recent sender messages; "
        "nonzero ratios versus all-zero regimes, plus counts and fractions above zero."
    ),
    "Sender recent: neighbor speed": (
        "Mean neighbor speed difference over recent sender messages; "
        "small diffs (many neighbors matching speed) can be a traffic-sybil signal when combined with other cues."
    ),
    "Sender recent: neighbor heading": (
        "Mean neighbor heading difference over recent sender messages; "
        "counts of very small angular diffs summarize repeated tight alignment with neighbors."
    ),
    "Sender recent history": (
        "Other recent-window summaries over the sender's prior messages (time-capped and count-capped)."
    ),
}


_REPLAY_GROUP_HINTS: Dict[str, str] = {
    "History and timing": (
        "These features indicate whether same-pseudonym history is available and whether recent timing "
        "is consistent with expected message progression."
    ),
    "Plausibility checks": (
        "Data replay attacks may inject stale but previously valid-looking states. "
        "Look for repeated or temporally inconsistent motion/plausibility patterns, not just one weak anomaly."
    ),
    "Neighbor context": (
        "Replay artifacts can break local consistency with live neighbors, especially when stale sender states "
        "are compared against current surrounding traffic."
    ),
    "Sender recent: window": (
        "How much sender-only history is available inside the recent window: message count and "
        "time span (window is time-capped and count-capped)."
    ),
    "Sender recent: MGTSV": (
        "Movement-gradient speed variation over the sender's prior messages in the window; "
        "replay-like behavior may show repeated low-variation or threshold-hit patterns."
    ),
    "Sender recent: overlap": (
        "Neighbor overlap plausibility over recent sender messages; "
        "stale replay states may repeatedly conflict with current local occupancy."
    ),
    "Sender recent: triplets": (
        "Similar-motion triplet ratio over recent sender messages; "
        "replay can produce repeated structures that diverge from live dynamics."
    ),
    "Sender recent: neighbor speed": (
        "Mean neighbor speed difference over recent sender messages; "
        "stale replay trajectories can repeatedly mismatch current neighbor speeds."
    ),
    "Sender recent: neighbor heading": (
        "Mean neighbor heading difference over recent sender messages; "
        "replay trajectories can repeatedly mismatch current neighbor headings."
    ),
    "Sender recent history": (
        "Other recent-window summaries over the sender's prior messages (time-capped and count-capped)."
    ),
}


PROMPT_VARIANT_SPECS: Dict[str, PromptVariantSpec] = {
    "default": PromptVariantSpec(
        intro_lines=(
            "You are an onboard CAV intrusion-detection model.",
            "",
            "Task:",
            "Given receiver-visible features for one BSM, predict whether its sender is attacker-controlled.",
            "Focus on plausibility checks, short-history consistency, and neighbor-context consistency.",
            "",
            "Output rules:",
            "- Return ONLY one label token: benign or attack.",
            "- benign means non-attacker sender; attack means attacker-controlled sender.",
            "- If evidence is mixed, prefer benign unless multiple checks are clearly suspicious.",
            "- Do not output explanations or extra text.",
            "",
            "BSM features:",
        ),
        group_hints={},
    ),
    "traffic_neutral": PromptVariantSpec(
        intro_lines=(
            "You are an onboard CAV intrusion-detection model.",
            "",
            "Task:",
            "Given receiver-visible features for one BSM, predict whether the sender is benign or attacker-controlled in a traffic sybil scenario.",
            "",
            "Decision rule:",
            "- Use the overall pattern across plausibility checks, short-history consistency, and neighbor-context consistency.",
            "- Traffic sybil attacks often appear as groups of nearby identities with overly similar motion or implausible local behavior.",
            "- Do not rely on a single weak anomaly in isolation.",
            "",
            "Output rules:",
            "- Return ONLY one label token: benign or attack.",
            "- Do not output explanations or extra text.",
            "",
            "BSM features:",
        ),
        group_hints=_TRAFFIC_GROUP_HINTS,
    ),
    "traffic_neutral_recentk": PromptVariantSpec(
        intro_lines=(
            "You are an onboard CAV intrusion-detection model.",
            "",
            "Task:",
            "Given receiver-visible features for one BSM, predict whether the sender is benign or attacker-controlled in a traffic sybil scenario.",
            "",
            "Decision rule:",
            "- Use the overall pattern across plausibility checks, short-history consistency, neighbor-context consistency, and sender recent-history summaries.",
            "- Sender recent-history lines aggregate key signals over the sender's prior messages in a short time window (time-capped and count-capped).",
            "- Traffic sybil attacks often appear as groups of nearby identities with overly similar motion or implausible local behavior.",
            "- Do not rely on a single weak anomaly in isolation.",
            "",
            "Output rules:",
            "- Return ONLY one label token: benign or attack.",
            "- Do not output explanations or extra text.",
            "",
            "BSM features:",
        ),
        group_hints=_TRAFFIC_GROUP_HINTS,
    ),
    "replay_neutral_recentk": PromptVariantSpec(
        intro_lines=(
            "You are an onboard CAV intrusion-detection model.",
            "",
            "Task:",
            "Given receiver-visible features for one BSM, predict whether the sender is benign or attacker-controlled in a data replay scenario.",
            "",
            "Decision rule:",
            "- Use the overall pattern across plausibility checks, short-history consistency, neighbor-context consistency, and sender recent-history summaries.",
            "- Sender recent-history lines aggregate key signals over the sender's prior messages in a short time window (time-capped and count-capped).",
            "- Data replay attacks may reuse stale states that looked plausible in the past but are inconsistent with the current scene.",
            "- Do not rely on a single weak anomaly in isolation.",
            "",
            "Output rules:",
            "- Return ONLY one label token: benign or attack.",
            "- Do not output explanations or extra text.",
            "",
            "BSM features:",
        ),
        group_hints=_REPLAY_GROUP_HINTS,
    ),
    "traffic_benign_prior": PromptVariantSpec(
        intro_lines=(
            "You are an onboard CAV intrusion-detection model.",
            "",
            "Task:",
            "Given receiver-visible features for one BSM, predict whether the sender is benign or attacker-controlled in a traffic sybil scenario.",
            "",
            "Decision rule:",
            "- Use the overall pattern across plausibility checks, short-history consistency, and neighbor-context consistency.",
            "- Traffic sybil attacks often appear as groups of nearby identities with overly similar motion or implausible local behavior.",
            "- Do not rely on a single weak anomaly in isolation.",
            "- If evidence is mixed, prefer benign unless multiple signals are clearly suspicious.",
            "",
            "Output rules:",
            "- Return ONLY one label token: benign or attack.",
            "- Do not output explanations or extra text.",
            "",
            "BSM features:",
        ),
        group_hints=_TRAFFIC_GROUP_HINTS,
    ),
    "traffic_compact": PromptVariantSpec(
        intro_lines=(
            "You are an onboard CAV intrusion-detection model for traffic sybil detection.",
            "",
            "Task:",
            "Given receiver-visible features for one BSM, predict whether the sender is benign or attacker-controlled.",
            "Use plausibility checks, short-history consistency, and neighbor-context consistency together.",
            "Traffic sybil attacks may appear as nearby identities with implausibly similar local behavior.",
            "",
            "Output rules:",
            "- Return ONLY one label token: benign or attack.",
            "- Do not output explanations or extra text.",
            "",
            "BSM features:",
        ),
        group_hints={},
    ),
}


def role_lines_for_prompt_variant(prompt_variant: str) -> List[str]:
    v = str(prompt_variant).strip().lower()
    if v in PROMPT_VARIANT_SPECS:
        return list(PROMPT_VARIANT_SPECS[v].intro_lines)
    raise ValueError(
        f"Unknown prompt_variant={prompt_variant!r}; expected one of {list(PROMPT_VARIANTS)}"
    )


def group_hint_for_prompt_variant(prompt_variant: str, group: str) -> str | None:
    v = str(prompt_variant).strip().lower()
    spec = PROMPT_VARIANT_SPECS.get(v)
    if spec is None:
        raise ValueError(
            f"Unknown prompt_variant={prompt_variant!r}; expected one of {list(PROMPT_VARIANTS)}"
        )
    return spec.group_hints.get(group)


@dataclass
class GridSybilPlausibilityPrompt:
    prompt_text: str
    fixed_tokens: int
    feature_line_tokens: List[int]
    features_total: int
    features_kept: int
    is_truncated_features: bool
    feature_budget: int | None
    tokens_after_budget_build: int
    visible_feature_keys: List[str]


def _is_missing(x: Any) -> bool:
    if x is None:
        return True
    try:
        if isinstance(x, float) and math.isnan(x):
            return True
        if isinstance(x, np.floating) and np.isnan(x):
            return True
    except Exception:
        return False
    return False


def fmt_scalar(x: Any, decimals: int = 3) -> str:
    if _is_missing(x):
        return "null"
    if isinstance(x, (bool, np.bool_)):
        return "true" if bool(x) else "false"
    if isinstance(x, (int, np.integer)):
        return str(int(x))
    if isinstance(x, (float, np.floating)):
        v = float(x)
        if math.isinf(v) or math.isnan(v):
            return "null"
        return f"{v:.{decimals}f}"
    if isinstance(x, str):
        s = x.replace("\n", " ").strip()
        return s[:500] + ("..." if len(s) > 500 else "")
    return json.dumps(x, ensure_ascii=False)[:500]


def label_text_from_int(label: int) -> str:
    return "attack" if int(label) == 1 else "benign"


def answer_suffix_text_from_label(label: int) -> str:
    return f" {label_text_from_int(label)}"


_FIRST_BINARY_RE = re.compile(r"[01]")
_FIRST_ATTACK_RE = re.compile(r"\battack\b", re.IGNORECASE)
_FIRST_BENIGN_RE = re.compile(r"\bbenign\b", re.IGNORECASE)


def parse_prediction_label(text: str) -> Tuple[int | None, bool]:
    if not text:
        return None, False
    s = str(text).strip()
    if _FIRST_ATTACK_RE.search(s):
        return 1, True
    if _FIRST_BENIGN_RE.search(s):
        return 0, True
    # Backward-compatible parsing for numeric outputs.
    m = _FIRST_BINARY_RE.search(s)
    if not m:
        return None, False
    return int(m.group(0)), True


def _recentk_subgroup_title(key: str) -> str:
    """Subgroup heading for ctx_recentk_* (order: specific patterns before fallback)."""
    rest = key[len("ctx_recentk_") :] if key.startswith("ctx_recentk_") else key
    if rest.startswith("hist_"):
        return "Sender recent: window"
    if "mgtsv" in rest:
        return "Sender recent: MGTSV"
    if "int_min_neighbor" in rest:
        return "Sender recent: overlap"
    if "triplet_ratio" in rest:
        return "Sender recent: triplets"
    if "speed_diff_mean" in rest:
        return "Sender recent: neighbor speed"
    if "head_diff_mean_deg" in rest:
        return "Sender recent: neighbor heading"
    return "Sender recent history"


def _render_feature_name(key: str, style: str) -> str:
    s = str(style).strip().lower()
    if s == "raw":
        return key
    if s == "descriptive":
        if key in FEATURE_ALIASES:
            return FEATURE_ALIASES[key]
        return key.replace("_", " ")
    raise ValueError(f"Unknown feature_name_style={style!r}; expected raw/descriptive")


def _feature_group_title(key: str) -> str:
    if key.startswith("msg_catch_"):
        return "Plausibility checks"
    if key.startswith("ctx_recentk_"):
        return _recentk_subgroup_title(key)
    if key.startswith("ctx_"):
        return "Neighbor context"
    return "History and timing"


def _select_feature_keys(
    sample: Dict[str, Any],
    include_prefixes: List[str],
    include_columns: Sequence[str],
    exclude_columns: Set[str],
) -> List[str]:
    out: List[str] = []
    include_columns_set = set(include_columns)
    for k in include_columns:
        if k in sample and k not in exclude_columns and k not in out:
            out.append(k)
    for k in sorted(sample.keys()):
        if k in exclude_columns or k in out:
            continue
        if k in include_columns_set or any(k.startswith(p) for p in include_prefixes):
            out.append(k)
    return out


def build_plausibility_prompt(
    sample: Dict[str, Any],
    tokenizer: Any,
    simulate_budget_cutoff: bool,
    total_budget: int,
    reserve_answer_tokens: int,
    prompt_variant: str,
    feature_name_style: str,
    include_prefixes: List[str] | None,
    include_columns: Set[str] | None,
    exclude_columns: Set[str],
) -> GridSybilPlausibilityPrompt:
    role_lines = role_lines_for_prompt_variant(prompt_variant)
    include_prefixes = list(include_prefixes or [])
    include_columns = list(include_columns or DEFAULT_FEATURE_WHITELIST)
    feature_keys = _select_feature_keys(
        sample=sample,
        include_prefixes=include_prefixes,
        include_columns=include_columns,
        exclude_columns=exclude_columns,
    )
    per_feature_lines: List[str] = []
    group_by_key: Dict[str, str] = {}
    for k in feature_keys:
        group = _feature_group_title(k)
        group_by_key[k] = group
        per_feature_lines.append(
            f"- {_render_feature_name(k, feature_name_style)}: {fmt_scalar(sample.get(k))}"
        )
    feature_line_tokens = [
        len(tokenizer(line, truncation=False, add_special_tokens=False)["input_ids"])
        for line in per_feature_lines
    ]

    footer = ["", "Answer:"]
    fixed_text_no_features = "\n".join(role_lines)
    fixed_with_footer = "\n".join([fixed_text_no_features, *footer])
    fixed_tokens = len(
        tokenizer(fixed_with_footer, truncation=False, add_special_tokens=False)["input_ids"]
    )

    if not simulate_budget_cutoff:
        body_lines: List[str] = []
        last_group: str | None = None
        for k, line in zip(feature_keys, per_feature_lines):
            group = group_by_key[k]
            if group != last_group:
                body_lines.append(f"[{group}]")
                group_hint = group_hint_for_prompt_variant(prompt_variant, group)
                if group_hint:
                    body_lines.append(group_hint)
                last_group = group
            body_lines.append(line)
        prompt_text = "\n".join([fixed_text_no_features, *body_lines, *footer])
        tokens_after_budget_build = len(
            tokenizer(prompt_text, truncation=False, add_special_tokens=False)["input_ids"]
        )
        return GridSybilPlausibilityPrompt(
            prompt_text=prompt_text,
            fixed_tokens=fixed_tokens,
            feature_line_tokens=feature_line_tokens,
            features_total=len(per_feature_lines),
            features_kept=len(feature_keys),
            is_truncated_features=False,
            feature_budget=None,
            tokens_after_budget_build=tokens_after_budget_build,
            visible_feature_keys=list(feature_keys),
        )

    feature_budget = max(0, total_budget - fixed_tokens - reserve_answer_tokens)
    kept = 0
    token_sum = 0
    truncated = False
    visible_per_feature_lines: List[str] = []
    visible_keys: List[str] = []
    for k, line, tok in zip(feature_keys, per_feature_lines, feature_line_tokens):
        if token_sum + tok <= feature_budget:
            visible_per_feature_lines.append(line)
            visible_keys.append(k)
            token_sum += tok
            kept += 1
        else:
            truncated = True
            break

    if kept == 0 and per_feature_lines:
        visible_per_feature_lines = [per_feature_lines[0]]
        visible_keys = [feature_keys[0]]
        kept = 1
        truncated = len(per_feature_lines) > 1

    visible_lines: List[str] = []
    last_group = None
    for k, line in zip(visible_keys, visible_per_feature_lines):
        group = group_by_key[k]
        if group != last_group:
            visible_lines.append(f"[{group}]")
            group_hint = group_hint_for_prompt_variant(prompt_variant, group)
            if group_hint:
                visible_lines.append(group_hint)
            last_group = group
        visible_lines.append(line)

    prompt_text = "\n".join([fixed_text_no_features, *visible_lines, *footer])
    tokens_after_budget_build = len(
        tokenizer(prompt_text, truncation=False, add_special_tokens=False)["input_ids"]
    )
    return GridSybilPlausibilityPrompt(
        prompt_text=prompt_text,
        fixed_tokens=fixed_tokens,
        feature_line_tokens=feature_line_tokens,
        features_total=len(per_feature_lines),
        features_kept=kept,
        is_truncated_features=truncated,
        feature_budget=feature_budget,
        tokens_after_budget_build=tokens_after_budget_build,
        visible_feature_keys=visible_keys,
    )
