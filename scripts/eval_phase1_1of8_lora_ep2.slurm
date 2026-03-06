#!/bin/bash
#SBATCH --job-name=veremi-eval-lora-2ep
#SBATCH --partition=work1
#SBATCH --time=36:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=a100:1
#SBATCH --output=/scratch/%u/veremi_agent/slurm/eval_1of8_lora2ep_%j.out
#SBATCH --error=/scratch/%u/veremi_agent/slurm/eval_1of8_lora2ep_%j.err
#
# Evaluate Phase 1 binary (1of8) on TEST split using 2.0-epoch LoRA model.
# Defaults:
#   JSONL_DIR=$HOME/veremi_agent/data/processed/jsonl/phase1_binary
#   LORA_DIR=/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug-long

JSONL_DIR="${JSONL_DIR:-$HOME/veremi_agent/data/processed/jsonl/phase1_binary_1of8}"
LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug-long}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$HOME/veremi_agent"
echo "JSONL_DIR=$JSONL_DIR"
echo "LORA_DIR=$LORA_DIR"

srun python -m src.eval.eval_phase1_binary \
  --jsonl_dir "$JSONL_DIR" \
  --split test \
  --mode lora \
  --lora_dir "$LORA_DIR" \
  --max_seq_len 1536 \
  --max_samples 0 \
  --show_examples 0

