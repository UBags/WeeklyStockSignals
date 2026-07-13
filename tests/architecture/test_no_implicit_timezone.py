"""Source guard: no implicit timezone anywhere in trip_ema.  # L5-GUARD-ACTUALLY-RUNS

Every timezone bug in this codebase was written by someone who knew the rule and
had no mechanism. This is the mechanism.

Four rules, each one the shape of a bug that actually shipped:

  1. `timestamptz::date` with no `AT TIME ZONE` — a silent read of the session
     clock. Five sites, all "correct" because the server's TZ is Asia/Kolkata.
  2. `CURRENT_DATE` / `CURRENT_TIMESTAMP` — the same clock, wearing a keyword.
     market_overview_daily was WRITTEN with (NOW() AT TIME ZONE :tz)::date and
     READ with CURRENT_DATE: two clocks, one column.
  3. A bare `.date()` on a timestamp read back from the database, or on an instant
     whose zone the caller chose — psycopg hands back an aware datetime in the
     session zone, and `datetime.now(UTC).date()` is the UTC date, not the
     market's.
  4. `datetime.combine(..., tzinfo=UTC)` — midnight UTC as a stand-in for a
     trading date. Invertible only for zones east of UTC: correct for India,
     wrong for New York.

The sanctioned spellings: `AT TIME ZONE :tz` in SQL, `trading_date_of(ts, exchange)`
and `bar_anchor(d, exchange)` in Python. See domain/market_session.py.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "trip_ema"

# Columns/values that are instants. Casting one to a date, or calling .date() on
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
    return sorted(p for p in PKG.rglob("*.py") if "archive" not in p.parts)


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments and docstrings removed.

    Prose is allowed to DESCRIBE the bug — several modules carry a post-mortem in
    a comment — and a guard that cannot tell an explanation from an occurrence is
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


def _violations(rule) -> list[str]:
    hits: list[str] = []
    for path in _all_sources():
        for lineno, line in _code_lines(path):
            if rule(line):
                hits.append(f"{path.relative_to(PKG.parent)}:{lineno}: {line}")
    return hits


def _utc_midnight_anchors(sources=None) -> list[str]:
    """Every `datetime.combine(..., tzinfo=UTC)` in the package.

    An AST walk, not a line regex, and the distinction is load-bearing:
    `combine(d, datetime.min.time(), tzinfo=UTC)` closes a paren inside the call,
    so a regex like combine-open-paren-not-paren-star-tzinfo never matches it — and the multi-line
    spelling is invisible to any line-based rule. A guard that fails OPEN is worse
    than no guard, because it is trusted.
    """
    hits: list[str] = []
    for path in (sources if sources is not None else _all_sources()):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                     else getattr(node.func, "id", ""))
            if fname != "combine":
                continue
            for kw in node.keywords:
                if kw.arg != "tzinfo":
                    continue
                vname = (kw.value.attr if isinstance(kw.value, ast.Attribute)
                         else getattr(kw.value, "id", ""))
                if vname == "UTC":
                    hits.append(f"{path.name}:{node.lineno}: combine(..., tzinfo=UTC)")
    return hits


# domain/market_session.py legitimately builds naive datetimes: combine(
# date(2000, 1, 1), open_time) exists only to subtract two times and get a session
# LENGTH. A duration has no timezone. Nothing else in the package has that excuse.
_NAIVE_COMBINE_ALLOWED = {"market_session.py"}


def _naive_combines(sources=None) -> list[str]:
    """Every `datetime.combine(...)` that passes no tzinfo."""
    hits: list[str] = []
    for path in (sources if sources is not None else _all_sources()):
        if path.name in _NAIVE_COMBINE_ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                     else getattr(node.func, "id", ""))
            if fname != "combine":
                continue
            if any(kw.arg == "tzinfo" for kw in node.keywords):
                continue
            hits.append(f"{path.name}:{node.lineno}: datetime.combine() with no tzinfo")
    return hits


class TestNoImplicitTimezone:

    def test_no_unqualified_timestamptz_to_date_cast(self):
        def rule(line: str) -> bool:
            if "::date" not in line:
                return False
            if not any(c in line for c in TZ_COLUMNS):
                return False
            return "AT TIME ZONE" not in line

        hits = _violations(rule)
        assert not hits, (
            "timestamptz::date is evaluated in the SESSION timezone. Say which "
            "zone you mean: (col AT TIME ZONE :tz)::date" + NL + NL.join(hits)
        )

    def test_no_current_date(self):
        hits = _violations(
            lambda line: "CURRENT_DATE" in line or "CURRENT_TIMESTAMP" in line
        )
        assert not hits, (
            "CURRENT_DATE reads the session clock, which is not the market's. "
            "Use (NOW() AT TIME ZONE :tz)::date." + NL + NL.join(hits)
        )

    def test_no_bare_date_on_an_instant(self):
        def rule(line: str) -> bool:
            return any(f"{c}.date()" in line for c in TZ_COLUMNS)

        hits = _violations(rule)
        assert not hits, (
            "A bare .date() reads whatever zone the datetime happens to carry — "
            "the session's, from psycopg, or UTC, from datetime.now(UTC). Neither "
            "is the market's. Use trading_date_of(ts, exchange)." + NL + NL.join(hits)
        )

    def test_no_naive_datetime_combine(self):
        """Rule 5: combine() with NO tzinfo at all.

        The sneakiest of the five, because it does not name a WRONG zone — it names
        no zone, and lets whoever is downstream pick. Bound against a timestamptz,
        Postgres picks the SESSION's, and market_cap_compute's day window was
        thereby a full day off in the wrong direction: it selected the NEXT trading
        day's close and computed market cap with lookahead.

        The four original rules all missed it. This is the one that would not have.
        """
        hits = _naive_combines()
        assert not hits, (
            "A naive datetime names no instant. Bound against a timestamptz it is "
            "coerced using the SESSION timezone — a clock nobody chose. Use "
            "bar_anchor / week_anchor / day_bounds / range_bounds." + NL
            + NL.join(hits)
        )

    def test_no_utc_midnight_as_a_trading_date(self):
        hits = _utc_midnight_anchors()
        assert not hits, (
            "Midnight UTC is invertible only for zones EAST of UTC — correct for "
            "India, wrong for New York. Use bar_anchor(d, exchange) / "
            "week_anchor(d, exchange)." + NL + NL.join(hits)
        )


class TestTheGuardCanActuallyFail:
    """A guard that cannot fail is decoration — and this one shipped as a
    SyntaxError, which is a guard that cannot even run. Each rule is exercised
    here against the exact code that was in the tree before the fix."""

    @pytest.mark.parametrize("line", [
        "SELECT DISTINCT b.bar_timestamp::date AS d",
        "SELECT MIN(b.bar_timestamp)::date AS min_date",
        "ON o.bar_timestamp::date = wa.today_d",
        "AND bar_timestamp::date BETWEEN :start AND :end",
    ])
    def test_rejects_the_unqualified_casts(self, line):
        assert "::date" in line
        assert any(c in line for c in TZ_COLUMNS)
        assert "AT TIME ZONE" not in line

    def test_rejects_the_current_date_read(self):
        assert "CURRENT_DATE" in ">= CURRENT_DATE - CAST(:days AS integer)"

    @pytest.mark.parametrize("line", [
        "trading_date=r.bar_timestamp.date(),",
        "to_date = as_of.date()",
        "to_date=as_of.date() if as_of is not None else None,",
    ])
    def test_rejects_the_bare_date_calls(self, line):
        assert any(f"{c}.date()" in line for c in TZ_COLUMNS)

    @pytest.mark.parametrize("src", [
        # Nested parens: the first `)` belongs to time(), which is exactly why the
        # line-regex version of this rule silently passed the real bug.
        "import datetime\nts = datetime.combine(w.trading_date, datetime.min.time(), tzinfo=UTC)\n",
        # Multi-line: invisible to any line-based rule.
        "import datetime\nx = datetime.combine(\n    from_monday, time(0, 0), tzinfo=UTC,\n)\n",
    ])
    def test_rejects_the_utc_midnight_anchor(self, src, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text(src)
        assert _utc_midnight_anchors([f]), "the guard fails OPEN on this shape"

    def test_rejects_the_naive_day_window(self, tmp_path):
        """The exact two lines that produced the market-cap lookahead.

        Built with NL.join and no escape sequences: three separate rounds of this
        workstream were lost to a backslash-n surviving one layer of quoting too
        many, and the last one shipped a guard that could not parse. A constant is
        not clever, and it cannot be mis-escaped.
        """
        f = tmp_path / "bad.py"
        f.write_text(NL.join([
            "import datetime",
            "day_start = _dt.combine(trade_date, _dt.min.time())",
            "day_end = _dt.combine(trade_date, _dt.max.time())",
            "",
        ]))
        assert len(_naive_combines([f])) == 2

    def test_accepts_a_market_local_anchor(self, tmp_path):
        f = tmp_path / "good.py"
        f.write_text("import datetime\nx = datetime.combine(d, time.min, tzinfo=zone)\n")
        assert not _utc_midnight_anchors([f])

    def test_accepts_the_sanctioned_spellings(self):
        for line in (
            "SELECT DISTINCT (b.bar_timestamp AT TIME ZONE :tz)::date AS d",
            "trading_date=trading_date_of(r.bar_timestamp, _ex),",
            "ts = bar_anchor(w.trading_date, _ex)",
        ):
            assert not ("::date" in line
                        and any(c in line for c in TZ_COLUMNS)
                        and "AT TIME ZONE" not in line)
            assert not any(f"{c}.date()" in line for c in TZ_COLUMNS)
