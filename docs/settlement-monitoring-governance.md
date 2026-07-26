# Settlement, monitoring, and model governance

This operational layer consumes only persisted, verified evidence. It does not
scrape result sites, place bets, submit bookmaker transactions, manage a wallet,
or represent a real sportsbook account.

## Result snapshot lifecycle

Canonical results use `canonical-event-result-v1` and the closed statuses
`scheduled`, `in-progress`, `completed`, `postponed`, `cancelled`, `abandoned`,
and `incomplete`. The production adapter is limited to football full-match 1X2.
It derives `home`, `draw`, or `away` only from two verified non-negative integer
full-time scores. Half-time, partial, negative, missing, or contradictory scores
are rejected.

A result is first built as a typed `CanonicalResult`, then published as an
immutable `canonical-result-snapshot-v1` analytical artifact under the configured
snapshots root. Publication uses canonical JSON, a content-addressed identity,
an exact two-file layout, SHA-256 sidecar verification, root containment, and
symlink rejection. Registration in SQLite occurs only after verification and is
idempotent. READY evidence is never repaired or mutated.

## Analytical settlement

Settlement operates on persisted opportunities and exact persisted combination
legs from a verified `analysis-v2` artifact. It is a flat one-unit analytical
simulation, not confirmation of a bookmaker bet.

Statuses are:

- `pending`: no completed evidence is available yet, including postponement;
- `win` and `loss`: the exact canonical selection was resolved;
- `push`: the verified market result explicitly says push;
- `void`: cancellation/abandonment, or an explicitly void market result;
- `unresolved`: incomplete evidence that cannot safely produce an outcome.

`settlement-policy-v1` preserves every combination leg. A loss settles the
combination as a loss. Otherwise pending or unresolved legs keep it pending or
unresolved. A void/push leg remains recorded and contributes a unit odds factor;
winning active legs retain their persisted odds. No leg is silently removed,
replaced, repriced, dependency-checked again, or correlation-adjusted.

Every settlement and run identity includes the analysis artifact identity and
checksum, position and leg identities, result snapshot identities and checksums,
explicit as-of time, odds, calculated units, and policy version. Conflicting
evidence never overwrites an existing position.

## Monitoring

`monitoring-report-v1` evaluates persisted evidence at an explicit as-of time and
window. It covers snapshot/artifact validity, source staleness, unresolved or
duplicate identity counts, market completeness, result coverage, settlement
backlog and lag, prediction/opportunity counts, probability and quality
completeness, log loss, multiclass Brier score, calibration error, hit rate, and
ROI.

Every metric carries its policy/version, window, numerator and denominator when
applicable, sample size, value, closed status, threshold, and evidence
references. Exact threshold boundaries are inclusive of the worse state.
Missing denominators or evidence produce `unknown`, never `healthy`. Hit rate and
ROI are secondary metrics and are not promotion criteria.

Reports and finding IDs are deterministic and content-addressed. Identical replay
does not duplicate current indexes or findings.

## Champion–challenger governance

Only a safely loaded, checksum-verified, pickle-free `ModelArtifact` may be
registered. Registry roles are `champion`, `challenger`, and `archived`;
lifecycle states are `registered`, `eligible`, `promoted`, `demoted`, `archived`,
and `rejected`. SQLite enforces one active champion per exact sport/market scope
and prevents checksum replacement.

Evaluation and mutation are separate. `promotion-policy-v1` compares the same
scope, evaluation mode, window, event population, sample count, completed-result
coverage, log loss, multiclass Brier score, and calibration error. Insufficient
evidence holds; incompatible or unequal evidence rejects; ties, worse results,
and improvements below every configured margin retain; only a challenger meeting
all requirements receives `promote`. ROI alone cannot promote a model.

Applying a promotion requires an immutable recorded `promote` decision and
unchanged registry versions. Champion and challenger roles change atomically.
Rollback is an explicit atomic operation referencing an audited promotion.
Artifacts are never deleted and no operation retrains a model.

## Engine CLI

All mutating operations require an explicit `--as-of-utc` directly or in their
strict request JSON. Success output is canonical machine-readable JSON.

```text
engine.py --verify-result-snapshot RELATIVE_DIRECTORY [--checksum SHA256]
engine.py --register-result-snapshot RELATIVE_DIRECTORY --as-of-utc TIMESTAMP
engine.py --settle-analysis REQUEST_JSON
engine.py --list-settlement-runs
engine.py --verify-settlement-report RELATIVE_DIRECTORY [--checksum SHA256]
engine.py --run-monitoring REQUEST_JSON
engine.py --verify-monitoring-report RELATIVE_DIRECTORY [--checksum SHA256]
engine.py --register-model RELATIVE_PATH --checksum SHA256 --as-of-utc TIMESTAMP
engine.py --list-model-registry
engine.py --evaluate-challenger REQUEST_JSON
engine.py --apply-promotion DECISION_ID --as-of-utc TIMESTAMP
engine.py --rollback-promotion TRANSITION_ID --as-of-utc TIMESTAMP
engine.py --governance-history
```

The durable worker registry also exposes `settlement.settle-analysis` and
`monitoring.run`. Both use the same verified services, checkpoint before SQLite
mutation, and reuse an identical immutable output on at-least-once replay.

## Migration and limitations

Migration `0004_settlement_monitoring_governance.sql` adds result registrations,
settlement runs/evidence/audit rows, monitoring run/finding indexes, model
registry entries, immutable promotion decisions, and role transitions. Exact
unit values are stored as decimal text and all writes use caller-owned explicit
transactions.

There is no live result provider, result-site scraper, bookmaker execution,
alert delivery, automatic retraining, automatic promotion, staking/bankroll
management, new market adapter, or Streamlit decision logic.
