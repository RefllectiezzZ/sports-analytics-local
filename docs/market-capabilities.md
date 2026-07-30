# Market capability matrix

The runtime registry in `sports_analytics.markets.capabilities` is
authoritative. Taxonomy membership alone does not imply analytical support.

| Sport | Market family | Required data | Model | Probability | Fair odds | Offered price | Opportunity | Combination | Limitation |
|---|---|---|---|---|---|---|---|---|---|
| football | 1X2, double chance, draw no bet | full-time scores | dynamic Poisson/DC | supported | supported | supported | supported | supported | full match |
| football | correct score, totals, BTTS, team totals, odd/even, winning margin | full-time scores | joint score matrix | supported | supported | supported | supported | supported | tail reported |
| football | European handicap | full-time scores | joint score matrix | supported | supported | supported | supported | supported | reviewed integer three-way lines |
| football | Asian handicap and totals | score surface and split-stake settlement | none | model-unavailable | model-unavailable | supported | model-unavailable | model-unavailable | exact five-state settlement contract missing |
| football | result/total and result/BTTS AND/OR | full-time scores | fixed predicates | supported | supported | supported | supported | supported | same-event placement needs combined offer |
| football | first-half families | timestamp-safe half-time history | none | model-unavailable | model-unavailable | supported | model-unavailable | model-unavailable | no evaluated champion |
| football | corners and shots | complete leakage-safe count history | none | data-insufficient | data-insufficient | supported | data-insufficient | data-insufficient | separate model required |
| football | player markets | identity, availability, role, minutes | none | player-data-required | player-data-required | supported | player-data-required | player-data-required | team model cannot price players |
| football | next goal/corner | verified chronological live state | none | live-state-required | live-state-required | supported | live-state-required | live-state-required | no pre-match proxy |
| basketball | all | sport-specific history/features/rules | possessions/efficiency | model-unavailable | model-unavailable | supported | model-unavailable | model-unavailable | no sport model |
| tennis | all | serve/return, games, sets, rules | point/game/set | model-unavailable | model-unavailable | supported | model-unavailable | model-unavailable | no sport model |

Run `python engine.py --football-market-capabilities` for exact per-family rows.
