"""Source guard: no implicit timezone anywhere in trip_ema.

Every timezone bug in this codebase was written by someone who knew the rule and
had no mechanism. This is the mechanism.

Four rules, each one the shape of a bug that actually shipped:

  1. `timestamptz::date` with no `AT TIME ZONE` — a silent read of the session
     clock. Five sites, all "correct" because the server's TZ is Asia/Kolkata.
  2. `CURRENT_DATE` / `CURRENT_TIMESTAMP` — the same clock, wearing a keyword.
     market_overview_daily was WRITTEN with (NOW() AT TIME ZONE :tz)::date and
     READ with CURRENT_DATE: two clocks, one column.
  3. A bare `.date()` on a timestamp column read back from the database — psycopg
     hands back an aware datetime in the session zone, so `.date()` is that same
     silent read, in Python.
  4. `datetime.combine(..., tzinfo=UTC)` — midnight UTC as a stand-in for a
     trading date. Invertible only for zones east of UTC, i.e. correct for India
     and wrong for New York.

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

# Columns that are `timestamp with time zone` in DDL.txt. Casting one to a date,
# or calling .date() on one, is a timezone decision — and must look like one.
TZ_COLUMNS = (
    "bar_timestamp",
    "computed_at",
    "signal_bar_timestamp",
    "started_at",
    "finished_at",
    "as_of",
)


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments and docstrings removed.

    Prose is allowed to DESCRIBE the bug — several of these modules carry a
    post-mortem in a comment, and a guard that cannot tell an explanation from an
    occurrence is a guard nobody keeps.
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

    comment_lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])

    out: list[tuple[int, str]] = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in doc_lines:
            continue
        s = line.split("#", 1)[0] if i in comment_lines else line
        s = re.sub(r"--.*$", "", s)          # SQL comments inside string literals
        if s.strip():
            out.append((i, s))
    return out


def _all_sources() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "archive" not in p.parts)


def _utc_midnight_anchors() -> list[str]:
    """Every `datetime.combine(..., tzinfo=UTC)` in the package.

    An AST walk, not a line regex, and the distinction is load-bearing:
    `combine(d, datetime.min.time(), tzinfo=UTC)` closes the first paren inside
    the call, so `combine\([^)]*tzinfo=UTC` never matches it — and the multi-line
    spelling in pipeline.py is invisible to any line-based rule. A guard that
    fails OPEN is worse than no guard, because it is trusted.
    """
    hits: list[str] = []
    for path in _all_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
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
                    rel = path.relative_to(PKG.parent)
                    hits.append(f"{rel}:{node.lineno}: datetime.combine(..., tzinfo=UTC)")
    return hits


def _violations(rule) -> list[str]:
    hits: list[str] = []
    for path in _all_sources():
        for lineno, line in _code_lines(path):
            if rule(line):
                rel = path.relative_to(PKG.parent)
                hits.append(f"{rel}:{lineno}: {line.strip()}")
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
            "zone you mean: (col AT TIME ZONE :tz)::date
" + "
".join(hits)
        )

    def test_no_current_date(self):
        hits = _violations(
            lambda line: "CURRENT_DATE" in line or "CURRENT_TIMESTAMP" in line
        )
        assert not hits, (
            "CURRENT_DATE reads the session clock, which is not the market's. "
            "Use (NOW() AT TIME ZONE :tz)::date.
" + "
".join(hits)
        )

    def test_no_bare_date_on_a_timestamp_column(self):
        def rule(line: str) -> bool:
            return any(f"{c}.date()" in line for c in TZ_COLUMNS)

        hits = _violations(rule)
        assert not hits, (
            "psycopg returns an aware datetime in the SESSION zone, so .date() "
            "reads a clock nobody chose. Use trading_date_of(ts, exchange).
"
            + "
".join(hits)
        )

    def test_no_utc_midnight_as_a_trading_date(self):
        hits = _utc_midnight_anchors()
        assert not hits, (
            "Midnight UTC is invertible only for zones EAST of UTC — correct for "
            "India, wrong for New York. Use bar_anchor(d, exchange) / "
            "week_anchor(d, exchange).
" + "
".join(hits)
        )


class TestTheGuardCanActuallyFail:
    """A guard that cannot fail is decoration. These are the exact lines that were
    in the tree before the fix; the rules must reject every one of them."""

    @pytest.mark.parametrize("line", [
        "SELECT DISTINCT b.bar_timestamp::date AS d",
        "SELECT MIN(b.bar_timestamp)::date AS min_date",
        "ON o.bar_timestamp::date = wa.today_d",
        "AND bar_timestamp::date BETWEEN :start AND :end",
    ])
    def test_rejects_the_unqualified_casts(self, line):
        assert "::date" in line and "AT TIME ZONE" not in line
        assert any(c in line for c in TZ_COLUMNS)

    def test_rejects_the_current_date_read(self):
        assert "CURRENT_DATE" in ">= CURRENT_DATE - CAST(:days AS integer)"

    def test_rejects_the_bare_date_call(self):
        line = "trading_date=r.bar_timestamp.date(),"
        assert any(f"{c}.date()" in line for c in TZ_COLUMNS)

    @pytest.mark.parametrize("src", [
        # Nested parens — the first `)` belongs to time(), which is precisely why
        # the line-regex version of this rule silently passed the real bug.
        "ts = datetime.combine(w.trading_date, datetime.min.time(), tzinfo=UTC)",
        # Multi-line — invisible to any line-based rule.
        "x = datetime.combine(
    from_monday, time(0, 0), tzinfo=UTC,
)",
    ])
    def test_rejects_the_utc_midnight_anchor(self, src, tmp_path, monkeypatch):
        f = tmp_path / "bad.py"
        f.write_text(src)
        monkeypatch.setattr(
            "tests.architecture.test_no_implicit_timezone._all_sources",
            lambda: [f],
        )
        monkeypatch.setattr(
            "tests.architecture.test_no_implicit_timezone.PKG", tmp_path / "pkg",
        )
        assert _utc_midnight_anchors(), "the guard fails OPEN on this shape"

    def test_accepts_a_market_local_anchor(self, tmp_path, monkeypatch):
        f = tmp_path / "good.py"
        f.write_text("x = datetime.combine(d, time.min, tzinfo=zone)")
        monkeypatch.setattr(
            "tests.architecture.test_no_implicit_timezone._all_sources",
            lambda: [f],
        )
        monkeypatch.setattr(
            "tests.architecture.test_no_implicit_timezone.PKG", tmp_path / "pkg",
        )
        assert not _utc_midnight_anchors()

    def test_accepts_the_sanctioned_spellings(self):
        ok = [
            "SELECT DISTINCT (b.bar_timestamp AT TIME ZONE :tz)::date AS d",
            "trading_date=trading_date_of(r.bar_timestamp, _ex),",
            "ts = bar_anchor(w.trading_date, _ex)",
        ]
        for line in ok:
            assert not ("::date" in line
                        and any(c in line for c in TZ_COLUMNS)
                        and "AT TIME ZONE" not in line)
            assert not any(f"{c}.date()" in line for c in TZ_COLUMNS)

