#!/bin/bash
# Submit a single (model, attack, seed) train->eval pair from the seed sweep.
#
# Useful for retrying just the specific combo(s) that failed (OOM, preemption, ...)
# without resubmitting the other, already-successful attacks for that model+seed the
# way scripts/seed_sweep/submit_<model>_seed<N>.slurm would.
#
# Usage:
#   ./scripts/retry_seed_sweep_combo.sh <model> <attack> <seed>
#     model:  qwen | llama | mistral | gemma
#     attack: gridsybil | datareplay | dosdisruptive
#     seed:   any seed you've sampled+uploaded data for
#
# Run from the repo root on the HPC login node (same environment as the
# scripts/seed_sweep/*.slurm submitters -- WORK_DIR/CONFIG_ROOT default the same way).

set -euo pipefail

MODEL="${1:?Usage: $0 <model> <attack> <seed>}"
ATTACK="${2:?Usage: $0 <model> <attack> <seed>}"
SEED="${3:?Usage: $0 <model> <attack> <seed>}"

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG_ROOT="${CONFIG_ROOT:-$WORK_DIR/configs/llm_foundation}"
SPLIT="${SPLIT:-test}"
MODE="${MODE:-lora}"
MAX_SAMPLES="${MAX_SAMPLES:-0}"
NUM_SHARDS="${NUM_SHARDS:-8}"

case "$MODEL" in
  qwen)    LABEL="qwen2.5-7b";  CFG_PREFIX="qwen2.5_7b";  IS_GEMMA=0 ;;
  llama)   LABEL="llama3.1-8b"; CFG_PREFIX="llama3.1_8b"; IS_GEMMA=0 ;;
  mistral) LABEL="mistral-7b";  CFG_PREFIX="mistral_7b";  IS_GEMMA=0 ;;
  gemma)   LABEL="gemma2-9b";   CFG_PREFIX="gemma2_9b";   IS_GEMMA=1 ;;
  *) echo "[ERROR] unknown model: $MODEL (expected qwen|llama|mistral|gemma)" >&2; exit 2 ;;
esac

case "$ATTACK" in
  gridsybil)
    CFG_SUFFIX="gridsybil_plausibility_recentk_autoq_t1.yaml"
    TRAIN_SCRIPT_SHARED="scripts/train_gridsybil_plausibility_recentk_autoq_t1.slurm"
    TRAIN_SCRIPT_GEMMA="scripts/train_gridsybil_plausibility_recentk_autoq_t1_gemma.slurm"
    EVAL_SCRIPT="scripts/eval_gridsybil_plausibility_recentk_autoq_t1.slurm"
    EXP_SUFFIX="gridsybil_recentk_autoq_t1"
    OUT_SUFFIX="gridsybil-plausibility-recentk-autoq-t1"
    PARQUET_DIR="/scratch/$USER/veremi_collusion/data/plausibility_messages_senderk_autoq_mid_v2_split_sampled_50k_20k_50k_seed${SEED}"
    ;;
  datareplay)
    CFG_SUFFIX="datareplay_plausibility_recentk_autoq.yaml"
    TRAIN_SCRIPT_SHARED="scripts/train_datareplay_plausibility_recentk_autoq_t1.slurm"
    TRAIN_SCRIPT_GEMMA="scripts/train_datareplay_plausibility_recentk_autoq_t1_gemma.slurm"
    EVAL_SCRIPT="scripts/eval_datareplay_plausibility_recentk_autoq_t1.slurm"
    EXP_SUFFIX="datareplay_recentk_autoq"
    OUT_SUFFIX="datareplay-plausibility-recentk-autoq"
    PARQUET_DIR="/scratch/$USER/veremi_collusion/data/datareplay_messages_senderk_autoq_t1_split_sampled_50k_20k_50k_seed${SEED}"
    ;;
  dosdisruptive)
    CFG_SUFFIX="dosdisruptive_plausibility_recentk_autoq_classic.yaml"
    TRAIN_SCRIPT_SHARED="scripts/train_dosdisruptive_plausibility_recentk_autoq_classic.slurm"
    TRAIN_SCRIPT_GEMMA="scripts/train_dosdisruptive_plausibility_recentk_autoq_classic_gemma.slurm"
    EVAL_SCRIPT="scripts/eval_dosdisruptive_plausibility_recentk_autoq_classic.slurm"
    EXP_SUFFIX="dosdisruptive_recentk_autoq_classic"
    OUT_SUFFIX="dosdisruptive-plausibility-recentk-autoq-classic"
    PARQUET_DIR="/scratch/$USER/veremi_collusion/data/dosdisruptive_senderk_autoq_classic_split_sampled_50k_20k_50k_seed${SEED}"
    ;;
  *) echo "[ERROR] unknown attack: $ATTACK (expected gridsybil|datareplay|dosdisruptive)" >&2; exit 2 ;;
esac

if [[ "$IS_GEMMA" -eq 1 ]]; then
  TRAIN_SCRIPT="$TRAIN_SCRIPT_GEMMA"
else
  TRAIN_SCRIPT="$TRAIN_SCRIPT_SHARED"
fi

CONFIG_NAME="${CFG_PREFIX}_${CFG_SUFFIX}"
CONFIG_PATH="$CONFIG_ROOT/$CONFIG_NAME"
OUTPUT_DIR="/scratch/$USER/veremi_collusion/outputs/foundation/${LABEL}-${OUT_SUFFIX}-seed${SEED}"
EXP_NAME="foundation_${CFG_PREFIX}_${EXP_SUFFIX}_seed${SEED}"
DATE_TAG="$(date +%Y%m%d_%H%M%S)"
RUN_ID="${DATE_TAG}_${EXP_NAME}"

for f in "$CONFIG_PATH" "$TRAIN_SCRIPT" "$EVAL_SCRIPT"; do
  if [[ ! -f "$f" ]]; then
    echo "[ERROR] Missing: $f" >&2
    exit 2
  fi
done

cd "$WORK_DIR"

echo "[retry] model=$LABEL attack=$ATTACK seed=$SEED"
echo "        config=$CONFIG_PATH"
echo "        train_script=$TRAIN_SCRIPT"
echo "        parquet_dir=$PARQUET_DIR"
echo "        output_dir=$OUTPUT_DIR"

train_submit=$(sbatch \
  --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$CONFIG_PATH",OUTPUT_DIR="$OUTPUT_DIR",PARQUET_DIR="$PARQUET_DIR" \
  "$TRAIN_SCRIPT")
train_job="$(echo "$train_submit" | awk '{print $4}')"
echo "        train_job=$train_job"

eval_submit=$(sbatch \
  --dependency=afterok:${train_job} \
  --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$CONFIG_PATH",MODE="$MODE",LORA_DIR="$OUTPUT_DIR",SPLIT="$SPLIT",EXP_NAME="$EXP_NAME",RUN_ID="$RUN_ID",MAX_SAMPLES="$MAX_SAMPLES",NUM_SHARDS="$NUM_SHARDS",PARQUET_DIR="$PARQUET_DIR" \
  "$EVAL_SCRIPT")
eval_job="$(echo "$eval_submit" | awk '{print $4}')"
echo "        eval_job=$eval_job depends_on=$train_job"
echo "        run_id=$RUN_ID"
