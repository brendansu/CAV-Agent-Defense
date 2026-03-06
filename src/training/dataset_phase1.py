from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from datasets import Dataset, load_dataset
from transformers import PreTrainedTokenizerBase


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

        lines.append(
            f"t{dt:+.1f}s: sender={snd}, "
            f"claimed_pos={pos}, claimed_spd={spd}, "
            f"ego_pos={ego_pos}, ego_spd={ego_spd}, "
            f"rssi={rssi}"
        )

    lines.append("")
    lines.append("Answer with only one token: BENIGN or ATTACK.")
    lines.append(f"Answer: {label}")

    return "\n".join(lines)


def preprocess_example(example: dict) -> dict:
    """
    datasets.map 用的预处理函数：
        raw JSONL 样本 -> 添加 'text' 字段（完整 prompt+label）。
    """
    text = build_phase1_prompt(example)
    return {"text": text}


def tokenize_function(
    examples: dict,
    tokenizer: PreTrainedTokenizerBase,
    max_seq_len: int,
) -> dict:
    """
    将批量样本的 'text' 字段编码成 token，并构造 labels。
    对于因果语言模型，最简单的做法是 labels = input_ids。
    """
    encoded = tokenizer(
        examples["text"],
        truncation=True,
        max_length=max_seq_len,
    )
    # 在 batched=True 时，encoded["input_ids"] 是 List[List[int]]
    # 这里直接复制一份作为 labels，Trainer 或自定义 Trainer 可据此计算 loss
    encoded["labels"] = encoded["input_ids"]
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