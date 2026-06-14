# `.github/` — GitHub configuration

Holds GitHub-specific configuration for this repository. Today the only
content is `workflows/` — the GitHub Actions CI/CD workflows added in
Phase 11 (Post-Phase-11 follow-ups, Tier 2) and expanded in Phase 12.

## Contents

| Path | Purpose |
|---|---|
| `workflows/` | Actions workflow YAMLs (tests, release, docs) |

See `workflows/README.md` for per-workflow detail.

## Related repo-root files

- `release.yml` at the repo root is a legacy/duplicate of the release
  workflow (the canonical one lives under `workflows/`).
- `mkdocs.yml` at the repo root drives the MkDocs Material site built
  by the `docs.yml` workflow.
