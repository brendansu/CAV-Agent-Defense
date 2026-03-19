#!/bin/bash
#SBATCH --job-name=episode-build
#SBATCH --partition=work1
#SBATCH --time=12:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err
#
# Build episode JSONL for a single VeReMi run directory.
#
# Usage examples:
#   sbatch --export=RUN_DIR=data/raw/GridSybil_0709/VeReMi_25200_28800_2025-11-15_13_57_9 scripts/build_episode_run.slurm
#   sbatch --export=RUN_DIR=data/raw/GridSybil_0709/VeReMi_28800_32400_2025-11-15_13_57_9,OUT_DIR=data/processed/episodes_gridsybil_v2 scripts/build_episode_run.slurm
#
# Optional exports:
#   WORK_DIR        default: $HOME/veremi_collusion
#   OUT_DIR         default: data/processed/episodes_gridsybil_v2
#   WINDOW_SEC      default: 10
#   STEP_SEC        default: 5
#   PRECISION       default: 2
#   POS_CLUSTER_THR default: 80
#   SPD_CLUSTER_THR default: 5
#   TIME_CLUSTER_THR default: 0.2
#   PROGRESS_EVERY  default: 50

set -euo pipefail

RUN_DIR="${RUN_DIR:-}"
WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
OUT_DIR="${OUT_DIR:-data/processed/episodes_gridsybil_v2}"
WINDOW_SEC="${WINDOW_SEC:-10}"
STEP_SEC="${STEP_SEC:-5}"
PRECISION="${PRECISION:-2}"
POS_CLUSTER_THR="${POS_CLUSTER_THR:-80}"
SPD_CLUSTER_THR="${SPD_CLUSTER_THR:-5}"
TIME_CLUSTER_THR="${TIME_CLUSTER_THR:-0.2}"
PROGRESS_EVERY="${PROGRESS_EVERY:-50}"

if [[ -z "$RUN_DIR" ]]; then
  echo "ERROR: RUN_DIR must be provided via sbatch --export=RUN_DIR=..." >&2
  exit 1
fi

module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

echo "which python: $(which python)"
echo "which python3: $(which python3)"

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "RUN_DIR=$RUN_DIR"
echo "OUT_DIR=$OUT_DIR"

srun python -m src.data.episode_builder \
  --run_dir "$RUN_DIR" \
  --out_dir "$OUT_DIR" \
  --window_sec "$WINDOW_SEC" \
  --step_sec "$STEP_SEC" \
  --precision "$PRECISION" \
  --pos_cluster_thr "$POS_CLUSTER_THR" \
  --spd_cluster_thr "$SPD_CLUSTER_THR" \
  --time_cluster_thr "$TIME_CLUSTER_THR" \
  --progress_every "$PROGRESS_EVERY"
