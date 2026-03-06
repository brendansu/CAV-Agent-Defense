#!/bin/bash
#SBATCH --job-name=veremi-eval
#SBATCH --partition=work1
#SBATCH --time=01:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=a100:1
#SBATCH --output=/scratch/%u/veremi_agent/slurm/eval_%j.out
#SBATCH --error=/scratch/%u/veremi_agent/slurm/eval_%j.err
#
# Run Phase 1 binary eval (base + LoRA) on test split after training.
# Set LORA_DIR to your output_dir (e.g. .../checkpoint-1250 or .../qwen2.5-1.5b-phase1-binary-debug for final).
# Example: sbatch scripts/eval_phase1_debug.slurm
#          sbatch --export=LORA_DIR=/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug/checkpoint-1250 scripts/eval_phase1_debug.slurm

LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug}"
JSONL_DIR="${JSONL_DIR:-data/processed/jsonl/phase1_binary_debug}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source $(conda info --base)/etc/profile.d/conda.sh
source activate veremi

cd $HOME/veremi_agent
echo "LORA_DIR=$LORA_DIR"
echo "JSONL_DIR=$JSONL_DIR"

srun python -m src.eval.eval_phase1_binary \
  --jsonl_dir "$JSONL_DIR" \
  --split test \
  --mode both \
  --lora_dir "$LORA_DIR" \
  --max_seq_len 1536 \
  --max_samples 0
