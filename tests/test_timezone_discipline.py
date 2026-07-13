"""The timezone guard. Fails the build on a NEW naked clock or hardcoded zone.

WHY

Every timezone bug in this project had one shape: code assuming the MACHINE's
timezone is the MARKET's timezone. On a NUC set to Asia/Kolkata running NSE that
assumption is invisibly true — which is exactly why it survived across three
compute modules, the live orchestrator (writing signal_date, part of a UNIQUE
key), an HTTP route's default parameter, two SQL snapshots, the email composer and
three GUI views.

Fixing them was easy. KEEPING them fixed is not, and a convention will not do it:
the next `date.today()` reintroduces the bug and nothing notices, because on this
machine, for this exchange, it works.

So the ban is a test.

WHY AST, NOT GREP

A grep would fire on the ~20 comments and docstrings that legitimately mention
Asia/Kolkata — including the comments explaining why the literal was removed.
Writing these fixes I hit that trap ELEVEN separate times: a sentinel that was a
substring of the thing it replaced; a count that included the comment describing
the clause being counted; a `next()` that grabbed a docstring because the
docstring explains `date_trunc`.

The rule, generally: never check code with a substring scan over text that also
contains prose describing that code. Walk the tree. String constants are code;
docstrings and comments are not.

ALIAS TRACKING

gui/views/signals.py does `from zoneinfo import ZoneInfo as _ZI` and calls
`_ZI("Asia/Kolkata")`. A guard matching the NAME "ZoneInfo" misses it entirely.
A guard with a hole is worse than no guard, because it is trusted. Imports are
resolved first, so any local alias is caught.

THE BASELINE IS A RATCHET, NOT AN AMNESTY

28 sites remain. They are grandfathered so this can land TODAY and stop the
bleeding; the test fails on any NEW violation, or on an increased count in a
listed file. The list only shrinks — test_baseline_is_not_stale enforces that.

Do not add to it. Every call site that needs "now" has `exchange` in scope, or a
db_engine from which exchange_of_engine() derives it. That was true at all twelve
sites already fixed, and not one needed a new parameter threaded more than a
single call.
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "trip_ema"

# The one module allowed to name a market's timezone: it IS the registry.
TZ_AUTHORITY = {"market_session.py"}

# ---------------------------------------------------------------- baseline
#
# Grandfathered violations, by file. The number is a CEILING.
#
# The burn-down list, in priority order:
#
#   notifications/*   ZoneInfo — decides WHICH DAY's signals get emailed. Needs
#                     `exchange` threaded from the subscriber's rows. Not
#                     cosmetic: it is the product. FIX NEXT.
#   gui/views/*       ZoneInfo — renders bar timestamps. The GUI knows its
#                     exchange (one database per exchange).
#   etl/runner.py     date.today — fetch windows. On the NUC, that is IST today.
#   fundamentals/cli, transports/.../fundamentals.py, backfill/pipeline.py
#                     date.today — CLI and route defaults for date ranges.
#   gui/csv_export, settings, system_health, subscribers, notifications
#                     datetime.now — filename and display timestamps. These are
#                     genuinely MACHINE time and may well stay; listed so the
#                     count cannot creep.
#   archive/*         dead code, on no path.
#
_BASELINE: dict[str, int] = {
    "trip_ema/archive/index_value_backfill.py": 1,
    "trip_ema/backfill/pipeline.py": 1,
    "trip_ema/etl/runner.py": 4,
    "trip_ema/etl/yfinance_source.py": 1,
    "trip_ema/fundamentals/cli.py": 3,
    "trip_ema/fundamentals/market_cap_fetch.py": 1,
    "trip_ema/fundamentals/monthly_recheck.py": 1,
    "trip_ema/gui/csv_export.py": 1,
    "trip_ema/gui/views/notifications.py": 1,
    "trip_ema/gui/views/settings.py": 1,
    "trip_ema/gui/views/subscribers.py": 1,
    "trip_ema/gui/views/system_health.py": 1,
    "trip_ema/gui/views/tickers.py": 1,
    "trip_ema/gui/views/widgets/backfill_dialog.py": 2,
    "trip_ema/gui/views/widgets/backfill_shares_dialog.py": 1,
    "trip_ema/transports/http/routes/fundamentals.py": 2,
}


class _ClockVisitor(ast.NodeVisitor):
    """Finds naked clock reads and hardcoded market timezones — in CODE only."""

    def __init__(self, tree: ast.AST, filename: str):
        self.hits: list[tuple[int, str]] = []
        self._authority = Path(filename).name in TZ_AUTHORITY
        # Resolve aliases FIRST. `from zoneinfo import ZoneInfo as _ZI` slips
        # past any guard that only knows the name "ZoneInfo".
        self._zi_names = {"ZoneInfo"}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "zoneinfo":
                for a in n.names:
                    if a.name == "ZoneInfo":
                        self._zi_names.add(a.asname or a.name)

    def visit_Call(self, node: ast.Call) -> None:
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        bare = not node.args and not node.keywords

        if name == "now" and bare:
            self.hits.append((
                node.lineno,
                "datetime.now() has no timezone — it reads the MACHINE's clock. "
                "Use datetime.now(market_tz(exchange)), or datetime.now(UTC) if "
                "the instant is genuinely absolute.",
            ))
        elif name == "today" and bare:
            self.hits.append((
                node.lineno,
                "date.today() is the MACHINE's date, not the market's. For NYSE "
                "it is wrong for 10.5 hours out of every 24. Use "
                "datetime.now(market_tz(exchange)).date().",
            ))
        elif name == "utcnow":
            self.hits.append((
                node.lineno,
                "datetime.utcnow() is naive by construction (and deprecated in "
                "3.12). Use datetime.now(UTC).",
            ))

        if name in self._zi_names and not self._authority:
            for a in node.args:
                if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                        and a.value != "UTC"):
                    self.hits.append((
                        node.lineno,
                        f"ZoneInfo({a.value!r}) hardcodes a market's timezone. "
                        f"market_session.py's _EXCHANGE_SPECS is the single "
                        f"authority — use market_tz(exchange).",
                    ))
        self.generic_visit(node)


def _scan_source(src: str, filename: str) -> list[tuple[int, str]]:
    tree = ast.parse(src, filename=filename)
    v = _ClockVisitor(tree, filename)
    v.visit(tree)
    return v.hits


def _scan_pkg() -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for p in PKG.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(PKG.parent))
        hits = _scan_source(p.read_text(encoding="utf-8"), rel)
        if hits:
            out[rel] = hits
    return out


def test_no_new_machine_clock_dependency():
    """No NEW code may ask the machine what time it is, or name a market's tz."""
    found = _scan_pkg()

    new_files = sorted(set(found) - set(_BASELINE))
    regressed = sorted(
        f for f in found
        if f in _BASELINE and len(found[f]) > _BASELINE[f]
    )

    problems: list[str] = []
    for f in new_files:
        problems.append(f"NEW FILE: {f}")
        for lineno, why in found[f]:
            problems.append(f"    {f}:{lineno}\n      {why}")
    for f in regressed:
        problems.append(
            f"REGRESSED: {f} — baseline allows {_BASELINE[f]}, "
            f"found {len(found[f])}"
        )
        for lineno, why in found[f]:
            problems.append(f"    {f}:{lineno}\n      {why}")

    assert not problems, (
        "\n\nThe machine's time is not the market's time.\n\n"
        + "\n".join(problems)
        + "\n\nEvery call site that needs 'now' has `exchange` in scope, or a "
          "db_engine\nfrom which exchange_of_engine() can derive it. Use "
          "market_tz(exchange).\nDo NOT add to _BASELINE.\n"
    )


def test_baseline_is_not_stale():
    """The baseline may only shrink.

    A grandfather list that outlives its entries lies about the debt — and
    silently re-permits a violation someone already paid to remove.
    """
    found = _scan_pkg()
    stale = sorted(f for f in _BASELINE if f not in found)
    assert not stale, (
        "\n\nThese files are clean now — delete them from _BASELINE:\n\n  "
        + "\n  ".join(stale)
        + "\n\nLeaving them re-permits a violation that was already fixed.\n"
    )
    loose = sorted(
        f"{f}: baseline {_BASELINE[f]}, actual {len(found[f])}"
        for f in _BASELINE
        if f in found and len(found[f]) < _BASELINE[f]
    )
    assert not loose, (
        "\n\nBaseline looser than reality — tighten it so the count cannot creep "
        "back:\n\n  " + "\n  ".join(loose) + "\n"
    )


def test_the_guard_actually_catches_things():
    """A guard that cannot fail is not a guard.

    Proves the walk sees CODE — and, just as importantly, does NOT fire on a
    docstring or comment merely MENTIONING the forbidden thing. That is the
    substring-vs-prose trap this whole approach exists to avoid.
    """
    bad = (
        "from datetime import date, datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "from zoneinfo import ZoneInfo as _ZI\n"
        "def f():\n"
        "    a = datetime.now()\n"
        "    b = date.today()\n"
        "    c = datetime.utcnow()\n"
        "    d = ZoneInfo('Asia/Kolkata')\n"
        "    e = _ZI('America/New_York')\n"
        "    return a, b, c, d, e\n"
    )
    hits = _scan_source(bad, "bad.py")
    kinds = set()
    for _, w in hits:
        if "datetime.now()" in w:
            kinds.add("now")
        elif "date.today()" in w:
            kinds.add("today")
        elif "utcnow" in w:
            kinds.add("utcnow")
        elif "ZoneInfo(" in w:
            kinds.add("zone")
    assert kinds == {"now", "today", "utcnow", "zone"}, f"guard missed: {kinds}"
    assert sum(1 for _, w in hits if "ZoneInfo(" in w) == 2, (
        "the aliased ZoneInfo (_ZI) slipped through — a guard with a hole is "
        "worse than no guard, because it is trusted"
    )

    good = (
        '"""Docstring naming datetime.now(), date.today() and '
        'ZoneInfo("Asia/Kolkata").\n\n'
        "Prose is not code. Grepping for these strings is exactly the mistake\n"
        "this guard exists to avoid.\n"
        '"""\n'
        "from datetime import UTC, datetime\n"
        "def f(exchange):\n"
        "    # A comment naming date.today() and Asia/Kolkata. Also not code.\n"
        "    a = datetime.now(market_tz(exchange))\n"
        "    b = datetime.now(UTC)\n"
        "    c = datetime.now(tz=market_tz(exchange)).date()\n"
        "    return a, b, c\n"
    )
    assert _scan_source(good, "good.py") == [], (
        "the guard fired on a DOCSTRING or COMMENT that merely mentions the "
        "forbidden thing"
    )
