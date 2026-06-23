"""Commit 1, patch 01.

Delete `run_signal_events_for_industry` and
`run_signal_events_for_all_industries` from
`trip_ema/fundamentals/runners.py`.

Uses AST to locate the exact line range of each top-level function and
splices it out. Refuses to run if either function is missing, or if any
unexpected sibling function got captured.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _patch_lib import PatchError, run as _run


TARGET = "trip_ema/fundamentals/runners.py"
FUNCS_TO_DELETE = (
    "run_signal_events_for_industry",
    "run_signal_events_for_all_industries",
)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if not matches:
        raise PatchError(f"Function {name!r} not found at module level.")
    if len(matches) > 1:
        raise PatchError(
            f"Function {name!r} found {len(matches)} times at module level "
            "(must be unique)."
        )
    return matches[0]


def patch(text: str) -> str:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)

    # Locate each target. Compute ranges *first*, sort descending, then
    # splice — so earlier ranges don't shift later ones.
    ranges: list[tuple[int, int, str]] = []
    for fname in FUNCS_TO_DELETE:
        node = _find_function(tree, fname)
        start = node.lineno - 1  # 0-indexed inclusive
        end = node.end_lineno  # 0-indexed exclusive after this conversion
        if end is None:
            raise PatchError(
                f"AST gave no end_lineno for {fname!r}; need Python 3.8+."
            )
        # Eat one trailing blank line if present, to keep the file from
        # accumulating triple blanks. AST end_lineno points AT the last
        # statement line; we want to drop the line AFTER it if it's blank.
        while end < len(lines) and lines[end].strip() == "":
            end += 1
            # Stop after eating one blank, otherwise we collapse two
            # separator blanks that belong to the NEXT function.
            break
        ranges.append((start, end, fname))

    # Sanity: ranges must not overlap.
    sorted_ranges = sorted(ranges)
    for (s1, e1, n1), (s2, e2, n2) in zip(sorted_ranges, sorted_ranges[1:]):
        if e1 > s2:
            raise PatchError(
                f"Function ranges overlap: {n1}=[{s1},{e1}) {n2}=[{s2},{e2})"
            )

    # Apply in reverse order (latest line first) so indices stay valid.
    for start, end, _name in sorted(ranges, reverse=True):
        del lines[start:end]

    new_text = "".join(lines)

    # Cross-check: neither function should now appear at any line start.
    for fname in FUNCS_TO_DELETE:
        if f"\ndef {fname}(" in new_text:
            raise PatchError(
                f"Post-delete check failed: 'def {fname}(' still present."
            )

    return new_text


if __name__ == "__main__":
    _run(TARGET, patch)
