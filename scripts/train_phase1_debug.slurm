#!/bin/bash
#SBATCH --job-name=veremi-debug
#SBATCH --partition=work1
# 0.5 epoch 训练约 1 h，留 2 h 缓冲（含 eval、保存等）
#SBATCH --time=02:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=a100:1
#SBATCH --output=/scratch/%u/veremi_agent/slurm/%j.out
#SBATCH --error=/scratch/%u/veremi_agent/slurm/%j.err

# ~1 h run: 0.5 epoch on phase1_binary_debug (HPC config, weighted loss)
# Uses configs/qwen2.5_1.5b_phase1_binary_debug_hpc.yaml (replace YOUR_USERNAME in that file on HPC)

module load cuda/12.3.0
module load anaconda3/2023.09-0
source $(conda info --base)/etc/profile.d/conda.sh
source activate veremi

cd $HOME/veremi_agent
srun python -m src.training.train_lora_qwen_phase1_binary --config configs/qwen2.5_1.5b_phase1_binary_debug_hpc.yaml
