"""Loader tests: nselib feed rows -> atomic records -> upsert.

Rows are shaped exactly like nselib's `corporate_actions_for_equity` output
(the real column names). No DB or nselib needed — the repo + downloader are
injected fakes.
"""

from __future__ import annotations

from datetime import date

from trip_ema.etl.nse_actions_loader import (
    build_records,
    refresh_with_repo,
)


def _row(isin, symbol, ex, subject, **extra):
    base = {
        "bcEndDate": "", "bcStartDate": "", "caBroadcastDate": "",
        "comp": symbol, "exDate": ex, "faceVal": "10", "ind": "-",
        "isin": isin, "ndEndDate": "", "ndStartDate": "", "recDate": "",
        "series": "EQ", "subject": subject, "symbol": symbol,
    }
    base.update(extra)
    return base


ISIN_MAP = {"INE001A01011": 100, "INE002B02022": 200}


def _rows():
    return [
        _row("INE001A01011", "ACME", "06-Jan-2020",
             "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"),
        _row("INE002B02022", "BETA", "10-Feb-2019",
             "Bonus 1:1 And Face Value Split From Rs.10 To Re.1"),   # compound -> 2
        _row("INE999Z09099", "GAMMA", "03-Mar-2021", "Dividend-12.5%"),  # unresolved isin
        _row("INE001A01011", "ACME", "01-Apr-2018", "Scheme Of Arrangement"),  # censor
        _row("INE001A01011", "ACME", "-", "Bonus 1:2"),                 # bad date -> skipped
    ]


# --------------------------------------------------------------------------- #
# build_records
# --------------------------------------------------------------------------- #

def test_build_records_counts():
    recs, stats = build_records(_rows(), ISIN_MAP)
    assert stats["rows_fetched"] == 5
    assert stats["rows_skipped_bad_date"] == 1
    # split(1) + compound(2) + dividend(1) + demerger(1) = 5
    assert stats["actions_built"] == 5
    assert len(recs) == 5


def test_split_record_fields():
    recs, _ = build_records(_rows(), ISIN_MAP)
    split = next(r for r in recs if r.action_type == "split")
    assert split.ticker_id == 100
    assert split.isin == "INE001A01011"
    assert split.ex_date == date(2020, 1, 6)
    assert (split.ratio_numerator, split.ratio_denominator) == (5, 1)
    assert split.price_factor == 0.2
    assert split.censor is False
    assert split.raw_payload["series"] == "EQ"       # full source row retained


def test_compound_row_yields_two_atomic_records():
    recs, _ = build_records(_rows(), ISIN_MAP)
    beta = [r for r in recs if r.symbol == "BETA"]
    assert {r.action_type for r in beta} == {"split", "bonus"}
    assert all(r.ticker_id == 200 for r in beta)
    assert all(r.ex_date == date(2019, 2, 10) for r in beta)


def test_unresolved_isin_gives_null_ticker_id():
    recs, stats = build_records(_rows(), ISIN_MAP)
    div = next(r for r in recs if r.action_type == "dividend")
    assert div.ticker_id is None
    assert div.detail == "12.5%"
    assert stats["actions_unresolved"] == 1
    assert stats["actions_resolved"] == 4


def test_demerger_record_is_censored():
    recs, _ = build_records(_rows(), ISIN_MAP)
    dem = next(r for r in recs if r.action_type == "demerger")
    assert dem.censor is True
    assert dem.price_factor is None


def test_bad_date_row_is_skipped_not_stored():
    recs, stats = build_records(_rows(), ISIN_MAP)
    assert stats["rows_skipped_bad_date"] == 1
    # the "Bonus 1:2" bad-date row produced no record
    assert not any(r.action_type == "bonus" and r.ratio_denominator == 2 for r in recs)


def test_store_all_action_types():
    # dividends and demergers are stored, not filtered out.
    recs, _ = build_records(_rows(), ISIN_MAP)
    assert {r.action_type for r in recs} == {"split", "bonus", "dividend", "demerger"}


# --------------------------------------------------------------------------- #
# refresh_with_repo
# --------------------------------------------------------------------------- #

class _FakeRepo:
    def __init__(self, isin_map):
        self._map = isin_map
        self.upserted = []

    def isin_to_ticker_id(self):
        return self._map

    def upsert_many(self, records):
        self.upserted.extend(records)
        return len(records)          # pretend all were new


def test_refresh_with_repo_end_to_end():
    repo = _FakeRepo(ISIN_MAP)
    rows = _rows()
    summary = refresh_with_repo(
        repo, download=lambda a, b: rows, from_date="01-01-2018", to_date="31-12-2021"
    )
    assert summary.rows_fetched == 5
    assert summary.rows_skipped_bad_date == 1
    assert summary.actions_built == 5
    assert summary.actions_inserted == 5
    assert summary.actions_resolved == 4
    assert summary.actions_unresolved == 1
    assert len(repo.upserted) == 5


def test_refresh_downloader_receives_window():
    repo = _FakeRepo(ISIN_MAP)
    seen = {}

    def dl(a, b):
        seen["from"], seen["to"] = a, b
        return []

    refresh_with_repo(repo, download=dl, from_date="01-01-2020", to_date="15-07-2026")
    assert seen == {"from": "01-01-2020", "to": "15-07-2026"}
