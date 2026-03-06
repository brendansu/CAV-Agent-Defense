#!/bin/bash
#SBATCH --job-name=veremi-eval-lora-05ep
#SBATCH --partition=work1
#SBATCH --time=08:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=a100:1
#SBATCH --array=0-7
#SBATCH --output=/scratch/%u/veremi_agent/slurm/eval_1of8_lora05ep_%A_%a.out
#SBATCH --error=/scratch/%u/veremi_agent/slurm/eval_1of8_lora05ep_%A_%a.err
#
# Evaluate Phase 1 binary (full) on TEST split using 0.5-epoch LoRA model.
# Defaults:
#   JSONL_DIR=$HOME/veremi_agent/data/processed/jsonl/phase1_binary
#   LORA_DIR=/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug

JSONL_DIR="${JSONL_DIR:-$HOME/veremi_agent/data/processed/jsonl/phase1_binary_1of8}"
LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug}"
NUM_SHARDS=8
SHARD_INDEX=${SLURM_ARRAY_TASK_ID}

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$HOME/veremi_agent"
echo "JSONL_DIR=$JSONL_DIR"
echo "LORA_DIR=$LORA_DIR"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SHARD_INDEX=$SHARD_INDEX"

srun python -m src.eval.eval_phase1_binary \
  --jsonl_dir "$JSONL_DIR" \
  --split test \
  --mode lora \
  --lora_dir "$LORA_DIR" \
  --max_seq_len 1536 \
  --max_samples 0 \
  --show_examples 0 \
  --num_shards "$NUM_SHARDS" \
  --shard_index "$SHARD_INDEX"

