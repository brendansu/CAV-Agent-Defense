# HPC 迁移上下文（给新 chat 用）

把本文件内容（或本文件路径）提供给新开的 chat，用于讨论把 Phase 1 训练与评估迁移到 HPC 上运行。

---

## 1. 项目与任务简述

- **仓库**：`veremi_agent`，VeReMi 数据集上的 CAV 入侵检测，Phase 1 为单 sender 时序二分类（BENIGN/ATTACK）。
- **目标**：在 HPC 上跑训练和 eval，快速得到**第一版 finetuned baseline 模型**，后续在 HPC baseline 上做 loss 变体、数据 stratification 等调整。
- **当前状态**：本地 pipeline 已跑通；训练与 eval 脚本、YAML 配置、数据与 prompt 设计已稳定，准备迁到 HPC。

---

## 2. 关键脚本与入口

- **训练**（从仓库根目录执行）  
  `python -m src.training.train_lora_qwen_phase1_binary --config configs/qwen2.5_1.5b_phase1_binary_debug.yaml`
- **Tokenizer / 截断检查**（迁到 HPC 后建议先跑一次确认无截断）  
  `python -m src.training.tokenizer_eval --config configs/qwen2.5_1.5b_phase1_binary_debug.yaml`
- **分类评估**（base vs LoRA，accuracy/F1 + 样例）  
  `python -m src.eval.eval_phase1_binary --jsonl_dir data/processed/jsonl/phase1_binary_debug --split test --mode both --lora_dir <输出目录> --max_seq_len 1536`

所有路径均相对于**仓库根目录**；HPC 上需保证同一目录结构（或修改 YAML/命令行中的路径）。

---

## 3. 配置文件（当前 debug 用）

- **主配置**：`configs/qwen2.5_1.5b_phase1_binary_debug.yaml`
- 关键字段含义与当前取值：
  - `model_name`: `Qwen/Qwen2.5-1.5B-Instruct`（HuggingFace 名；若 HPC 无外网可改为本地权重路径）
  - `jsonl_dir`: `data/processed/jsonl/phase1_binary_debug`（训练/验证用 JSONL 目录）
  - `output_dir`: `outputs/qwen2.5-1.5b-phase1-binary-debug`（checkpoint 与最终 adapter 输出）
  - `max_seq_len`: `1536`（必须 ≥ 约 1050，否则 prompt 含 “Answer:” 会被截断；详见下方「Prompt 与截断」）
  - `num_train_epochs`: 当前 debug 用 `0.05`；正式 baseline 可改为 `0.3` 或 1.0
  - `eval_strategy`: `steps`（或 `epoch` / `no`）
  - `eval_steps`: `500`（仅当 `eval_strategy: steps` 时生效）
  - `max_eval_samples`: `500`（训练过程中只用 val 的前 500 条做 eval，减少时间；0 表示用全量 val）
  - `save_steps`: `500`，`save_total_limit`: `2`
  - LoRA：`lora_r: 16`, `lora_alpha: 32`, `lora_dropout: 0.05`，`target_modules: [q_proj, k_proj, v_proj, o_proj]`

Full 数据集配置在 `configs/qwen2.5_1.5b_phase1_binary.yaml`，将 `jsonl_dir` 改为 `data/processed/jsonl/phase1_binary` 即可用于完整数据训练。

---

## 4. 数据与目录结构（需拷到 HPC 的内容）

- **训练/评估用 JSONL**（必需）  
  - Debug：`data/processed/jsonl/phase1_binary_debug/train.jsonl`, `val.jsonl`, `test.jsonl`（约 50k / 5k / 5k 条）  
  - Full：`data/processed/jsonl/phase1_binary/train.jsonl`, `val.jsonl`, `test.jsonl`
- **代码**：整个仓库（或至少 `src/`、`configs/`、根目录下执行命令时的入口）。
- **模型**：若 HPC 可访问外网，训练时会从 HuggingFace 自动下载 `Qwen/Qwen2.5-1.5B-Instruct`；否则需在本地下载后拷到 HPC 并在 YAML 中把 `model_name` 改为该路径。

---

## 5. 环境与依赖（与本地一致为宜）

- **Python**：3.10+（与本地 `cavdefense` 环境一致即可）。
- **关键包**：`torch`（带 CUDA）、`transformers==5.2.0`、`peft`、`bitsandbytes`（若 HPC 有 A100 等，可用对应 CUDA 的 wheel）。
- **注意**：训练脚本里用的是 `eval_strategy`（Transformers 5.x 参数名），不是旧的 `evaluation_strategy`；保持 `transformers` 版本一致可避免参数/行为差异。

---

## 6. Prompt 与截断（必读）

- Prompt 在 `src/training/dataset_phase1.py` 中由 `build_phase1_prompt(example)` 生成，包含：系统说明 + 元数据 + 10 条历史消息（pos/spd/rssi 等，已做 2 位小数等精度控制）+ “Answer: BENIGN/ATTACK”。
- 若 `max_seq_len` 过小，tokenizer 会截断**前面**部分，模型看不到 “Answer:”，会续写消息内容而非输出标签；因此 **max_seq_len 必须 ≥ 约 1050**（实测约 1048），当前取 1536。
- 迁到 HPC 后，**先跑一次** `tokenizer_eval`，确认输出里 `Truncated: 0 / N`、`Max token count` 在 1536 以内，再跑长时间训练。

---

## 7. 本地资源与耗时（供 HPC 对比参考）

- **机器**：RTX 2080 Max-Q，8GB 显存。
- **显存**：训练时约 7070 MiB / 8192 MiB；不建议再增大 `max_seq_len` 或 batch。
- **训练**：313 个 iteration（0.05 epoch，steps_per_epoch≈6250）约 **1 小时**；每 step 约 12 s。
- **Eval**：全量 5000 条 val 约 45 分钟；改为 500 条子集（`max_eval_samples: 500`）后时间待用户反馈。训练中建议用 500 子集或 `evaluation_strategy: epoch` 控制 eval 频率；完整评估用 `eval_phase1_binary.py` 单独跑。

---

## 8. 希望在 HPC 上完成的事

1. **环境**：复现当前 Python 与依赖（含 transformers 5.x、bitsandbytes、peft）。
2. **数据与代码**：将 JSONL（至少 debug）与仓库拷到 HPC；若无法访问 HuggingFace，则提供本地模型路径并改 YAML。
3. **验证**：在 HPC 上跑 `tokenizer_eval` 与一次短训练（如 0.05 epoch），确认无截断、无 OOM、eval 正常。
4. **正式 baseline**：用 debug 或 full 配置跑完 0.3～1 epoch，得到第一版 finetuned baseline；训练结束后用 `eval_phase1_binary.py` 在 test 上评估 base vs LoRA。
5. **作业调度**：提供或调整 SLURM（或该 HPC 使用的调度器）作业脚本，指定 GPU 数量、显存、时长、日志路径等。

---

## 9. 数据上传与输出检查（Palmetto）

### 9.1 如何上传数据到 HPC

- **JSONL 建议放在 home**（`$HOME/veremi_agent/data/processed/jsonl/`），避免 scratch 30 天 purge。
- **从本机上传**（在**本机** PowerShell，仓库根目录或 `data/processed/jsonl` 的父目录执行）：
  ```powershell
  # 上传整个 phase1_binary_debug 目录（替换 haotias 为你的 Palmetto 用户名）
  scp -r data/processed/jsonl/phase1_binary_debug haotias@slogin.palmetto.clemson.edu:$HOME/veremi_agent/data/processed/jsonl/
  ```
  若 HPC 上还没有 `data/processed/jsonl`，先 SSH 登录后执行：
  ```bash
  mkdir -p $HOME/veremi_agent/data/processed/jsonl
  ```
  然后再从本机执行上面的 `scp`。
- **数据量很大时**：用 Globus 或连接 Palmetto 的 Data Transfer Node (DTN) 做 scp，不要用登录节点传大量数据；详见 [Palmetto Data Transfer](https://docs.rcd.clemson.edu/palmetto/transfer/overview/)。

### 9.2 如何检查输出文件夹

- **训练输出** 使用 HPC 配置时在 **scratch**，例如：`/scratch/<用户名>/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug`。
- 在 Palmetto 终端检查：
  ```bash
  ls -la /scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug/
  ls -la /scratch/$USER/veremi_agent/slurm/   # 作业日志
  ```
- 若目录不存在，说明训练尚未写入或作业未跑到 save；可先创建目录再提交作业：
  ```bash
  mkdir -p /scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug
  mkdir -p /scratch/$USER/veremi_agent/slurm
  ```
- **注意**：scratch 有 30 天未访问 purge 政策；重要 checkpoint 需定期拷回 home 或备份。

---

## 10. 相关文档

- 更完整的数据 pipeline、训练说明与截断排查过程：`doc/training_notes.md`
- 本文件：`doc/hpc_migration_context.md`（即本上下文）
- **HPC 专用配置**：`configs/qwen2.5_1.5b_phase1_binary_debug_hpc.yaml`（使用前将 `YOUR_USERNAME` 改为 Palmetto 用户名）。

新 chat 可基于以上内容讨论 HPC 环境、路径、资源配置和作业脚本，无需再从头梳理项目与配置。
