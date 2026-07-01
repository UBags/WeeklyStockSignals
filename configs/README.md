# `configs/` — operator-editable auxiliary YAML

## 1. Role in the larger application

`configs/` holds **non-secret runtime YAML** that the trip_ema
service reads at startup. The main `config.yaml` lives at the repo
root; this directory holds auxiliary files that `config.yaml`
references by path.

The pattern is: structural configuration in the main `config.yaml`
(database connection, exchanges, scheduling, transports);
operator-curated tables and per-exchange data files in separate
YAML files under this directory.

Currently one file:

- **`nse_holidays_2026.yaml`** — per-year overrides for NSE's
  holiday calendar.

The directory exists in advance of further use: future per-year
holiday overrides, per-exchange parameter overrides, operator-
curated industry classifications, etc. all land here.

```mermaid
flowchart LR
    op[Operator] -.edits.-> yaml[configs/nse_holidays_2026.yaml]
    yaml --> cfg[config.yaml<br/>market_hours.holidays_override_file]
    cfg --> svc[trip-ema startup]
    svc --> cal[TradingCalendar]
    cal --> sched[MarketOrchestrator]
```

## 2. Architecture and constraints

| Rule | Why |
|------|-----|
| **Non-secret only** | Credentials live in environment variables, referenced from `config.yaml` via `*_env` keys |
| **Read at startup, not hot-reloaded** | Service must restart to pick up edits. Simplifies reasoning about config consistency mid-cycle |
| **Paths in `config.yaml` are CWD-relative** | Service is started from repo root in production; `restart_service.sh` enforces this |
| **Best-effort load** | A malformed YAML logs a warning and falls back to "no overrides"; the calendar still functions from python-holidays + weekday filter. Hard-failing here would prevent service startup, which is worse than degraded overrides |
| **One file per year, per concern** | Don't pile multiple years into one file. Easier to find, diff, and version |
| **Schema mirrors what `TradingCalendar._load_overrides` parses** | Schema changes here require coordinating with parser code |

## 3. File inventory

```
configs/
├── README.md
└── nse_holidays_2026.yaml
```

## 4. The override-file schema

```yaml
# configs/nse_holidays_2026.yaml
#
# NSE holiday overrides for 2026.
# python-holidays "IN" already covers federal holidays; this file is
# for exchange-specific additions and Muhurat trading days.
#
# Schema:
#   extra_holidays:        list of YYYY-MM-DD dates to TREAT AS CLOSED
#                          even if python-holidays says open.
#   special_trading_days:  list of YYYY-MM-DD dates that are NORMALLY
#                          closed (weekend/holiday) but the exchange
#                          opens for a special session (e.g. Muhurat
#                          on Diwali).

extra_holidays:
  - 2026-01-26          # Republic Day (also covered by IN holidays;
                        # listed here for completeness)
  - 2026-08-15          # Independence Day

special_trading_days:
  - 2026-11-12          # Muhurat Trading on Diwali (special session)
```

### 4.1 The two override types

| Field | Meaning | Override against |
|-------|---------|------------------|
| `extra_holidays` | Dates that should be **closed** even if python-holidays says open | python-holidays' "open by default" verdict |
| `special_trading_days` | Dates that should be **open** for a special session even though weekend/holiday | weekend filter + python-holidays' "closed" verdict |

### 4.2 The decision order (from `TradingCalendar.is_trading_day`)

```python
def is_trading_day(d: date) -> bool:
    # 1. Special-trading override — wins over EVERYTHING
    if d in self._override_special_trading:
        return True
    # 2. Weekday filter
    if d.isoweekday() not in self._config.weekdays:
        return False
    # 3. Explicit holiday override
    if d in self._override_holidays:
        return False
    # 4. Algorithmic holiday lookup
    if self._config.holidays_country is None:
        return True
    return d not in self._holidays_for_year(d.year)
```

`special_trading_days` wins over everything. `extra_holidays` wins
only against algorithmic holidays (it can't reopen a date that's a
weekend; that requires `special_trading_days`).

### 4.3 NSE-specific events the override handles

**Muhurat Trading.** On Diwali (typically a weekend in some years),
NSE opens for a special 60-90 minute session in the evening. Listed
in `special_trading_days`.

**Mid-week closures.** When the NSE board declares an extra holiday
(e.g. after a particularly disruptive market event, or for a
state-level event python-holidays doesn't model). Listed in
`extra_holidays`.

**Operator-asserted closures.** When the operator wants to keep
the service idle on a specific day (e.g. for planned maintenance),
add to `extra_holidays`. Service treats it as closed; no cycle runs.

## 5. How the override is loaded

`TradingCalendar._load_overrides()`:

```python
def _load_overrides(self) -> None:
    path = self._config.holidays_override_file
    if path is None or not path.exists():
        return     # no override configured
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; can't load %s", path)
        return
    try:
        data = yaml.safe_load(path.read_text())
        if not data:
            return
        for v in data.get("extra_holidays", []) or []:
            self._override_holidays.add(_coerce_date(v))
        for v in data.get("special_trading_days", []) or []:
            self._override_special_trading.add(_coerce_date(v))
    except Exception:
        logger.exception(
            "Failed to load holiday override file for %s; ignoring",
            self._config.exchange,
            extra={"path": str(path)},
        )
```

**Three soft-fail paths**, all logged but non-fatal:

1. **File doesn't exist** → silent skip. Allows the calendar to
   work with just python-holidays.
2. **PyYAML not installed** → WARNING log. Calendar still works
   without overrides.
3. **YAML parse / type error** → ERROR log via `logger.exception`.
   Calendar still works without overrides.

The strict alternative would be to raise on malformed YAML. That
would prevent service startup; degraded overrides is the lesser
evil.

### 5.1 `_coerce_date` flexibility

```python
def _coerce_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v)
    raise ValueError(f"can't coerce to date: {v!r}")
```

YAML's `safe_load` parses `2026-01-26` as a `datetime.date` directly
(YAML 1.1 type tags). Strings also accepted for fault tolerance —
some YAML files have `'2026-01-26'` quoted by accident.

## 6. Wiring from `config.yaml`

```yaml
# At the repo root: config.yaml

market_hours:
  - exchange: NSE
    tz: Asia/Kolkata
    open: "09:15"
    close: "15:30"
    weekdays: [1, 2, 3, 4, 5]
    holidays_country: IN
    holidays_override_file: configs/nse_holidays_2026.yaml
    # ... tick_schedule, etc.
```

`holidays_override_file` is the explicit reference. The path is
relative to CWD (the repo root in production).

### 6.1 Multiple exchanges, multiple overrides

```yaml
market_hours:
  - exchange: NSE
    holidays_country: IN
    holidays_override_file: configs/nse_holidays_2026.yaml
  - exchange: NYSE
    holidays_country: US
    holidays_override_file: configs/nyse_holidays_2026.yaml
```

Each exchange has its own override file. The TradingCalendar built
from each config entry loads independently.

## 7. Per-year file naming convention

```
nse_holidays_2025.yaml
nse_holidays_2026.yaml      ← current
nse_holidays_2027.yaml      ← future
```

Why per-year files:

- **Smaller diff at year boundary.** New year requires a new file,
  not edits to a long-living file.
- **Cherry-pick freedom.** Backporting a year-specific change
  doesn't risk other years.
- **Operator clarity.** "Which file do I edit for 2026?" is
  immediate from the filename.

The config-side reference must be updated at year boundary:

```yaml
# config.yaml — annual maintenance:
market_hours:
  - exchange: NSE
    holidays_override_file: configs/nse_holidays_2027.yaml   # bumped
```

Operator can also keep multiple years in one file by editing
schema to:

```yaml
extra_holidays:
  - 2026-01-26
  - 2027-01-26
special_trading_days:
  - 2026-11-12
  - 2027-11-01
```

The TradingCalendar caches per-year holiday lookups, so the
operational difference is negligible. Per-year files are the
recommended pattern for clarity.

## 8. Operator workflow — adding a new override

```bash
# 1. Edit the file
$EDITOR configs/nse_holidays_2026.yaml

# 2. Validate YAML
python -c "import yaml; print(yaml.safe_load(open('configs/nse_holidays_2026.yaml')))"

# 3. Validate date format
python -c "from datetime import date; date.fromisoformat('2026-MM-DD')"

# 4. Restart service
./scripts/restart_service.sh
```

After step 4, the calendar reflects the new override. The
SystemHealthView and SignalsView's exchange-state row will both
show "closed" (or "open") for the modified date.

### 8.1 Verification

```bash
# Via psql:
psql -d trip_ema -c "SELECT now()::date as today, 'is_trading_day' as fn;"

# Via service:
curl -s http://localhost:8000/v1/admin/exchanges \
    -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
    | jq '.exchanges[] | select(.name == "NSE") | .calendar'
```

The latter returns the parsed override list as JSON.

## 9. Failure modes

| Failure | Behaviour |
|---------|-----------|
| File path in config.yaml is wrong | Logged ERROR; calendar uses only python-holidays + weekdays. Calendar still works |
| YAML syntax error | Caught by `_load_overrides`; logged; treated as empty overrides |
| Date format wrong (e.g. `2026/01/26` instead of `2026-01-26`) | `date.fromisoformat` raises; `_coerce_date` re-raises; outer try-catch logs and treats as empty overrides |
| `special_trading_days` entry on a normal trading day | No-op; the date is already trading. Redundant but harmless |
| `extra_holidays` entry on a python-holidays-recognized holiday | No-op; the date is already closed. Redundant but harmless |
| Operator forgets to restart after edit | Old behaviour persists until next restart. Documented as "not hot-reloaded" |
| Operator edits the wrong year's file | Override silently ignored (no errors). Verify via the admin endpoint above |

## 10. Common pitfalls

1. **Not hot-reloaded.** Edit + restart, every time.
2. **Path is CWD-relative.** Run service from repo root.
   `restart_service.sh` enforces this.
3. **Year boundary requires both file update AND config update.**
   New file + bump the path in config.yaml.
4. **Quote-vs-unquoted dates parse differently in YAML.** Both
   work after `_coerce_date`, but stick to one style.
5. **`special_trading_days` doesn't override `extra_holidays`.**
   They're independent — a date in `extra_holidays` is closed; a
   date in `special_trading_days` is open. A date in both is
   open (special wins).
6. **Calendar caches per-year on first lookup.** If you edit the
   file at noon and the calendar already cached today's year, the
   change isn't visible until restart.
7. **YAML lists not in YAML format.** `extra_holidays: 2026-01-26`
   (single value, not a list) parses as a string, not a date.
   `extra_holidays: [2026-01-26]` or the multi-line `- 2026-01-26`.

## 11. Extension paths

- **A new year file.** Copy the template, populate, update
  `config.yaml`.
- **A new exchange.** Add `nyse_holidays_2026.yaml`,
  `lse_holidays_2026.yaml`, etc. Each exchange's
  `market_hours` entry references its own.
- **A new override category.** If `extra_holidays` /
  `special_trading_days` are insufficient (e.g. early-close days
  for NYSE half-days), add a new YAML key + handler in
  `TradingCalendar._load_overrides`. Document the schema.
- **A per-exchange parameter override file.** Today exchange
  overrides live inline in `config.yaml`. If they grow, a
  separate `configs/nse_indicator_params.yaml` could carry them.

## 12. Why these files live here, not in the package tree

`trip_ema/etl/nse_holidays_2026.yaml` was the original location.
Phase 14 moved it here because:

1. **Operator-edited, not developer-edited.** Belongs in a
   directory operators expect to look in.
2. **Outside the wheel.** `trip_ema/` ships as a Python wheel;
   the YAML doesn't belong in site-packages.
3. **Version-controlled separately.** Operators can git-diff the
   configs directory for their year's changes without scanning
   the source tree.

`trip_ema/etl/nse_holidays_2026.yaml` (source code copy) is the
**legacy location**, retained for backward compatibility with
configs that reference the old path. New deployments should
reference `configs/`.

## 13. Related packages

- `trip_ema/scheduler/market_calendar.py` — `TradingCalendar`
  loads these files.
- `trip_ema/etl/holidays_calendar.py` — legacy `NseTradingCalendar`
  with its own override-loading code; same schema.
- `trip_ema/config/schema.py` — `MarketHoursConfig.holidays_override_file`
  Pydantic field.
- `trip_ema/etl/nse_holidays_override.yaml` — the legacy default
  file (preserved for old deployments).
- `config.yaml` (repo root) — the top-level config that
  references these files.
