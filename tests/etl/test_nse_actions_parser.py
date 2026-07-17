"""NSE corporate-action parser tests.

Real `subject` strings from the nselib feed. Pins the atomic-row output and
the reduced share-ratio / price_factor convention (validated 99.6% row-for-row
against the authoritative parsed export), plus the money-safety rule that a
non-equity bonus NEVER produces an equity price factor.
"""

from __future__ import annotations

import math

import pytest

from trip_ema.etl.nse_actions_parser import (
    CENSOR_TYPES,
    FACTOR_TYPES,
    parse_subject,
)


def one(subject):
    acts = parse_subject(subject)
    assert len(acts) == 1, f"expected 1 action, got {[a.action_type for a in acts]}"
    return acts[0]


# --- splits: NEW:OLD shares reduced; price_factor = den/num = new_fv/old_fv --- #

@pytest.mark.parametrize("subject,num,den,f", [
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 5, 1, 0.2),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", 10, 1, 0.1),
    ("Fv Split Rs.10/- To Rs.2/", 5, 1, 0.2),
    ("Face Value Split From Rs.10/- To Rs.5/-", 2, 1, 0.5),   # 10->5
    ("Fv Splt Frm Rs 10 To Rs 2", 5, 1, 0.2),
    ("Sub-Division From Rs 10/- Per Share To Rs 2/- Per Share", 5, 1, 0.2),  # no fv prefix
    ("Face Valus Split (Sub-Division) - From Rs 10/- Per To Rs 2/-", 5, 1, 0.2),  # typo
    ("Fv Spl-Rs10tors2", 5, 1, 0.2),                          # mashed
])
def test_splits(subject, num, den, f):
    a = one(subject)
    assert a.action_type == "split"
    assert (a.ratio_numerator, a.ratio_denominator) == (num, den)
    assert a.price_factor == pytest.approx(f)
    assert a.censor is False


def test_split_non_integer_ratio_is_reduced():
    # 5 -> 2 face value: share mult 5/2, reduced 5:2, f = 2/5
    a = one("Face Value Split From Rs.5/- To Rs.2/-")
    assert (a.ratio_numerator, a.ratio_denominator) == (5, 2)
    assert a.price_factor == pytest.approx(0.4)


def test_consolidation_reverse_split_factor_above_one():
    a = one("Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share")
    assert a.action_type == "consolidation"
    assert (a.ratio_numerator, a.ratio_denominator) == (1, 10)
    assert a.price_factor == pytest.approx(10.0)


# --- bonuses: a:b -> NEW:OLD = (a+b):b reduced; f = b/(a+b) --- #

@pytest.mark.parametrize("subject,num,den,f", [
    ("Bonus 1:1", 2, 1, 0.5),
    ("Bonus 1:2", 3, 2, 2 / 3),
    ("Bonus 2:1", 3, 1, 1 / 3),
    ("Bonus 3:1", 4, 1, 0.25),
    ("Bonus 4:1", 5, 1, 0.2),
    ("Bonus 1:5", 6, 5, 5 / 6),
    ("Bonus Shares In The Ratio Of 1:1", 2, 1, 0.5),
    ("Bonus Issue 1 : 1", 2, 1, 0.5),
    ("Intdiv-Rs 3persh/Bonus1:1", 2, 1, 0.5),
])
def test_bonuses(subject, num, den, f):
    a = one(subject)
    assert a.action_type == "bonus"
    assert (a.ratio_numerator, a.ratio_denominator) == (num, den)
    assert a.price_factor == pytest.approx(f)


# --- compound split + bonus => TWO atomic rows --- #

@pytest.mark.parametrize("subject,factors", [
    ("Fv Spl-Rs10tore1/Bon-1:1", {0.1, 0.5}),
    ("Fv Spl-10 To 5 / Bon 1:2", {0.5, 2 / 3}),
    ("Bonus 1:1 And Face Value Split From Rs.10/- To Rs.2/-", {0.2, 0.5}),
    ("Annual General Meeting/Bonus 2:1/Face Value Split From Rs.10 To Re.1", {0.1, 1 / 3}),
])
def test_compound_split_bonus_two_rows(subject, factors):
    acts = parse_subject(subject)
    assert len(acts) == 2
    assert {a.action_type for a in acts} == {"split", "bonus"}
    assert {round(a.price_factor, 6) for a in acts} == {round(x, 6) for x in factors}
    # composed factor is the product — what the corrector applies per ex_date
    prod = acts[0].price_factor * acts[1].price_factor
    assert prod == pytest.approx(math.prod(factors))


# --- censor events --- #

@pytest.mark.parametrize("subject", [
    "Scheme Of Arrangement", "Demerger", "Scheme Of Demerger",
    "Composite Scheme Of Arrangement", "Scheme Of Amalgamation",
])
def test_demerger_censored(subject):
    a = one(subject)
    assert a.action_type == "demerger"
    assert a.censor is True
    assert a.price_factor is None


def test_capital_reduction_censored():
    a = one("Capital Reduction Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To Rs.10")
    assert a.action_type == "capital_reduction"
    assert a.censor is True
    assert a.price_factor is None


# --- MONEY SAFETY: non-equity bonuses carry NO equity factor --- #

@pytest.mark.parametrize("subject", [
    "Sch Of Agmt- Bonus Deb1:1",
    "Scheme Of Arrangement - Bonus Debentures 6:1",
    "Scheme Of Arrangement - Bonus Ncrps 4:1",
    "Bonus Ncrps 1:116",
    "Bonus Preference Shares 21:1",
])
def test_non_equity_bonus_has_no_price_factor(subject):
    for a in parse_subject(subject):
        assert a.action_type not in FACTOR_TYPES
        assert a.price_factor is None
    # at least one should be flagged bonus_other (not a silent 'other')
    assert any(a.action_type == "bonus_other" for a in parse_subject(subject))


# --- traps: special dividends are NOT splits --- #

@pytest.mark.parametrize("subject", [
    "Agm/Div-Fin-45% + Spl-55%", "Fin Div-Rs14+Spl Div-Rs14",
    "Agm/Spl Dividend @ 120%", "Spl Int Div-150%",
])
def test_special_dividend_not_split(subject):
    a = one(subject)
    assert a.action_type == "dividend"
    assert a.price_factor is None


# --- dividend amount in detail --- #

@pytest.mark.parametrize("subject,detail", [
    ("Dividend-12.5%", "12.5%"),
    ("Agm/Dividend-30%", "30%"),
    ("Agm/Div-Rs.2.50 Per Sh", "Rs 2.50"),
])
def test_dividend_amount_in_detail(subject, detail):
    a = one(subject)
    assert a.action_type == "dividend"
    assert a.detail == detail


# --- non-factor buckets --- #

@pytest.mark.parametrize("subject,atype", [
    ("Annual General Meeting", "agm"),
    ("Extra Ordinary General Meeting", "egm"),
    ("Interest Payment", "interest"),
    ("Buy Back", "buyback"),
    ("Annual Book Closure", "book_closure"),
    ("E-Voting", "voting"),
    ("Rigths-Eq 3:10@Prem Rs.30", "rights"),
])
def test_non_factor_buckets(subject, atype):
    a = one(subject)
    assert a.action_type == atype
    assert a.price_factor is None


# --- invariants / edges --- #

@pytest.mark.parametrize("subject", ["", "   ", None])
def test_empty_is_unparsed(subject):
    assert one(subject).action_type == "unparsed"


def test_factor_invariants():
    for subj in ("Bonus 1:1", "Fv Split Rs.10/- To Rs.2/",
                 "Consolidation From Re 1 To Rs 10"):
        a = one(subj)
        assert a.action_type in FACTOR_TYPES
        assert a.price_factor is not None and a.price_factor > 0
        assert math.isfinite(a.price_factor)
        # price_factor == den/num exactly
        assert a.price_factor == pytest.approx(a.ratio_denominator / a.ratio_numerator)
    for subj in ("Demerger", "Capital Reduction Of Shares"):
        a = one(subj)
        assert a.action_type in CENSOR_TYPES
        assert a.censor is True and a.price_factor is None


def test_raw_subject_retained():
    assert one("Bonus 1:1").raw_subject == "Bonus 1:1"
