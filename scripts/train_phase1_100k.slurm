#!/bin/bash
#SBATCH --job-name=veremi-100k
#SBATCH --partition=work1
# 100k samples, 1 epoch => ~12500 steps; ~5–6 h on A100, leave buffer for eval/save
#SBATCH --time=08:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=a100:1
#SBATCH --output=/scratch/%u/veremi_agent/slurm/train_100k_%j.out
#SBATCH --error=/scratch/%u/veremi_agent/slurm/train_100k_%j.err
#
# Phase 1 binary: train LoRA on 100k stratified subset (train.jsonl + val.jsonl on scratch).
# Config: read from $HOME/veremi_agent_local/ so repo stays generic (no username in GitHub).
# Copy qwen2.5_1.5b_phase1_binary_100k_hpc.yaml there and set YOUR_USERNAME in that copy.

CONFIG="${CONFIG:-$HOME/veremi_agent_local/qwen2.5_1.5b_phase1_binary_100k_hpc.yaml}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$HOME/veremi_agent"
srun python -m src.training.train_lora_qwen_phase1_binary --config "$CONFIG"
