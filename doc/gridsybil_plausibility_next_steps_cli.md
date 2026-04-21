# GridSybil Plausibility Next Steps CLI

This note provides runnable command templates for:
- row-log aggregation and bucketed error analysis
- phase-1 sender recent-K feature rollout
- FN/recall-focused validation

## 1) Row-Log Aggregation (MVP)

```bash
python -m src.eval.aggregate_gridsybil_plausibility_rows \
  --input_glob "/scratch/$USER/veremi_collusion/outputs/eval_logs/plausibility_100k/run_${RUN_ID}_model_lora_split_${SPLIT}_shard*.jsonl.gz" \
  --run_id "$RUN_ID" \
  --model_tag lora \
  --split "$SPLIT" \
  --bucket_preset old_eval_actual \
  --merged_out_parquet "/scratch/$USER/veremi_collusion/outputs/eval_integration_plausibility/rows_${RUN_ID}_lora_${SPLIT}.parquet" \
  --overall_out_json "/scratch/$USER/veremi_collusion/outputs/eval_integration_plausibility/overall_rows_${RUN_ID}_lora_${SPLIT}.json" \
  --bucket_out_csv "/scratch/$USER/veremi_collusion/outputs/eval_integration_plausibility/buckets_${RUN_ID}_lora_${SPLIT}.csv" \
  --metrics_json_glob "/scratch/$USER/veremi_collusion/slurm/eval_plausibility_lora_100k/log_*.out"
```

`old_eval_actual` preset targets D+E+F features actually used by the old eval setup.
For continuous features, buckets use `null`, `0`, `1`, plus middle ranges.

## 2) Build Features with Sender Recent-K

```bash
python -m src.data.build_gridsybil_plausibility \
  --input-glob "data/raw/**/traceJSON-*.json" \
  --output-dir "data/processed/plausibility_messages_senderk" \
  --meta-dir "data/processed/plausibility_meta_senderk" \
  --format parquet \
  --context-window-sec 1.0 \
  --sender-recent-k 8 \
  --sender-recent-window-sec 5.0 \
  --workers 8
```

Then rebuild split parquet from the new features (same split spec as baseline):

```bash
python -m src.data.build_gridsybil_plausibility_split \
  --input-parquet "data/processed/plausibility_messages_senderk/message_features.parquet" \
  --raw-glob "data/raw/**/traceJSON-*.json" \
  --split-json "data/splits/gridsybil_split_v1.json" \
  --output-dir "data/processed/plausibility_messages_senderk_split" \
  --seed 42
```

Optional 100k/50k resampling:

```bash
python -m src.data.sample_plausibility_parquet_splits \
  --in_dir "data/processed/plausibility_messages_senderk_split" \
  --out_dir "data/processed/plausibility_messages_senderk_split_sampled_100k_5k_100k" \
  --train_rows 100000 \
  --val_rows 5000 \
  --test_rows 100000 \
  --seed 42 \
  --stratify_label \
  --label_col label
```

## 3) Token Budget Probe (Before Train)

```bash
python -m src.training.token_length_probe_gridsybil_plausibility \
  --config "configs/qwen2.5_7b_gridsybil_plausibility.yaml" \
  --split all \
  --max_samples 5000 \
  --include_output
```

## 4) Train and Eval (Phase-1)

Train:

```bash
python -m src.training.train_lora_qwen_gridsybil_plausibility \
  --config "configs/qwen2.5_7b_gridsybil_plausibility.yaml"
```

Shard eval:

```bash
python -m src.eval.eval_gridsybil_plausibility \
  --config "configs/qwen2.5_7b_gridsybil_plausibility.yaml" \
  --mode lora \
  --lora_dir "$LORA_DIR" \
  --split test \
  --num_shards 8 \
  --shard_index "$SHARD_INDEX" \
  --run_id "$RUN_ID" \
  --log_row_path "/scratch/$USER/veremi_collusion/outputs/eval_logs/plausibility_100k/run_{run_id}_model_{model_tag}_split_{split}_shard{shard_index}.jsonl.gz" \
  --print_predictions 0 \
  --show_examples 0
```

## 5) FN/Recall-Focused Validation

1. Run the same row aggregation command for baseline and sender-K runs.
2. Compare `overall_rows_*.json`:
   - `recall` should improve
   - precision drop should stay bounded (for example <= 0.02)
3. Compare bucket rows in `buckets_*.csv`:
   - `bucket_key=error_type` and `bucket_value=fn`
   - `bucket_key=run_stem`
   - `bucket_key=msg_dt_prev_same_pseudo` (check low-history bins)
   - `bucket_key=msg_catch_mgtd` / `msg_catch_mgts` / `msg_catch_phc` (check low-score bins)
4. Confirm `metrics_alignment.ok=true` so row-level and METRICS_JSON-level summaries are consistent.

## 6) Phase-2 Enhanced Direction

If phase-1 improves FN/recall:
- increase `--sender-recent-window-sec` (for example 8-12s)
- try larger `--sender-recent-k` (for example 12 or 16)
- keep summary-style features first, avoid episode-style long prompt input until summary features saturate
