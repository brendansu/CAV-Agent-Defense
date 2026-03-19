# GridSybil Episode Builder V2 Plan

Status: approved implementation checklist before Agent mode execution.

This document captures the agreed implementation scope for upgrading
`src/data/episode_builder.py` for the `GridSybil` / ghost-traffic line of work.
The goal is to improve episode-level embeddings, formalize the Phase 1 label
set, and add lightweight progress monitoring while keeping the scope focused.

---

## 1. Scope

Implement only the `GridSybil` episode-builder v2 upgrades in
`src/data/episode_builder.py`.

Do:

- strengthen `GridSybil`-specific embeddings
- formalize the approved Phase 1 label set
- add stdout progress monitoring
- retain source / traffic-regime metadata

Do not do:

- `DoSDisruptiveSybil` / `DataReplaySybil`-specific logic
- realtime JSON progress writing
- HPC refactor or job orchestration
- prompt-builder changes
- higher-level semantic labels such as `has_fake_congestion`

---

## 2. Design Principles

- Use only receiver-observable information for detection-facing embeddings.
- Keep any sender-derived information strictly on the label / analysis side.
- Make the builder more group-centric for `GridSybil` rather than
  pseudo-track-centric.
- Improve clustering quality by enforcing temporal overlap.
- Add heading information to embeddings, but do not use heading as a hard
  clustering criterion in v2.
- Keep thresholds configurable so later tuning does not require code changes.

---

## 3. Embedding Changes

## 3.1 Meta

Keep current metadata:

- `run_id`
- `receiver_id`
- `receiver_is_attacker`
- `attack_flag`
- time-window information

Add source/regime indicators:

- `attack_type`
- `traffic_regime`

Notes:

- `traffic_regime` should distinguish the two `GridSybil` folders for later
  analysis of peak vs off-peak behavior.
- The builder should remain regime-agnostic; the metadata is for downstream
  analysis, splitting, and interpretation.

## 3.2 Ego Embeddings

Keep:

- `num_updates`
- `start_pos`
- `end_pos`
- `displacement`
- `avg_speed`
- `speed_std`

Add:

- `avg_heading`
- `heading_std`

Purpose:

- support ego-relative group interpretation
- preserve directional context for later prompt design

## 3.3 Region Embeddings

Keep detection-facing:

- `num_bsms`
- `num_unique_pseudos`
- `avg_speed`
- `median_speed`
- `speed_std`
- `min_speed`
- `max_speed`

Add:

- `slow_msg_fraction`
- `medium_msg_fraction`
- `fast_msg_fraction`
- `single_message_pseudo_fraction`
- `short_lived_pseudo_fraction`

Suggested speed buckets:

- `slow`: speed < `2.0 m/s`
- `medium`: `2.0 <= speed < 8.0 m/s`
- `fast`: speed >= `8.0 m/s`

Important:

- `num_unique_senders` and `pseudo_sender_ratio` must not be treated as
  model-facing detection embeddings because they depend on VeReMi-only sender
  information.
- If retained at all, they should live in an analysis/debug-only subsection.

## 3.4 Pseudo-Track Embeddings

Keep:

- `num_msgs`
- `time_span`
- `duration`
- `msg_rate`
- `iat_mean`
- `iat_std`
- `avg_pos`
- `pos_spread`
- `avg_speed`
- `speed_std`
- `min_speed`
- `max_speed`
- `avg_acl`
- `acl_std`
- `heading_std`

Add:

- `avg_heading`

Purpose:

- keep `pseudo_tracks` as the fine-grained internal layer used by grouping and
  diagnostics

## 3.5 Group-Level Embeddings

Keep:

- `group_id`
- `pseudos`
- `size`
- `avg_pos`
- `avg_speed`
- `speed_cv`
- `total_msgs`

Add:

- `group_pos_spread`
- `group_radius`
- `group_density_proxy`
- `group_time_span`
- `group_duration`
- `group_lifetime_fraction`
- `avg_heading`
- `group_heading_std`
- `distance_to_ego`

Purpose:

- make groups interpretable as local ghost-traffic entities
- expose spatial compactness, temporal persistence, and ego-relative position

## 3.6 Low-Speed Group Summary

Add higher-level group/congestion summaries, either under `macro` or a new
summary subsection:

- `largest_low_speed_group_size`
- `largest_low_speed_group_msgs`
- `largest_low_speed_group_avg_speed`
- `fraction_msgs_in_low_speed_groups`
- `fraction_pseudos_in_low_speed_groups`

Suggested definition:

- a low-speed group is one whose `avg_speed < 2.0 m/s`

Purpose:

- make ghost-congestion semantics explicit

---

## 4. Clustering Changes

## 4.1 Current Problem

The current clustering rule only checks:

- spatial proximity
- average-speed similarity

This can incorrectly merge normal vehicles that traverse similar areas at
different times.

## 4.2 Required Clustering Rule in V2

Pairwise clustering should require all of:

- `pos_dist < pos_cluster_thr`
- `speed_diff < spd_cluster_thr`
- `temporal_iou > time_cluster_thr`

Do not add heading as a hard clustering criterion in this version.

## 4.3 Temporal IoU

`temporal_iou` should be promoted from a computed-but-unused similarity term to
an actual clustering criterion.

Meaning:

- it measures how much two pseudonyms are active during the same time interval
- it reduces false grouping of vehicles that only pass through similar
  trajectories sequentially

## 4.4 Threshold Exposure

Keep configurable:

- `--pos_cluster_thr`
- `--spd_cluster_thr`

Add:

- `--time_cluster_thr`

Goal:

- future threshold tuning should require command-line changes only

## 4.5 Heading Policy

Heading should be added to embeddings only:

- keep `avg_heading` / `heading_std` in `ego`, `pseudo_tracks`, and groups
- do not use `heading_diff` as a hard criterion in clustering v2

Reason:

- preserve directional semantics
- avoid over-fragmenting groups in the first upgraded version

---

## 5. Label Changes

Adopt the approved Phase 1 label set only.

Target schema:

```json
"label": {
  "contains_sybil_attacker": true,
  "num_attacker_vehicles": 3,
  "attacker_vehicle_ids": [10431, 10515, 10551],
  "num_sybil_pseudos": 13,
  "sybil_pseudo_fraction": 0.68
}
```

## 5.1 Main Label

- `contains_sybil_attacker`

Definition:

- `true` if at least one attacker-controlled pseudonym appears in the window
- `false` otherwise

Role:

- this is the primary binary training target for Phase 1

## 5.2 Auxiliary Episode Labels

- `num_attacker_vehicles`
- `attacker_vehicle_ids`
- `num_sybil_pseudos`
- `sybil_pseudo_fraction`

Notes:

- `attacker_vehicle_ids` is for analysis/debug/evaluation only
- label generation may rely on VeReMi GT and sender-side information
- label-side leakage is acceptable; embedding-side leakage is not

## 5.3 Out of Scope for This Phase

Do not implement yet:

- `has_fake_congestion`
- `attack_severity`
- group-level attacker labels
- low-speed attack-cluster labels

These can be added in a later phase after the upgraded group embeddings are
stable.

---

## 6. Progress Monitoring

Use stdout progress only.

Do not write realtime JSON progress files.

## 6.1 Printing Policy

Add run-time terminal output for:

- current attack type / source folder
- current run id
- processed receiver count vs total receivers in the run
- cumulative episode count
- cumulative positive episode count, if already available

## 6.2 Print Interval

Do not print every receiver.

Use interval-based printing, for example:

- every `25` or `50` receivers

Also print:

- at run start
- at run end
- at final job completion

## 6.3 Performance Constraint

Progress monitoring must remain lightweight:

- stdout only
- no per-episode side logs
- no realtime JSON state writes

---

## 7. Peak vs Off-Peak Consideration

The two `GridSybil` folders correspond to different traffic regimes
(high-traffic vs low-traffic simulation settings).

Implementation guidance:

- keep builder logic the same across both folders
- record regime/source information in `meta`
- leave regime-aware analysis and split strategy to downstream stages

Important downstream implication:

- later train/val/test analysis should explicitly compare peak vs off-peak
  distributions to avoid learning traffic-density shortcuts instead of attack
  structure

---

## 8. Runtime Expectations

Current dataset size:

- `GridSybil_0709`: 4064 receiver logs across 2 runs
- `GridSybil_1416`: 1689 receiver logs across 2 runs
- total: 5753 receiver logs across 4 runs

Practical expectation:

- current builder full run: roughly several hours on local hardware
- v2 builder will likely be moderately slower due to richer embeddings and
  temporal-overlap-aware clustering

If runtime becomes too long:

- keep local stdout monitoring
- consider moving full-data processing to HPC later
- use run-level parallelism first

HPC migration itself is out of scope for this document.

---

## 9. Validation Plan

After implementation:

## 9.1 Lint Check

- verify that `src/data/episode_builder.py` has no new diagnostics

## 9.2 Smoke Test

Run a small test such as:

- `GridSybil_0709`
- `--max_receivers 3`

Check that:

- JSONL output is produced successfully
- new embeddings appear in the expected sections
- the label schema matches the approved Phase 1 structure
- progress printing is informative but not too frequent

## 9.3 Manual Output Inspection

Inspect at least one episode and confirm:

- `meta` includes source/regime info
- `ego` includes heading embeddings
- `region` includes speed buckets and pseudo-churn summaries
- `pseudo_groups` include spatial, temporal, and ego-relative fields
- labels match the approved schema
- sender-derived leakage is absent from model-facing embeddings

---

## 10. Success Criteria

This v2 implementation is complete when:

1. `episode_builder.py` builds upgraded `GridSybil` episodes successfully
2. clustering uses `temporal_iou` as a thresholded criterion
3. heading is included in embeddings but not in clustering
4. `pseudo_tracks` is optional and disabled by default
5. label schema matches the approved Phase 1 plan
6. stdout progress is helpful and lightweight
7. a smoke test completes successfully and output structure looks correct

---

## 11. Explicit Non-Goals

Do not implement in this round:

- prompt generation
- prompt formatting for LLM training
- `DoSDisruptiveSybil` features
- `DataReplaySybil` features
- duplicate-message logic
- HPC-specific job wrappers
- realtime progress files
- higher-level semantic labels beyond the Phase 1 set

