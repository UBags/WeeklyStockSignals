# READMEs bundle

One `README.md` per package under `trip_ema/`. Drop each into the
matching package directory and commit.

## Mapping (bundle path → repo path)

| Bundle | Repo path |
|---|---|
| `api/README.md` | `trip_ema/api/README.md` |
| `backtest/README.md` | `trip_ema/backtest/README.md` |
| `cache/README.md` | `trip_ema/cache/README.md` |
| `config/README.md` | `trip_ema/config/README.md` |
| `domain/README.md` | `trip_ema/domain/README.md` |
| `etl/README.md` | `trip_ema/etl/README.md` |
| `fundamentals/README.md` | `trip_ema/fundamentals/README.md` |
| `gui/README.md` | `trip_ema/gui/README.md` *(replaces existing)* |
| `indicators/README.md` | `trip_ema/indicators/README.md` |
| `notifications/README.md` | `trip_ema/notifications/README.md` |
| `observability/README.md` | `trip_ema/observability/README.md` |
| `persistence/README.md` | `trip_ema/persistence/README.md` |
| `registry/README.md` | `trip_ema/registry/README.md` |
| `scheduler/README.md` | `trip_ema/scheduler/README.md` |
| `transports_http/README.md` | `trip_ema/transports/http/README.md` |
| `transports_http_routes/README.md` | `trip_ema/transports/http/routes/README.md` |
| `migrations/README.md` | `trip_ema/migrations/README.md` |

## Caveats

- Only the existing `gui/README.md` was attached. The other 16 are
  generated from source-file headers + project context — verify before
  committing.
- Bundle dir names use underscores where the repo uses slashes
  (`transports_http_routes/` → `transports/http/routes/`).
- Paths assume the standard layout `trip_ema/<package>/`. Adjust if
  your tree differs.

## Spot-check first (per your house rule)

Before staging, eyeball a couple of files for accuracy on the bits
that bite — file lists per package, the EoD pipeline order in
`fundamentals/`, the 4-dot import note in `transports_http_routes/`,
and the migration 0016 note in `persistence/` and `migrations/`.

## Paste-ready git block

```bash
# from repo root

cp readmes/api/README.md                    trip_ema/api/README.md
cp readmes/backtest/README.md               trip_ema/backtest/README.md
cp readmes/cache/README.md                  trip_ema/cache/README.md
cp readmes/config/README.md                 trip_ema/config/README.md
cp readmes/domain/README.md                 trip_ema/domain/README.md
cp readmes/etl/README.md                    trip_ema/etl/README.md
cp readmes/fundamentals/README.md           trip_ema/fundamentals/README.md
cp readmes/gui/README.md                    trip_ema/gui/README.md
cp readmes/indicators/README.md             trip_ema/indicators/README.md
cp readmes/notifications/README.md          trip_ema/notifications/README.md
cp readmes/observability/README.md          trip_ema/observability/README.md
cp readmes/persistence/README.md            trip_ema/persistence/README.md
cp readmes/registry/README.md               trip_ema/registry/README.md
cp readmes/scheduler/README.md              trip_ema/scheduler/README.md
cp readmes/transports_http/README.md        trip_ema/transports/http/README.md
cp readmes/transports_http_routes/README.md trip_ema/transports/http/routes/README.md
cp readmes/migrations/README.md             trip_ema/migrations/README.md

git add trip_ema/*/README.md trip_ema/transports/http/README.md trip_ema/transports/http/routes/README.md

git commit -m "docs: refresh per-package READMEs

One README per trip_ema sub-package, covering the file list, the
public surface, key invariants, and operator-facing notes. Updates
the existing gui/ README and adds READMEs for api, backtest, cache,
config, domain, etl, fundamentals, indicators, notifications,
observability, persistence, registry, scheduler, transports/http,
transports/http/routes, and migrations."

git push origin master
```
