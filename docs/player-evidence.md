# Player evidence boundary

No configured repository source currently supplies verified historical players,
lineups, appearances, minutes, injuries, or suspensions. Player context is
therefore `player-context-not-trainable`.

The canonical boundary separates:

- canonical and source player identities;
- explicit identity reconciliation;
- date-bounded team membership and player role;
- medical injury;
- sporting suspension;
- selection decisions;
- expected and confirmed lineups;
- expected minutes;
- post-match participation and statistics.

Equal normalized display names never merge identities. Missing evidence remains
`unknown`; it never becomes `available`. Not being confirmed as a starter does
not imply unavailability.

Pre-match observations must precede kickoff, match membership dates, use bounded
confidence, and carry source observation identity and time. Contradictory
confirmed lineups are rejected. Expected minutes are restricted to 0–120.

Every import row also declares the source display identity, reconciliation
state/confidence/reason, membership dates, and player role. A resolved canonical
player cannot be imported without matching membership evidence; an unresolved
player cannot claim membership.

Current context can be imported only through the exact CSV or JSON templates:

```powershell
python -m sports_analytics.services.lifecycle_cli --export-player-csv-template
python -m sports_analytics.services.lifecycle_cli --export-player-json-template
python -m sports_analytics.services.lifecycle_cli --validate-player-evidence --input <file>
python -m sports_analytics.services.lifecycle_cli --import-player-evidence --input <file>
python -m sports_analytics.services.lifecycle_cli --verify-player-artifact --artifact <directory>
python -m sports_analytics.services.lifecycle_cli --inspect-player-capability
```

The UI displays only persisted evidence and explicitly reports whether it is
unavailable, stale, unresolved, display-only, or consumed by a model. Until
historically equivalent pre-kickoff evidence exists, no team-level player
feature or player-market probability is emitted. Player markets remain
`player-data-required`.
