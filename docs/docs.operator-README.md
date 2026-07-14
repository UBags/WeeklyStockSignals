# `docs/operator/` — Operator runbooks

Documentation for people **running** the deployed `trip-ema` service —
distinct from `../dev/` (which is for people writing code).

## Contents

| File | Purpose |
|---|---|
| `configuration.md` | `config.yaml` reference — every block, every field, defaults, examples |
| `db-roles.md` | The optional Phase 12 per-process DB role workflow — three roles (etl/engine/gui), how to enable them, how to roll back |
| `dlq.md` | Notifier dead-letter queue workflow — when entries land in DLQ, how to inspect, replay, or permanently delete; Redis HASH attempt counter |
| `running.md` | Day-to-day operations — start/stop the service, monitor health, common workflows |

## Other operator-facing material

Living *outside* this directory but with the same audience:

- `../../scripts/operator_commands.md` — every common operator
  workflow as a copy-paste recipe (much longer than the docs here)
- `../../scripts/debug_recipes.md` — less-common recipes for when
  something's broken
- `../gui_endpoint_audit.md` — what's accessible via the GUI vs.
  what still needs curl/CLI

## When to read which

| Question | Answer |
|---|---|
| "How do I configure X?" | `configuration.md` |
| "How do I start / stop / restart?" | `running.md` |
| "Why are notifications failing?" | `dlq.md` + `../../scripts/debug_recipes.md` |
| "How do I set up the per-process DB roles?" | `db-roles.md` |
| "How do I run a backfill?" | `../../scripts/operator_commands.md` §3-5 |
| "Anyone else seeing this error?" | `../../scripts/debug_recipes.md` (likely yes) |
