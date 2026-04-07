from __future__ import annotations

"""
Build few-shot demonstration episode id lists for GPT API eval.

This script selects 8 training episodes (d1..d8) in a fixed order such that:
  - K=1 uses d1
  - K=4 uses d1..d4 (prefix)
  - K=8 uses d1..d8

Selection buckets follow `src/eval/metric_integration_gridsybil.py` bin logic,
but use episode-level fields from train.jsonl:
  - eval_meta.num_candidates
  - eval_meta.num_true_attackers
  - input.meta.traffic_regime (only for printing/debug; selection ignores regime)

It also excludes any episode ids listed in an eval test ids file
to avoid overlap (defensive even when train/test are separate).
"""

import argparse
import json
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pick 8 few-shot demo episode ids from train.jsonl."
    )
    p.add_argument(
        "--train_jsonl_path",
        type=str,
        required=True,
        help="Path to processed train.jsonl (episode-level data).",
    )
    p.add_argument(
        "--exclude_ids_file",
        type=str,
        default=None,
        help="Optional txt file: one episode id per line to exclude (e.g. test ids).",
    )
    p.add_argument(
        "--output_demo_ids",
        type=str,
        required=True,
        help="Output txt file: one episode id per line (d1..d8).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for tie-breaking among candidates.",
    )
    return p.parse_args()


def load_episode_rows(jsonl_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {jsonl_path}:{line_no}: {exc}") from exc
    return rows


def load_id_set(path: Path) -> Set[str]:
    out: Set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                out.add(s)
    return out


def candidate_bin_from_num(num_candidates: int) -> str:
    n = int(num_candidates)
    if n <= 8:
        return "less than 9"
    if n <= 16:
        return "9 to 16"
    if n <= 24:
        return "17 to 24"
    if n <= 32:
        return "25 to 32"
    return "more than 32"


def attacker_bin_from_num(num_true_attackers: int) -> str:
    n = int(num_true_attackers)
    if n <= 1:
        return "0 to 1"
    if n <= 4:
        return "2 to 4"
    if n <= 8:
        return "5 to 8"
    if n <= 14:
        return "9 to 14"
    return "more than 14"


def pick_one(
    candidates: List[Dict[str, Any]],
    *,
    mode: str,
    rng: random.Random,
    used_ids: Set[str],
) -> Dict[str, Any]:
    """
    Pick one candidate with deterministic tie-breaking.

    mode:
      - "min_num_true_attackers" => smaller num_true_attackers first
      - "max_num_true_attackers" => larger num_true_attackers first
      - "random" => random among candidates
      - "min_num_candidates" / "max_num_candidates" similarly
    """
    remaining = [ex for ex in candidates if str(ex.get("id")) not in used_ids]
    if not remaining:
        raise ValueError("No remaining candidates after excluding used_ids.")

    if mode == "random":
        return rng.choice(remaining)

    if mode == "min_num_true_attackers":
        min_v = min(int(ex["eval_meta"]["num_true_attackers"]) for ex in remaining)
        bucket = [ex for ex in remaining if int(ex["eval_meta"]["num_true_attackers"]) == min_v]
        return rng.choice(bucket)
    if mode == "max_num_true_attackers":
        max_v = max(int(ex["eval_meta"]["num_true_attackers"]) for ex in remaining)
        bucket = [ex for ex in remaining if int(ex["eval_meta"]["num_true_attackers"]) == max_v]
        return rng.choice(bucket)

    if mode == "min_num_candidates":
        min_v = min(int(ex["eval_meta"]["num_candidates"]) for ex in remaining)
        bucket = [ex for ex in remaining if int(ex["eval_meta"]["num_candidates"]) == min_v]
        return rng.choice(bucket)
    if mode == "max_num_candidates":
        max_v = max(int(ex["eval_meta"]["num_candidates"]) for ex in remaining)
        bucket = [ex for ex in remaining if int(ex["eval_meta"]["num_candidates"]) == max_v]
        return rng.choice(bucket)

    raise ValueError(f"Unknown pick_one mode={mode!r}")


def extract_episode_fields(ex: Dict[str, Any]) -> Tuple[str, str, int, int]:
    """
    Returns:
      (episode_id, traffic_regime, num_candidates, num_true_attackers)
    """
    ep_id = str(ex.get("id"))
    inp = ex.get("input", {})
    meta = inp.get("meta", {}) if isinstance(inp, dict) else {}
    regime = str(meta.get("traffic_regime", "null"))

    eval_meta = ex.get("eval_meta", {}) if isinstance(ex, dict) else {}
    num_candidates = int(eval_meta.get("num_candidates", 0))
    num_true_attackers = int(eval_meta.get("num_true_attackers", 0))

    return ep_id, regime, num_candidates, num_true_attackers


def select_demos(
    rows: List[Dict[str, Any]],
    exclude_ids: Optional[Set[str]],
    seed: int,
) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    used_ids: Set[str] = set()

    def eligible(ex: Dict[str, Any]) -> bool:
        ep_id, _, _, _ = extract_episode_fields(ex)
        if not ep_id:
            return False
        if exclude_ids is not None and ep_id in exclude_ids:
            return False
        if ep_id in used_ids:
            return False
        return True

    def filter_by(
        *,
        cand_bin: Optional[str] = None,
        attack_bin: Optional[str] = None,
        num_true_attackers_exact: Optional[int] = None,
        cand_bin_force: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # Some parameters are aliases to keep call sites readable.
        _cand_bin = cand_bin_force or cand_bin
        out: List[Dict[str, Any]] = []
        for ex in rows:
            if not eligible(ex):
                continue
            _ep_id, _regime, num_cand, num_true = extract_episode_fields(ex)
            if num_true_attackers_exact is not None and num_true != num_true_attackers_exact:
                continue
            if _cand_bin is not None and candidate_bin_from_num(num_cand) != _cand_bin:
                continue
            if attack_bin is not None and attacker_bin_from_num(num_true) != attack_bin:
                continue
            out.append(ex)
        return out

    # d1: K=1 => attack 5–8, cand=9–16, pick closest to 5 => min num_true_attackers
    d1_pool = filter_by(cand_bin="9 to 16", attack_bin="5 to 8")
    if not d1_pool:
        raise ValueError("Cannot find candidates for d1 (attack 5-8, cand 9-16).")
    d1 = pick_one(
        d1_pool, mode="min_num_true_attackers", rng=rng, used_ids=used_ids
    )
    used_ids.add(str(d1.get("id")))

    # d2: K=4 second => attack 0–1 with num_true_attackers=0, cand=less than 9
    d2_pool = filter_by(
        cand_bin="less than 9",
        attack_bin="0 to 1",
        num_true_attackers_exact=0,
    )
    if not d2_pool:
        raise ValueError("Cannot find candidates for d2 (attack 0-1 with num_true=0, cand<9).")
    d2 = pick_one(d2_pool, mode="random", rng=rng, used_ids=used_ids)
    used_ids.add(str(d2.get("id")))

    # d3: K=4 third => attack 9–14, cand=17–24 (we keep it aligned with your final 2-of-9-14 plan)
    d3_pool = filter_by(cand_bin="17 to 24", attack_bin="9 to 14")
    if not d3_pool:
        raise ValueError("Cannot find candidates for d3 (attack 9-14, cand 17-24).")
    d3 = pick_one(d3_pool, mode="random", rng=rng, used_ids=used_ids)
    used_ids.add(str(d3.get("id")))

    # d4: K=4 fourth => attack more than 14, cand more than 32
    d4_pool = filter_by(cand_bin="more than 32", attack_bin="more than 14")
    if not d4_pool:
        raise ValueError("Cannot find candidates for d4 (attack>14, cand>32).")
    d4 = pick_one(d4_pool, mode="random", rng=rng, used_ids=used_ids)
    used_ids.add(str(d4.get("id")))

    # d5: extra for K=8 => attack 5–8, cand=9–16, pick closest to 8 => max num_true_attackers
    d5_pool = filter_by(cand_bin="9 to 16", attack_bin="5 to 8")
    if not d5_pool:
        raise ValueError("Cannot find candidates for d5 (attack 5-8, cand 9-16).")
    d5 = pick_one(d5_pool, mode="max_num_true_attackers", rng=rng, used_ids=used_ids)
    used_ids.add(str(d5.get("id")))

    # d6: extra => attack 0–1 with num_true_attackers=1, cand=9–16
    d6_pool = filter_by(
        cand_bin="9 to 16",
        attack_bin="0 to 1",
        num_true_attackers_exact=1,
    )
    if not d6_pool:
        raise ValueError("Cannot find candidates for d6 (attack 0-1 with num_true=1, cand 9-16).")
    d6 = pick_one(d6_pool, mode="random", rng=rng, used_ids=used_ids)
    used_ids.add(str(d6.get("id")))

    # d7: extra => attack 9–14, cand=17–24
    d7_pool = filter_by(cand_bin="17 to 24", attack_bin="9 to 14")
    if not d7_pool:
        raise ValueError("Cannot find candidates for d7 (attack 9-14, cand 17-24).")
    d7 = pick_one(d7_pool, mode="random", rng=rng, used_ids=used_ids)
    used_ids.add(str(d7.get("id")))

    # d8: extra => attack more than 14, cand 25–32
    d8_pool = filter_by(cand_bin="25 to 32", attack_bin="more than 14")
    if not d8_pool:
        raise ValueError("Cannot find candidates for d8 (attack>14, cand 25-32).")
    d8 = pick_one(d8_pool, mode="random", rng=rng, used_ids=used_ids)
    used_ids.add(str(d8.get("id")))

    return [d1, d2, d3, d4, d5, d6, d7, d8]


def main() -> None:
    args = parse_args()
    train_jsonl_path = Path(args.train_jsonl_path)
    if not train_jsonl_path.exists():
        raise FileNotFoundError(f"train_jsonl_path not found: {train_jsonl_path}")

    exclude_ids: Optional[Set[str]] = None
    if args.exclude_ids_file:
        ex_path = Path(args.exclude_ids_file)
        if not ex_path.exists():
            raise FileNotFoundError(f"exclude_ids_file not found: {ex_path}")
        exclude_ids = load_id_set(ex_path)

    rows = load_episode_rows(train_jsonl_path)
    print(f"Loaded {len(rows)} train episodes from {train_jsonl_path}", flush=True)
    if exclude_ids is not None:
        print(f"Excluding {len(exclude_ids)} episode ids from demos.", flush=True)

    demos = select_demos(
        rows=rows,
        exclude_ids=exclude_ids,
        seed=args.seed,
    )

    for i, d in enumerate(demos, start=1):
        ep_id, regime, num_cand, num_true = extract_episode_fields(d)
        print(
            f"d{i} id={ep_id} regime={regime} num_candidates={num_cand} "
            f"(cand_bin={candidate_bin_from_num(num_cand)}) "
            f"num_true_attackers={num_true} (attack_bin={attacker_bin_from_num(num_true)})",
            flush=True,
        )

    out_path = Path(args.output_demo_ids)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for d in demos:
            f.write(f"{str(d.get('id'))}\n")

    print(f"Wrote 8 few-shot demo ids to {out_path}", flush=True)


if __name__ == "__main__":
    main()

