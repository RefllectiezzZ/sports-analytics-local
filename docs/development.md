# Development guide

Conventions for contributing to `sports-analytics-local`.

## Branch naming

- Do not commit directly to `main`.
- Prefer short, descriptive branch names.
- Cloud Agent / Cursor feature branches follow: `cursor/<descriptive-name>-<suffix>`.
- Use lowercase letters in branch names.

Examples:

- `cursor/repo-bootstrap-3ad9`
- `feat/sqlite-job-queue`
- `fix/parquet-snapshot-paths`

## Commit messages

Use conventional-style commit messages:

- `feat: ...` — user-visible capability
- `fix: ...` — bug fix
- `docs: ...` — documentation only
- `chore: ...` — tooling, packaging, housekeeping
- `test: ...` — tests only
- `refactor: ...` — internal restructuring without behaviour change

Keep the subject concise and imperative.

## Pull requests

- Keep pull requests focused on one concern.
- Include a clear summary of what changed and why.
- Note any intentional non-goals or follow-up work.
- Link related issues when applicable.
- Expect review before merge; do not merge your own bootstrap or experimental work without review.
- Use the pull-request template under `.github/pull_request_template.md`.

### GitHub quality gates

- All new pull requests must pass the GitHub quality workflow (`.github/workflows/quality.yml`).
- Do **not** merge while checks are failing or still running.
- The author must wait for review before merging.
- A successful Cursor Agent report does **not** replace GitHub CI.
- Run local checks before opening a pull request.
- Failed CI checks must be investigated and fixed; do not bypass them.
- Branch protection may be configured manually after the quality workflow is active. Do not assume it is already enabled.

## Tests and validation

Before requesting review, run:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

- Behaviour changes require tests.
- Do not add meaningless assertions (for example `assert True`).
- Prefer deterministic fixtures under `tests/fixtures/`.

## Prohibitions

- Do **not** commit directly to `main`.
- Do **not** commit generated data (Parquet, SQLite, model artifacts, exports, logs).
- Do **not** commit credentials, tokens, API keys, or secrets.
- Do **not** add paid API dependencies without an explicit architecture decision.
- Do **not** add external AI / LLM runtime dependencies.
- Do **not** commit virtual environments or tool caches.

## Code expectations

- Keep code typed (annotations throughout).
- Prefer deterministic behaviour and explicit failure modes.
- Keep changes auditable and reviewable.
- Avoid speculative interfaces and unused abstractions in early milestones.

## Recommended review outcomes

Reviewers should conclude with one of:

- **MERGE** — ready to merge as-is (or with trivial nits).
- **CHANGES REQUIRED** — must address specific feedback before merge.
- **DO NOT MERGE** — design or scope issues block the change.

## Local quality hooks

Install pre-commit once per clone:

```bash
pre-commit install
```

Hooks enforce whitespace, end-of-file, YAML/TOML validity, private-key detection, Ruff lint (with safe fixes), and Ruff formatting.
