"""The month a request names: "the November plan", "nov 2026", "2026-11", "11/2026".

A plan for one month has to be filtered by that month. The model does not remember to do it - it groups the whole
year and calls it a November plan - so the month is read from the request and handed to the query as a parameter.

"may" and "march" are English words as well, so a bare mention of them is not a month: from free text a month only
counts when it stands next to a year or next to a word that asks for that month's data ("november plan", "data for
november"). A month named in the tool arguments is taken as it is.
"""

import re
from datetime import date

MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
          "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
          "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}

_ISO_RX = re.compile(r"\b(\d{4})-(\d{1,2})\b")
_MDY_RX = re.compile(r"\b(\d{1,2})[/-](\d{4})\b")
_NAME_RX = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b", re.I)
_YEAR_RX = re.compile(r"\b(20\d{2})\b")
_ABOUT_RX = re.compile(r"\b(plan|month|months|data|report|target|targets|activity|activities|forecast|schedule|"
                       r"pipeline|proposal|review|summary|numbers?|figures?|performance|list|breakdown|status)\b", re.I)


def parse(text, default_year=None, allow_number=False) -> str:
    """"November", "nov 2026", "2026-11", "11/2026" -> "2026-11". "" when no month is named."""
    t = str(text or "")
    m = _ISO_RX.search(t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    m = _MDY_RX.search(t)
    if m:
        mo, y = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    m = _NAME_RX.search(t)
    if m:
        mo = MONTHS[m.group(1).lower()]
        ym = _YEAR_RX.search(t)
        y = int(ym.group(1)) if ym else (default_year or date.today().year)
        return f"{y:04d}-{mo:02d}"
    if allow_number:
        m = re.search(r"\b(\d{1,2})\b", t)
        if m and 1 <= int(m.group(1)) <= 12:
            y = default_year or date.today().year
            return f"{y:04d}-{int(m.group(1)):02d}"
    return ""


def _all_months(text: str) -> list:
    """Every month word in the text: (start, end, number)."""
    return [(m.start(), m.end(), MONTHS[m.group(1).lower()]) for m in _NAME_RX.finditer(text)]


def from_request(text) -> str:
    """The month a request asks data for, or "" when the request names none (or several).

    Only one month can be picked: "November and December" is a question for the model to work out, not a filter.
    """
    t = str(text or "")
    found = _all_months(t)
    if len(found) > 1:
        return ""
    iso = _ISO_RX.search(t)
    if iso:
        y, mo = int(iso.group(1)), int(iso.group(2))
        if 1 <= mo <= 12 and _ABOUT_RX.search(t[max(0, iso.start() - 25): iso.end() + 25]):
            return f"{y:04d}-{mo:02d}"
    if not found:
        return ""
    start, end, mo = found[0]
    window = t[max(0, start - 25): end + 25]
    ym = _YEAR_RX.search(window)
    y = int(ym.group(1)) if ym else date.today().year
    if ym or _ABOUT_RX.search(window):
        return f"{y:04d}-{mo:02d}"
    # the request is nothing but the month: "November", "the November"
    only = re.fullmatch(r"(the\s+)?(month\s+)?(of\s+)?" + re.escape(t[start:end]) + r"(\s+(19|20)\d{2})?",
                        t.strip(), re.I)
    if only:
        y2 = re.search(r"(19|20)\d{2}", only.group(0))
        return f"{int(y2.group(0)) if y2 else date.today().year:04d}-{mo:02d}"
    return ""


# ---- days -------------------------------------------------------------------------------------------------------
_DAY_NAME = "|".join(sorted(MONTHS, key=len, reverse=True))
_ISO_DAY_RX = re.compile(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUM_DAY_RX = re.compile(r"(?<![\d/-])(\d{1,2})[/-](\d{1,2})[/-](20\d{2})(?!\d)")          # day first, year required
_DAY_FIRST_RX = re.compile(r"(?<!\d)(\d{1,2})(st|nd|rd|th)?\s+(?:of\s+)?(" + _DAY_NAME + r")\b(?:,?\s*(20\d{2})(?!\d))?", re.I)
_MONTH_FIRST_RX = re.compile(r"\b(" + _DAY_NAME + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(20\d{2})(?!\d))?", re.I)
_RANGE_RX = re.compile(r"\b(between|from|since|until|till|before|after|through|week)\b", re.I)
_RANK_RX = re.compile(r"\b(top|first|last|best|worst|next|previous|past)\s+$", re.I)


def _this_year() -> int:
    return date.today().year


def _day_candidates(t: str, default_year=None) -> list:
    """Every date-looking span in the text: (start, end, iso or "" when not a real date, bare number?)."""
    found = []
    for m in _ISO_DAY_RX.finditer(t):
        found.append((m.start(), m.end(), m.group(1), m.group(2), m.group(3), False))
    for m in _NUM_DAY_RX.finditer(t):
        found.append((m.start(), m.end(), m.group(3), m.group(2), m.group(1), False))
    for m in _DAY_FIRST_RX.finditer(t):
        mo = MONTHS[m.group(3).lower()]
        y = m.group(4) or default_year or _this_year()
        found.append((m.start(), m.end(), y, mo, m.group(1), m.group(2) is None))
    for m in _MONTH_FIRST_RX.finditer(t):
        mo = MONTHS[m.group(1).lower()]
        y = m.group(3) or default_year or _this_year()
        found.append((m.start(), m.end(), y, mo, m.group(2), False))
    found.sort()
    out = []
    for c in found:                                     # a span already taken by an earlier match is the same date
        if not out or c[0] >= out[-1][1]:
            out.append(c)
    return out


def _iso_day(y, mo, d) -> str:
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except (ValueError, TypeError):
        return ""


def parse_day(text, default_year=None) -> str:
    """A day a tool argument names: "2026-11-04", "4 November 2026", "04/11/2026". "" when it is not one day."""
    t = str(text or "")
    cands = _day_candidates(t, default_year)
    if len(cands) != 1:
        return ""
    s, e, y, mo, d, _ = cands[0]
    return _iso_day(y, mo, d)


def day_from_request(text) -> str:
    """The one day a request asks data for, or "" (a range, several dates, a rank like "top 5", or no date)."""
    t = str(text or "")
    if len(_all_months(t)) > 1:
        return ""
    cands = _day_candidates(t)
    if len(cands) != 1:
        return ""
    s, e, y, mo, d, bare = cands[0]
    if _RANGE_RX.search(t[max(0, s - 15): s]):          # "from 4th november", "between ..." name a range
        return ""
    if bare and _RANK_RX.search(t[:s]):                  # "top 5 november deals": 5 is a rank
        return ""
    if re.match(r"\s*(?:to|until|till|through|[-–])\s*\d", t[e:], re.I):     # "4th november to 6th": a range
        return ""
    if (s and (t[s - 1] in "-/" or t[s - 1].isdigit())) or re.match(r"[-/]?\d", t[e:]):   # part of a longer number: "12-11-2026-7"
        return ""
    span = t[s:e]
    word = _NAME_RX.search(span)
    if word and word.group(1).lower() in ("may", "mar", "march"):           # English words too: "you may 5 times", "march 3 teams"
        sure = _YEAR_RX.search(span) or re.search(r"\d(?:st|nd|rd|th)\b", span, re.I)
        if not sure and not _ABOUT_RX.search(t[max(0, s - 25): e + 25]):
            return ""
    return _iso_day(y, mo, d)


def apply_request_period(args: dict, request_text) -> None:
    """Hand the day (or else the month) the user asked for to a kb_sql / data_sql call that did not name one."""
    if "day" not in args:                               # "" from the model means: no day, on purpose
        d = day_from_request(request_text)
        if d:
            args["day"] = d
            return
    if not args.get("month") and not args.get("day"):
        ym = from_request(request_text)
        if ym:
            args["month"] = ym
