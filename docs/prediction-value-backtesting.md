# Prediction, value, combinations, and backtesting

This layer is deterministic, immutable, and sport/market/provider neutral. The
first production adapter is football full-match 1X2 with the existing
`football-1x2-logistic-v1` model. Football-Data market-average closing prices are
used only as a clearly labeled historical benchmark.

## Prediction contract

`MarketPrediction` contains one complete probability distribution for one
canonical event market. Each `CanonicalSelectionIdentity` reuses every canonical
market dimension (sport, family, key, period, scope, participant, line, outcome)
and deliberately excludes source market and selection IDs.

Invariants:

- the declared ordered selection space contains exactly 2, 3, or 4 unique
  selections from one canonical market, and probability rows follow that order;
- probability keys are complete and probabilities are finite, in `[0, 1]`, and
  sum to one within `1e-9`;
- the model and feature artifact IDs, specification versions, SHA-256 checksums,
  feature row ID, input snapshot lineage, and training/calibration cutoffs are retained;
- calibration, model/feature artifact verification, sufficient-history, and
  data-quality flags are explicit;
- feature availability is no later than prediction time;
- prediction time is strictly before event start;
- training and calibration history end before the event date;
- `prediction_id` is derived from canonical content and lineage.

The football adapter additionally requires exact model-to-feature artifact ID,
checksum, specification, and ordered feature-name agreement before inference. It
rejects target/result/score/post-event feature names in the production input path.

## Complete-market value

Value is evaluated only after the quote covers exactly the prediction outcome
space. One `CompleteMarketQuote` cannot mix event, source, provider, phase, or
timestamp dimensions. Source identity (`source_name`) and price-provider identity
(`provider_type`, `provider_id`) stay separate.

For decimal odds \(o_i > 1\):

- raw implied probability: \(q_i = 1/o_i\)
- overround: \(\sum_i q_i - 1\)
- normalized implied probability: \(\bar q_i = q_i / \sum_j q_j\)
- edge: \(p_i - \bar q_i\)
- expected value per flat unit: \(p_i o_i - 1\)

Odds must be finite and are canonicalized to four decimal places.

`live-safe` evaluation requires an exact/minute provider quote timestamp and:

```text
max(prediction_time, quote_time) < event_start
```

Closing prices and observation-only timestamps are refused in live-safe mode.
`closing-line-historical-benchmark` accepts only closing prices and always emits
a warning that pre-kickoff availability is not claimed.

## Opportunities

Typed filters independently cover:

- sport, market, provider, and event-start range;
- minimum model probability, normalized edge, and EV;
- per-selection decimal odds;
- explicit inclusion of historical benchmarks.

Every evaluated row gets a persisted decision with the versioned filter identity,
eligibility, deterministic rejection codes, and optional accepted rank. Live
`decision_as_of` is `max(prediction time, exact quote time, source observation
time)`; closing benchmarks conservatively use event start. Filters support a
maximum accepted count and
versioned EV, model-probability, or edge ranking. Default order is EV descending,
model probability descending, canonical event ID, then canonical selection ID.

## Combinations

The shared contracts support sports, markets, and dates without sport-specific
fields. The deterministic v1 dependency classifier returns:

- `conflict` for duplicate selections or different outcomes of one event market;
- `unknown` for different markets on one event, missing dependency/participant
  metadata, shared dependency keys, or shared participants across events;
- `structurally_separate` only when complete metadata proves disjoint dependency
  keys and participants.

The automatic builder always rejects unknown dependencies, even if manual policy
allows them. This is a structural classifier, not a correlation estimate.

Manual validation and the bounded builder enforce:

- independent `selection_odds_range` for each leg;
- independent `combined_odds_range` for the product;
- allowed sports/markets, per-leg/total odds, joint-probability and accumulator-EV
  floors, multi-sport/date controls, event horizon, and bounded candidates/work/output;
- no duplicate/conflicting legs;
- common information timing across all legs:

```text
max(all prediction times, all quote times) < earliest event start
```

Historical closing-line opportunities are never accepted as production
combinations. Timestamped synthetic combinations are supported by backtesting,
but are not operational settlement. Each accepted combination stores leg count,
odds product, probability product, EV, earliest/latest starts, common decision
time, policy identity, dependency assessments, eligibility/reasons, and the
structural-independence approximation warning.

## Rolling-origin backtesting

Each fold has disjoint chronological train, calibration, and untouched test
windows. Models are fitted and calibrated within each fold. Training and
calibration cutoffs must precede the test window. A single content-addressed
strategy configuration is fixed across every fold.

Supported evidence modes:

1. `closing-line-historical-benchmark`: singles only; Football-Data closing
   market-average quotes; no availability or production execution claim.
2. `timestamped-synthetic`: singles and combinations with explicit provider
   quote timestamps and common pre-start timing.

Production closing-line accumulators are explicitly refused.

Settlement is pure flat-unit arithmetic:

- win: `odds - 1` profit;
- loss: `-1`;
- v1 rejects `push` and `void` for singles and combinations;
- a combination loses if any leg loses, otherwise every leg must win.

Metrics include candidate/rejection and accepted single/combination counts,
gross return, chronological cumulative P&L and drawdown, average model
probability/edge/EV, all/selected multiclass log loss and Brier score, and
sample-sized fold/sport/market/provider/EV-bucket slices where data is available.

## Artifacts

Backtests publish under `storage/exports/backtests/` as atomic typed directories.
The manifest declares every authoritative JSONL filename, schema, row count, and
SHA-256. Backtest layouts require:

- `manifest.json`
- `manifest_checksum.sha256`
- `predictions.jsonl`, `market_evaluations.jsonl`,
  `opportunity_decisions.jsonl`, `opportunities.jsonl`, `combinations.jsonl`,
  `rejections.jsonl`, `settlements.jsonl`, `fold_metrics.jsonl`, and
  `aggregate_metrics.jsonl`.

The loader rejects absolute/traversing paths, symlinks, missing or extra files,
malformed schemas, checksum mismatches, and artifact IDs that do not match
canonical dataset content. It also checks canonical JSONL ordering, duplicate
IDs, probability spaces, EV arithmetic, timing, lineage, and v1 settlement values.

## Engine commands

```bash
python engine.py --config config/settings.toml --backtest-football-1x2 \
  --features football/football-1x2-prematch-features-v1/<competition>/<artifact-id> \
  --checksum <feature-manifest-sha256> \
  --minimum-probability 0.40 \
  --minimum-edge 0.02 \
  --minimum-expected-value 0.01 \
  --selection-minimum-odds 1.20 \
  --selection-maximum-odds 10

python engine.py --config config/settings.toml --verify-backtest-artifact \
  backtests/football-1x2-closing-backtest-v1/<competition>/<backtest-id> \
  --artifact-schema football-1x2-closing-backtest-v1 \
  --checksum <manifest-sha256>

python engine.py --generate-predictions request.json
python engine.py --evaluate-opportunities request.json
python engine.py --build-combinations request.json
python engine.py --validate-combination request.json
python engine.py --run-backtest request.json
python engine.py --config config/settings.toml --verify-analysis-artifact \
  <explicit-relative-directory> --artifact-schema <schema>
python engine.py --config config/settings.toml --artifact-summary \
  <explicit-relative-directory> --artifact-type analysis --artifact-schema <schema>
```

The benchmark skips events without a precise scheduled start and reports quote
coverage. It does not scrape live prices, size stakes, write operational
settlements, estimate correlation, or create bookmaker recommendations.
