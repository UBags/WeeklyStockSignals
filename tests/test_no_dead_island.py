"""Tripwire: assert the dead island under trip_ema/transports/ stays dead.

The trip_ema/transports/ directory historically contained a parallel
older copy of most of the codebase. It was removed in <PR #NNN>.
Only trip_ema/transports/http/ is legitimate. If any of the deleted
subpackages reappears — by accident, by a bad merge, or because
someone copied an old branch — this test fails and tells them why.

This test is skipped while the deletion is still pending (Stage 3
of the cleanup plan). Unskip it as part of the deletion PR.
"""
import importlib
import pkgutil

import pytest

import trip_ema.transports as transports_pkg

# Every subpackage that should NOT exist under trip_ema.transports.
# trip_ema.transports.http is the only legitimate child.
DEAD_SUBPACKAGES = [
    "api",
    "cache",
    "config",
    "domain",
    "engine",
    "indicators",
    "notifications",
    "observability",
    "persistence",
    "registry",
    "scheduler",
    "transports",  # the nested trip_ema/transports/transports/
    "app",         # trip_ema/transports/app.py — orphaned entry point
]


@pytest.mark.skip(reason="Enable after Stage 3 deletion lands")
@pytest.mark.parametrize("name", DEAD_SUBPACKAGES)
def test_dead_subpackage_is_gone(name: str) -> None:
    """No file or subpackage by this name should exist under trip_ema.transports."""
    full_name = f"trip_ema.transports.{name}"
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(full_name)


@pytest.mark.skip(reason="Enable after Stage 3 deletion lands")
def test_only_http_lives_under_transports() -> None:
    """Belt-and-braces: enumerate what's actually under trip_ema.transports
    and assert it's exactly {http}. Catches anyone adding a new sibling
    that the parametrized list above hasn't been updated to cover."""
    children = {
        info.name
        for info in pkgutil.iter_modules(transports_pkg.__path__)
    }
    # Allow http (legitimate) plus any future tests/conftest discovery
    # noise pkgutil might pick up; if anything else appears, fail loud.
    assert children == {"http"}, (
        f"Unexpected children under trip_ema.transports: {children - {'http'}}. "
        f"Only 'http' is allowed. See Stage 3 cleanup plan."
    )