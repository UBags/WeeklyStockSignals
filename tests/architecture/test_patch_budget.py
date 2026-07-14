"""Patch budget — a one-way ratchet on test-side patching.

Phase 0 of the de-monkeypatching programme. Every `monkeypatch.setattr`,
`unittest.mock.patch`, and `monkeypatch.setenv` in the suite is a hole cut in
production code so a test can reach through it. Each one is a missing seam:
a collaborator that should have been injected, a clock that should have been
a parameter, a data source that should have been a Protocol.

This test does not forbid them. It freezes them:

  * a file may never EXCEED its budget  -> new patching is rejected at the gate
  * a file may never come in UNDER it   -> when a phase removes patches, the
                                           budget must be edited down in the
                                           SAME commit, so the ledger stays
                                           honest and the count only travels
                                           one way
  * a file not in the ledger has budget 0

This file counts. It cannot see WHAT is being patched, so it cannot stop
someone from removing a patch here and re-drilling the same hole there. Its
sibling, test_closed_seams.py, names every seam a phase has closed and fails
if one is reopened. The two are a pair: this one caps the debt, that one stops
you paying for the same ground twice.

WHERE THIS STANDS (one injection from the hard ban):

    ~480 patches at the start. EIGHT remain, in three files. Production code
    contains none: every `monkeypatch` in trip_ema/ is prose in a docstring
    explaining the seam that replaced it.

    Seven of the eight are permanent, and are on the `exempt` list:

      6  observability/test_tracing.py
         monkeypatch.setitem(sys.modules, "opentelemetry*", None). The
         condition under test IS the ImportError. Injecting a tracer
         collaborator tests a fake tracer instead — and leaves the
         degradation branch, the one that runs on a box without OTel
         installed, with no coverage at all. Not debt.
      1  config/test_loader.py
         monkeypatch.setenv. test_default_env_is_os_environ asserts that
         `env` DEFAULTS to the real process environment. You cannot assert
         that binding without touching the real environment. Not debt.

    One is real, and is the whole of `files`:

      1  gui/test_slice4_wiring.py
         monkeypatch.setattr(about_dialog, "show_about_dialog", ...).
         MainWindow._on_about_clicked does a function-local import and calls
         a module function; MainWindow takes no Prompts. A Phase 7 residue —
         `about()` on the Prompts port retires it, and `files` goes empty.

    That last one is an injection, not a phase. When it lands, this budget
    should be deleted and replaced by a hard ban: no test file may name
    `monkeypatch` at all, save the two on the exempt list.

To re-baseline after legitimately removing patches:

    python tests/architecture/test_patch_budget.py

which rewrites patch_budget.json from the current tree. Read the diff before
you commit it — a count going UP in that diff means you just added a hole.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SELF = Path(__file__).resolve()
TESTS_ROOT = SELF.parents[1]
BUDGET_FILE = SELF.with_name("patch_budget.json")

# The two architecture guards. Both necessarily contain the patterns they
# police, so neither may be counted as debt.
_GUARDS = {SELF, SELF.with_name("test_closed_seams.py")}

# Categories are tracked separately because they retire in different phases:
#   setattr    -> phases 4-8 (ports, contexts, stage registries, GUI prompts)
#   mock_patch -> phase 4 (data-source ports)
#   env        -> phases 2-3 (config env seam, admin-token dependency)
PATTERNS: dict[str, re.Pattern[str]] = {
    "setattr": re.compile(r"monkeypatch\.(?:setattr|setitem|delattr)\("),
    # `patch(` / `@patch(` / `mock.patch(` — but NOT `client.patch(`, which is
    # an HTTP PATCH in the route tests. The lookbehind does that work.
    "mock_patch": re.compile(r"(?<![\w.])patch\(|(?<![\w])mock\.patch\("),
    "env": re.compile(r"monkeypatch\.(?:setenv|delenv)\("),
}

ZERO = {k: 0 for k in PATTERNS}


def _scan(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    return {k: len(p.findall(text)) for k, p in PATTERNS.items()}


def _load_exempt_paths() -> set[str]:
    """Files permanently exempt from budget tracking. Each entry in
    patch_budget.json's `exempt` list names a file plus a one-line
    rationale. The ledger is the audit trail — don't delete an entry
    without a commit message that replaces its rationale.

    Legitimate reasons to be here:
      - the file is the seam's own spec test (e.g. config/test_loader.py
        proves the default env=os.environ path);
      - the file uses monkeypatch.setitem(sys.modules, ...) to simulate
        a package-not-installed environment condition, which is under
        test rather than a production-code seam.

    Not legitimate: 'we didn't get around to it.'
    """
    raw = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    return {e["path"] for e in raw.get("exempt", [])}


def _scan_tree() -> dict[str, dict[str, int]]:
    """{relative posix path: {category: count}} for every non-zero file.
    Skips guards and permanently-exempt paths."""
    exempt = _load_exempt_paths()
    found: dict[str, dict[str, int]] = {}
    for py in sorted(TESTS_ROOT.rglob("*.py")):
        if py.resolve() in _GUARDS:
            # The guards quote the very patterns they police — as literals in
            # PATTERNS here, and as sample banned code in test_closed_seams
            # (which feeds each rule the code it bans and asserts it still
            # matches). Counting those as debt would be counting the alarm as
            # the burglary.
            continue
        rel = py.relative_to(TESTS_ROOT).as_posix()
        if rel in exempt:
            continue
        counts = _scan(py)
        if any(counts.values()):
            found[rel] = counts
    return found


def _load_budget() -> dict[str, dict[str, int]]:
    raw = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    return {k: {**ZERO, **v} for k, v in raw["files"].items()}


def _regen() -> None:
    # Preserve the exempt list across regen — the scanner would otherwise
    # blow it away and silently reopen the entries as debt.
    existing = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    payload = {
        "_comment": (
            "Patch budget ledger. Counts may only go DOWN. See "
            "tests/architecture/test_patch_budget.py. Regenerate with: "
            "python tests/architecture/test_patch_budget.py"
        ),
        "exempt": existing.get("exempt", []),
        "files": _scan_tree(),
    }
    BUDGET_FILE.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    total = {
        k: sum(f[k] for f in payload["files"].values()) for k in PATTERNS
    }
    print(f"wrote {BUDGET_FILE}")
    print(f"files: {len(payload['files'])}  totals: {total}")


def test_no_file_exceeds_its_patch_budget() -> None:
    budget = _load_budget()
    actual = _scan_tree()
    over: list[str] = []
    for path, counts in sorted(actual.items()):
        allowed = budget.get(path, ZERO)
        for cat, n in counts.items():
            if n > allowed[cat]:
                over.append(
                    f"  {path}: {cat} {allowed[cat]} -> {n} (+{n - allowed[cat]})"
                )
    assert not over, (
        "New test-side patching was introduced. Every one of these is a "
        "missing seam — inject the collaborator instead:\n"
        + "\n".join(over)
        + "\n\nIf the patch is genuinely unavoidable, raising the budget is a "
        "deliberate act: say so in the commit message."
    )


def test_budget_is_ratcheted_down() -> None:
    """Removing patches without editing the ledger leaves a stale budget that
    silently permits their return. Fail until the ledger is updated."""
    budget = _load_budget()
    actual = _scan_tree()
    stale: list[str] = []
    for path, allowed in sorted(budget.items()):
        counts = actual.get(path, ZERO)
        for cat, n in counts.items():
            if n < allowed[cat]:
                stale.append(
                    f"  {path}: {cat} {allowed[cat]} -> {n} ({n - allowed[cat]})"
                )
    assert not stale, (
        "Patches were removed but the ledger still budgets for them:\n"
        + "\n".join(stale)
        + "\n\nRatchet it down in the same commit:\n"
        "    python tests/architecture/test_patch_budget.py"
    )


def test_budget_has_no_entries_for_deleted_files() -> None:
    budget = _load_budget()
    missing = [
        p for p in sorted(budget) if not (TESTS_ROOT / p).is_file()
    ]
    assert not missing, (
        "patch_budget.json lists files that no longer exist:\n  "
        + "\n  ".join(missing)
        + "\n\nRegenerate: python tests/architecture/test_patch_budget.py"
    )


def test_exempt_paths_exist() -> None:
    """Stale exempt entries silently permit patches in files that no
    longer exist. Fail loudly instead."""
    raw = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    missing = [
        e["path"] for e in raw.get("exempt", [])
        if not (TESTS_ROOT / e["path"]).is_file()
    ]
    assert not missing, (
        "patch_budget.json's `exempt` list names files that no longer "
        "exist:\n  " + "\n  ".join(missing)
    )


if __name__ == "__main__":
    _regen()
