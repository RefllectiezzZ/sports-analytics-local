# Football score-model tournament

The tournament is deterministic and chronological.

## Evaluation provenance and eligibility

Artifacts declare `synthetic-contract`, `small-fixture-contract`, or
`verified-historical`. Contract fixtures validate chronology, fitting,
persistence, and product wiring; their selected candidate is a provisional
winner, never a production champion.

Production eligibility requires conservative typed evidence gates: at least
1,000 completed matches, 300 per competition, three rolling folds,
500/100/100 training/calibration/test rows per fold, two years of history, 95%
prediction coverage, no more than 1% missing targets, no more than 5% unseen
team fallback, and convergence. Failure is persisted as
`insufficient-real-evaluation-data`.

The PR #13 consolidation audit ingested and strictly reloaded three completed
seasons for each configured competition through the fixed allowlisted
Football-Data workflow. The real tournaments therefore use verified historical
evidence rather than contract fixtures. Production eligibility remains distinct
from promotion: the English competition clears the evidence-volume gates, the
Portuguese competition remains below the 1,000-match total gate, and neither
result silently changes the champion. Historical closing-price benchmarks are
research diagnostics, not executable strategy evidence.

## Candidates

- the existing safe multinomial logistic model remains the required 1X2
  baseline and optional specialist;
- dynamic independent Poisson is the first joint-score candidate;
- dynamic Dixon–Coles is the low-score-corrected candidate;
- market-only and market-aware variants are enabled only with exact source
  identity, appropriate timing semantics, and equivalent production input;
- nonlinear, first-half, corner, and shot candidates remain unavailable until
  data, evaluation, and safe-serialization gates pass.

## Folds and calibration

The common-row contract tournament adds a compact versioned form-covariate
challenger and a convex ensemble of complete score surfaces. Ensemble weights
are non-negative, sum to one, and are selected on calibration rows only.

Every rolling-origin fold has distinct regions:

```text
training_end < calibration_start
calibration_end < test_start
```

Fitting sees only training rows. A predeclared global-temperature grid uses
only calibration rows. Test rows are used only for final metrics. Candidate
configurations and grids are immutable artifact identity inputs.

## Metrics and champion policy

The score-candidate gate reports exact-score negative log likelihood, 1X2 log
loss, Brier score, Ranked Probability Score, mean absolute goal error,
convergence, and coverage. The champion is the converged candidate with lowest
exact-score NLL; 1X2 log loss, RPS, and canonical identity are deterministic
tie-breakers. ROI and accuracy cannot independently select the champion.

Training never promotes a model. The tournament records
`not-promoted-explicit-governance-required`; promotion remains an explicit,
atomic, audited governance action.

Metrics also include Poisson deviance, correct-score top-three coverage, tail
diagnostics, and prediction coverage. Calibration and temporally blocked
bootstrap diagnostics return `insufficient-sample` rather than misleading
fixture precision. Rho diagnostics retain search-boundary,
correction-positivity, and low-score sample warnings; the range is not widened
or silently clipped.
