## Summary

<!-- What changed and why. -->

## Scope

<!-- What this pull request intentionally covers. -->

## Non-goals

<!-- What this pull request deliberately does not include. -->

## Validation performed

### Local Cursor / Linux validation

<!-- Commands run in the Cursor agent environment and their real results. -->

### Hosted Windows / Python 3.12 check

<!-- GitHub Actions Windows compatibility result. The Cursor report does not replace this check or code review. -->

## Database / migrations

- [ ] Database migrations changed (list versions)
- [ ] Migration checksums changed (list old/new when rewriting is intentional and not yet applied)
- [ ] Confirmation that no already-applied migration file was modified after shipping
- [ ] Migration files and documented checksums were verified (including unchanged applied migrations)

## Durable worker / queue semantics

- [ ] Queue lifecycle and lease semantics were updated intentionally (or N/A)
- [ ] Atomic claim ordering, lease ownership, heartbeat, expiry, and recovery were considered
- [ ] Retry/backoff behaviour and at-least-once handler idempotency were considered
- [ ] No running-job force cancellation path was introduced without explicit design review

## Risks

<!-- Known risks, trade-offs, or areas needing careful review. -->

## Follow-up work

<!-- Deferred work or related follow-ups, if any. -->

## Checklist

Confirm each item, or mark it N/A with a brief reason in the Summary / Non-goals section.

- [ ] Pull request is focused on one concern
- [ ] Tests were added or updated when behaviour changed (or N/A)
- [ ] Concurrency / lease-fencing tests were added or updated when queue behaviour changed (or N/A)
- [ ] Worker subprocess / supervisor tests were added or updated when process behaviour changed (or N/A)
- [ ] Local Cursor quality suite passed (`pytest`, `ruff`, `mypy`, and related checks)
- [ ] Local validation commands and results are listed above
- [ ] Hosted Windows / Python 3.12 GitHub check passed
- [ ] Hosted Windows result is listed above
- [ ] GitHub does not redundantly run the Linux suite already covered by Cursor
- [ ] Cursor report does not replace code review
- [ ] No generated data was committed
- [ ] No credentials or secrets were committed
- [ ] No paid API dependency was introduced
- [ ] No external AI or LLM runtime dependency was introduced
- [ ] Documentation was updated when required (or N/A)
- [ ] Pull request is ready for review
- [ ] Author has not merged this pull request before review
