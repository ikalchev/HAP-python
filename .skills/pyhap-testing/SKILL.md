---
name: pyhap-testing
description: Run tests and static checks (pytest, ruff, ty) for pyhap (HAP-python). Use when the user asks to test the code, run tests, lint, or type-check pyhap.
---

# pyhap testing

How to run tests and static checks for HAP-python.

## Environment setup

Before running anything, ask the user whether to use an existing virtualenv or
create a new one (unless the choice is already obvious from the session):

- **Existing venv**: the repo may already contain a `venv/` directory in the repo
  root. If the user points to a venv, use its interpreter (e.g. `venv/bin/python`,
  `venv/bin/pytest`) and prefix commands accordingly.
- **New venv**: create one (e.g. `python3 -m venv venv`), then install the
  dependencies with `scripts/setup`, which runs:

  ```sh
  pip install -e .[qrcode,talkback]
  python3 -m pip install -r requirements_test.txt
  ```

Alternatively the user may prefer running through `tox` (see `tox.ini`) with the
chosen interpreter.

All commands are run from the repo root.

## Tests

Run the full test suite with pytest:

```sh
python -m pytest
# or, if using a venv:
venv/bin/python -m pytest
```

A quick sanity check before the full suite: `python -m pytest tests -x -q`.

All tests must pass. Report any failures with the failing tests and errors.

## Ruff (lint + format)

CI lints a scoped set of paths (`tox -e lint`, see `tox.ini`) — `accessories/`
and `main.py` are legacy examples with pre-existing findings and are not
linted. Use the same scope:

```sh
python -m ruff check pyhap tests benchmarks scripts/gen_hap_types.py
python -m ruff format --check pyhap tests benchmarks scripts/gen_hap_types.py
```

(or `ruff <cmd>` from the active venv, or `tox -e lint`.) Configuration lives
in `pyproject.toml`. All findings must be fixed or reported to the user.

## ty (type checking)

```sh
python -m ty check .
```

Sources are configured via `[tool.ty.src]` in `pyproject.toml`. The branch
carries a pre-existing baseline of diagnostics (mostly in `tests/`): capture
it before your changes and ensure your edits add no new findings. Otherwise
report the findings.

## Reporting

When asked to "test everything", run **pytest, ruff check/format, and ty** in that
order and summarize the results of each step. If any step fails, stop and report
exactly what failed.
