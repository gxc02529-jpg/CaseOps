# Contributing

Thanks for taking a look at CaseOps. This repository is a de-identified reference
implementation, so contributions are expected to keep the offline
"no model, no database, no network" test path working.

## Development setup

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Checks before opening a pull request

```bash
python -m pytest -q
python -m compileall -q src
```

## Guidelines

- Keep the domain layer free of framework and infrastructure imports.
- Keep the model boundary safe: model output must never bypass the approval gate
  or cite evidence outside the set retrieved for the current request.
- Add or update a test for every behaviour change; the suite must pass without a
  model key or a running database.
- Use conventional commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, `chore:`,
  `ci:`.
