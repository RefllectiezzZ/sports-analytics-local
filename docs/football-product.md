# Coherent football fair-odds and proposal product

The local end-to-end proof is `synthetic-contract`. Its provisional fold winner
proves the pipeline contract only and is not a production champion. There is no
verified real historical football, basketball, or tennis modelling snapshot in
local storage, so production eligibility remains
`insufficient-real-evaluation-data`.

The primary v1 path is local historical modelling, not direct bookmaker
acquisition:

```text
historical results
-> deterministic data audit
-> leakage-safe chronological model tournament
-> reviewed joint-score champion
-> bounded P(home goals, away goals)
-> coherent market probabilities and fair odds
-> optional strict current offered-quote import
-> EV/opportunity decisions
-> proposed singles and same-bookmaker accumulators
-> persisted read-only Streamlit state
```

Football-Data and already verified historical snapshots are training evidence.
Price columns without a trustworthy provider observation timestamp are
`historical-closing-benchmark` evidence, not current executable prices.
Direct Betclic/Betano acquisition remains experimental and unsupported for any
provider/sport without an exact installed extraction profile. No additional
live probing is part of the v1 product path.

Proposal artifacts persist an immutable ordered `allowed_sports` list and one
exact builder mode. `combine-selected-sports` permits eligible real-priced legs
from selected sports in one provider-bound accumulator.
`separate-by-sport` partitions eligible legs and builds independent groups.
The policy is applied before enumeration and is included in artifact identity.
A selected sport without eligible legs retains `no-eligible-opportunity`.

Basketball and tennis remain
`analysis-unavailable: no-sport-specific-model`; football models never evaluate
their selections. European three-way integer handicaps are derived exactly
from the score matrix. Asian lines remain model-unavailable because the current
fair-price contract cannot express win, half-win, push, half-loss, and loss
without approximation.

PR #14 is reserved for release hardening, resilience, backup/restore,
installation, packaging, and release-candidate work.

## Probability surface

`models.football_scores` fits deterministic time-decayed independent Poisson
and Dixon–Coles candidates. The JSON-safe fitted representation includes
canonical team identities, parameters, configuration, chronology, and
optimizer diagnostics. Attack and defence effects use explicit sum-to-zero
constraints. Unknown teams use a named competition-average fallback that may
cause abstention.

The public probability object is one immutable bounded matrix:

```text
P(home_goals = h, away_goals = a)
```

The grid grows until omitted tail mass is below its configured tolerance,
subject to a hard maximum. Failure to meet the tolerance is an error. Residual
mass, grid, intensities, rho, cutoff, fallback state, lineage, and calibration
method are persisted. Global temperature scaling and 1X2 region raking operate
on the whole matrix; markets are not calibrated independently.

## Fair odds and offered odds

- **Model probability** is estimated from the score surface.
- **Fair odds** are a model estimate, normally `1 / probability`; draw no bet
  uses exact stake-return-on-draw settlement.
- **Offered odds** are real external prices from verified or strict operator
  evidence.
- **Market probability** is normalized no-vig probability from a complete
  offered market.
- **Edge** is model probability minus reviewed market probability.
- **Expected net return per unit** is model probability times offered decimal
  odds, minus one.

Without offered odds, fair odds remain available but market probability, edge,
EV, executable opportunity, and proposed price-based bet are unavailable.

## Offline current quote input

`bookmakers.operator_quotes` accepts exact canonical UTF-8 CSV, canonical JSON,
or manual rows through one validator. It validates registered provider,
reconciled future event, supported semantics, half-goal line, regulation/rules
scope, finite price, canonical UTC observation, freshness, validity, duplicate
identity, and complete-market outcomes. It rejects URLs, request headers,
cookies, scripts, selectors, browser profiles, and tokens.

Validated catalogues use the existing immutable, content-addressed artifact
boundary. Complete offered markets receive no-vig probabilities.

## Proposals and dependence

Singles require an exact real current quote plus completeness, calibration,
uncertainty, odds, edge, safety-margin, and EV gates. Abstentions persist
deterministic reason codes.

Separate-event accumulators are bounded, deterministic, and same-provider.
Same-event score selections use exact predicate intersection over the matrix,
never marginal multiplication. An analytical conjunction is not placeable
without one real bookmaker-offered combined-selection price.

There is no staking, login, bookmaker submission, or automatic placement path.
Final placement remains manual.

## Commands and UI

```powershell
python engine.py --config config/settings.toml --run-football-product request.json
python engine.py --football-market-capabilities
python engine.py --export-current-quote-template
```

The complete workflow publishes tournament, model, probability, quote,
proposal, and Streamlit read-model artifacts below the configured exports
root. Streamlit only reads the verified read model; it never trains, scrapes,
imports, evaluates, or writes on rerun.
