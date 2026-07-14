# `docs/` — Project documentation

Source for the MkDocs Material site published by `.github/workflows/docs.yml`
(Phase 12). Also browsed directly in-repo by developers.

## Top-level documents

| File | Purpose |
|---|---|
| `index.md` | Site landing page — where new readers start |
| `ARCHITECTURE.md` | The canonical contract — 14 invariants + system map. Referenced inline in code |
| `FUNDAMENTALS_PHASES_1_TO_8.md` | Long-form design doc for the fundamentals vertical slice (industry, shares, financials, market cap, indices, indicator state, signal events) |
| `PHASE_15_DESIGN.md` | Original design spec for the Phase 15 backtest pipeline |
| `gui_endpoint_audit.md` | 25-row living catalogue of every HTTP route × GUI surface × status (Phase 15d) |

## Subdirectories

| Path | Purpose |
|---|---|
| `architecture/` | Architecture reference — index, invariants, operations |
| `changelog/` | Per-phase changelog files (`phase-11.md`, `phase-12.md`, …) |
| `dev/` | Developer-facing docs — migrations workflow, parity harness, testing, transports |
| `operator/` | Operator runbooks — configuration, DB roles, DLQ, running the service |

## How the site is built

`mkdocs.yml` at the repo root configures MkDocs Material:
- Nav structure is hand-curated (not auto-generated from filesystem)
- Material theme, with dark/light toggle
- `mkdocs-mermaid2-plugin` renders Mermaid diagrams (extensively used in
  the architecture + fundamentals docs)
- Published to GitHub Pages on push to main; only rebuilds when
  `docs/**`, `mkdocs.yml`, or the workflow itself changes

## Authoring conventions

- All diagrams in **Mermaid** — kept inline in the markdown for
  diffability; rendered by the plugin at build time
- Long-form thinking goes in the `pine_indicator_port_conversation.docx`
  at the repo root (history of decisions); short, current-state material
  lives here
- Invariants in `architecture/invariants.md` are numbered; code comments
  reference them by number (`# Invariant 3.14: process boundaries`)
- Changelog files are tagged per phase — phase numbers are the project's
  units of delivery (Phase 11, 12, 14.6, 15a-d, etc.)

## Repo-root doc summary

Three documents at the repo root supplement these:

| File | Purpose |
|---|---|
| `README.md` | Project introduction + quickstart |
| `01_FUNCTIONALITY.md` | Inventory of what the system does end-to-end (Phase 15 snapshot) |
| `02_ARCHITECTURE.md` | How the pieces fit together (companion to `docs/ARCHITECTURE.md`, less invariant-focused, more system-map-focused) |
| `03_OPEN_ITEMS.md` | What's deliberately not done yet + roadmap of future phases |
