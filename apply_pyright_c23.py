"""Pyright C.2 + C.3 — pandas casts + alembic stub limitation (7 errors).

C.2 (5): pyright sees .ewm/.rolling/.apply/.shift as returning
`DataFrame | Series` even when the input is a Series. At runtime
they always return Series for Series input. cast(pd.Series, ...)
documents the invariant and silences the lint.

C.3 (2): alembic.config.Config.cmd_opts is typed as Namespace |
None in the stubs, but the documented way to inject -x options is
to assign an ad-hoc object exposing `.x`. Stub limitation, not a
code defect.
"""
from __future__ import annotations
import pathlib

REPO = pathlib.Path.home() / "PycharmProjects" / "WeeklyStockSignals"


def patch(rel: str, old: str, new: str, *, label: str) -> None:
    p = REPO / rel
    text = p.read_text()
    n = text.count(old)
    assert n == 1, f"[{label}] expected 1 occurrence, found {n} in {rel}"
    p.write_text(text.replace(old, new))
    print(f"  ✓ {label}")


# Ensure cast is importable in indicator_primitives.py — add to its
# typing import. The grep earlier didn't show a `from typing import`
# line; we'll add it. If it does exist, the patcher's assert will fire
# and we'll re-anchor.
patch(
    "trip_ema/fundamentals/indicator_primitives.py",
    label="indicator_primitives.import.cast",
    old="import pandas as pd\n",
    new=(
        "import pandas as pd\n"
        "from typing import cast\n"
    ),
)

# pine_ema
patch(
    "trip_ema/fundamentals/indicator_primitives.py",
    label="indicator_primitives.pine_ema.cast",
    old=(
        "    # adjust=False uses recursive formula with src[0] as seed;\n"
        "    # this matches Pine's ta.ema.\n"
        "    return src.ewm(span=n, adjust=False).mean()\n"
    ),
    new=(
        "    # adjust=False uses recursive formula with src[0] as seed;\n"
        "    # this matches Pine's ta.ema.\n"
        "    return cast(pd.Series, src.ewm(span=n, adjust=False).mean())\n"
    ),
)

# pine_wma — the rolling.apply return
patch(
    "trip_ema/fundamentals/indicator_primitives.py",
    label="indicator_primitives.pine_wma.cast",
    old=(
        "    # rolling.apply lets us pass a custom weighted sum.\n"
        "    return src.rolling(window=n, min_periods=n).apply(\n"
        "        lambda window: np.dot(window, weights) / weight_sum,\n"
        "        raw=True,\n"
        "    )\n"
    ),
    new=(
        "    # rolling.apply lets us pass a custom weighted sum.\n"
        "    return cast(pd.Series, src.rolling(window=n, min_periods=n).apply(\n"
        "        lambda window: np.dot(window, weights) / weight_sum,\n"
        "        raw=True,\n"
        "    ))\n"
    ),
)

# pine_sma
patch(
    "trip_ema/fundamentals/indicator_primitives.py",
    label="indicator_primitives.pine_sma.cast",
    old=(
        "    if n < 1:\n"
        "        raise ValueError(f\"n must be >= 1, got {n}\")\n"
        "    return src.rolling(window=n, min_periods=n).mean()\n"
    ),
    new=(
        "    if n < 1:\n"
        "        raise ValueError(f\"n must be >= 1, got {n}\")\n"
        "    return cast(pd.Series, src.rolling(window=n, min_periods=n).mean())\n"
    ),
)

# pine_rma
patch(
    "trip_ema/fundamentals/indicator_primitives.py",
    label="indicator_primitives.pine_rma.cast",
    old=(
        "    # alpha = 1/n means span = 2n - 1 (from alpha = 2/(span+1)).\n"
        "    # Easier to specify alpha directly.\n"
        "    return src.ewm(alpha=1.0 / n, adjust=False).mean()\n"
    ),
    new=(
        "    # alpha = 1/n means span = 2n - 1 (from alpha = 2/(span+1)).\n"
        "    # Easier to specify alpha directly.\n"
        "    return cast(pd.Series, src.ewm(alpha=1.0 / n, adjust=False).mean())\n"
    ),
)

# open_ shift assignment
patch(
    "trip_ema/fundamentals/indicator_primitives.py",
    label="indicator_primitives.open_shift.cast",
    old=(
        "    # Default open_ to prev_close for the index case.\n"
        "    if open_ is None:\n"
        "        open_ = close.shift(1)\n"
    ),
    new=(
        "    # Default open_ to prev_close for the index case.\n"
        "    if open_ is None:\n"
        "        open_ = cast(pd.Series, close.shift(1))\n"
    ),
)

# alembic_bridge — two identical cmd_opts assignments
patch(
    "trip_ema/persistence/alembic_bridge.py",
    label="alembic_bridge.cmd_opts.stamp",
    old=(
        "    alembic_cfg = AlembicConfig(\"alembic.ini\")\n"
        "    if config_path is not None:\n"
        "        # env.py reads -x config=... before falling back to env var.\n"
        "        alembic_cfg.cmd_opts = type(\"X\", (), {\"x\": [f\"config={config_path}\"]})()\n"
        "\n"
        "    command.stamp(alembic_cfg, _BASELINE_REVISION)\n"
    ),
    new=(
        "    alembic_cfg = AlembicConfig(\"alembic.ini\")\n"
        "    if config_path is not None:\n"
        "        # env.py reads -x config=... before falling back to env var.\n"
        "        # Stub-deficiency: Config.cmd_opts is typed Namespace | None,\n"
        "        # but the documented -x injection uses an ad-hoc obj exposing .x\n"
        "        alembic_cfg.cmd_opts = type(\"X\", (), {\"x\": [f\"config={config_path}\"]})()  # pyright: ignore[reportAttributeAccessIssue]\n"
        "\n"
        "    command.stamp(alembic_cfg, _BASELINE_REVISION)\n"
    ),
)

patch(
    "trip_ema/persistence/alembic_bridge.py",
    label="alembic_bridge.cmd_opts.upgrade",
    old=(
        "    alembic_cfg = AlembicConfig(\"alembic.ini\")\n"
        "    if config_path is not None:\n"
        "        alembic_cfg.cmd_opts = type(\"X\", (), {\"x\": [f\"config={config_path}\"]})()\n"
        "\n"
        "    logger.info(\"Running alembic upgrade head\")\n"
    ),
    new=(
        "    alembic_cfg = AlembicConfig(\"alembic.ini\")\n"
        "    if config_path is not None:\n"
        "        # Same stub-deficiency as the stamp path above.\n"
        "        alembic_cfg.cmd_opts = type(\"X\", (), {\"x\": [f\"config={config_path}\"]})()  # pyright: ignore[reportAttributeAccessIssue]\n"
        "\n"
        "    logger.info(\"Running alembic upgrade head\")\n"
    ),
)

print("Done.")