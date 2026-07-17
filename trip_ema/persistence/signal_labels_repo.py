"""signal_labels persistence: one row per enriched signal, upserted on
the signal_enrichment_id FK."""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.forward_walk import GAIN_RUNGS, LOSS_RUNGS, SignalForwardWalk
from ..domain.signal_label import SignalLabel

_INSERT = text(
    """
    INSERT INTO signal_labels (
        signal_enrichment_id, fwd_ret_4w, fwd_ret_8w, fwd_ret_13w,
        fwd_max_dd, fwd_rr, horizon_complete, computed_at
    ) VALUES (
        :seid, :r4, :r8, :r13, :dd, :rr, :hc, NOW()
    )
    ON CONFLICT (signal_enrichment_id) DO UPDATE SET
        fwd_ret_4w = EXCLUDED.fwd_ret_4w,
        fwd_ret_8w = EXCLUDED.fwd_ret_8w,
        fwd_ret_13w = EXCLUDED.fwd_ret_13w,
        fwd_max_dd = EXCLUDED.fwd_max_dd,
        fwd_rr = EXCLUDED.fwd_rr,
        horizon_complete = EXCLUDED.horizon_complete,
        computed_at = NOW()
    """
)

# --------------------------------------------------------------------------- #
# Forward-walk upsert (spec 02_signal_forward_walk_labels).
#
# The column list is built from the domain rung SSOT (GAIN_RUNGS / LOSS_RUNGS),
# the same lists migration 0055 spells literally and the arch test ties the two
# together — so this upsert cannot silently drift from the schema. This path
# touches ONLY the forward-walk columns; the weekly fwd_ret_* / horizon_complete
# columns are left untouched (the two labelers are complementary writers to the
# same row).
# --------------------------------------------------------------------------- #
_FW_BOOKKEEPING = [
    "forward_walk_done", "side", "entry_date", "entry_open", "exit_date",
    "exit_close", "exit_reason", "bars_in_window", "realized_ret",
    "window_censored",
]
_FW_GAIN = [
    c for lbl, _f in GAIN_RUNGS
    for c in (f"hit_gain_{lbl}_pct", f"bars_to_gain_{lbl}_pct")
]
_FW_LOSS = [
    c for lbl, _f in LOSS_RUNGS
    for c in (f"hit_loss_{lbl}_pct", f"bars_to_loss_{lbl}_pct")
]
_FW_EXTRA = [
    "ever_in_profit", "mae_pct", "mae_bar", "mfe_pct", "mfe_bar",
    "mae_before_mfe", "excess_ret_industry", "excess_ret_market",
    "mfe_atr20", "mae_atr20", "time_underwater_bars", "slope_per_bar",
    "realized_vol", "sharpe_in_window", "capture_ratio",
]
_FW_COLUMNS = _FW_BOOKKEEPING + _FW_GAIN + _FW_LOSS + _FW_EXTRA  # 79


def _build_fw_upsert() -> "text":
    cols = ["signal_enrichment_id", *_FW_COLUMNS]
    collist = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _FW_COLUMNS)
    return text(
        f"INSERT INTO signal_labels ({collist}, computed_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT (signal_enrichment_id) DO UPDATE SET "
        f"{updates}, computed_at = NOW()"
    )


_UPSERT_FW = _build_fw_upsert()


def _fw_params(row: SignalForwardWalk) -> dict:
    """Flatten one SignalForwardWalk into the upsert bind params.

    When ``result`` is None (data-quality censored) every measured column is
    written at its NULL/-1/false default — explicitly, so a re-run that now
    censors a window overwrites any previously-computed metrics rather than
    leaving stale numbers behind. Bookkeeping (side, dates, bars_in_window,
    window_censored) is always recorded.
    """
    r = row.result
    p: dict = {
        "signal_enrichment_id": row.signal_enrichment_id,
        "forward_walk_done": row.forward_walk_done,
        "side": row.side,
        "entry_date": row.entry_date,
        "exit_date": row.exit_date,
        "exit_reason": row.exit_reason,
        "bars_in_window": row.bars_in_window,
        "window_censored": row.window_censored,
        "entry_open": r.entry_open if r else None,
        "exit_close": r.exit_close if r else None,
        "realized_ret": r.realized_ret if r else None,
        "ever_in_profit": r.ever_in_profit if r else False,
        "mae_pct": r.mae_pct if r else None,
        "mae_bar": r.mae_bar if r else -1,
        "mfe_pct": r.mfe_pct if r else None,
        "mfe_bar": r.mfe_bar if r else -1,
        "mae_before_mfe": r.mae_before_mfe if r else False,
        "excess_ret_industry": r.excess_ret_industry if r else None,
        "excess_ret_market": r.excess_ret_market if r else None,
        "mfe_atr20": r.mfe_atr20 if r else None,
        "mae_atr20": r.mae_atr20 if r else None,
        "time_underwater_bars": r.time_underwater_bars if r else -1,
        "slope_per_bar": r.slope_per_bar if r else None,
        "realized_vol": r.realized_vol if r else None,
        "sharpe_in_window": r.sharpe_in_window if r else None,
        "capture_ratio": r.capture_ratio if r else None,
    }
    for lbl, _f in GAIN_RUNGS:
        p[f"hit_gain_{lbl}_pct"] = r.hit_gain[lbl] if r else False
        p[f"bars_to_gain_{lbl}_pct"] = r.bars_to_gain[lbl] if r else -1
    for lbl, _f in LOSS_RUNGS:
        p[f"hit_loss_{lbl}_pct"] = r.hit_loss[lbl] if r else False
        p[f"bars_to_loss_{lbl}_pct"] = r.bars_to_loss[lbl] if r else -1
    return p


class SignalLabelsRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    @staticmethod
    def _params(row: SignalLabel) -> dict:
        return {
            "seid": row.signal_enrichment_id,
            "r4": row.fwd_ret_4w, "r8": row.fwd_ret_8w,
            "r13": row.fwd_ret_13w, "dd": row.fwd_max_dd,
            "rr": row.fwd_rr,
            "hc": row.horizon_complete,
        }

    def upsert_many(self, rows: list[SignalLabel]) -> int:
        if not rows:
            return 0
        self._conn.execute(_INSERT, [self._params(r) for r in rows])
        return len(rows)

    def upsert_forward_walk(self, rows: Iterable[SignalForwardWalk]) -> int:
        """Upsert forward-walk columns for a batch of signals. Idempotent on
        signal_enrichment_id; leaves the weekly labeler's columns untouched."""
        batch = list(rows)
        if not batch:
            return 0
        self._conn.execute(_UPSERT_FW, [_fw_params(r) for r in batch])
        return len(batch)

    def count(self) -> int:
        return int(
            self._conn.execute(
                text("SELECT count(*) FROM signal_labels")
            ).scalar_one()
        )
