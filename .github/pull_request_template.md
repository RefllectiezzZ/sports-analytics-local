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

## Risks

<!-- Known risks, trade-offs, or areas needing careful review. -->

## Follow-up work

<!-- Deferred work or related follow-ups, if any. -->

## Checklist

Confirm each item, or mark it N/A with a brief reason in the Summary / Non-goals section.

- [ ] Pull request is focused on one concern
- [ ] Tests were added or updated when behaviour changed (or N/A)
- [ ] Local Cursor quality suite passed (`pytest`, `ruff`, `mypy`, and related checks)
- [ ] Hosted Windows / Python 3.12 GitHub check passed
- [ ] GitHub does not redundantly run the Linux suite already covered by Cursor
- [ ] Cursor report does not replace code review
- [ ] No generated data was committed
- [ ] No credentials or secrets were committed
- [ ] No paid API dependency was introduced
- [ ] No external AI or LLM runtime dependency was introduced
- [ ] Documentation was updated when required (or N/A)
- [ ] Pull request is ready for review
- [ ] Author has not merged this pull request before review
