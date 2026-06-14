# `.github/workflows/` — GitHub Actions workflows

CI/CD workflows for the `trip-ema` package. Added in the Post-Phase-11
Tier-2 follow-ups; the `docs.yml` workflow is a Phase 12 addition.

## Workflows

| File | Trigger | Purpose |
|---|---|---|
| `tests.yml` | push + PR | Three jobs: `db-free`, `postgres-integration`, `lint` |
| `release.yml` | git tag | Builds sdist + wheel, publishes to PyPI, builds standalone GUI installer |
| `docs.yml` | push to main (paths-filtered) | Builds MkDocs Material site and publishes to GitHub Pages |
| `install-smoke.yml` | push + PR | Smoke-tests `pip install` of the package and its extras |

## `tests.yml` in detail

Three independent jobs:

- **`db-free`** — runs the ~588-test DB-free suite on every push and
  PR. Split into **two pytest invocations** to dodge a pytest-qt
  teardown race: a `--ignore=tests/gui` invocation for the 465 non-Qt
  tests, and a `tests/gui` invocation for the 123 Qt tests with
  `QT_QPA_PLATFORM=offscreen`. The Qt step checks pass/fail by
  parsing pytest output instead of trusting the exit code, so a
  benign Qt teardown segfault doesn't fail the build. Real test
  failures still do.
- **`postgres-integration`** — provisions a `postgres:16` service
  container and runs the Postgres-backed slices
  (`tests/persistence`, `tests/scheduler`, `tests/registry`,
  `tests/transports`, `tests/notifier`, `tests/api`). Gated to main
  pushes, manual dispatch, and same-repo PRs (forks can't access
  secrets).
- **`lint`** — runs `ruff check`; advisory only (doesn't block).

## `release.yml` in detail

Tag-triggered. Steps:

1. Build sdist + wheel via hatchling
2. Verify the wheel's `__version__` matches the tag
3. Publish to PyPI via trusted publishing (no API token)
4. **`build-gui-installer` job** (Phase 12) — runs PyInstaller against
   `gui_installer.spec` to produce a standalone Linux x86_64 binary,
   packages as `trip-ema-console-vX.Y.Z-linux-x86_64.tar.gz`, attaches
   to the GitHub Release. Windows + macOS deferred.

## `docs.yml` in detail

Phase 12. Builds the MkDocs Material site (config in `mkdocs.yml` at
repo root, sources in `docs/`) and pushes to GitHub Pages on push to
main. Paths-filtered so it only runs when `docs/**`, `mkdocs.yml`, or
the workflow itself changes.
