# GUI endpoint audit

**Last reviewed:** Phase 15d (2026-05-21)
**Maintainer convention:** when a new HTTP route is added, update this
table in the same commit. If a route gets a new GUI surface, update
the "Current GUI access" column.

This document maps every HTTP route exposed by the service to its
operator-facing GUI surface (if any). It exists to make the
operator-experience gaps visible at a glance and to record explicit
decisions about which gaps are deliberate vs. deferred.

Status legend:
- ✅ **OK** — endpoint has appropriate GUI coverage
- ➕ **Added in 15d** — gap closed by Phase 15d
- ⏳ **Deferred** — gap known and deliberate; reason captured below
- 🛠 **CLI only** — diagnostic/admin endpoint not appropriate for GUI

---

## Audit table

| Endpoint | Method | Current GUI access | Status | Notes |
|---|---|---|---|---|
| `/healthz` | GET | Status bar (implicit, connection indicator) | 🛠 | Liveness probe; no GUI needed |
| `/readyz` | GET | None | 🛠 | Readiness probe; `curl /readyz` is the canonical use |
| `/v1/admin/etl/run-eod` | POST | EtlStatusView "Run EoD" button | ✅ | |
| `/v1/admin/etl/run-intraday` | POST | EtlStatusView "Run Intraday" button | ✅ | |
| `/v1/admin/etl/backfill` | POST | EtlStatusView "Backfill…" button (15d) | ➕ | Modal dialog: start_date + optional tier |
| `/v1/admin/etl/refresh-listings` | POST | EtlStatusView "Refresh Listings" button | ✅ | |
| `/v1/admin/etl/recompute-tiers` | POST | EtlStatusView "Recompute Tiers" button | ✅ | |
| `/v1/admin/etl/unpark-all` | POST | EtlStatusView "Unpark All" button | ✅ | |
| `/v1/admin/etl/status` | GET | EtlStatusView 5s poll | ✅ | |
| `/v1/admin/etl/parked` | GET | EtlStatusView parked list | ✅ | |
| `/v1/admin/exchange-state` (list + per-exchange) | GET | EtlStatusView state panel | ✅ | |
| `/v1/admin/exchanges` | GET | ExchangePicker (used in EtlStatusView + Signals tab) | ✅ | |
| `/v1/admin/orchestrator/status` | GET | None — see [Orchestrator status](#orchestrator-status) below | ⏳ | Server-side card design needed first |
| `/v1/admin/orchestrator/{exchange}/run-now` | POST | None | ⏳ | Niche; see [Orchestrator actions](#orchestrator-actions) |
| `/v1/admin/orchestrator/{exchange}/pause` | POST | None | ⏳ | Niche; see [Orchestrator actions](#orchestrator-actions) |
| `/v1/admin/orchestrator/{exchange}/resume` | POST | None | ⏳ | Niche; see [Orchestrator actions](#orchestrator-actions) |
| `/v1/admin/subscribers` (list/create) | GET/POST | Subscribers tab | ✅ | |
| `/v1/admin/subscribers/{id}` (get/update/delete) | GET/PATCH/DELETE | Subscribers tab | ✅ | |
| `/v1/admin/subscribers/{id}/filters` | PUT | Subscribers tab filter editor | ✅ | |
| `/v1/admin/subscribers/{id}/test` | POST | Subscribers tab "Send test" | ✅ | |
| `/v1/admin/notifications/recent` | GET | Notifications tab | ✅ | |
| `/v1/admin/notifications/dlq` | GET | Notifications tab DLQ panel | ✅ | |
| `/v1/admin/health` | GET | System Health tab | ✅ | Card-driven; server controls which cards render |
| `/v1/admin/circuit-breakers` | GET | System Health tab (as a card from `/v1/admin/health`) | ✅ | Already in the health snapshot — no separate panel needed |
| `/v1/admin/reprime` | POST | None | ⏳ | See [Reprime](#reprime) |
| `/v1/pulse/stream` | GET (SSE) | Internal-only (some views subscribe) | 🛠 | Documented in debug_recipes.md |
| `/v1/signals/{exchange}/{symbol}/history` | GET | SignalsView chart | ✅ | |
| `/v1/admin/signals/generate` (15b) | POST | SignalsView "Generate signals" button (15c) | ✅ | |
| `/v1/admin/signals/generated` (15b) | GET | SignalsView "last generation per exchange" panel (15c) | ✅ | |

---

## Deferred-with-reason details

### Orchestrator status

The design (§8.1) flagged this as "Add now. Already exposed in System Health
tab as a card. Verify it's there; if not, add."

**Audit finding:** the System Health view consumes whatever cards
`/v1/admin/health` returns. The health route currently does NOT build
an orchestrator card. So the gap is real, but closing it requires
server-side work (a new `_orchestrator_card(...)` builder in
`trip_ema/transports/http/routes/admin_health.py`), not just a GUI
addition.

**Why deferred from 15d:** designing the card needs answers that
weren't in the §8 audit:

  - What status thresholds map to ok/warning/crit? (E.g. is a paused
    orchestrator "crit" or "warning"?)
  - How to summarize multiple markets in one card vs. one card per
    market? (System Health is intentionally compact.)
  - What goes in the headline vs. the detail? The orchestrator
    status payload has nested per-market state — flattening it for
    a card needs a deliberate choice.

Inventing answers in 15d would produce an opinionated card that
operators then have to live with. Better to capture the gap here
and design it in a follow-up.

**Workaround today:** `curl -s /v1/admin/orchestrator/status | jq` is
the canonical access. Add to `scripts/operator_commands.md`.

### Orchestrator actions

`run-now`, `pause`, `resume` per exchange. None of these has a
natural GUI home today. They'd belong on a "Markets" or
"Orchestrator" tab that doesn't exist. The §8 audit explicitly
defers them with "Add later. A 'force tick now' button would be
useful operationally but feels niche; defer unless workflow demands
it."

**Trigger to revisit:** an operator workflow conversation that says
"I need to pause NSE while we investigate, then resume." If that
happens, the natural shape is a per-market button row on a new tab
or on the System Health card.

**Workaround today:** `curl -X POST` per `scripts/operator_commands.md`.

### Reprime

`/v1/admin/reprime` re-primes one ticker's engine state from scratch.
Used after a data-quality fix on one ticker that's now showing wrong
signals. Genuinely niche — the operator who triggers this knows the
ticker symbol they care about.

**Workaround today:** `curl -X POST '/v1/admin/reprime?exchange=NSE&symbol=RELIANCE'`
documented in `scripts/operator_commands.md`. A "Reprime this ticker"
button on the Signals tab is possible if demand grows.

---

## Maintenance protocol

When a new HTTP route is added:

1. Add a row to the table above with the proposed status (`✅`, `➕`,
   `⏳`, or `🛠`).
2. If `⏳`, add a short reason section below explaining what's blocking.
3. If `🛠`, no GUI work; just document the CLI use.
4. If `✅` or `➕`, the same PR/commit that adds the route should add
   the GUI surface, or this table is stale.

When an existing route gets a new GUI surface, update the "Current GUI
access" column.

When deferred items get implemented, update the status and remove the
"deferred reason" section.

The point of this document is to make every gap deliberate. If you
find yourself reading it and seeing a `⏳` whose reason no longer
applies, that's the signal to act.
