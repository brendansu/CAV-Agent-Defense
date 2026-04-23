# GridSybil Plausibility Recent-K Progress (2026-04)

This note records the recent-K feature rollout for the GridSybil message-level plausibility task, the resulting train/eval observations, and the current plan for re-validating the pipeline under a stricter split protocol.

## 1. Scope and Current Task

Current main task:

- **Per-message** attack/benign classification for **GridSybil plausibility**
- Goal: improve **recall / reduce FN** without clearly harming precision

This note is about the **sender recent-K / recent-window summary** extension added on top of the message-level plausibility pipeline. It is separate from the earlier **per-episode attacker pseudo local ID** line.

## 2. Recent-K Design Summary

Recent-K was introduced to let the model use short sender-local history without moving fully to an episode-level prompt.

Implemented design:

- baseline sender-history window: **5s**
- sender recent-K cap: **8**
- grouping based on sender / pseudo history
- generated feature prefix: **`ctx_recentk_*`**

Core source signals summarized over recent sender history:

- `msg_catch_mgtsv`
- `msg_catch_int_min_neighbor`
- `ctx_triplet_ratio`
- `ctx_speed_diff_mean`
- `ctx_head_diff_mean_deg`

Summary types included:

- `last`
- `mean`
- `min`
- threshold count features
- threshold fraction features
- support features such as history count / span

Important prompt/config alignment that was completed:

- prompt variant: **`traffic_neutral_recentk`**
- recent-K fields switched to actual `ctx_recentk_*` names
- recent-K prompt grouping split into six smaller sections instead of one large block

Main related files:

- `src/data/build_gridsybil_plausibility.py`
- `src/training/gridsybil_plausibility_utils.py`
- `configs/qwen2.5_7b_gridsybil_plausibility_recentk_50k.yaml`

## 3. Feature Engineering Rationale

The recent-K design was **not** produced by simply finding buckets with weak performance and directly creating a feature for "being in that bad bucket."

The main logic was:

- examine where raw plausibility scores show strong **attack vs benign separation**
- then summarize whether a sender's recent history repeatedly enters those discriminative score regimes

Example pattern:

- if a low range of some plausibility score is strongly attack-heavy, then recent-window summaries can ask whether the same pseudo has repeatedly visited that low-score region

This is a more meaningful use of history than simply encoding bucket membership from weakly separable regions.

That said, threshold choice and bucket intuition were still influenced by observed held-out distributions, so later final evaluation still needs a refreshed untouched test protocol.

## 4. Prompt Length and Training Setup

Token probe on the recent-K prompt showed:

- max prompt length roughly **2272-2274 tokens**

Based on that, the recent-K training config used:

- `max_seq_len = 2560`
- `reserve_answer_tokens = 16`
- `simulate_budget_cutoff = true`

The recent-K 50k config points at the sender-K sampled parquet and uses the dedicated recent-K prompt variant.

## 5. Training and Evaluation Timeline

### 5.1 Base-model behavior

When the **base model** was evaluated with the newer recent-K feature set, performance was much weaker than the older feature-set baseline. In one early comparison, base performance was approximately:

- accuracy around **0.52**
- precision around **0.95**
- recall around **0.23**
- F1 around **0.37**

Interpretation:

- the base model became extremely conservative
- it predicted attack only rarely, giving high precision but very low recall
- this made it unsuitable as the main signal for judging recent-K value

### 5.2 LoRA training observations

Recent-K LoRA training on the 50k sampled pipeline showed much stronger optimization behavior than an earlier comparable run on the same data scale.

Example comparison around epoch `0.096`:

- newer recent-K run: train loss about **0.262**
- earlier run: train loss about **0.832**

The newer run therefore looked much more promising even before full evaluation.

There was also a noticeable throughput cost from the longer recent-K prompt:

- single-GPU run time was much slower than shorter-prompt earlier runs
- this motivated exploration of 4-GPU smoke tests and distributed launch stabilization

### 5.3 4-GPU smoke / DDP troubleshooting

An earlier 4-GPU plausibility run failed almost immediately during the first backward pass with:

- `RuntimeError: Resource temporarily unavailable`

Observed context:

- kernel warning on **4.18.0**
- failure at initial distributed backward rather than CUDA OOM
- likely interaction among launch topology, NCCL / system resource limits, and environment stability

To isolate this, several 4-GPU smoke scripts were created that only varied launch/runtime settings:

- baseline
- strict topology
- NCCL IB disabled
- NCCL IB + P2P disabled
- NCCL IB + P2P + SHM disabled

### 5.4 LoRA eval tokenizer issue

The LoRA evaluation script originally selected tokenizer source using:

- `lora_dir` when `mode in ("lora", "both")`
- `model_name` otherwise

This caused failures when evaluating adapter-only checkpoints that contained no tokenizer assets. A temporary workaround was to copy tokenizer files into the checkpoint directory.

Later inspection showed that the correct long-term behavior should be:

- tokenizer from the **official base model**
- adapter weights from the **LoRA checkpoint**

This matters both for correctness and for prompt-length comparability.

## 6. Current Recent-K Results

### 6.1 400-step checkpoint

Integrated 8-shard test result for the recent-K LoRA **400-step checkpoint**:

- rows evaluated: **50,000**
- parse-ok rate: **1.000**
- prompt-too-long rate: **0.000**
- TP = **30,362**
- FP = **214**
- FN = **444**
- TN = **18,980**
- accuracy = **0.98684**
- precision = **0.99300**
- recall = **0.98559**
- F1 = **0.98928**

Source:

- `outputs/eval_integration_plausibility/overall_rows_50k_recentk_400pt_20260421.json`

### 6.2 1-epoch checkpoint

The later **1 epoch** checkpoint also reached very strong results, with the four main metrics reported above **0.98** as well.

At this stage, the working conclusion is:

- recent-K + LoRA is highly effective on the current split
- the strongest observed behavior is not just high precision, but simultaneously high recall and low FN

## 7. Evaluation-Protocol Concern: Test-Driven Development

After obtaining strong results, an important methodological concern was identified.

Although recent-K features were motivated by score-separation structure rather than naive "bad bucket" patching, the development process still used information from the old held-out test distribution to guide thresholded summary design.

Therefore:

- this is **not** the most severe form of label leakage
- but it **is** a meaningful form of **test-set contamination / evaluation pollution**
- current 0.95+ / 0.98+ results should be treated as **promising exploratory results**, not as the final unbiased estimate

Current interpretation:

- the feature list likely captures something real and useful
- but it still needs confirmation on a refreshed, untouched split protocol

## 8. Data-Split Constraints and Better Protocol

The VeReMi data available here comes from a single 2-hour run. Because vehicle lifetimes are only a few minutes, the strongest practical isolation is time-based splitting rather than random message-level splitting.

Recommended next protocol:

- **train**: first 50 minutes
- **dev/val**: middle 20 minutes
- **test**: last 50 minutes

This is better than the current setup because it:

- preserves temporal causality more naturally
- reduces cross-split contamination for sender-history features
- gives a dedicated dev split for error analysis and feature iteration
- restores the test split to a true final-check role

## 9. Recommended Next Steps

### Phase A: confirm current feature list on a refreshed split

Freeze the current recent-K feature list and do **not** further tune it on the old test.

Then:

1. rebuild a new time-isolated `train/dev/test` split
2. treat the old test as a historical **analysis/dev-test** only
3. rerun the pipeline with the **current fixed feature list**
4. compare:
   - baseline old-feature setup
   - base recent-K setup
   - LoRA recent-K setup

Main question for this phase:

- does the current recent-K feature list still give strong recall/FN gains on a fresh holdout?

### Phase B: normalize and partially automate feature design

If Phase A confirms the current list generalizes, then move to a more standardized pipeline:

- define thresholds from train-only statistics
- use dev only for bucket analysis and feature iteration
- reduce hand-crafted threshold selection via quantile / percentile templates
- keep final test untouched until the very end

This should reduce over-crafting risk while preserving the core recent-window idea.

## 10. Broader Research Direction

If the refreshed split still confirms strong recent-K gains, the next extension paths are:

- continue strengthening **traffic sybil** under a stricter benchmark
- then consider extending to additional attack types such as:
  - `data replay`
  - `dos disruptive`

Among these, **data replay** may be the more natural next task because history-aware summary features are likely to transfer more directly than they would for all disruptive DoS behaviors.

## 11. Current Bottom Line

As of 2026-04-21:

- recent-K plausibility features have been fully integrated into the data / prompt / training pipeline
- LoRA results on the current split are extremely strong, with the 400-step checkpoint already reaching roughly **0.987 accuracy / 0.993 precision / 0.986 recall / 0.989 F1**
- however, because the feature design loop was partially informed by old test-distribution observations, the next essential step is **not more test-driven tuning**, but a **fresh time-isolated revalidation cycle**

This makes the current result best understood as:

- a strong positive research signal
- likely real progress
- still needing one cleaner validation pass before being treated as a final benchmark claim
