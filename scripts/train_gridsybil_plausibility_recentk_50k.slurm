#!/bin/bash
#SBATCH --job-name=veremi-gridsybil-plausibility-recentk-50k
#SBATCH --partition=work1
#SBATCH --time=07:00:00
#SBATCH --mem=8gb
#SBATCH --cpus-per-task=2
#SBATCH --gpus-per-node=a100:1
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/train_gridsybil_plausibility_recentk_50k/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/train_gridsybil_plausibility_recentk_50k/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_7b_gridsybil_plausibility_recentk_50k.yaml}"
OUTPUT_DIR="${OUTPUT_DIR:-}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$WORK_DIR"

export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=1

echo "WORK_DIR=$WORK_DIR"
echo "CONFIG=$CONFIG"
echo "OUTPUT_DIR=$OUTPUT_DIR"

if [[ -n "$OUTPUT_DIR" ]]; then
  srun python -m src.training.train_lora_qwen_gridsybil_plausibility \
    --config "$CONFIG" \
    --output_dir "$OUTPUT_DIR"
else
  srun python -m src.training.train_lora_qwen_gridsybil_plausibility \
    --config "$CONFIG"
fi
