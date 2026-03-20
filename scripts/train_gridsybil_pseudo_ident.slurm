#!/bin/bash
#SBATCH --job-name=veremi-gridsybil-pseudo-ident-4gpu
#SBATCH --partition=work1
#SBATCH --time=24:00:00
#SBATCH --mem=160gb
#SBATCH --cpus-per-task=32

# 如果你们集群要求写成 a100:4 这种形式，把下面这一行替换为：
# #SBATCH --gpus-per-node=a100:4
#SBATCH --gpus-per-node=a100:4

#SBATCH --output=/scratch/%u/veremi_collusion/slurm/train_gridsybil_%x-%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/train_gridsybil_%x-%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_1.5b_gridsybil_pseudo_ident_hpc.yaml}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$WORK_DIR"

export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=1

echo "WORK_DIR=$WORK_DIR"
echo "CONFIG=$CONFIG"

srun torchrun --standalone --nproc_per_node=4 \
  -m src.training.train_lora_qwen_gridsybil_pseudo_ident \
  --config "$CONFIG"