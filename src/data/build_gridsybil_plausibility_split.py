from __future__ import annotations

import argparse
import glob
import hashlib
import heapq
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pyarrow as pa
import pyarrow.parquet as pq


def _log(msg: str) -> None:
    print(msg, flush=True)


def _parquet_num_rows(path: Path) -> Optional[int]:
    try:
        n = pq.ParquetFile(path).metadata.num_rows
        return int(n) if n is not None else None
    except Exception:
        return None


def _pct_suffix(rows_seen: int, total_rows: Optional[int]) -> str:
    if total_rows is None or total_rows <= 0:
        return ""
    return f" ({100.0 * rows_seen / total_rows:.1f}% of input rows)"


def parse_trace_filename(name: str) -> Dict[str, int]:
    # traceJSON-<vehicle_id>-<obu_id>-A<attack_flag>-<start_sec>-<run_idx>.json
    stem = name.replace(".json", "")
    parts = stem.split("-")
    if len(parts) < 6 or parts[0] != "traceJSON":
        raise ValueError(f"Unexpected trace filename: {name}")
    return {
        "vehicle_id": int(parts[1]),
        "obu_id": int(parts[2]),
        "attack_flag": int(parts[3].lstrip("A")),
        "start_sec": int(parts[4]),
        "run_idx": int(parts[5]),
    }


def run_stem_from_episode_id(episode_id: str) -> str:
    return episode_id.replace("/", "_")


def trace_run_stem(path: Path) -> str:
    # .../GridSybil_0709/VeReMi_25200.../traceJSON-*.json
    return f"{path.parent.parent.name}_{path.parent.name}"


def normalize_windows(raw: Any, field_name: str) -> List[Tuple[float, float]]:
    if not isinstance(raw, list):
        raise ValueError(f"{field_name} must be a list of {{start,end}} mappings.")
    out: List[Tuple[float, float]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{field_name}[{i}] must be a mapping.")
        if "start" not in item or "end" not in item:
            raise ValueError(f"{field_name}[{i}] must include start and end.")
        start = float(item["start"])
        end = float(item["end"])
        if not start < end:
            raise ValueError(f"{field_name}[{i}] must satisfy start < end; got {start} >= {end}.")
        out.append((start, end))
    return out


def overlap_windows(a: Tuple[float, float], b: Tuple[float, float]) -> bool:
    # left-closed right-open intervals [start, end)
    return a[0] < b[1] and b[0] < a[1]


def validate_no_cross_split_overlap(
    train_windows: List[Tuple[float, float]],
    val_windows: List[Tuple[float, float]],
    test_windows: List[Tuple[float, float]],
) -> None:
    named = [
        ("train", train_windows),
        ("val", val_windows),
        ("test", test_windows),
    ]
    for i in range(len(named)):
        left_name, left_windows = named[i]
        for j in range(i + 1, len(named)):
            right_name, right_windows = named[j]
            for a in left_windows:
                for b in right_windows:
                    if overlap_windows(a, b):
                        raise ValueError(
                            f"Time windows overlap between {left_name} and {right_name}: {a} vs {b}"
                        )


def split_name_from_time(
    rcv_time: Any,
    train_windows: List[Tuple[float, float]],
    val_windows: List[Tuple[float, float]],
    test_windows: List[Tuple[float, float]],
) -> Optional[str]:
    if rcv_time is None:
        return None
    t = float(rcv_time)
    for start, end in train_windows:
        if start <= t < end:
            return "train"
    for start, end in val_windows:
        if start <= t < end:
            return "val"
    for start, end in test_windows:
        if start <= t < end:
            return "test"
    return None


def load_split(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    has_time_windows = all(k in payload for k in ("train_windows", "val_windows", "test_windows"))
    if has_time_windows:
        train_windows = normalize_windows(payload.get("train_windows"), "train_windows")
        val_windows = normalize_windows(payload.get("val_windows"), "val_windows")
        test_windows = normalize_windows(payload.get("test_windows"), "test_windows")
        validate_no_cross_split_overlap(train_windows, val_windows, test_windows)
        payload["split_mode"] = "time_window"
        payload["train_windows"] = train_windows
        payload["val_windows"] = val_windows
        payload["test_windows"] = test_windows
        return payload

    if "train_runs" not in payload or "test_runs" not in payload:
        raise ValueError(
            "split json must include either train_windows/val_windows/test_windows "
            "or train_runs/test_runs."
        )
    train_runs = list(payload["train_runs"])
    test_runs = list(payload["test_runs"])
    overlap = set(train_runs).intersection(set(test_runs))
    if overlap:
        raise ValueError(f"train_runs and test_runs overlap: {sorted(overlap)}")
    payload["split_mode"] = "run"
    return payload


def collect_trace_paths(raw_glob: str) -> List[Path]:
    paths = [Path(p) for p in glob.glob(raw_glob, recursive=True)]
    files = [p for p in paths if p.is_file() and p.name.startswith("traceJSON-") and p.suffix == ".json"]
    return sorted(files)


def update_attack_map(m: Dict[int, int], vehicle_id: int, attack_flag: int) -> None:
    prev = m.get(vehicle_id)
    if prev is None:
        m[vehicle_id] = attack_flag
        return
    if prev != attack_flag:
        m[vehicle_id] = max(prev, attack_flag)


def build_attack_maps(
    trace_paths: List[Path],
    attack_map_mode: str,
) -> Tuple[Dict[int, int], Dict[str, Dict[int, int]]]:
    global_map: Dict[int, int] = {}
    per_run_map: Dict[str, Dict[int, int]] = {}

    for p in trace_paths:
        meta = parse_trace_filename(p.name)
        vehicle_id = int(meta["vehicle_id"])
        attack_flag = int(meta["attack_flag"])

        if attack_map_mode == "global":
            update_attack_map(global_map, vehicle_id, attack_flag)
        else:
            run_stem = trace_run_stem(p)
            run_map = per_run_map.setdefault(run_stem, {})
            update_attack_map(run_map, vehicle_id, attack_flag)

    return global_map, per_run_map


def is_attacker(attack_flag: int, attacker_rule: str, attack_flag_eq: int) -> bool:
    if attacker_rule == "non_zero":
        return attack_flag != 0
    if attacker_rule == "equals":
        return attack_flag == attack_flag_eq
    raise ValueError(f"Unknown attacker_rule: {attacker_rule}")


def label_sender(
    sender_id: int,
    run_stem: str,
    attacker_rule: str,
    attack_flag_eq: int,
    global_map: Dict[int, int],
    per_run_map: Dict[str, Dict[int, int]],
    attack_map_mode: str,
) -> int:
    if attack_map_mode == "global":
        attack_flag = global_map.get(sender_id)
    else:
        attack_flag = per_run_map.get(run_stem, {}).get(sender_id)

    if attack_flag is None:
        return 0
    return 1 if is_attacker(int(attack_flag), attacker_rule, attack_flag_eq) else 0


def split_name(
    run_stem: str,
    train_runs: Set[str],
    test_runs: Set[str],
    *,
    split_mode: str,
    rcv_time: Any = None,
    train_windows: Optional[List[Tuple[float, float]]] = None,
    val_windows: Optional[List[Tuple[float, float]]] = None,
    test_windows: Optional[List[Tuple[float, float]]] = None,
) -> Optional[str]:
    if split_mode == "time_window":
        if train_windows is None or val_windows is None or test_windows is None:
            raise ValueError("time_window mode requires train_windows/val_windows/test_windows.")
        return split_name_from_time(
            rcv_time=rcv_time,
            train_windows=train_windows,
            val_windows=val_windows,
            test_windows=test_windows,
        )
    if run_stem in test_runs:
        return "test"
    if run_stem in train_runs:
        return "train"
    return None


def compute_val_targets(n_train_0: int, n_train_1: int, requested_val: int) -> Tuple[int, int]:
    total = n_train_0 + n_train_1
    if total <= 0 or requested_val <= 0:
        return 0, 0

    requested_val = min(requested_val, total)
    if n_train_1 == 0:
        return requested_val, 0
    if n_train_0 == 0:
        return 0, requested_val

    val_1 = int(round(requested_val * (n_train_1 / total)))
    val_1 = max(1, min(val_1, n_train_1))
    val_0 = requested_val - val_1
    val_0 = min(val_0, n_train_0)
    if val_0 + val_1 < requested_val:
        rem = requested_val - (val_0 + val_1)
        add0 = min(rem, n_train_0 - val_0)
        val_0 += add0
        rem -= add0
        if rem > 0:
            add1 = min(rem, n_train_1 - val_1)
            val_1 += add1
    return val_0, val_1


def build_row_key(
    row: Dict[str, Any],
    available_cols: Set[str],
) -> str:
    parts: List[str] = []
    for col in ("episode_id", "receiver_id", "sender_id", "message_id", "rcv_time", "source_line_idx"):
        if col in available_cols:
            parts.append(str(row.get(col)))
    if not parts:
        parts.append(str(row))
    return "|".join(parts)


def stable_score(seed: int, key: str) -> int:
    digest = hashlib.blake2b(f"{seed}|{key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


def iter_row_dicts(batch: pa.RecordBatch) -> List[Dict[str, Any]]:
    return batch.to_pylist()


def count_train_labels(
    parquet_path: Path,
    batch_size: int,
    train_runs: Set[str],
    test_runs: Set[str],
    split_mode: str,
    train_windows: Optional[List[Tuple[float, float]]],
    val_windows: Optional[List[Tuple[float, float]]],
    test_windows: Optional[List[Tuple[float, float]]],
    attacker_rule: str,
    attack_flag_eq: int,
    global_map: Dict[int, int],
    per_run_map: Dict[str, Dict[int, int]],
    attack_map_mode: str,
    strict_unknown_runs: bool,
    *,
    total_rows: Optional[int],
    progress_every_batches: int,
    quiet: bool,
) -> Tuple[Dict[int, int], Dict[str, int]]:
    pf = pq.ParquetFile(parquet_path)
    needed = ["episode_id", "sender_id"]
    if split_mode == "time_window":
        needed.append("rcv_time")
    present = set(pf.schema_arrow.names)
    for col in needed:
        if col not in present:
            raise ValueError(f"input parquet missing required column: {col}")

    train_label_counts = {0: 0, 1: 0}
    split_row_counts = {"train_candidate": 0, "val": 0, "test": 0, "unknown_run": 0}

    if not quiet:
        _log(
            "[pass1/count_train_labels] scanning parquet (columns episode_id, sender_id only) "
            f"batch_size={batch_size}"
            + (f", ~{total_rows} rows (metadata)" if total_rows else "")
        )
    t0 = time.perf_counter()
    batch_idx = 0
    rows_seen = 0

    for batch in pf.iter_batches(batch_size=batch_size, columns=needed):
        bn = batch.num_rows
        rows_seen += bn
        for row in iter_row_dicts(batch):
            episode_id = str(row["episode_id"])
            run_stem = run_stem_from_episode_id(episode_id)
            s = split_name(
                run_stem,
                train_runs,
                test_runs,
                split_mode=split_mode,
                rcv_time=row.get("rcv_time"),
                train_windows=train_windows,
                val_windows=val_windows,
                test_windows=test_windows,
            )
            if s is None:
                split_row_counts["unknown_run"] += 1
                continue
            if s == "val":
                split_row_counts["val"] += 1
                continue
            if s == "test":
                split_row_counts["test"] += 1
                continue

            sender_id = int(row["sender_id"])
            label = label_sender(
                sender_id=sender_id,
                run_stem=run_stem,
                attacker_rule=attacker_rule,
                attack_flag_eq=attack_flag_eq,
                global_map=global_map,
                per_run_map=per_run_map,
                attack_map_mode=attack_map_mode,
            )
            train_label_counts[label] += 1
            split_row_counts["train_candidate"] += 1

        batch_idx += 1
        if (
            not quiet
            and progress_every_batches > 0
            and (batch_idx == 1 or batch_idx % progress_every_batches == 0)
        ):
            _log(
                f"[pass1/count_train_labels] batch={batch_idx} rows_seen={rows_seen}"
                f"{_pct_suffix(rows_seen, total_rows)}"
            )

    if not quiet:
        _log(
            f"[pass1/count_train_labels] done in {time.perf_counter() - t0:.1f}s "
            f"batches={batch_idx} rows_seen={rows_seen} "
            f"train_candidates={split_row_counts['train_candidate']} "
            f"label0={train_label_counts[0]} label1={train_label_counts[1]}"
        )

    if strict_unknown_runs and split_row_counts["unknown_run"] > 0:
        raise ValueError(f"Found rows with run_stem absent from split spec: {split_row_counts['unknown_run']}")

    return train_label_counts, split_row_counts


def select_val_keys(
    parquet_path: Path,
    batch_size: int,
    train_runs: Set[str],
    test_runs: Set[str],
    split_mode: str,
    train_windows: Optional[List[Tuple[float, float]]],
    val_windows: Optional[List[Tuple[float, float]]],
    test_windows: Optional[List[Tuple[float, float]]],
    attacker_rule: str,
    attack_flag_eq: int,
    global_map: Dict[int, int],
    per_run_map: Dict[str, Dict[int, int]],
    attack_map_mode: str,
    seed: int,
    val_targets: Dict[int, int],
    *,
    total_rows: Optional[int],
    progress_every_batches: int,
    quiet: bool,
) -> Set[str]:
    pf = pq.ParquetFile(parquet_path)
    available_cols = set(pf.schema_arrow.names)
    key_cols = ["episode_id", "sender_id"]
    for c in ("receiver_id", "message_id", "rcv_time", "source_line_idx"):
        if c in available_cols:
            key_cols.append(c)

    heaps: Dict[int, List[Tuple[int, str]]] = {0: [], 1: []}

    if not quiet:
        _log(
            "[pass2/select_val_keys] scanning parquet (key columns for val sampling) "
            f"batch_size={batch_size}"
            + (f", ~{total_rows} rows (metadata)" if total_rows else "")
        )
    t0 = time.perf_counter()
    batch_idx = 0
    rows_seen = 0

    for batch in pf.iter_batches(batch_size=batch_size, columns=key_cols):
        bn = batch.num_rows
        rows_seen += bn
        rows = iter_row_dicts(batch)
        for row in rows:
            episode_id = str(row["episode_id"])
            run_stem = run_stem_from_episode_id(episode_id)
            s = split_name(
                run_stem,
                train_runs,
                test_runs,
                split_mode=split_mode,
                rcv_time=row.get("rcv_time"),
                train_windows=train_windows,
                val_windows=val_windows,
                test_windows=test_windows,
            )
            if s != "train":
                continue

            sender_id = int(row["sender_id"])
            label = label_sender(
                sender_id=sender_id,
                run_stem=run_stem,
                attacker_rule=attacker_rule,
                attack_flag_eq=attack_flag_eq,
                global_map=global_map,
                per_run_map=per_run_map,
                attack_map_mode=attack_map_mode,
            )

            cap = val_targets[label]
            if cap <= 0:
                continue

            key = build_row_key(row, set(key_cols))
            score = stable_score(seed, key)
            neg_score = -score
            heap = heaps[label]
            if len(heap) < cap:
                heapq.heappush(heap, (neg_score, key))
            else:
                # Keep smallest scores -> largest neg_score should be replaced.
                if neg_score > heap[0][0]:
                    heapq.heapreplace(heap, (neg_score, key))

        batch_idx += 1
        if (
            not quiet
            and progress_every_batches > 0
            and (batch_idx == 1 or batch_idx % progress_every_batches == 0)
        ):
            _log(
                f"[pass2/select_val_keys] batch={batch_idx} rows_seen={rows_seen}"
                f"{_pct_suffix(rows_seen, total_rows)}"
            )

    out: Set[str] = set()
    for label in (0, 1):
        out.update(key for _, key in heaps[label])
    if not quiet:
        _log(
            f"[pass2/select_val_keys] done in {time.perf_counter() - t0:.1f}s "
            f"batches={batch_idx} val_target_sizes={val_targets} "
            f"selected_unique_keys={len(out)}"
        )
    return out


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_split_parquets(
    parquet_path: Path,
    batch_size: int,
    train_runs: Set[str],
    test_runs: Set[str],
    split_mode: str,
    train_windows: Optional[List[Tuple[float, float]]],
    val_windows: Optional[List[Tuple[float, float]]],
    test_windows: Optional[List[Tuple[float, float]]],
    attacker_rule: str,
    attack_flag_eq: int,
    global_map: Dict[int, int],
    per_run_map: Dict[str, Dict[int, int]],
    attack_map_mode: str,
    strict_unknown_runs: bool,
    val_keys: Set[str],
    out_train: Path,
    out_val: Path,
    out_test: Path,
    *,
    total_rows: Optional[int],
    progress_every_batches: int,
    quiet: bool,
) -> Dict[str, Dict[str, int]]:
    ensure_parent(out_train)
    ensure_parent(out_val)
    ensure_parent(out_test)

    pf = pq.ParquetFile(parquet_path)
    names = pf.schema_arrow.names
    available_cols = set(names)
    key_cols = set(["episode_id", "sender_id", "receiver_id", "message_id", "rcv_time", "source_line_idx"]).intersection(available_cols)

    writer_train: Optional[pq.ParquetWriter] = None
    writer_val: Optional[pq.ParquetWriter] = None
    writer_test: Optional[pq.ParquetWriter] = None

    stats: Dict[str, Dict[str, int]] = {
        "train": {"rows": 0, "label0": 0, "label1": 0},
        "val": {"rows": 0, "label0": 0, "label1": 0},
        "test": {"rows": 0, "label0": 0, "label1": 0},
        "dropped": {"unknown_run": 0},
    }

    if not quiet:
        _log(
            "[pass3/write_split_parquets] reading ALL columns + writing train/val/test parquet "
            f"batch_size={batch_size}"
            + (f", ~{total_rows} rows (metadata)" if total_rows else "")
        )
    t0 = time.perf_counter()
    batch_idx = 0
    rows_seen = 0

    try:
        for batch in pf.iter_batches(batch_size=batch_size):
            bn = batch.num_rows
            rows_seen += bn
            batch_idx += 1
            rows = iter_row_dicts(batch)
            labels: List[int] = []
            target_split: List[Optional[str]] = []

            for row in rows:
                episode_id = str(row["episode_id"])
                run_stem = run_stem_from_episode_id(episode_id)
                s = split_name(
                    run_stem,
                    train_runs,
                    test_runs,
                    split_mode=split_mode,
                    rcv_time=row.get("rcv_time"),
                    train_windows=train_windows,
                    val_windows=val_windows,
                    test_windows=test_windows,
                )
                if s is None:
                    stats["dropped"]["unknown_run"] += 1
                    labels.append(0)
                    target_split.append(None)
                    continue

                sender_id = int(row["sender_id"])
                label = label_sender(
                    sender_id=sender_id,
                    run_stem=run_stem,
                    attacker_rule=attacker_rule,
                    attack_flag_eq=attack_flag_eq,
                    global_map=global_map,
                    per_run_map=per_run_map,
                    attack_map_mode=attack_map_mode,
                )

                if split_mode == "run" and s == "train":
                    key = build_row_key(row, key_cols)
                    s = "val" if key in val_keys else "train"

                labels.append(label)
                target_split.append(s)

            table = pa.Table.from_batches([batch])
            label_arr = pa.array(labels, type=pa.int8())
            table = table.append_column("label", label_arr)

            split_idx = {"train": [], "val": [], "test": []}
            for i, s in enumerate(target_split):
                if s in split_idx:
                    split_idx[s].append(i)

            for s in ("train", "val", "test"):
                if not split_idx[s]:
                    continue
                idx = pa.array(split_idx[s], type=pa.int64())
                chunk = table.take(idx)

                if s == "train":
                    if writer_train is None:
                        writer_train = pq.ParquetWriter(out_train, chunk.schema)
                    writer_train.write_table(chunk)
                elif s == "val":
                    if writer_val is None:
                        writer_val = pq.ParquetWriter(out_val, chunk.schema)
                    writer_val.write_table(chunk)
                else:
                    if writer_test is None:
                        writer_test = pq.ParquetWriter(out_test, chunk.schema)
                    writer_test.write_table(chunk)

                split_labels = chunk.column("label").to_pylist()
                n1 = int(sum(int(x) for x in split_labels))
                n0 = len(split_labels) - n1
                stats[s]["rows"] += len(split_labels)
                stats[s]["label0"] += n0
                stats[s]["label1"] += n1

            if (
                not quiet
                and progress_every_batches > 0
                and (batch_idx == 1 or batch_idx % progress_every_batches == 0)
            ):
                _log(
                    f"[pass3/write_split_parquets] batch={batch_idx} rows_seen={rows_seen}"
                    f"{_pct_suffix(rows_seen, total_rows)} "
                    f"cumulative_out train={stats['train']['rows']} "
                    f"val={stats['val']['rows']} test={stats['test']['rows']}"
                )

        if not quiet:
            _log(
                f"[pass3/write_split_parquets] done in {time.perf_counter() - t0:.1f}s "
                f"batches={batch_idx} rows_seen={rows_seen} "
                f"out train={stats['train']['rows']} val={stats['val']['rows']} "
                f"test={stats['test']['rows']} dropped_unknown_run={stats['dropped']['unknown_run']}"
            )
    finally:
        if writer_train is not None:
            writer_train.close()
        if writer_val is not None:
            writer_val.close()
        if writer_test is not None:
            writer_test.close()

    if strict_unknown_runs and stats["dropped"]["unknown_run"] > 0:
        raise ValueError(f"Found rows with run_stem absent from split spec: {stats['dropped']['unknown_run']}")

    return stats


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build train/val/test parquet splits for GridSybil plausibility message-level classification."
    )
    ap.add_argument(
        "--input-parquet",
        type=Path,
        default=Path("data/processed/plausibility_messages/message_features.parquet"),
        help="Source message-level feature parquet (all columns preserved).",
    )
    ap.add_argument(
        "--split-json",
        type=Path,
        default=Path("data/processed/splits/gridsybil_plausibility_messages.json"),
        help=(
            "Split config. Supports either "
            "{train_runs,test_runs,val_policy} (legacy) or "
            "{train_windows,val_windows,test_windows} with left-closed right-open windows."
        ),
    )
    ap.add_argument(
        "--raw-glob",
        type=str,
        default="data/raw/GridSybil_*/*/traceJSON-*.json",
        help="Glob to raw GridSybil trace files for sender vehicle_id -> attack_flag map.",
    )
    ap.add_argument(
        "--attack-map-mode",
        choices=["global", "per_run"],
        default="per_run",
        help="How vehicle attack map is built from trace filenames.",
    )
    ap.add_argument(
        "--attacker-rule",
        choices=["non_zero", "equals"],
        default="non_zero",
        help="Attacker set criterion over attack_flag.",
    )
    ap.add_argument(
        "--attack-flag",
        type=int,
        default=16,
        help="When attacker-rule=equals, attacker iff attack_flag equals this value.",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=65536,
        help="Parquet batch size for streaming.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "Optional override for val sampling seed in legacy run-based mode; "
            "ignored in time-window mode."
        ),
    )
    ap.add_argument(
        "--out-train",
        type=Path,
        default=Path("data/processed/plausibility_messages_split/train.parquet"),
    )
    ap.add_argument(
        "--out-val",
        type=Path,
        default=Path("data/processed/plausibility_messages_split/val.parquet"),
    )
    ap.add_argument(
        "--out-test",
        type=Path,
        default=Path("data/processed/plausibility_messages_split/test.parquet"),
    )
    ap.add_argument(
        "--dataset-meta",
        type=Path,
        default=Path("data/processed/plausibility_messages_split/dataset_meta.json"),
    )
    ap.add_argument(
        "--strict-unknown-runs",
        action="store_true",
        help="Fail if any row's run_stem is absent from split spec.",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-batch progress lines (phase headers may still print).",
    )
    ap.add_argument(
        "--progress-every-batches",
        type=int,
        default=20,
        metavar="N",
        help="Print progress every N batches in each parquet pass (0 = only start/end of each pass).",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if not args.input_parquet.exists():
        raise FileNotFoundError(f"input parquet not found: {args.input_parquet}")
    if not args.split_json.exists():
        raise FileNotFoundError(f"split json not found: {args.split_json}")

    total_rows = _parquet_num_rows(args.input_parquet)
    t_main = time.perf_counter()
    if not args.quiet:
        _log(
            "[start] build_gridsybil_plausibility_split "
            f"input={args.input_parquet} split={args.split_json} raw_glob={args.raw_glob}"
        )
        _log(
            f"[start] attack_map_mode={args.attack_map_mode} attacker_rule={args.attacker_rule} "
            f"batch_size={args.batch_size} progress_every_batches={args.progress_every_batches}"
        )
        _log("[start] parquet row count (metadata): " + (str(total_rows) if total_rows is not None else "unknown"))

    split_spec = load_split(args.split_json)
    split_mode = str(split_spec.get("split_mode", "run"))
    train_runs: Set[str] = set(split_spec.get("train_runs", []))
    test_runs: Set[str] = set(split_spec.get("test_runs", []))
    train_windows: Optional[List[Tuple[float, float]]] = None
    val_windows: Optional[List[Tuple[float, float]]] = None
    test_windows: Optional[List[Tuple[float, float]]] = None
    val_policy: Dict[str, Any] = {}
    requested_val = 0
    seed: Optional[int] = None
    val_targets: Optional[Dict[int, int]] = None
    val_keys: Set[str] = set()
    train_label_counts: Optional[Dict[int, int]] = None
    pass1_split_counts: Optional[Dict[str, int]] = None

    if split_mode == "time_window":
        train_windows = split_spec["train_windows"]
        val_windows = split_spec["val_windows"]
        test_windows = split_spec["test_windows"]
        present_cols = set(pq.ParquetFile(args.input_parquet).schema_arrow.names)
        if "rcv_time" not in present_cols:
            raise ValueError("time_window split mode requires rcv_time column in input parquet.")
        if not args.quiet:
            _log(
                "[split] mode=time_window "
                f"train_windows={train_windows} val_windows={val_windows} test_windows={test_windows}"
            )
    else:
        val_policy = split_spec.get("val_policy", {})
        requested_val = int(val_policy.get("n", 5000))
        seed = int(args.seed if args.seed is not None else val_policy.get("seed", 42))
        if not args.quiet:
            _log(
                "[split] mode=run "
                f"train_runs={len(train_runs)} test_runs={len(test_runs)} "
                f"val_policy={val_policy}"
            )

    trace_paths = collect_trace_paths(args.raw_glob)
    if not trace_paths:
        raise FileNotFoundError(f"No trace files matched raw_glob: {args.raw_glob}")
    global_map, per_run_map = build_attack_maps(trace_paths, args.attack_map_mode)
    if not args.quiet:
        if args.attack_map_mode == "global":
            _log(f"[traces] matched {len(trace_paths)} traceJSON files; global vehicle_ids={len(global_map)}")
        else:
            _log(
                f"[traces] matched {len(trace_paths)} traceJSON files; "
                f"distinct run stems={len(per_run_map)}"
            )

    if split_mode == "run":
        assert seed is not None
        train_label_counts, pass1_split_counts = count_train_labels(
            parquet_path=args.input_parquet,
            batch_size=args.batch_size,
            train_runs=train_runs,
            test_runs=test_runs,
            split_mode=split_mode,
            train_windows=train_windows,
            val_windows=val_windows,
            test_windows=test_windows,
            attacker_rule=args.attacker_rule,
            attack_flag_eq=args.attack_flag,
            global_map=global_map,
            per_run_map=per_run_map,
            attack_map_mode=args.attack_map_mode,
            strict_unknown_runs=args.strict_unknown_runs,
            total_rows=total_rows,
            progress_every_batches=args.progress_every_batches,
            quiet=args.quiet,
        )

        val0, val1 = compute_val_targets(
            n_train_0=train_label_counts[0],
            n_train_1=train_label_counts[1],
            requested_val=requested_val,
        )
        val_targets = {0: val0, 1: val1}
        if not args.quiet:
            _log(
                f"[between] val sampling: requested={requested_val} from train rows; "
                f"targets label0={val_targets[0]} label1={val_targets[1]} (seed={seed})"
            )

        val_keys = select_val_keys(
            parquet_path=args.input_parquet,
            batch_size=args.batch_size,
            train_runs=train_runs,
            test_runs=test_runs,
            split_mode=split_mode,
            train_windows=train_windows,
            val_windows=val_windows,
            test_windows=test_windows,
            attacker_rule=args.attacker_rule,
            attack_flag_eq=args.attack_flag,
            global_map=global_map,
            per_run_map=per_run_map,
            attack_map_mode=args.attack_map_mode,
            seed=seed,
            val_targets=val_targets,
            total_rows=total_rows,
            progress_every_batches=args.progress_every_batches,
            quiet=args.quiet,
        )

    write_stats = write_split_parquets(
        parquet_path=args.input_parquet,
        batch_size=args.batch_size,
        train_runs=train_runs,
        test_runs=test_runs,
        split_mode=split_mode,
        train_windows=train_windows,
        val_windows=val_windows,
        test_windows=test_windows,
        attacker_rule=args.attacker_rule,
        attack_flag_eq=args.attack_flag,
        global_map=global_map,
        per_run_map=per_run_map,
        attack_map_mode=args.attack_map_mode,
        strict_unknown_runs=args.strict_unknown_runs,
        val_keys=val_keys,
        out_train=args.out_train,
        out_val=args.out_val,
        out_test=args.out_test,
        total_rows=total_rows,
        progress_every_batches=args.progress_every_batches,
        quiet=args.quiet,
    )

    args.dataset_meta.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": (
            "gridsybil_plausibility_message_split_v2_time_window"
            if split_mode == "time_window"
            else "gridsybil_plausibility_message_split_v1"
        ),
        "source_parquet": str(args.input_parquet).replace("\\", "/"),
        "split_json": str(args.split_json).replace("\\", "/"),
        "split_mode": split_mode,
        "time_windows": (
            {
                "train": [{"start": s, "end": e} for s, e in (train_windows or [])],
                "val": [{"start": s, "end": e} for s, e in (val_windows or [])],
                "test": [{"start": s, "end": e} for s, e in (test_windows or [])],
            }
            if split_mode == "time_window"
            else None
        ),
        "raw_glob": args.raw_glob,
        "attack_map_mode": args.attack_map_mode,
        "attacker_rule": args.attacker_rule,
        "attack_flag_equals": args.attack_flag if args.attacker_rule == "equals" else None,
        "label_definition": "label=1 iff sender_id belongs to attacker vehicle set derived from trace filename vehicle_id->attack_flag map",
        "seed": seed,
        "val_policy": val_policy if split_mode == "run" else None,
        "train_label_counts_before_val": train_label_counts,
        "val_targets": val_targets,
        "num_val_selected_keys": len(val_keys),
        "pass1_split_counts": pass1_split_counts,
        "output_rows": write_stats,
        "outputs": {
            "train": str(args.out_train).replace("\\", "/"),
            "val": str(args.out_val).replace("\\", "/"),
            "test": str(args.out_test).replace("\\", "/"),
        },
    }
    with args.dataset_meta.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    wall_s = time.perf_counter() - t_main
    print(
        "Done. "
        f"train={write_stats['train']['rows']} "
        f"val={write_stats['val']['rows']} "
        f"test={write_stats['test']['rows']} "
        f"dropped_unknown_run={write_stats['dropped']['unknown_run']} "
        f"wall_time_s={wall_s:.1f}"
    )
    print(f"Meta: {args.dataset_meta}")


if __name__ == "__main__":
    main()