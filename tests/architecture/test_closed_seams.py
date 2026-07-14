"""Closed seams — the holes that are filled STAY filled.

The patch budget (test_patch_budget.py) counts patches. It cannot tell you WHAT
is being patched, so it cannot stop someone from removing a patch in one file
and re-drilling the same hole in another. This test can.

Every entry below is a seam that a phase of the de-monkeypatching programme
closed. Each names the global that used to be reachable, the port that replaced
it, and what to do instead. If any of them comes back — in production or in a
test — this fails with that message.

That matters more than the count. A budget going up tells you someone added a
hole. This tells you they reopened a specific one you have already paid to
close, and hands them the seam they should have used.

The check is textual, deliberately: it runs in milliseconds, needs no imports,
and cannot be defeated by accident — only on purpose, which is the point. An
intentional exception is a conversation, not a slip.

It matches CODE only. Comments and string literals are blanked before scanning,
because the files that document these seams — tests/gui/fakes.py, tests/etl/
fakes.py — necessarily quote the very patterns they exist to replace, and a rule
that fires on its own explanation is a rule people learn to switch off.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

SELF = Path(__file__).resolve()
REPO = SELF.parents[2]
TESTS = REPO / "tests"
SRC = REPO / "trip_ema"


class Seam:
    """One closed hole."""

    def __init__(
        self,
        name: str,
        phase: str,
        pattern: str,
        instead: str,
        *,
        scope: str = "tests",
        exempt: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.phase = phase
        # MULTILINE: several patterns anchor with ^ to mean 'at the start of a
        # line'. Without it they anchor to the start of the FILE and can never
        # fire — a rule that is always green and always useless.
        self.re = re.compile(pattern, re.MULTILINE)
        self.instead = instead
        self.scope = scope            # "tests" | "src" | "both"
        self.exempt = exempt          # paths allowed to contain it

    def roots(self) -> list[Path]:
        return {
            "tests": [TESTS],
            "src": [SRC],
            "both": [TESTS, SRC],
        }[self.scope]


CLOSED_SEAMS: list[Seam] = [
    # ---- Phase 1: the clock ----
    Seam(
        "module-global clock in the circuit breaker",
        "1",
        r'setattr\(\s*[\w.]*circuit_breaker[\w.]*\s*,\s*["\']_now["\']',
        "CircuitBreakingTransport takes `clock: MonotonicClock`. Pass a "
        "ManualClock.",
    ),

    # ---- Phase 2/3: config env + admin token ----
    Seam(
        "TRIP_EMA_ADMIN_TOKEN via the environment",
        "3",
        r'(?:setenv|delenv|environ\.pop|environ\[)\s*\(?\s*["\']TRIP_EMA_ADMIN_TOKEN',
        "override the get_admin_token dependency: "
        "app.dependency_overrides[get_admin_token] = lambda: 'tok'. "
        "os.environ leaks across xdist workers.",
    ),

    # ---- Phase 4A/4B: the data-source ports ----
    Seam(
        "patching yfinance itself",
        "4",
        r'(?:patch|setattr)\(\s*\n?\s*["\']?(?:yfinance|yf)["\']?\s*[,.]',
        "inject a Downloader (trip_ema/etl/ports.py) or a FundamentalsSource "
        "(trip_ema/fundamentals/ports.py). See tests/etl/fakes.py.",
        scope="tests",
    ),
    Seam(
        "the _fetch_yfinance_* helpers",
        "4A",
        r'_fetch_yfinance_\w+',
        "these were deleted. Pass a FundamentalsSource; see "
        "tests/fundamentals/fakes.py::FakeFundamentalsSource.",
        scope="both",
    ),
    Seam(
        "inline yfinance download in the ETL source",
        "4B",
        r'^\s*import yfinance',
        "YFinanceSource takes `download: Downloader`. The only place yfinance "
        "may be imported is trip_ema/etl/ports.py and "
        "trip_ema/fundamentals/ports.py, lazily.",
        scope="src",
        exempt=("etl/ports.py", "fundamentals/ports.py"),
    ),

    # ---- Phase 5: AppContext ----
    Seam(
        "create_engine_from_config as a CLI seam",
        "5",
        r'(?:patch|setattr)\([^)]*create_engine_from_config',
        "the commands take an AppContext. Build one: "
        "AppContext(config=..., db_engine=db_engine).",
        scope="tests",
    ),
    Seam(
        "a CLI command building its own engine",
        "5",
        r'^\s*(?:db_)?engine\s*=\s*create_engine_from_config\(',
        "main() owns the engine and passes an AppContext. A command must not "
        "build (or dispose) one.",
        scope="src",
        exempt=("app_context.py", "app.py", "persistence/connection.py"),
    ),

    # ---- Phase 6: the orchestrator stage registry ----
    Seam(
        "recompute_market's stage functions as module attributes",
        "6",
        r'setattr\(\s*\n?\s*\w*rm_mod\w*\s*,\s*["\']_stage_',
        "recompute_market takes `stages: MarketStages`. Derive one: "
        "DEFAULT_MARKET_STAGES.with_(iiv=boom).",
    ),

    # ---- Phase 7: the GUI prompts port ----
    Seam(
        "patching Qt's dialog statics",
        "7",
        r'(?:patch|setattr)\(\s*\n?\s*(?:QMessageBox|QFileDialog|QInputDialog)\s*,',
        "the views take `prompts: Prompts`. Pass a FakePrompts "
        "(tests/gui/fakes.py). A real QMessageBox opens a modal that wedges "
        "pytest-qt's event loop — this is not merely style.",
        scope="tests",
    ),
    Seam(
        "a view calling a Qt dialog static",
        "7",
        r'(?:QMessageBox|QFileDialog|QInputDialog)\.\w+\(',
        "call self._prompts.<warning|information|critical|confirm|"
        "get_save_filename|get_item>(...). Only trip_ema/gui/prompts.py may "
        "touch the statics.",
        scope="src",
        exempt=("gui/prompts.py",),
    ),

    # ---- Phase 8: the populator contract ----
    Seam(
        "patching the backfill populators",
        "8",
        r'(?:patch|setattr)\(\s*\n?\s*["\']trip_ema\.backfill\.pipeline\.',
        "backfill() takes `stages: BackfillStages`. Use "
        "DEFAULT_BACKFILL_STAGES.with_(...). NOTE: a patched stock-state "
        "populator is SILENTLY DISCARDED by the spawned children at "
        "stock_state_processes > 1 — which is the bug this port exists to "
        "prevent.",
        scope="tests",
    ),
    Seam(
        "patching the industry populators through the orchestrator",
        "8",
        r'(?:patch|setattr)\(\s*\n?\s*["\']trip_ema\.orchestrator\.industry_stages\.',
        "pass no-op industry_state / industry_enrichment stages via "
        "BackfillStages.with_(...); the pipeline calls the STAGE, not the "
        "populator beneath it.",
        scope="tests",
    ),

    # ---- Phase 9: the fundamentals runner registry ----
    Seam(
        "patching a fundamentals runner by module path",
        "9",
        r'(?:patch|setattr)\(\s*\n?\s*["\']trip_ema\.fundamentals\.'
        r'(?:runners|v1_runners|scheduler)\.(?:run_|populate_)',
        "the scheduler takes `runners: FundamentalsRunners`; the routes "
        "resolve one via Depends(get_fundamentals_runners). Derive a "
        "variant: DEFAULT_FUNDAMENTALS_RUNNERS.with_(shares=spy), or use "
        "the use_runners fixture in the route tests.",
        scope="tests",
    ),
    Seam(
        "a function-local import of a fundamentals runner",
        "9",
        # [ \t]+, NOT \s+ : \s matches newlines, so `^\s+from` anchors on the
        # blank line above a MODULE-TOP import, eats the newline, and matches
        # it. The first version of this rule flagged industry_stages.py's
        # top-level import and was correct about nothing.
        r'^[ \t]+from \.{2,4}fundamentals\.(?:runners|v1_runners) import',
        "import at module top, or take the registry. A runner imported "
        "INSIDE a function body has no runtime purpose here — no cycle, "
        "nothing lazy. It is a patch target. That is what it was for.",
        scope="src",
        # Phase 9 finish: the two exemptions here (admin_etl.py and
        # etl/cli.py) are gone. `run_shares_backfill` is now a
        # FundamentalsRunners field (`shares_backfill`), so admin_etl.py
        # dispatches via the registry rather than a function-local import.
        # etl/cli.py::cmd_refresh_reference reads ctx.runners and calls
        # runners.for_phase(name) — again, no local import.
    ),

    # ---- Phase 10: the Gateways registry ----
    Seam(
        "patching a repository or read-helper on an admin route module",
        "10",
        r'(?:patch|setattr)\(\s*\n?\s*[\w.]*\s*,\s*'
        r'["\'](?:EtlStatusRepository|ExchangeStateRepository|_tier_counts)["\']',
        "the routes take `gateways: Gateways` via Depends(get_gateways). "
        "Derive a variant: DEFAULT_GATEWAYS.with_(etl_status=FakeRepo) or "
        ".with_(exchange_state=FakeRepo, tier_counts=lambda conn, ex: ...) "
        "and pass through _make_client(gateways=...).",
        scope="tests",
        exempt=(
            # test_runner_progress patches EtlStatusRepository on the ETL
            # runner module (runner_mod), not a route module. Same shape,
            # different port — belongs to the EtlRunners phase, not Phase 10.
            "etl/test_runner_progress.py",
        ),
    ),

    # ---- Phase 11: notify collaborators as injectables ----
    Seam(
        "patching notify collaborators (send_email, notify's signals repo, "
        "or admin_subscribers' email transport builder)",
        "11",
        r'(?:patch|setattr)\([^)\n]*?'
        r'["\'](?:trip_ema\.notifications\.signal_report\.send_email|'
        r'_build_email_transport_or_null|CurrentSignalsRepository)["\']',
        "the collaborators are now injectable kwargs: "
        "`notify_subscribers(..., send=fake, signals_repo_factory=lambda conn: repo)` "
        "and `_build_transport(name, config, email_builder=fake_builder)`.",
        scope="tests",
    ),
]


def _blank(out: list[list[str]], r1: int, c1: int, r2: int, c2: int) -> None:
    for row in range(r1, r2 + 1):
        if row - 1 >= len(out):
            break
        line = out[row - 1]
        start = c1 if row == r1 else 0
        end = c2 if row == r2 else len(line)
        for col in range(start, min(end, len(line))):
            if line[col] != "\n":
                line[col] = " "


def _code_only(text: str) -> str:
    """The source with COMMENTS and DOCSTRINGS blanked out — and nothing else.
    Line numbers and columns are preserved, so reported positions stay true.

    Docstrings only, NOT every string literal. That distinction is the whole
    game: these seams are patched BY STRING —

        monkeypatch.setattr("trip_ema.etl.cli.create_engine_from_config", f)

    — so blanking string literals wholesale would blind the check to the exact
    thing it exists to catch. (It did, in the first draft. The rule passed
    everywhere, including on the unported code it was supposed to reject.)

    Comments and docstrings still have to go, because the files that document
    these seams necessarily quote the patterns they replace, and a rule that
    fires on its own explanation is a rule people learn to switch off.
    """
    lines = text.splitlines(keepends=True)
    out = [list(line) for line in lines]

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type is tokenize.COMMENT:
                _blank(out, tok.start[0], tok.start[1], tok.end[0], tok.end[1])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "".join("".join(line) for line in out)

    for node in ast.walk(tree):
        # A docstring is a string that is a statement in its own right. Any
        # bare string-expression counts, which also catches the block comments
        # people write between functions.
        if not isinstance(node, ast.Expr):
            continue
        v = node.value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            _blank(
                out, v.lineno, v.col_offset,
                v.end_lineno or v.lineno, v.end_col_offset or 0,
            )

    return "".join("".join(line) for line in out)


def _files(root: Path) -> list[Path]:
    return [
        p for p in root.rglob("*.py")
        if p.resolve() != SELF and "__pycache__" not in p.parts
    ]


def _violations(seam: Seam) -> list[tuple[Path, int, str]]:
    """Every place this seam has been reopened, as (path, line, source).

    Matches are found with finditer over the WHOLE file and the offset is then
    turned into a line number. Do not be tempted to scan line by line: most of
    these patterns span lines —

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )

    — so a per-line scan finds nothing, reports nothing, and the seam passes
    while wide open. (It did. That is why this comment is here.)
    """
    hits: list[tuple[Path, int, str]] = []
    for root in seam.roots():
        if not root.is_dir():
            continue
        for path in _files(root):
            rel = path.relative_to(REPO).as_posix()
            if any(ex in rel for ex in seam.exempt):
                continue
            raw = path.read_text(encoding="utf-8")
            code = _code_only(raw)
            raw_lines = raw.splitlines()
            for m in seam.re.finditer(code):
                line_no = code.count("\n", 0, m.start()) + 1
                src = (
                    raw_lines[line_no - 1].strip()
                    if line_no - 1 < len(raw_lines) else ""
                )
                hits.append((path, line_no, src))
    return hits


def test_closed_seams_stay_closed():
    """Every seam a phase closed must remain closed."""
    failures: list[str] = []
    for seam in CLOSED_SEAMS:
        hits = _violations(seam)
        if not hits:
            continue
        lines = "\n".join(
            f"      {p.relative_to(REPO).as_posix()}:{n}  {src[:88]}"
            for p, n, src in hits[:6]
        )
        more = f"\n      ... and {len(hits) - 6} more" if len(hits) > 6 else ""
        failures.append(
            f"\n  REOPENED (phase {seam.phase}): {seam.name}\n"
            f"    Use instead: {seam.instead}\n"
            f"    Found at:\n{lines}{more}"
        )

    assert not failures, (
        "A seam that a previous phase closed has been reopened.\n"
        + "".join(failures)
        + "\n\n  These are not style rules. Each one was a hole cut in "
        "production code so a test\n  could reach through it, and each was "
        "closed by giving the collaborator a name.\n  If you genuinely need an "
        "exception, add it to `exempt` in this file — deliberately,\n  in a "
        "commit that says why.\n"
    )


def test_every_seam_pattern_is_live():
    """A regex that can no longer match anything is a rule that has quietly
    stopped enforcing. Prove each pattern still fires on the code it bans."""
    probes = {
        "module-global clock in the circuit breaker":
            'monkeypatch.setattr(circuit_breaker, "_now", lambda: 0.0)',
        "TRIP_EMA_ADMIN_TOKEN via the environment":
            'monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "tok")',
        "patching yfinance itself":
            'patch("yfinance.download")',
        "the _fetch_yfinance_* helpers":
            'def _fetch_yfinance_info(symbol): ...',
        "inline yfinance download in the ETL source":
            '    import yfinance as yf',
        "create_engine_from_config as a CLI seam":
            'monkeypatch.setattr("trip_ema.etl.cli.create_engine_from_config", f)',
        "a CLI command building its own engine":
            '    db_engine = create_engine_from_config(config)',
        "recompute_market's stage functions as module attributes":
            'monkeypatch.setattr(rm_mod, "_stage_iiv_incremental", boom)',
        "patching Qt's dialog statics":
            'monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)',
        "a view calling a Qt dialog static":
            '        QMessageBox.warning(self, "t", "m")',
        "patching the backfill populators":
            'monkeypatch.setattr("trip_ema.backfill.pipeline.refresh_listings", f)',
        "patching the industry populators through the orchestrator":
            'monkeypatch.setattr("trip_ema.orchestrator.industry_stages.x", f)',
        "patching a fundamentals runner by module path":
            'monkeypatch.setattr("trip_ema.fundamentals.runners.run_shares_refresh", f)',
        "a function-local import of a fundamentals runner":
            # INDENTED, deliberately. A module-top import is not the thing
            # this seam bans, and the probe has to prove the difference.
            '    from ....fundamentals.runners import run_shares_refresh',
        "patching a repository or read-helper on an admin route module":
            'monkeypatch.setattr(mod, "EtlStatusRepository", FakeRepo)',
        "patching notify collaborators (send_email, notify's signals repo, "
        "or admin_subscribers' email transport builder)":
            'patch("trip_ema.notifications.signal_report.send_email")',
    }
    for seam in CLOSED_SEAMS:
        probe = probes[seam.name]
        assert seam.re.search(probe), (
            f"seam {seam.name!r} (phase {seam.phase}) no longer matches the "
            f"very code it bans:\n    {probe}\n"
            "The rule has stopped enforcing. Fix the pattern."
        )
