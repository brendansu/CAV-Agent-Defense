# Phase 1 HPC 跑完后的下一步：看结果、跑 Eval、调参

在 HPC 上跑完 0.2 epoch 的 debug 训练（带 `answer_loss_weight_attack=3.0`）后，按下面步骤查看结果、跑 test 集 eval，并根据 F1(ATTACK) 决定是否调参或改 loss/数据。

---

## 1. 训练结果在哪里看

- **Slurm 日志**（stdout/stderr）  
  - 路径：`/scratch/$USER/veremi_agent/slurm/<JOBID>.out` 和 `.err`  
  - 内容：每 `logging_steps` 的 loss、每 `eval_steps` 的 eval_loss、learning_rate 等  
  - 查 JobID：`sacct -u $USER` 或提交时记下的编号

- **Trainer 状态与 checkpoint**  
  - 输出目录（HPC 配置里）：`/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug/`  
  - `trainer_state.json`：按 step 的 `log_history`（loss、eval_loss、lr）  
  - `checkpoint-500`、`checkpoint-1000`、…：按 `save_steps` 存的 checkpoint  
  - 训练结束时该目录下还有 adapter 权重与 tokenizer（与最后一个 checkpoint 一致），可直接用作「最终模型」做 eval

- **Eval 是在训练过程中的验证集**  
  - 配置里 `eval_strategy: steps`、`eval_steps: 500`、`max_eval_samples: 500`  
  - 所以 `trainer_state.json` 里的 `eval_loss` 是**验证集 500 条子集**上的 loss，不是 F1。  
  - **F1(ATTACK)** 需要单独跑 `eval_phase1_binary` 在 **test** 上算。

---

## 2. 在 HPC 上跑 Test 集 Eval（拿 F1）

用仓库里的 Slurm 脚本（需先 `cd $HOME/veremi_agent`）：

```bash
# 默认用「输出目录」作为 LoRA 目录（即最终 adapter）
sbatch scripts/eval_phase1_debug.slurm
```

脚本默认行为：

- `LORA_DIR=/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug`（与 HPC 训练配置的 `output_dir` 一致）
- `JSONL_DIR=data/processed/jsonl/phase1_binary_debug`
- `--split test`、`--mode both`（base + LoRA）、`--max_seq_len 1536`、`--max_samples 0`（全量 test）

若要评估**某个 checkpoint**（例如 1250 步）：

```bash
export LORA_DIR=/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug/checkpoint-1250
sbatch --export=LORA_DIR,ALL scripts/eval_phase1_debug.slurm
```

或先编辑 `scripts/eval_phase1_debug.slurm` 里的 `LORA_DIR` 再 `sbatch`。

Eval 结果在：

- `/scratch/$USER/veremi_agent/slurm/eval_<JOBID>.out`  
  - 会打印 **Accuracy** 和 **F1 (ATTACK)**（以及 base / LoRA 两套，若 `--mode both`）。

---

## 3. 如何根据 F1(ATTACK) 调参

- **F1(ATTACK) 仍接近 0（几乎全预测 BENIGN）**  
  - 先试：把 `answer_loss_weight_attack` 调到 **4.0～5.0**，再跑一版短训练（如 0.2 epoch）。  
  - 若仍无起色：加长训练（如 **0.3～0.5 epoch**），或同时提高 weight。  
  - 再考虑：对 ATTACK **过采样**（在 dataset 或 sampler 里），与当前 loss 做对比实验。

- **F1(ATTACK) 已明显 &gt; 0，但 precision 很低（误报多）**  
  - 适当**降低** `answer_loss_weight_attack`（如 2.0～2.5）。  
  - 或保持 weight，延长训练看曲线是否再平衡。

- **F1(ATTACK) 和 precision/recall 都还行**  
  - 可做**更长训练**（如 0.5～1.0 epoch）看是否再提升。  
  - 再考虑：prompt 强调「依据轨迹一致性」、加 error/residual 特征、或 CoT 等推理向改进（见 `training_notes.md` 与 transcript）。

---

## 4. 常用配置与脚本

- **HPC 训练配置**：`configs/qwen2.5_1.5b_phase1_binary_debug_hpc.yaml`  
  - 记得把 `output_dir` 里的 `YOUR_USERNAME` 改成你的 Palmetto 用户名。  
  - 调参时主要改：`answer_loss_weight_attack`、`num_train_epochs`。

- **训练**：`sbatch scripts/train_phase1_debug.slurm`（内部用上述 HPC YAML）

- **Eval**：`sbatch scripts/eval_phase1_debug.slurm`（可选 `LORA_DIR` 指向 checkpoint 或最终目录）

- **文档**：`doc/hpc_migration_context.md`、`doc/training_notes.md`、transcript `doc/transcripts/2026-03-04_phase1-hpc-debug-run-loss-design.md`。

---

## 5. 可选：把 checkpoint 拷回本机再 Eval

若想在本地快速看几条预测是否正确：

1. 从 HPC 把输出目录（或某个 checkpoint）拷到本机，例如：  
   `scp -r user@palmetto:/scratch/user/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug ./hpc_adapter`
2. 本地（仓库根目录）：  
   `python -m src.eval.eval_phase1_binary --jsonl_dir data/processed/jsonl/phase1_binary_debug --split test --mode lora --lora_dir ./hpc_adapter --max_seq_len 1536 --max_samples 50 --print_predictions 10`  
   即可看前几条的 true/pred 与 raw 生成。
