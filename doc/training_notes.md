## Phase 1 LLM Training Notes

### Data pipeline overview

- **Raw VeReMi tgz**: `data/raw/*.tgz`
- **Extraction**: `src/data/extract_runs.py`
  - Usage (from repo root, once):  
    `python src/data/extract_runs.py --raw_dir data/raw --out_dir data/extracted`
  - Output per run: `data/extracted/<run_id>/{sca,groundtruth,vehicles}`
- **Run manifest**: `src/data/build_manifest.py`
  - Usage:  
    `python src/data/build_manifest.py --extracted_dir data/extracted --write_csv`
  - Output:  
    - `data/processed/manifests/runs.parquet`  
    - `data/processed/manifests/runs.csv`  
    - `data/processed/manifests/runs_summary.json`
- **Per-run events (receiver, sender, message-level)**: `src/data/build_events.py`
  - Usage:  
    `python src/data/build_events.py --extracted_dir data/extracted --out_dir data/processed/events --only_missing`
  - Each run → `data/processed/events/<run_id>.parquet`
  - Columns include:
    - `run_id`, `receiver_id`, `sender_id`
    - `rcvTime`, `sendTime`, `messageID`, `RSSI`
    - `pos_x,pos_y` (claimed), `spd_x,spd_y` (claimed)
    - `ego_pos_x,ego_pos_y`, `ego_spd_x,ego_spd_y`
    - `snd_pos_x_true,snd_pos_y_true`, `snd_spd_x_true,snd_spd_y_true`
    - `err_pos,err_spd`
    - `attack_msg` (0/1, label from residual thresholds)
- **Train/val/test split (run-level)**: `src/data/build_split.py`
  - Usage:  
    `python src/data/build_split.py`
  - Strategy:
    - Per config `(attacker_type, start_h, attacker_prob)` (5 runs per config):
      - 4 runs → train
      - 1 run → pool
    - Pool (45 runs total) split into val/test:
      - Only stratified by `attacker_type` (hard-coded 4/5/4/5/4 for val vs 5/4/5/4/5 for test)
  - Output:  
    `data/processed/splits/split_v1.json` with `runs.train/val/test`.
- **JSONL generation (phase 1, per-message windows)**: `src/data/build_jsonl.py`
  - Phase 1 binary (single sender per window):
    - Windows are grouped by `(receiver_id, sender_id)`, sorted by `rcvTime`.
    - Fixed window length `K` (currently 10), sliding with `step=1`.
    - Each JSONL line is one window; label is the last message’s `attack_msg`.
  - Main usage (full dataset):  
    `python src/data/build_jsonl.py --phase phase1 --label_mode binary --K 10 --step 1`
  - Output:
    - `data/processed/jsonl/phase1_binary/train.jsonl`
    - `data/processed/jsonl/phase1_binary/val.jsonl`
    - `data/processed/jsonl/phase1_binary/test.jsonl`

### Debug JSONL subset

- To speed up local training, a smaller debug subset is generated from the full JSONL:
  - Script: `src/data/build_debug_sample.py`
  - Example usage:  
    `python src/data/build_debug_sample.py --source_dir data/processed/jsonl/phase1_binary --out_dir data/processed/jsonl/phase1_binary_debug --max_train 50000 --max_val 5000 --max_test 5000`
- Debug JSONL directory:
  - `data/processed/jsonl/phase1_binary_debug/`
    - `train.jsonl` (~50k lines, ~150MB)
    - `val.jsonl` (~5k lines, ~15MB)
    - `test.jsonl` (~5k lines, ~15MB)

### Dataset preparation for LLM (Phase 1, binary)

- Module: `src/training/dataset_phase1.py`
- `Phase1DatasetConfig`:
  - `jsonl_dir: Path` (e.g., `data/processed/jsonl/phase1_binary_debug`)
  - `max_seq_len: int` (default 512)
- `load_raw_phase1_splits(config)`:
  - Uses `datasets.load_dataset("json")` to read `train/val/test.jsonl`.
- `build_phase1_prompt(example)`:
  - Builds a text prompt from a single JSONL sample:
    - System-style description:
      - “You are an onboard CAV intrusion detection system...”
      - Clarifies that the target is to decide whether the **last** message is `BENIGN` or `ATTACK`.
    - Metadata: `phase`, `run_id`, `receiver_id`, `sender_id`, optional `attacker_type`.
    - History lines: from oldest to newest (`dt` increasing), each with:
      - `dt`, `sender`, `claimed_pos/spd`, `ego_pos/spd`, `rssi`.
    - Final line: `Answer with only one token: BENIGN or ATTACK.`  
      Followed by `Answer: <LABEL>`.
- `preprocess_example`:
  - Adds a `"text"` field containing the full prompt + label string.
- `tokenize_function`:
  - Takes a batch with `"text"` and applies tokenizer:
    - `encoded = tokenizer(examples["text"], truncation=True, max_length=max_seq_len)`
    - Sets `encoded["labels"] = encoded["input_ids"]` (full-sequence causal LM supervision).
- `load_phase1_binary_datasets(jsonl_dir, tokenizer, max_seq_len)`:
  - End-to-end:
    - Raw JSONL → text prompts → tokenized outputs (`input_ids`, `attention_mask`, `labels`).
  - Returns tokenized `train_ds`, `val_ds` for Trainer.

### Training script (Qwen2.5-1.5B + QLoRA, Phase 1 binary)

- Script: `src/training/train_lora_qwen_phase1_binary.py`
- Config (full): `configs/qwen2.5_1.5b_phase1_binary.yaml`
- Config (debug): `configs/qwen2.5_1.5b_phase1_binary_debug.yaml`
  - `model_name: Qwen/Qwen2.5-1.5B-Instruct`
  - `jsonl_dir: data/processed/jsonl/phase1_binary_debug`
  - `output_dir: outputs/qwen2.5-1.5b-phase1-binary-debug`
  - `max_seq_len: 1536` (see prompt-length debugging notes below)
  - `learning_rate: 1e-4`
  - `num_train_epochs: 1.0` (for debug, can be reduced, e.g. 0.1–0.2)
  - `per_device_train_batch_size: 1`
  - `per_device_eval_batch_size: 1`
  - `gradient_accumulation_steps: 8`
  - LoRA:
    - `lora_r: 16`
    - `lora_alpha: 32`
    - `lora_dropout: 0.05`
    - `target_modules: [q_proj, k_proj, v_proj, o_proj]`
- Model + LoRA setup:
  - `AutoTokenizer.from_pretrained(model_name)`
    - `pad_token` set to `eos_token` if missing.
  - `AutoModelForCausalLM.from_pretrained(...)` with 4-bit QLoRA:
    - `BitsAndBytesConfig(load_in_4bit=True, quant_type='nf4', compute_dtype=torch.float16, use_double_quant=True)`.
  - `LoraConfig(...)` applied via `get_peft_model(base_model, lora_config)`.
- Custom Trainer:
  - `CausalLMTrainer(Trainer)` overrides `compute_loss`:
    - Uses `inputs["labels"]` if present, otherwise falls back to `labels = input_ids.clone()`.
    - Calls `model(**inputs, labels=labels)` and returns `outputs.loss`.
- TrainingArguments (debug):
  - Uses `warmup_steps` instead of deprecated `warmup_ratio`:
    - Computes:
      - `steps_per_epoch = ceil(train_size / (per_device_train_batch_size * gradient_accumulation_steps))`
      - `total_training_steps = steps_per_epoch * num_train_epochs`
      - `warmup_steps = int(total_training_steps * warmup_ratio)`
    - Prints these estimates for visibility.
  - Key args:
    - `per_device_train_batch_size`, `per_device_eval_batch_size`
    - `gradient_accumulation_steps`
    - `num_train_epochs`
    - `learning_rate`
    - `lr_scheduler_type` (cosine)
    - `warmup_steps`
    - `logging_steps`, `save_total_limit`
    - `fp16=True`, `gradient_checkpointing=True`

### Prompt length / max_seq_len debugging (2026-03-03)

- **Symptom (evaluation looked like message continuation instead of prediction)**:
  - When evaluating on the debug JSONL (both base and LoRA models), the model outputs looked like:
    - `', claimed_pos=[3587.658'`, `'667871411327'`, `'s: sender=109, claimed_pos=[3'`, etc.
  - All predictions were parsed as `UNKNOWN`, so `Used samples (after skipping UNKNOWN/invalid)` was 0 and metrics were 0.
  - Hypothesis: the model was only seeing truncated message history and never the `Answer:` instruction, so it was just continuing the last line of the history instead of answering BENIGN/ATTACK.

- **Diagnosis 1: eval-side instrumentation**:
  - In `src/eval/eval_phase1_binary.py`, added:
    - Per-sample logging/printing of:
      - `prompt_tokens` (tokenized length of the prompt),
      - `truncated` flag (whether `prompt_tokens >= max_seq_len`),
      - `prompt_tail` (last ~500 chars of the full prompt).
  - With `max_seq_len=512`:
    - For the first few test samples: `prompt_tokens=512 (truncated=True, max_seq_len=512)`.
    - `prompt_tail` (raw string) contained:
      - `Answer with only one token: BENIGN or ATTACK.\nAnswer: <LABEL>`.
    - But the model-facing text (after tokenization and truncation) **did not** end with `Answer:`.
  - Conclusion:
    - `prompt_tail` is just for human inspection; tokenizer truncation keeps the **first** `max_seq_len` tokens.
    - With `max_seq_len=512`, the `Answer:` line is completely cut off in the model input, explaining why the model keeps emitting message fragments instead of labels.

- **Diagnosis 2: tokenizer-only checker (`tokenizer_eval.py`)**:
  - Added `src/training/tokenizer_eval.py`:
    - Reads the same YAML as training (`--config`).
    - Uses `Phase1DatasetConfig`, `load_raw_phase1_splits`, and `build_phase1_prompt` to build the exact training prompts.
    - Tokenizes prompts with `truncation=True, max_length=max_seq_len`.
    - For a small number of samples (default 8), prints:
      - Char length, token count, `truncated` flag.
      - Whether the raw `prompt` tail contains `"Answer:"`.
      - Whether the **decoded, truncated** text (what the model actually sees) ends with `"Answer:"`.
      - The tail of both raw prompt and decoded/truncated prompt.
  - Results:
    - With `max_seq_len=512`:
      - `Truncated: 8 / 8`, `Max token count: 512`.
      - Raw prompts all ended with `Answer: <LABEL>`, but decoded/truncated text did **not** have `Answer:` for any sample.
    - With `max_seq_len=1024`:
      - Still `Truncated: 8 / 8`, `Max token count: 1024`.
      - Decoded prompts still did not end with `Answer:`.
    - With `max_seq_len=1536`:
      - `Truncated: 0 / 8`, `Max token count: 1048`.
      - For all checked samples, the decoded/truncated prompt tails now **include** the `Answer:` line.
  - Conclusion:
    - Typical Phase 1 prompts with 10-message windows and full numeric precision are around ~1050 tokens.
    - `max_seq_len=1536` leaves comfortable headroom above the observed max (1048), and for the checked batch there is **no prompt truncation**.

- **Prompt numeric precision optimization (to help context efficiency)**:
  - The original prompt used raw floats like:
    - `claimed_pos=[3587.658379958466, 5834.075148962773]`
    - `rssi=1.701623176387148e-08`
  - This is quite token-inefficient and obscures the essential physics.
  - In `src/training/dataset_phase1.py`:
    - Introduced helpers `_fmt_num`, `_fmt_list`, `_fmt_rssi` to format:
      - `pos` / `ego_pos` (m) and `spd` / `ego_spd` (m/s) with **2 decimal places**:
        - e.g. `claimed_pos=[3587.66,5834.08]`, `ego_spd=[-0.03,32.41]`.
      - `rssi` with **2 significant figures** in either fixed or scientific notation:
        - e.g. `rssi=1.70e-08`.
      - `dt` with **2 decimal places** (`t-7.00s` instead of `t-7.00000058536898s`).
    - This reduces prompt length (fewer numeric tokens) while keeping physically meaningful resolution (cm-level for position, 0.01 m/s, 0.01 s).
  - The precision optimization plus increased `max_seq_len` help ensure that:
    - The full 10-message history window **and** the `Answer:` instruction fit into the model context.

- **Current status (after fixes)**:
  - Debug YAML now uses `max_seq_len: 1536`.
  - `tokenizer_eval.py` with this config:
    - Reports `Truncated: 0 / 8`, `Max token count: 1048`.
    - All decoded prompts (what the model sees) end with `"Answer: <LABEL>"`.
  - Both training and evaluation now:
    - Use the same prompt template with controlled numeric precision.
    - Use a sequence length that empirically avoids prompt truncation on debug data.
  - Based on this, Phase 1 debug training has been **restarted** with the updated `dataset_phase1.py` and `max_seq_len=1536`.

### Verified debug run status (RTX 2080 Max-Q, 8GB)

- Environment:
  - `torch` with CUDA
  - `transformers==5.2.0`
  - `bitsandbytes==0.49.2` (Windows wheel, working)
- Debug JSONL (`phase1_binary_debug`) with ~50k train / 5k val：
  - `train_lora_qwen_phase1_binary.py` with debug YAML successfully:
    - Loads Qwen2.5-1.5B weights (4-bit).
    - Builds LoRA adapter (~4.36M trainable params, ~0.49% of total).
    - Constructs datasets and prints:
      - `Train dataset size: 50000`
      - `Val dataset size:   5000`
      - `Estimated steps_per_epoch=6250, total_training_steps=6250, warmup_steps=187`
    - Starts training loop; observed logs:
      - Loss decreases early in training:
        - `loss≈2.06 → 1.47 → 1.21 → 1.18` as epoch progresses from ~0.008 to ~0.032.
      - Grad norms remain small (`~0.03–0.07`).
      - Learning rate smoothly ramps up from ~2.6e-05 to 1e-4 during warmup.
- Conclusion:
  - End-to-end pipeline from raw VeReMi → events → JSONL → prompts → tokenization → QLoRA training **is working** on the debug subset.
  - Next work can focus on:
    - Refining prompt design.
    - Adding evaluation metrics (message-level and event-level).
    - Scaling from debug JSONL to full `phase1_binary` JSONL and/or migrating to cluster (e.g., Palmetto).

### Class-imbalanced loss & HPC debug training (2026-03-04–03-06)

- **Objective**: move Phase 1 binary LoRA training from local 8GB GPU to Palmetto HPC, and make the loss more sensitive to rare ATTACK windows.
- **Loss design (class-imbalanced)**:
  - For **BENIGN** windows, keep standard causal LM loss on the **full sequence** (`labels = input_ids`).
  - For **ATTACK** windows, compute loss **only on the answer token** (`BENIGN` / `ATTACK`) and multiply it by `answer_loss_weight_attack` (currently `3.0`).
  - Implementation lives in:
    - `CausalLMTrainer.compute_loss` in `src/training/train_lora_qwen_phase1_binary.py`
    - `tokenize_function` / dataset pipeline in `src/training/dataset_phase1.py` (derives `is_attack` and length from `examples["output"]`).
- **HPC debug runs (phase1_binary_debug)**:
  - Config: `configs/qwen2.5_1.5b_phase1_binary_debug_hpc.yaml`
    - `num_train_epochs` tuned from `0.2` → `0.5` to match ~1 hour wall time on A100 with `per_device_train_batch_size=4`, `gradient_accumulation_steps=2`.
    - `max_eval_samples=1000` so each eval uses a deterministic prefix of the val set.
  - Training scripts:
    - `scripts/train_phase1_debug.slurm` (wall time currently `--time=02:00:00` for the 0.5 epoch run).
  - Status:
    - Two LoRA checkpoints have been fully trained on `phase1_binary_debug` (50k train / 5k val): **0.5 epoch** and **2.0 epoch** runs.
    - Slurm logs live under `slurm/` (e.g. `train_debug_ep2.0_*.out`), and Trainer state is in `/scratch/$USER/veremi_agent/outputs/qwen2.5-1.5b-phase1-binary-debug/`.
    - Loss curves look healthy for 0.5 epoch (loss steadily decreasing; no obvious overfitting), motivating the longer 1.5–2.0 epoch run.

### Evaluation pipeline on debug / full / 1-of-8 test

- **Eval entry point**: `src/eval/eval_phase1_binary.py`
  - Metrics: Accuracy + **F1(ATTACK)**, for both base model and LoRA (depending on `--mode`).
  - Uses the same prompt template and `max_seq_len=1536` as training.
- **Debug test eval (full debug JSONL)**:
  - Slurm script: `scripts/eval_phase1_debug.slurm`
    - Default `JSONL_DIR=data/processed/jsonl/phase1_binary_debug`
    - `--split test`, `--mode both`, `--max_seq_len 1536`, `--max_samples 0` (all debug test windows).
  - Used to sanity-check the effect of the new loss and additional epochs on the debug distribution.
- **Full / 1-of-8 Phase 1 test eval**:
  - Full test JSONL: `data/processed/jsonl/phase1_binary/test.jsonl` (~2.77M windows; too slow for frequent eval).
  - **1-of-8 test subset**:
    - Constructed as `data/processed/jsonl/phase1_binary_1of8/test.jsonl`, containing every 8th test window.
    - Eval scripts switch between **full** and **1-of-8** by changing only `JSONL_DIR` while keeping `--split test`.
  - Slurm eval scripts (1-of-8):
    - `scripts/eval_phase1_1of8_base.slurm`
    - `scripts/eval_phase1_1of8_lora_ep0_5.slurm`
    - `scripts/eval_phase1_1of8_lora_ep2.slurm`
  - Current status (2026-03-06):
    - **Base**, **0.5 epoch LoRA**, and **2.0 epoch LoRA** models are all being evaluated on the **full test set** (and/or 1-of-8 subset) to measure F1(ATTACK) and generalization beyond the debug test.
    - These results will drive the next decisions on:
      - whether to adjust `answer_loss_weight_attack`,
      - how many epochs to train on a fixed dataset size,
      - and whether to scale data beyond the 50k debug subset.

- **Additional HPC documentation**:
  - `doc/hpc_migration_context.md`: environment, data layout, and Slurm basics on Palmetto.
  - `doc/phase1_hpc_next_steps.md`: “after training” checklist (where to read logs, how to run eval, how to tune `answer_loss_weight_attack` based on F1(ATTACK)).

### Phase 1 stratified subsets & ≈100k training dataset (2026-03-06)

- **Goal**: move from the debug 50k train subset (first 50k lines) to a **more representative, stratified subset** of the full Phase 1 binary JSONL, while keeping training cost manageable.
- **Subset builder script**: `src/data/build_phase1_subset.py`
  - Purpose: stratified random sampling from large Phase 1 JSONL files.
  - **Grouping key**:
    - Each JSONL line is grouped by `(label, attacker_type_bucket)` where:
      - `label = output` (`"BENIGN"` / `"ATTACK"`).
      - `attacker_type_bucket` is derived from `input.attacker_type`:
        - `None` / missing → `"NONE"`,
        - integer → `"type_<int>"` (e.g. `"type_1"`),
        - other values → `str(raw_value)`.
    - This replaces the older per-pair grouping `(run_id, receiver_id, sender_id)` that made global fractions hard to control.
  - **Sampling rule**:
    - For each group `g` with size `n_g`, draw `k_g ≈ floor(n_g * keep_fraction)` indices uniformly at random.
    - Because groups are now large pools by `(label, attacker_type)`, `keep_fraction` directly controls the *global* subset size, while preserving label + attacker_type distribution.
  - **CLI (typical)**:
    - Train:
      ```bash
      python -m src.data.build_phase1_subset \
        --input  data/processed/jsonl/phase1_binary/train.jsonl \
        --output data/processed/jsonl/phase1_binary_100k/train.jsonl \
        --keep_fraction 0.00625 \
        --seed 42
      ```
    - Val:
      ```bash
      python -m src.data.build_phase1_subset \
        --input  data/processed/jsonl/phase1_binary/val.jsonl \
        --output data/processed/jsonl/phase1_binary_100k/val.jsonl \
        --keep_fraction 0.001 \
        --seed 42
      ```

- **Val subset (completed)**:
  - Source: full Phase 1 binary val (`~1.44M` windows).
  - Command: `keep_fraction=0.001` on `val.jsonl`.
  - Result:
    - `Total lines kept ≈ 1,438` (≈0.1% of all val windows).
    - Stratified across the 10 `(label, attacker_type)` groups.
    - Saved to: `data/processed/jsonl/phase1_binary_100k/val.jsonl`.
  - Intended use:
    - Quick Phase 1 experiments and HPC debug runs where full 1.44M val is too heavy.
    - Compatible with training-time eval (`max_eval_samples=1000`) which selects the first 1000 examples deterministically.

- **Train subset (~100k, in progress)**:
  - Source: full Phase 1 binary train (`~16M` windows, rough order-of-magnitude).
  - Target: **≈100k train windows**, as a “medium-scale” dataset between:
    - 50k debug subset (first 50k lines),
    - and the full 16M train set.
  - Strategy:
    - Choose `keep_fraction ≈ 100_000 / N_train_full` (empirically around `0.00625` for `N_train_full ≈ 16M`).
    - Run `build_phase1_subset.py` once on full train to produce:
      - `data/processed/jsonl/phase1_binary_100k/train.jsonl`
  - Experimental plan:
    - First, train on the 100k subset (0.5–1.0 epoch) and evaluate on:
      - debug test,
      - 1-of-8 test subset,
      - full test (as resources allow).
    - Compare F1(ATTACK) against:
      - the 50k-debug 0.5 epoch and 2.0 epoch LoRA runs,
      - and the base model.
    - If 100k clearly outperforms 50k, consider scaling to **200k** and later **500k** using the same stratified sampler; otherwise, treat 50k–100k as “data-saturated” for Phase 1 and focus on prompt / loss / model-side changes instead.

### Loss & eval curve visualization (planned)

- **Motivation**:
  - Current inspection of training relies on `trainer_state.json` and Slurm `.out` logs.
  - A dedicated plotting script will make it easier to compare:
    - 0.5 vs 2.0 epoch runs on debug,
    - 50k vs 100k vs larger subsets,
    - different `answer_loss_weight_attack` settings.
- **Planned approach**:
  - Parse `slurm/*.out` and/or `trainer_state.json` to extract:
    - step / epoch,
    - training `loss`,
    - `eval_loss` (on the 1000-example val subset),
    - learning rate.
  - Generate matplotlib (or similar) plots of:
    - loss vs. step/epoch,
    - eval_loss vs. step/epoch,
    - optionally annotate key checkpoints and selected eval runs.
- **Status (2026-03-06)**:
  - Plotting script is being prototyped in a separate notebook / chat, focusing first on parsing existing `slurm/train_debug_ep*.out` files.
  - Once stabilized, it should be referenced here with its file path (e.g. `src/analysis/plot_phase1_training_curves.py`) and a short “how to run” command.
