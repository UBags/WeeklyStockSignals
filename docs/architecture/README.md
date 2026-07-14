# `docs/architecture/` — Architecture reference

The MkDocs Material site's architecture section. These files are the
**developer-facing** companion to `02_ARCHITECTURE.md` at the repo root
(which is the broader system map). The canonical invariants live in
`invariants.md`.

## Contents

| File | Purpose |
|---|---|
| `index.md` | Landing page for the architecture section; high-level orientation |
| `invariants.md` | The 14 numbered invariants — encoded contracts the code obeys (single-writer cache, Pine numerical fidelity, process boundaries, observability is additive, etc.). Referenced inline by code comments |
| `operations.md` | Operational architecture — deployment topology, scaling notes, the "single-host, single-process" boundary |

## Why invariants are numbered

Each invariant is referenced from code (`# Invariant 3.14: ETL/Engine/GUI
process boundaries`) and from PR review. Numbering keeps cross-references
stable across edits — the underlying text can be reworded without
invalidating references, as long as the number means the same thing.

If an invariant is genuinely retired, leave the number reserved and add
a footnote explaining what replaced it.

## Two of the most-cited invariants

- **3.1 — The ETL is the source of truth for bar availability.** The
  application consumes what's in Postgres; it does not infer, generate,
  or interpolate missing bars.
- **3.3 — Pine Script numerical fidelity is non-negotiable.** If a
  refactor "simplifies" the indicator math, the parity harness should
  fail. If it doesn't, the test is under-specified, not the code.
