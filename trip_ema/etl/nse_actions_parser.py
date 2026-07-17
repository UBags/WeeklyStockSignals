"""Parser for the NSE corporate-actions `subject` free-text (nselib feed).

Turns one feed row's `subject` into a LIST of atomic :class:`ParsedAction`s.
A single subject can describe more than one action (e.g. a bonus AND a face
value split on the same ex-date); each becomes its own row, exactly as in the
authoritative parsed export. Every action that changes share count carries a
reduced share ratio and a price factor; value-transfer events (demerger,
capital reduction) are flagged `censor`; dividends carry their amount as
`detail`.

Conventions (match the authoritative export):
  * ratio_numerator : ratio_denominator = NEW shares : OLD shares, reduced.
  * price_factor = ratio_denominator / ratio_numerator = post/pre price
    (a value-neutral scale event leaves a *correctly adjusted* close series
    continuous; the factor is what the corrector snaps to, §6.1).
      split  10→2  ⇒ 5:1  f=0.2      bonus 1:1 ⇒ 2:1 f=0.5
      split  5→2   ⇒ 5:2  f=0.4      bonus 1:2 ⇒ 3:2 f=0.667
      consolidation 1→10 ⇒ 1:10 f=10.0   bonus 2:1 ⇒ 3:1 f=0.333

Compound "split AND bonus" ⇒ two rows (split, bonus); the reconcile/correction
layer groups by (ticker, ex_date) and composes the factors multiplicatively.

Pure: no I/O, no nselib import. The loader adapts feed rows to this and
persists the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

__all__ = [
    "ParsedAction",
    "parse_subject",
    "FACTOR_TYPES",
    "CENSOR_TYPES",
]

FACTOR_TYPES = frozenset({"split", "consolidation", "bonus"})
CENSOR_TYPES = frozenset({"demerger", "capital_reduction"})


@dataclass(frozen=True, slots=True)
class ParsedAction:
    action_type: str
    detail: str = ""
    ratio_numerator: int | None = None
    ratio_denominator: int | None = None
    price_factor: float | None = None
    censor: bool = False
    raw_subject: str = ""


_NUM = r"(\d+(?:\.\d+)?)"

_RE_CAPRED = re.compile(r"capital\s*reduction|reduction\s*of\s*capital", re.I)
_RE_DEMERGER = re.compile(
    r"demerg|de-merg|amalgamat|reconstruction|spin[-\s]?off|"
    r"scheme\s*of\s*arrangement|scheme\s*of\s*demerger|composite\s*scheme|"
    r"\bscheme\b|arrangement",
    re.I,
)

_RE_SPLIT = [
    re.compile(
        r"(?:f\.?\s*v\.?|face\s*valu\w*)\s*"
        r"(?:split|splt|spl|sub[-\s]?division|sub[-\s]?div)\b.*?"
        r"(?:from\s*)?(?:rs|re)?\.?\s*" + _NUM + r"\s*/?-?.*?\bto\b[^0-9]*?" + _NUM,
        re.I,
    ),
    re.compile(
        r"sub[-\s]?division\b.*?(?:from\s*)?(?:rs|re)?\.?\s*" + _NUM
        + r"\s*/?-?.*?\bto\b[^0-9]*?" + _NUM,
        re.I,
    ),
    re.compile(
        r"\bspl(?:it|t)?\b[-\s]*(?:rs|re)?\.?\s*" + _NUM
        + r"\s*/?-?.*?\bto\b[^0-9]*?" + _NUM,
        re.I,
    ),
    re.compile(r"(?:rs|re)\.?" + _NUM + r"\s*tor[se]\.?\s*" + _NUM, re.I),
]
_RE_CONSOL = re.compile(
    r"consolidat\w*.*?(?:from\s*)?(?:rs|re)?\.?\s*" + _NUM
    + r".*?\bto\b[^0-9]*?" + _NUM,
    re.I,
)
_RE_BONUS = re.compile(
    r"bon(?:us)?(?:\s*(?:issue|shares?))?(?:\s*in\s*the\s*ratio\s*of)?"
    r"[-\s:]*" + _NUM + r"\s*:\s*" + _NUM,
    re.I,
)
_RE_BONUS_OTHER = re.compile(
    r"debenture|\bdeb\d|\bdeb\b|dvr|differential|preference|\bpref\b|"
    r"warrant|ncd\b|ncrps|\bbond",
    re.I,
)

# dividend amount: "Rs 2.50", "Rs.14", "12.5%", "@ 120%"
_RE_DIV_RS = re.compile(r"(?:rs|re)\.?\s*" + _NUM, re.I)
_RE_DIV_PCT = re.compile(_NUM + r"\s*%")

_BUCKETS = [
    ("dividend", re.compile(r"divid|\bdiv\b|div[-\s.]|interim\s*div|final\s*div", re.I)),
    ("rights", re.compile(r"\brig?h?ts?\b|\brh?ts?\b|\brhgt\b|\brgts?\b|\brhs\b|@\s*prem", re.I)),
    ("buyback", re.compile(r"buy\s*back|buyback", re.I)),
    ("interest", re.compile(r"interest\s*payment|\binterest\b", re.I)),
    ("book_closure", re.compile(r"book\s*closure|annual\s*closure|record\s*date", re.I)),
    ("voting", re.compile(r"e-?voting|postal\s*ballot", re.I)),
    ("egm", re.compile(r"extra[-\s]?ordinary\s*general|extraordinary\s*general|\begm\b|eogm", re.I)),
    ("agm", re.compile(r"annual\s*general\s*meeting|\bagm\b", re.I)),
    ("meeting", re.compile(r"board\s*meeting|\bmeeting\b", re.I)),
    ("name_change", re.compile(r"change\s*of\s*name|name\s*change", re.I)),
    ("distribution", re.compile(r"distribution", re.I)),
]


def _find_split(s: str) -> tuple[float, float] | None:
    for rx in _RE_SPLIT:
        m = rx.search(s)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if 0 < new <= 1000 and 0 < old <= 1000 and old != new:
                return old, new
    return None


def _find_consolidation(s: str) -> tuple[float, float] | None:
    m = _RE_CONSOL.search(s)
    if m:
        old, new = float(m.group(1)), float(m.group(2))
        if 0 < new <= 1000 and 0 < old <= 1000 and old != new:
            return old, new
    return None


def _find_bonus(s: str) -> tuple[float, float] | None:
    m = _RE_BONUS.search(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if b > 0 and a >= 0:
            return a, b
    return None


def _ratio(new_shares: float, old_shares: float) -> tuple[int, int, float]:
    """Reduce NEW:OLD shares to lowest terms; price_factor = den/num."""
    fr = Fraction(new_shares).limit_denominator(100000) / Fraction(
        old_shares
    ).limit_denominator(100000)
    num, den = fr.numerator, fr.denominator
    return num, den, den / num


def _split_action(old_fv: float, new_fv: float, raw: str) -> ParsedAction:
    # shares scale by old_fv/new_fv; NEW:OLD shares = old_fv:new_fv
    num, den, f = _ratio(old_fv, new_fv)
    atype = "consolidation" if f > 1 else "split"
    return ParsedAction(
        atype, detail=f"{old_fv:g}\u2192{new_fv:g}",
        ratio_numerator=num, ratio_denominator=den, price_factor=f,
        raw_subject=raw,
    )


def _bonus_action(a: float, b: float, raw: str) -> ParsedAction:
    # a new shares per b held ⇒ NEW:OLD = (a+b):b
    num, den, f = _ratio(a + b, b)
    return ParsedAction(
        "bonus", detail=f"{a:g}:{b:g}",
        ratio_numerator=num, ratio_denominator=den, price_factor=f,
        raw_subject=raw,
    )


def _dividend_detail(s: str) -> str:
    pct = _RE_DIV_PCT.search(s)
    if pct:
        return f"{pct.group(1)}%"
    rs = _RE_DIV_RS.search(s)
    if rs:
        return f"Rs {rs.group(1)}"
    return ""


def parse_subject(subject: str | None) -> list[ParsedAction]:
    """Classify one `subject` into zero or more atomic actions.

    Never raises. An unrecognised non-empty subject → single 'other'; empty →
    single 'unparsed'.
    """
    raw = subject or ""
    s = raw.strip()
    if not s:
        return [ParsedAction("unparsed", raw_subject=raw)]

    # Capital reduction — real value transfer, wins outright.
    if _RE_CAPRED.search(s):
        return [ParsedAction("capital_reduction", detail="capital reduction",
                             censor=True, raw_subject=raw)]

    # Equity scale events. _find_bonus only matches an a:b ADJACENT to "bonus",
    # so "Bonus Ncrps 1:10" / "Bonus Debentures 6:1" do NOT extract here — the
    # instrument word breaks adjacency — and correctly get no equity factor.
    split = _find_split(s) or _find_consolidation(s)
    bonus = _find_bonus(s)

    actions: list[ParsedAction] = []
    if split:
        actions.append(_split_action(split[0], split[1], raw))
    if bonus:
        actions.append(_bonus_action(bonus[0], bonus[1], raw))
    if actions:
        return actions

    # A bonus OF a non-equity instrument (debenture / preference / NCRPS / DVR /
    # warrant) — recorded, NEVER an equity price factor.
    if re.search(r"\bbon(?:us)?\b", s, re.I) and _RE_BONUS_OTHER.search(s):
        return [ParsedAction("bonus_other", detail="non-equity bonus", raw_subject=raw)]

    # Demerger / scheme — real value transfer, censor.
    if _RE_DEMERGER.search(s):
        return [ParsedAction("demerger", detail="scheme/demerger",
                             censor=True, raw_subject=raw)]

    # Non-factor buckets.
    for name, rx in _BUCKETS:
        if rx.search(s):
            detail = _dividend_detail(s) if name == "dividend" else ""
            return [ParsedAction(name, detail=detail, raw_subject=raw)]

    return [ParsedAction("other", raw_subject=raw)]
