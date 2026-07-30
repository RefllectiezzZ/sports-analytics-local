# Closed-loop result learning

PR #13 implements an immutable evidence loop:

1. a verified canonical football snapshot supplies completed events;
2. `results.register-from-snapshot` projects only finished events with canonical
   identity, non-negative integer scores, observation time, source checksum, and
   source snapshot lineage;
3. each canonical result is published and strictly reloaded before idempotent
   SQLite registration;
4. `settlement.settle-new-results` resolves one exact analysis artifact plus
   exact registered result snapshots by content identity, then publishes and
   persists an immutable analytical settlement report;
5. `monitoring.refresh` resolves exact analysis evidence, derives its event
   window internally, and publishes and persists monitoring metrics;
6. the training-eligibility ledger links verified outcomes only to versioned
   pre-match feature artifacts;
7. the conservative retraining policy evaluates match volume, champion age,
   tournament age, coverage, competition count, failed-cycle cooldown, and the
   one-active-job scope lock;
8. challenger cycles create new feature/model/tournament artifacts and never
   mutate the active champion;
9. manual promotion records an append-only champion revision, and rollback
   appends another revision pointing at a retained prior artifact.

Predictions, accepted opportunities, offered-price outcomes, settlement labels,
and manually edited scores are not training inputs. Existing training snapshots
are never mutated when a result arrives.

The default policy requires 100 newly eligible completed matches, allows only one
active cycle per exact scope, applies a seven-day failed-cycle cooldown, and sets
`strict_policy_auto_promotion` to false.

Static worker job types:

- `results.register-from-snapshot`
- `settlement.settle-new-results`
- `monitoring.refresh`
- `training.evaluate-retraining-trigger`
- `training.run-challenger-cycle`

Lifecycle job payloads contain registered snapshot/artifact IDs and checksums, a
typed sport/competition scope, a policy ID, and a canonical UTC cutoff. Artifact
paths are resolved internally from those exact identities. Payloads do not accept
paths, URLs, import names, scripts, headers, cookies, tokens, or selectors.
Retraining-trigger and challenger-cycle payloads must reference the exact
verified training-eligibility ledger; registered result volume alone is never a
training gate.

Published challenger artifacts record the exact eligibility-ledger artifact ID
and checksum plus the sorted source snapshot IDs and checksums used for fitting.
The cycle identity includes the same lineage, policy, scope, and cutoff. A
worker retry with the same payload strictly reloads an existing publication and
accepts it only when its content identity is exactly the expected identity.

The granular operator surface is:

```powershell
python -m sports_analytics.services.lifecycle_cli --help
```

All output is canonical JSON or an exact CSV/JSON template. Invalid input exits
with code 2; success exits with code 0.

Promotion and rollback require explicit registered decision/transition IDs:

```powershell
python -m sports_analytics.services.lifecycle_cli --apply-explicit-promotion --decision-id <id>
python -m sports_analytics.services.lifecycle_cli --verify-champion
python -m sports_analytics.services.lifecycle_cli --rollback-promotion --transition-id <id>
```

The deterministic synthetic-contract lifecycle proof is separately labelled and
strictly reloadable:

```powershell
python -m sports_analytics.services.lifecycle_cli --run-offline-closed-loop-proof
python -m sports_analytics.services.lifecycle_cli --verify-offline-closed-loop-proof --artifact <directory>
```
