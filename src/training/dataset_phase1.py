from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from datasets import Dataset, load_dataset
from transformers import PreTrainedTokenizerBase


# --- Precision by physical quantity (units and token trade-off) ---
# pos, ego_pos: [x, y] in m  → 2 decimals (cm-level).
# spd, ego_spd: [vx, vy] in m/s  → 2 decimals.
# rssi: linear scale, often ~1e-8  → 2 significant figures (relative level matters).
# dt: seconds, formatted in loop as +.2f (0.01 s).


def _fmt_num(x: Any, decimals: int = 1) -> str:
    """Format a single number with limited precision (used by _fmt_*_list)."""
    if x is None:
        return "-"
    try:
        v = float(x)
        if abs(v) >= 0.01 and abs(v) < 1e6:
            return f"{v:.{decimals}f}"
        return f"{v:.2e}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_list(lst: Any, decimals: int = 1) -> str:
    """Format a list [a, b] with given decimal places (for pos/spd in m, m/s)."""
    if lst is None or (isinstance(lst, list) and len(lst) == 0):
        return "-"
    if not isinstance(lst, (list, tuple)):
        return _fmt_num(lst, decimals)
    parts = [_fmt_num(v, decimals) for v in lst]
    return "[" + ",".join(parts) + "]"


def _fmt_rssi(x: Any) -> str:
    """RSSI (linear, often ~1e-8): 2 significant figures; relative level matters for detection."""
    if x is None:
        return "-"
    try:
        v = float(x)
        if v == 0:
            return "0"
        if abs(v) >= 0.01 and abs(v) < 1000:
            return f"{v:.2f}"
        return f"{v:.2e}"
    except (TypeError, ValueError):
        return str(x)


@dataclass
class Phase1DatasetConfig:
    """
    配置 Phase 1（单 sender 时序）数据集的基本参数。
    """
    jsonl_dir: Path              # e.g. data/processed/jsonl/phase1_binary
    max_seq_len: int = 512       # tokenizer 截断长度


def load_raw_phase1_splits(config: Phase1DatasetConfig) -> Dict[str, Dataset]:
    """
    从 jsonl 目录读取 raw train/val/test splits（不做任何处理）。
    预期文件：
        - <jsonl_dir>/train.jsonl
        - <jsonl_dir>/val.jsonl
        - <jsonl_dir>/test.jsonl
    """
    base = config.jsonl_dir

    raw_train = load_dataset("json", data_files=str(base / "train.jsonl"))["train"]
    raw_val = load_dataset("json", data_files=str(base / "val.jsonl"))["train"]
    raw_test = load_dataset("json", data_files=str(base / "test.jsonl"))["train"]

    return {"train": raw_train, "val": raw_val, "test": raw_test}


def build_phase1_prompt(example: dict) -> str:
    """
    从一条 Phase 1 JSONL 样本构建训练用文本。
    当前采用简单的英文模板，并将标签附在末尾（适合因果 LM + LoRA SFT）。
    将来如果需要，可以只改这里的模板。
    """
    inp = example.get("input", {})
    label = example.get("output", "BENIGN")  # "BENIGN" / "ATTACK"

    phase = inp.get("phase", "phase1")
    run_id = inp.get("run_id", "UNKNOWN_RUN")
    receiver_id = inp.get("receiver_id", "UNKNOWN_RCV")
    sender_id = inp.get("sender_id", "UNKNOWN_SND")
    attacker_type = inp.get("attacker_type", None)

    history = inp.get("history", [])

    lines = []
    lines.append("You are an onboard CAV intrusion detection system.")
    lines.append(
        "Given the recent message history between a receiver and a sender, "
        "decide whether the LAST message in the sequence is BENIGN or ATTACK."
    )
    lines.append(f"Phase: {phase}")
    lines.append(f"Run ID: {run_id}")
    lines.append(f"Receiver vehicle ID: {receiver_id}")
    lines.append(f"Sender vehicle ID: {sender_id}")
    if attacker_type is not None:
        lines.append(f"(Scenario attacker_type: {attacker_type})")
    lines.append("")
    lines.append("History (from oldest to newest):")

    # 按 dt 从小到大排序（通常已经是按时间顺序）
    # dt 是相对于最后一条消息的时间差（负数到 0）
    try:
        history_sorted = sorted(history, key=lambda h: float(h.get("dt", 0.0)))
    except Exception:
        history_sorted = history

    for step in history_sorted:
        dt = float(step.get("dt", 0.0))
        snd = step.get("sender_id", sender_id)

        claimed = step.get("claimed", {})
        ego = step.get("ego", {})
        pos = claimed.get("pos", [None, None])
        spd = claimed.get("spd", [None, None])
        rssi = claimed.get("rssi", None)
        ego_pos = ego.get("pos", [None, None])
        ego_spd = ego.get("spd", [None, None])

        # Quantity-aware precision: pos/ego_pos (m) and spd/ego_spd (m/s) → 2 decimals; rssi → 2 sigfigs; dt → .2f s
        pos_s = _fmt_list(pos, decimals=2)
        spd_s = _fmt_list(spd, decimals=2)
        ego_pos_s = _fmt_list(ego_pos, decimals=2)
        ego_spd_s = _fmt_list(ego_spd, decimals=2)
        rssi_s = _fmt_rssi(rssi)

        lines.append(
            f"t{dt:+.2f}s: sender={snd}, "
            f"claimed_pos={pos_s}, claimed_spd={spd_s}, "
            f"ego_pos={ego_pos_s}, ego_spd={ego_spd_s}, "
            f"rssi={rssi_s}"
        )

    lines.append("")
    lines.append("Answer with only one token: BENIGN or ATTACK.")
    lines.append(f"Answer: {label}")

    return "\n".join(lines)


def preprocess_example(example: dict) -> dict:
    """
    datasets.map 用的预处理函数：
        raw JSONL 样本 -> 添加 'text' 与 'is_attack'（用于 loss 加权）。
    """
    text = build_phase1_prompt(example)
    label = example.get("output", "BENIGN")
    is_attack = 1 if label == "ATTACK" else 0
    return {"text": text, "is_attack": is_attack}


def tokenize_function(
    examples: dict,
    tokenizer: PreTrainedTokenizerBase,
    max_seq_len: int,
) -> dict:
    """
    将批量样本的 'text' 字段编码成 token，并构造 labels。
    同时保留 is_attack 与每条序列的 length（用于 answer 位置 loss 加权）。
    padding="max_length" 保证 batch 内长度一致，避免 default collator 堆叠时报错。
    """
    encoded = tokenizer(
        examples["text"],
        truncation=True,
        max_length=max_seq_len,
        padding="max_length",
        return_tensors=None,
    )
    # labels：padding 位置置 -100，否则与 input_ids 一致（causal LM）
    labels = [list(ids) for ids in encoded["input_ids"]]
    for i in range(len(labels)):
        for j in range(len(encoded["attention_mask"][i])):
            if encoded["attention_mask"][i][j] == 0:
                labels[i][j] = -100
    encoded["labels"] = labels
    # 答案 token 为每句最后一个非 pad token；length = 非 pad 的 token 数
    encoded["length"] = [sum(encoded["attention_mask"][i]) for i in range(len(encoded["attention_mask"]))]
    # 从 output 列派生 is_attack（第一轮 map 的 is_attack 可能未写入 dataset，此处保证有值）
    encoded["is_attack"] = [1 if (o == "ATTACK") else 0 for o in examples["output"]]
    return encoded


def load_phase1_binary_datasets(
    jsonl_dir: str | Path,
    tokenizer: PreTrainedTokenizerBase,
    max_seq_len: int = 512,
) -> Tuple[Dataset, Dataset]:
    """
    高层入口：加载 Phase 1 二分类任务的 train/val 数据集并完成 tokenization。
    返回可直接给 Trainer 使用的 tokenized datasets。
    """
    config = Phase1DatasetConfig(jsonl_dir=Path(jsonl_dir), max_seq_len=max_seq_len)

    raw_splits = load_raw_phase1_splits(config)
    raw_train = raw_splits["train"]
    raw_val = raw_splits["val"]

    # 先构造 text 字段
    proc_train = raw_train.map(preprocess_example)
    proc_val = raw_val.map(preprocess_example)

    # 再做 tokenization
    # 注意：remove_columns 去掉原始 JSON 字段，只保留 tokenized 内容
    tokenized_train = proc_train.map(
        lambda batch: tokenize_function(batch, tokenizer, config.max_seq_len),
        batched=True,
        remove_columns=proc_train.column_names,
    )
    tokenized_val = proc_val.map(
        lambda batch: tokenize_function(batch, tokenizer, config.max_seq_len),
        batched=True,
        remove_columns=proc_val.column_names,
    )

    return tokenized_train, tokenized_val