---
name: pyhap-release
description: Prepare and perform a new release of pyhap (HAP-python). Use when the user asks to prepare, cut, or publish a new release/version of pyhap.
---

# pyhap release

Release workflow for HAP-python. Work in the repo root (which contains `pyhap`, `scripts/`, `CHANGELOG.md`).

## Pre-flight checks (all must pass)

Perform these in order. If **any** step fails, report exactly what failed and **stop** — do not proceed with the release.

### 1. Tests, lint and type checks

Follow the `pyhap-testing` skill (`.skills/pyhap-testing/SKILL.md`) fully:
pick/confirm a venv, then run the full test suite, `ruff check .`,
`ruff format --check .`, and `ty check .`. All tests and checks must pass.
If anything fails, report exactly what failed (failing tests / ruff / ty
findings) and stop.

### 2. macOS plist freshness check

`scripts/gen_hap_types.py` generates `pyhap/resources/characteristics.json` and
`pyhap/resources/services.json` from the macOS HomeKit Accessory Simulator
plist:

```
/Applications/Xcode.app/Contents/Applications/HomeKit Accessory Simulator.app/Contents/Frameworks/HAPAccessoryKit.framework/Versions/A/Resources/default.metadata.plist
```

- Check whether this plist file is available locally at that path (or newer simulator metadata).
- If it is **not** available: warn the user that the generated HAP resources may be
  outdated relative to the latest macOS simulator types, and ask whether to continue
  the release anyway. Only continue if the user confirms.
- If it **is** available: re-run `python scripts/gen_hap_types.py` and check whether
  the generated JSON files changed (e.g. `git diff --exit-code pyhap/resources/`).
  If there are changes, notify the user that the generated resources are outdated,
  show the diff, and stop the release.

## Release steps

Only after all pre-flight checks pass:

1. **Bump the version** in `pyhap/const.py` (`__version__`).
   - Only bug fixes / performance improvements → **patch** bump.
   - New features, new services/characteristics → **minor** bump.
   - Large rework / breaking changes → **major** bump.
   Decide based on the commits since the last release. State your reasoning
   concisely and **ask the user to confirm** the chosen version before moving on.

2. **Update `CHANGELOG.md`** with the changes since the last release, based on the
   commits (`git log <last-tag>..HEAD`). Keep the existing Keep-a-Changelog format
   (version heading, date, bullet list with PR links).

3. **Commit** the version bump + changelog, then **wait for the user** to acknowledge
   the commit looks correct and that they pushed it. Do not continue before this ack.

4. **Pull latest master**, then create a release branch named `vx.x.x` (matching the
   new version) and **merge `dev` into it**.

5. Tell the user the branch name and **wait for them to acknowledge** that they opened
   and merged a PR of this branch into `master`. Do not continue before this ack.

6. **Get latest master** and **merge it into `dev`** (push if the user wants).

7. **Create a tag `vx.x.x`** on master for the new release (and push it if the user wants).

8. Run the release script: `scripts/release` (builds the distribution and uploads
   it to PyPI).

## Notes

- Tag and branch names use the format `vx.x.x` without a leading `v` on numbers
  (e.g. `v5.3.0`).
- After `scripts/release`, confirm the upload succeeded and report the released version.