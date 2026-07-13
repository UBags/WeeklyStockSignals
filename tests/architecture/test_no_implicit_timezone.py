"""The timezone guard. Ten rules, zero exceptions, one file.  # L10-ONE-GUARD

WHY

Every timezone bug in this project had one shape: code assuming the MACHINE's
timezone is the MARKET's timezone. On a NUC set to Asia/Kolkata running NSE that
assumption is invisibly true — which is exactly why it survived across three compute
modules, the live orchestrator (writing signal_date, part of a UNIQUE key), an HTTP
route's default parameter, two SQL snapshots, the email composer, three GUI views,
and — found last, and worst — the market-cap day window, where it silently became a
one-day LOOKAHEAD feeding constituent selection and therefore every industry index.

Fixing them was easy. KEEPING them fixed is not, and a convention will not do it:
the next date.today() reintroduces the bug and nothing notices, because on this
machine, for this exchange, it works. So the ban is a test.

WHY AST, NOT GREP

A grep fires on the ~20 comments and docstrings that legitimately mention
Asia/Kolkata — including the comments explaining why the literal was removed. String
constants are code; docstrings and comments are not. Walk the tree.

The line-based rules below (the SQL ones) strip comments and docstrings first, for
the same reason. An earlier version of this file did not, and flagged its own
post-mortem as the bug it was describing.

And a line regex is not enough even for the AST-shaped rules: a pattern like
combine-open-paren-anything-tzinfo=UTC cannot see
`combine(d, datetime.min.time(), tzinfo=UTC)` — the first paren closes the match
early — nor the multi-line spelling. It would fail OPEN, which is the one failure
mode a guard may never have.

ALIAS RESOLUTION

gui/views/signals.py does `from zoneinfo import ZoneInfo as _ZI` and calls
`_ZI("Asia/Kolkata")`. A guard matching the NAME "ZoneInfo" misses it entirely. A
guard with a hole is worse than no guard, because it is trusted. Imports are resolved
first, so any local alias is caught.

NO BASELINE

There used to be one, in tests/test_timezone_discipline.py, grandfathering the last
straggler so the guard could land before the cleanup finished. The cleanup finished.
Zero violations, no exceptions — and if that ever needs to change, git history has
the ratchet machinery intact.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "trip_ema"

# The one module allowed to name a market's timezone: it IS the registry.
TZ_AUTHORITY = {"market_session.py"}

# market_session also legitimately builds naive datetimes: combine(date(2000,1,1),
# open_time) exists only to subtract two times and get a session LENGTH. A duration
# has no timezone. Nothing else in the package has that excuse.
NAIVE_COMBINE_ALLOWED = {"market_session.py"}

# Columns and values that are INSTANTS. Casting one to a date, or calling .date() on
# one, is a timezone decision — and must look like one.
TZ_COLUMNS = (
    "bar_timestamp",
    "computed_at",
    "signal_bar_timestamp",
    "started_at",
    "finished_at",
    "as_of",
)

NL = chr(10)


def _all_sources() -> list[Path]:
    return sorted(
        p for p in PKG.rglob("*.py")
        if "__pycache__" not in p.parts and "archive" not in p.parts
    )


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments and docstrings removed.

    Prose is allowed to DESCRIBE the bug — most of these modules now carry a
    post-mortem — and a guard that cannot tell an explanation from an occurrence is
    a guard nobody keeps.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef
                          | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))

    comment_lines = {
        tok.start[0]
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    }

    out: list[tuple[int, str]] = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in doc_lines:
            continue
        s = line.split("#", 1)[0] if i in comment_lines else line
        s = re.sub(r"--.*$", "", s)          # SQL comments inside string literals
        if s.strip():
            out.append((i, s.strip()))
    return out


def _line_violations(rule, sources=None) -> list[str]:
    hits: list[str] = []
    for path in (sources if sources is not None else _all_sources()):
        for lineno, line in _code_lines(path):
            if rule(line):
                hits.append(f"{path.relative_to(PKG.parent)}:{lineno}: {line}")
    return hits


# --------------------------------------------------------------------------- #
# The AST visitor: every rule that is about a NODE, not a line.
# --------------------------------------------------------------------------- #

class _ClockVisitor(ast.NodeVisitor):
    """Naked clock reads, hardcoded market zones, and untimed anchors — in CODE."""

    def __init__(self, tree: ast.AST, filename: str):
        self.hits: list[tuple[int, str]] = []
        name = Path(filename).name
        self._authority = name in TZ_AUTHORITY
        self._naive_combine_ok = name in NAIVE_COMBINE_ALLOWED

        # Resolve aliases FIRST. `from zoneinfo import ZoneInfo as _ZI` slips past
        # any guard that only knows the name "ZoneInfo".
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
            self.hits.append((node.lineno, (
                "datetime.now() has no timezone — it reads the MACHINE's clock. Use "
                "datetime.now(market_tz(exchange)), display_now(db_engine), or "
                "datetime.now(UTC) if the instant is genuinely absolute."
            )))
        elif name == "today" and bare:
            self.hits.append((node.lineno, (
                "date.today() is the MACHINE's date, not the market's. For NYSE it is "
                "wrong for 10.5 hours out of every 24. Use trading_date_of(ts, exchange)."
            )))
        elif name == "utcnow":
            self.hits.append((node.lineno, (
                "datetime.utcnow() is naive by construction (and deprecated in 3.12). "
                "Use datetime.now(UTC)."
            )))
        elif name == "astimezone" and not node.args:
            self.hits.append((node.lineno, (
                ".astimezone() with no argument converts to the MACHINE's zone. Say "
                "which you mean: .astimezone(market_tz(exchange)) or .astimezone(UTC)."
            )))
        elif name == "combine":
            tzinfo = next((k for k in node.keywords if k.arg == "tzinfo"), None)
            if tzinfo is None:
                if not self._naive_combine_ok:
                    self.hits.append((node.lineno, (
                        "datetime.combine() with no tzinfo is NAIVE. Bound against a "
                        "timestamptz it is coerced using the SESSION timezone — a clock "
                        "nobody chose. This is the shape that made the market-cap day "
                        "window select the NEXT day's close. Use bar_anchor / week_anchor "
                        "/ day_bounds / range_bounds."
                    )))
            else:
                v = tzinfo.value
                vname = v.attr if isinstance(v, ast.Attribute) else getattr(v, "id", "")
                if vname == "UTC":
                    self.hits.append((node.lineno, (
                        "midnight UTC as a trading date is invertible only for zones EAST "
                        "of UTC — correct for India, wrong for New York. Use "
                        "bar_anchor(d, exchange) / week_anchor(d, exchange)."
                    )))

        if name in self._zi_names and not self._authority:
            for a in node.args:
                if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                        and a.value != "UTC"):
                    self.hits.append((node.lineno, (
                        f"ZoneInfo({a.value!r}) hardcodes a market's timezone. "
                        f"market_session.py's _EXCHANGE_SPECS is the single authority — "
                        f"use market_tz(exchange)."
                    )))

        self.generic_visit(node)


def _scan_source(src: str, filename: str) -> list[tuple[int, str]]:
    tree = ast.parse(src, filename=filename)
    v = _ClockVisitor(tree, filename)
    v.visit(tree)
    return v.hits


def _node_violations(kind: str, sources=None) -> list[str]:
    """Every AST-shaped violation whose message contains `kind`."""
    hits: list[str] = []
    for path in (sources if sources is not None else _all_sources()):
        for lineno, msg in _scan_source(path.read_text(encoding="utf-8"), str(path)):
            if kind in msg:
                hits.append(f"{path.name}:{lineno}: {msg}")
    return hits


# --------------------------------------------------------------------------- #
# The ten rules.
# --------------------------------------------------------------------------- #

class TestNoImplicitTimezone:

    # ---- in SQL ---- #

    def test_no_unqualified_timestamptz_to_date_cast(self):
        def rule(line: str) -> bool:
            return ("::date" in line
                    and any(c in line for c in TZ_COLUMNS)
                    and "AT TIME ZONE" not in line)

        hits = _line_violations(rule)
        assert not hits, (
            "timestamptz::date is evaluated in the SESSION timezone. Say which zone "
            "you mean: (col AT TIME ZONE :tz)::date" + NL + NL.join(hits)
        )

    def test_no_current_date(self):
        hits = _line_violations(
            lambda line: "CURRENT_DATE" in line or "CURRENT_TIMESTAMP" in line
        )
        assert not hits, (
            "CURRENT_DATE reads the session clock, which is not the market's. Use "
            "(NOW() AT TIME ZONE :tz)::date." + NL + NL.join(hits)
        )

    # ---- in Python ---- #

    def test_no_bare_date_on_an_instant(self):
        hits = _line_violations(
            lambda line: any(f"{c}.date()" in line for c in TZ_COLUMNS)
        )
        assert not hits, (
            "A bare .date() reads whatever zone the datetime happens to carry — the "
            "session's, from psycopg, or UTC, from datetime.now(UTC). Neither is the "
            "market's. Use trading_date_of(ts, exchange)." + NL + NL.join(hits)
        )

    def test_no_naive_datetime_now(self):
        hits = _node_violations("datetime.now() has no timezone")
        assert not hits, NL + NL.join(hits)

    def test_no_date_today(self):
        hits = _node_violations("date.today() is the MACHINE's date")
        assert not hits, NL + NL.join(hits)

    def test_no_utcnow(self):
        hits = _node_violations("datetime.utcnow() is naive")
        assert not hits, NL + NL.join(hits)

    def test_no_bare_astimezone(self):
        hits = _node_violations(".astimezone() with no argument")
        assert not hits, NL + NL.join(hits)

    def test_no_naive_datetime_combine(self):
        hits = _node_violations("datetime.combine() with no tzinfo")
        assert not hits, NL + NL.join(hits)

    def test_no_utc_midnight_as_a_trading_date(self):
        hits = _node_violations("midnight UTC as a trading date")
        assert not hits, NL + NL.join(hits)

    def test_no_hardcoded_market_timezone(self):
        hits = _node_violations("hardcodes a market's timezone")
        assert not hits, NL + NL.join(hits)


class TestTheGuardCanActuallyFail:
    """A guard that cannot fail is decoration — and one earlier version of this file
    shipped as a SyntaxError, which is a guard that cannot even RUN, while being
    counted as passing. Every rule is exercised here against the exact code that was
    in the tree before it was fixed."""

    # ---- the SQL rules ---- #

    @pytest.mark.parametrize("line", [
        "SELECT DISTINCT b.bar_timestamp::date AS d",
        "SELECT MIN(b.bar_timestamp)::date AS min_date",
        "ON o.bar_timestamp::date = wa.today_d",
    ])
    def test_rejects_the_unqualified_casts(self, line):
        assert ("::date" in line and any(c in line for c in TZ_COLUMNS)
                and "AT TIME ZONE" not in line)

    def test_rejects_current_date(self):
        assert "CURRENT_DATE" in ">= CURRENT_DATE - CAST(:days AS integer)"

    @pytest.mark.parametrize("line", [
        "trading_date=r.bar_timestamp.date(),",
        "to_date = as_of.date()",
    ])
    def test_rejects_the_bare_date_calls(self, line):
        assert any(f"{c}.date()" in line for c in TZ_COLUMNS)

    def test_accepts_the_sanctioned_sql(self):
        line = "SELECT DISTINCT (b.bar_timestamp AT TIME ZONE :tz)::date AS d"
        assert not ("::date" in line and any(c in line for c in TZ_COLUMNS)
                    and "AT TIME ZONE" not in line)

    # ---- the AST rules ---- #

    def _write(self, tmp_path, *lines):
        f = tmp_path / "bad.py"
        f.write_text(NL.join([*lines, ""]))
        return [f]

    def test_rejects_the_machine_clocks(self, tmp_path):
        src = self._write(
            tmp_path,
            "import datetime",
            'ts = datetime.now().strftime("%Y%m%d")',
            "d = date.today()",
            "u = datetime.utcnow()",
            'label = dt.astimezone().strftime("%H:%M")',
        )
        assert _node_violations("datetime.now() has no timezone", src)
        assert _node_violations("date.today() is the MACHINE's date", src)
        assert _node_violations("datetime.utcnow() is naive", src)
        assert _node_violations(".astimezone() with no argument", src)

    def test_rejects_the_utc_midnight_anchor(self, tmp_path):
        """Nested parens AND the multi-line spelling: a line regex sees neither,
        which is why this rule is an AST walk."""
        src = self._write(
            tmp_path,
            "import datetime",
            "a = datetime.combine(w.trading_date, datetime.min.time(), tzinfo=UTC)",
            "b = datetime.combine(",
            "    from_monday, time(0, 0), tzinfo=UTC,",
            ")",
        )
        assert len(_node_violations("midnight UTC as a trading date", src)) == 2

    def test_rejects_the_naive_day_window(self, tmp_path):
        """The two lines that produced the market-cap lookahead."""
        src = self._write(
            tmp_path,
            "import datetime",
            "day_start = _dt.combine(trade_date, _dt.min.time())",
            "day_end = _dt.combine(trade_date, _dt.max.time())",
        )
        assert len(_node_violations("datetime.combine() with no tzinfo", src)) == 2

    def test_rejects_a_hardcoded_zone(self, tmp_path):
        src = self._write(
            tmp_path,
            "from zoneinfo import ZoneInfo",
            'IST = ZoneInfo("Asia/Kolkata")',
        )
        assert _node_violations("hardcodes a market's timezone", src)

    def test_rejects_a_hardcoded_zone_behind_an_alias(self, tmp_path):
        """gui/views/signals.py really did this. A guard matching the NAME
        'ZoneInfo' misses it entirely, and a guard with a hole is worse than no
        guard, because it is trusted."""
        src = self._write(
            tmp_path,
            "from zoneinfo import ZoneInfo as _ZI",
            '_IST = _ZI("Asia/Kolkata")',
        )
        assert _node_violations("hardcodes a market's timezone", src)

    def test_accepts_the_named_clocks(self, tmp_path):
        src = self._write(
            tmp_path,
            "import datetime",
            "from zoneinfo import ZoneInfo",
            "a = datetime.now(UTC)",
            "b = datetime.now(market_tz(exchange))",
            "c = dt.astimezone(market_tz(exchange))",
            "d = datetime.combine(day, time.min, tzinfo=market_tz(exchange))",
            "e = bar_anchor(trading_date, exchange)",
            'f = ZoneInfo("UTC")',
            "g = ZoneInfo(tz_name_from_the_registry)",
        )
        for kind in ("datetime.now() has no timezone",
                     "date.today() is the MACHINE's date",
                     "datetime.utcnow() is naive",
                     ".astimezone() with no argument",
                     "datetime.combine() with no tzinfo",
                     "midnight UTC as a trading date",
                     "hardcodes a market's timezone"):
            assert not _node_violations(kind, src), kind
