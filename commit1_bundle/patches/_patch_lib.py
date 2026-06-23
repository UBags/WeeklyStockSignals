"""Shared patcher helpers.

Each patcher in this directory does a small number of `replace_unique` /
`delete_unique` operations on a file, asserting the pre-state matches
EXACTLY. If a block isn't found verbatim, the patcher fails loudly
*before* writing — no silent corruption.

Usage from any patcher:

    from _patch_lib import edit, delete, run

    def patch(text: str) -> str:
        text = delete(text, OLD_BLOCK_1)
        text = edit(text, OLD_BLOCK_2, NEW_BLOCK_2)
        return text

    if __name__ == "__main__":
        run("trip_ema/some/file.py", patch)
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Callable


class PatchError(RuntimeError):
    pass


def _normalize(s: str) -> str:
    # Trim trailing whitespace per line so accidental editor padding doesn't
    # cause a false mismatch. Keep newlines.
    return "\n".join(line.rstrip() for line in s.split("\n"))


def edit(text: str, old: str, new: str) -> str:
    """Replace `old` with `new` exactly once. Fail if not found or ambiguous."""
    if old == new:
        raise PatchError("edit(): old == new (no-op refused)")
    norm_text = _normalize(text)
    norm_old = _normalize(old)
    count = norm_text.count(norm_old)
    if count == 0:
        raise PatchError(
            "edit(): old block not found.\n"
            "First 5 lines of old:\n"
            + "\n".join("  " + l for l in old.splitlines()[:5])
            + "\n--- (file head ---)\n"
            + "\n".join("  " + l for l in text.splitlines()[:8])
        )
    if count > 1:
        raise PatchError(
            f"edit(): old block matched {count} times (must be unique).\n"
            "Tighten the block to include more context."
        )
    # Apply the substitution on the *normalized* text so the result is itself
    # canonical (no trailing-space drift introduced by re-saves).
    return norm_text.replace(norm_old, _normalize(new), 1)


def delete(text: str, old: str) -> str:
    """Remove `old` exactly once. Same uniqueness semantics as edit()."""
    return edit(text, old, "")


def run(rel_path: str, patch_fn: Callable[[str], str]) -> None:
    """Apply `patch_fn` to `rel_path` relative to the repo root.

    Convention: every patcher in this directory expects to be invoked from
    the repo root, e.g. `python patches/01_runners.py`. The repo root is
    detected by walking up until we find `pyproject.toml`.
    """
    cwd = Path.cwd().resolve()
    repo_root = cwd
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    else:
        raise PatchError(
            f"Could not locate pyproject.toml above {cwd}. "
            "Run patchers from the repo root."
        )

    target = repo_root / rel_path
    if not target.exists():
        raise PatchError(f"Target file does not exist: {target}")

    text = target.read_text(encoding="utf-8")
    try:
        new_text = patch_fn(text)
    except PatchError as e:
        print(f"FAILED  {rel_path}: {e}", file=sys.stderr)
        sys.exit(2)

    if new_text == text:
        print(f"NOOP    {rel_path} (already in target state)", file=sys.stderr)
        return

    # Atomic write: temp file in same directory, then rename.
    tmp = target.with_suffix(target.suffix + ".patch_tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)
    print(f"OK      {rel_path}")
