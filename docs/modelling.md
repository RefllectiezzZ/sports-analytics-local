# Modelling: football 1X2 baseline

This document describes the first production modelling pipeline for
`sports-analytics-local`.

## Status and limitations

**This is a team-level historical football full-match 1X2 baseline.**

It is **not** a betting recommendation engine. It does **not** yet use players,
injuries, or lineups. It does **not** use bookmaker odds as model features. It
does **not** produce expected value, stake sizing, or accumulators. Past
validation performance is **not** a guarantee of future performance.

Participant-scoped feature contracts are reserved for later work. Feature and
model manifests already record `feature_scope=team` so later participant-scoped
specifications can coexist without inventing player data that PR #6 does not
provide.

## Feature specification

Version: `football-1x2-prematch-features-v1`

Ordered model features (whitelist; never inferred from numeric columns):

1. `home_elo`
2. `away_elo`
3. `elo_diff`
4. `home_matches_played`
5. `away_matches_played`
6. `home_ppg_5`
7. `home_ppg_10`
8. `away_ppg_5`
9. `away_ppg_10`
10. `home_gf_pm_5`
11. `home_gf_pm_10`
12. `away_gf_pm_5`
13. `away_gf_pm_10`
14. `home_ga_pm_5`
15. `home_ga_pm_10`
16. `away_ga_pm_5`
17. `away_ga_pm_10`
18. `home_gd_pm_5`
19. `home_gd_pm_10`
20. `away_gd_pm_5`
21. `away_gd_pm_10`
22. `home_home_ppg_5`
23. `away_away_ppg_5`
24. `home_days_since_prev`
25. `away_days_since_prev`
26. `rest_day_diff`
27. `home_window5_count`
28. `home_window10_count`
29. `away_window5_count`
30. `away_window10_count`
31. `home_home_form_count`
32. `away_away_form_count`
33. `home_rest_available`
34. `away_rest_available`

Targets live in a separate `targets.parquet`. Odds, scores, and post-match
statistics are never model features.

### Feature cutoff and daily batching

For every event, the feature cutoff is that event's `event_date`.

Leakage-safe generation uses conservative calendar-date batching:

1. order finished canonical events by `event_date`, then `canonical_event_id`;
2. generate features for every event on a date from team state available
   **before** that date;
3. only after all features for the date are generated, update team state with
   that date's finished results.

Same-date matches cannot influence one another, even when kickoff times exist.
Changing a future event or result never alters an earlier feature row.

### Elo policy (`football-elo-v1`)

| Setting | Value |
| --- | --- |
| Initial rating | `1500.0` |
| K factor | `20.0` |
| Home advantage | `65.0` (added to home rating for expected score) |
| Season transition | carry forward without reset |

Cold starts use fixed neutral defaults plus explicit availability/count features.
Imputation values are never calculated from the complete dataset.

## Temporal validation

Validation is deterministic rolling-origin evaluation. Random train/test splits
and shuffled cross-validation are not used.

Each fold has three chronological regions aligned to calendar-date batches:

- training
- calibration
- test

The same calendar date never appears in more than one region. Folds fail when a
region is too small, training lacks all three outcomes, or chronology is
violated.

## Calibration

Deterministic multiclass temperature scaling:

1. fit multinomial logistic regression on the training region (scikit-learn);
2. compute raw logits on the calibration region;
3. choose one positive temperature by bounded deterministic search;
4. apply `softmax(logits / temperature)`;
5. evaluate once on the untouched test region.

Calibration is never fit on the test region. Calibration is only claimed to
improve the model when out-of-sample metrics show improvement.

## Metrics

Primary probability-quality metrics:

- multiclass log loss
- multiclass Brier score

Also reported: accuracy (secondary), class distribution, per-class recall,
expected calibration error with 10 equal-width confidence bins, and
calibration-bin tables. Metrics are recorded before and after temperature
scaling.

## Closing-market benchmark

Where a complete closing market-average 1X2 quote triple exists
(`provider_id=market-average`, `quote_phase=closing`):

1. convert decimal odds to inverse implied probabilities;
2. remove overround by normalizing the three probabilities;
3. evaluate on matching test events only.

This is an **external benchmark only**. Quotes are never used as model features,
never used for calibration, and Football-Data ingestion time is not quote time.
Benchmark coverage is reported because some events lack a complete triple.

## Model artifact format

Version: `football-1x2-logistic-v1`

Artifacts are explicit JSON parameters (`model.json`). Loading never executes
serialized Python. Pickle and joblib are rejected.

Persisted fields include:

- ordered feature names and outcome labels (`home`, `draw`, `away`);
- scaler means and scales;
- coefficients and intercepts;
- calibration temperature;
- model/feature specification versions;
- input snapshot identities;
- `trained_through_date` / `calibrated_through_date`;
- configuration, validation metrics, evaluation summary;
- SHA-256 checksum of the canonical JSON bytes.

Inference is pure NumPy from those parameters.

## Engine commands

```bash
python engine.py --build-football-1x2-features \
  --snapshot football-ingestion/football-canonical-v2/.../manifest.json \
  --snapshot football-ingestion/football-canonical-v2/.../manifest.json

python engine.py --train-football-1x2 \
  --features football/football-1x2-prematch-features-v1/<competition>/<artifact-id>

python engine.py --verify-model \
  football/football-1x2-logistic-v1/<competition>/<artifact-id>/model.json

python engine.py --infer-football-1x2 \
  --model football/football-1x2-logistic-v1/<competition>/<artifact-id>/model.json \
  --feature-row-json path/to/row.json
```

Shared modes (`--validate-config`, `--database-status`, `--migrate-database`)
remain available. Streamlit is not connected to training. `app.py` does not train
models.

## Input contract

Training inputs are explicit immutable football snapshot manifests. The engine
never silently trains from whichever snapshot happens to be latest. Mixed sports,
mixed competitions, incompatible schema versions, conflicting duplicate canonical
events, unresolved/source-scoped training rows, incomplete targets, and
insufficient chronological history are rejected.
