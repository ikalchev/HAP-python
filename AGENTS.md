# AGENTS.md — HAP-python

HAP-python is a Python framework for building HomeKit accessories and bridges
(the `pyhap` package; see `README.md`). Python 3.11+ required. All commands
below run from the repo root.

## Environment

A `venv/` directory in the repo root is the usual interpreter — use
`venv/bin/python` / `venv/bin/pytest`. For a fresh environment run
`scripts/setup`, which installs the package editable plus
`requirements_test.txt`. If in doubt, ask which interpreter to use.

## After making edits to the code

Do both of these, in this order — tests first: they may expose or require
more changes, and the docs should describe the final state of the code.

### 1. Run the tests skill

Follow `.skills/pyhap-testing/SKILL.md` in full: the full pytest suite,
`ruff check` / `ruff format --check`, and `ty check` — the skill documents
the exact commands, CI's lint scope, and the current `ty` baseline.

Run the checks even when you only changed docstrings — they are code too.
If any step fails, stop, fix the code and re-run the checks — there is no
point documenting a state the next round of fixes changes.

### 2. Update the docs

The docs (`docs/source/`, Sphinx + RST, mermaid diagrams enabled) must stay
in sync with the code:

* **Public API changes** (new/changed/removed classes, methods, arguments in
  `pyhap/`): update the matching autodoc page under `docs/source/api/` —
  one module per file, referenced from `docs/source/index.rst` and the
  `docs/source/api/index.rst` glob. Add or adjust `.. autoclass::` /
  `.. automodule::` entries for new public objects.
* **Docstrings are docs**: public docstrings are rendered by autodoc, so they
  must be valid RST. A docstring with broken RST makes the
  docs build fail with `-W` (see below), even if the code passes tests.
* **Architecturally significant changes** (how objects interact, lifecycle
  rules, what runs on which thread/loop): update the narrative pages —
  `intro/overview.rst` (object model + interaction diagrams),
  `advanced/state.rst` (persistence), `advanced/async.rst` (event loop rules
  and provided utilities), `media/camera.rst` and `media/architecture.rst`
  (camera streaming). Write about roles, contracts and lifecycles, not
  implementation details.
* **New example accessory** in `accessories/`: list it in
  `intro/examples.rst`.

Build and verify before finishing:

```sh
cd docs && ../venv/bin/python -m sphinx -W -b html source build/html
```

(or `tox -e docs`). The build must complete with **zero warnings** — CI
treats warnings as errors (`-W`). Watch Sphinx output for unresolved
cross-references (`:class:`/`:meth:`/`:func:`/`:doc:` targets). Never commit
`docs/build/` (it is gitignored).

If this step changes code or docstring — repeat.

## Skills

* `.skills/pyhap-testing/SKILL.md` — run tests/lint/type-checks. Use after
  any code edit.
* `.skills/pyhap-release/SKILL.md` — prepare/cut/publish a release. Use when
  asked to release a new version; it includes the testing skill as a
  pre-flight check.

## Repo layout

* `pyhap/` — the package (accessory, service, characteristic, driver, state,
  camera stack, `pyhap/media`).
* `accessories/` — runnable example accessories.
* `docs/` — Sphinx docs; source in `docs/source/`, built to `docs/build/`.
* `tests/`, `benchmarks/`, `scripts/` — tests, benchmarks, dev scripts.
* `CHANGELOG.md` — release notes (Keep-a-Changelog format).
