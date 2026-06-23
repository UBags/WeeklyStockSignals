"""Commit 1, patch 05.

Test-file cleanup:

  1. `tests/fundamentals/test_signal_event_runners.py`
     — entire file is industry tests (imports only the two industry
       runners; classes are TestRunSignalEventsForIndustry and
       TestRunSignalEventsForAllIndustries). Whole-file delete via
       git rm in the README. This patcher *verifies* the file's
       structure before delete to catch surprises.

  2. `tests/fundamentals/test_cli.py`
     — delete the `TestCmdComputeSignals` class (all 7 tests are
       industry-mode). The ticker-mode tests live in the separate
       `TestCmdComputeSignalsTickerModes` class which stays.

  3. `tests/transports/http/test_fundamentals_routes.py`
     — delete `TestGetIndustrySignalEvents` (industry GET endpoint)
       and `TestComputeSignalsEndpoint` (industry POST mode).
       `TestComputeSignalsTickerMode` stays. `TestGetRecentSignalEvents`
       also stays — uses repo.upsert directly with industry rows for
       setup, but the endpoint we're keeping reads whatever's in the
       table.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _patch_lib import PatchError, run as _run


# =====================================================================
# Verifier for test_signal_event_runners.py (whole-file delete)
# =====================================================================
#
# We *don't* delete the file from this patcher (git rm is in the
# README instead, so the deletion is visible in the commit's file
# list). But we DO verify that the file is what we think it is —
# imports only the industry runners and contains only the two
# industry test classes. If that's not true, abort.

VERIFY_TARGET_1 = "tests/fundamentals/test_signal_event_runners.py"

EXPECTED_IMPORTS_PREFIX_1 = """from trip_ema.fundamentals.runners import (
    run_signal_events_for_all_industries,
    run_signal_events_for_industry,
)"""

EXPECTED_CLASS_NAMES_1 = {
    "TestRunSignalEventsForIndustry",
    "TestRunSignalEventsForAllIndustries",
}


def verify_test_signal_event_runners() -> None:
    cwd = Path.cwd()
    repo_root = cwd
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    target = repo_root / VERIFY_TARGET_1
    if not target.exists():
        print(f"NOOP    {VERIFY_TARGET_1} already deleted")
        return
    text = target.read_text(encoding="utf-8")

    if EXPECTED_IMPORTS_PREFIX_1 not in text:
        raise PatchError(
            f"{VERIFY_TARGET_1}: expected industry runner imports not "
            "found verbatim. File may have drifted from expectation."
        )

    tree = ast.parse(text)
    class_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    unexpected = class_names - EXPECTED_CLASS_NAMES_1
    if unexpected:
        raise PatchError(
            f"{VERIFY_TARGET_1}: contains unexpected non-industry test "
            f"classes: {sorted(unexpected)}. Refusing to recommend "
            "whole-file delete."
        )
    missing = EXPECTED_CLASS_NAMES_1 - class_names
    if missing:
        raise PatchError(
            f"{VERIFY_TARGET_1}: expected industry test classes "
            f"missing: {sorted(missing)}. File may have drifted."
        )
    print(f"VERIFY  {VERIFY_TARGET_1} OK (safe to git rm)")


# =====================================================================
# Delete TestCmdComputeSignals from test_cli.py
# =====================================================================

TARGET_2 = "tests/fundamentals/test_cli.py"
CLASSES_TO_DELETE_2 = ("TestCmdComputeSignals",)


# =====================================================================
# Delete TestGetIndustrySignalEvents + TestComputeSignalsEndpoint
# from test_fundamentals_routes.py
# =====================================================================

TARGET_3 = "tests/transports/http/test_fundamentals_routes.py"
CLASSES_TO_DELETE_3 = (
    "TestGetIndustrySignalEvents",
    "TestComputeSignalsEndpoint",
)

# Section-header comment blocks above the deleted classes — delete
# alongside so we don't leave orphan `# ---------- GET /signal-events/...`
# banners pointing to nothing.
ORPHAN_COMMENTS_3 = (
    "# ---------- GET /signal-events/industries/{name} ---------- #",
    "# ---------- POST /compute-signals ---------- #",
)


def _delete_top_level_classes(text: str, class_names: tuple[str, ...]) -> str:
    """AST-based: find each top-level ClassDef by name, splice it out
    along with one trailing blank line. Sibling classes/functions are
    not touched."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)

    ranges: list[tuple[int, int, str]] = []
    for name in class_names:
        matches = [
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == name
        ]
        if not matches:
            raise PatchError(
                f"Class {name!r} not found at module level."
            )
        if len(matches) > 1:
            raise PatchError(
                f"Class {name!r} found {len(matches)} times "
                "(must be unique)."
            )
        node = matches[0]
        start = node.lineno - 1
        end = node.end_lineno
        if end is None:
            raise PatchError(
                f"AST gave no end_lineno for class {name!r}."
            )
        # Eat trailing blank lines (up to 2 — class separations are
        # PEP 8 two-blanks).
        eaten = 0
        while end < len(lines) and lines[end].strip() == "" and eaten < 2:
            end += 1
            eaten += 1
        ranges.append((start, end, name))

    # Overlap check.
    sorted_r = sorted(ranges)
    for (s1, e1, n1), (s2, e2, n2) in zip(sorted_r, sorted_r[1:]):
        if e1 > s2:
            raise PatchError(
                f"Class ranges overlap: {n1}=[{s1},{e1}) {n2}=[{s2},{e2})"
            )

    for start, end, _ in sorted(ranges, reverse=True):
        del lines[start:end]

    return "".join(lines)


def _strip_orphan_comments(text: str, banners: tuple[str, ...]) -> str:
    """Drop section-header comment lines whose target class is gone.
    Match against full-line; preserves leading/trailing whitespace
    context around the line."""
    out_lines = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped in banners:
            # Skip this banner. Also collapse any immediately-preceding
            # blank lines that were padding the banner (max 2) and
            # immediately-following blanks (max 2).
            # Roll back blank lines we already appended.
            tail_blanks = 0
            while out_lines and out_lines[-1].strip() == "" and tail_blanks < 2:
                out_lines.pop()
                tail_blanks += 1
            i += 1
            head_blanks = 0
            while i < len(lines) and lines[i].strip() == "" and head_blanks < 2:
                i += 1
                head_blanks += 1
            # Ensure a single blank separator survives so the surrounding
            # code blocks don't visually fuse.
            out_lines.append("\n\n")
            continue
        out_lines.append(lines[i])
        i += 1
    return "".join(out_lines)


def patch_test_cli(text: str) -> str:
    return _delete_top_level_classes(text, CLASSES_TO_DELETE_2)


def patch_test_routes(text: str) -> str:
    text = _delete_top_level_classes(text, CLASSES_TO_DELETE_3)
    text = _strip_orphan_comments(text, ORPHAN_COMMENTS_3)
    return text


if __name__ == "__main__":
    verify_test_signal_event_runners()
    _run(TARGET_2, patch_test_cli)
    _run(TARGET_3, patch_test_routes)
