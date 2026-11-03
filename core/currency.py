"""
core/currency.py - Real currency conversion to Bangladeshi Taka (BDT) for model output.

A model that sees "$100" or "Rs 5,000" in a web page must not write "Tk 100": the amount has to be
converted at an actual exchange rate. output_guard calls convert_text() on every response, so a
foreign amount "$100" becomes "৳12,200 (≈ $100)". Amounts the model writes directly in Tk / ৳ / BDT
are left alone.

Rates: config/fx_rates.json is a cache of https://open.er-api.com/v6/latest/USD (free, no key, no user
data sent), refreshed in a background thread when older than fx.cache_hours. Config (app.json "fx"):
  {"auto_convert": true, "cache_hours": 12, "source_url": "...", "overrides": {"USD": 122.5}}
"overrides" (BDT per 1 unit, e.g. a Bangladesh Bank or bank rate) beat the live rate. With no rate for
a currency the amount is left untouched - never guessed.
"""

import json
import re
import threading
import time
from datetime import datetime
from typing import Optional

from .config import CONFIG_DIR, atomic_write_json

CACHE_FILE = CONFIG_DIR / "fx_rates.json"
DEFAULT_SOURCE = "https://open.er-api.com/v6/latest/USD"
HOLD = 40            # streaming holdback (chars): longest amount like "US$ 12,345,678.90 million"

_lock = threading.Lock()
_refreshing = False
_mem: dict = {}      # {"ts": float, "date": "YYYY-MM-DD", "rates": {code: per-USD}}

_SYMBOLS = {"US$": "USD", "$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}
_CODES = ("USD", "INR", "EUR", "GBP", "AED", "SAR", "SGD", "MYR", "CAD", "AUD", "CNY", "JPY", "KWD", "QAR")
_SCALE = {"k": 1e3, "thousand": 1e3, "lakh": 1e5, "lac": 1e5, "m": 1e6, "mn": 1e6, "million": 1e6,
          "crore": 1e7, "cr": 1e7, "b": 1e9, "bn": 1e9, "billion": 1e9}
_NUM = r"\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?"
_SC = r"(?:\s?(?P<sc>thousand|lakh|lac|crore|million|billion|mn|bn|cr|[kKmMbB])\b)?"
AMOUNT_RX = re.compile(
    r"(?<![\w$])(?:(?P<sym>US\$|\$|₹|€|£)\s?(?P<n1>" + _NUM + r")" + _SC.replace("sc", "sc1") +
    r"|(?P<c1>" + "|".join(_CODES) + r")\s?(?P<n2>" + _NUM + r")" + _SC.replace("sc", "sc2") +
    r"|(?P<n3>" + _NUM + r")" + _SC.replace("sc", "sc3") + r"\s?(?P<c2>" + "|".join(_CODES) + r"))(?![\w])")
_FENCE = "```"


def cfg() -> dict:
    from .small_model import APP_CONFIG
    c = APP_CONFIG.get("fx")
    return c if isinstance(c, dict) else {}


def enabled() -> bool:
    return cfg().get("auto_convert", True) is not False


def _load_cache() -> dict:
    if _mem:
        return _mem
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("rates"), dict):
            _mem.update(data)
    except Exception:
        pass
    return _mem


def _fetch() -> None:
    global _refreshing
    try:
        import urllib.request
        url = str(cfg().get("source_url") or DEFAULT_SOURCE)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fx-rates)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        rates = data.get("rates") or data.get("conversion_rates")
        if isinstance(rates, dict) and float(rates.get("BDT") or 0) > 0:
            clean = {str(k).upper(): float(v) for k, v in rates.items() if isinstance(v, (int, float)) and v > 0}
            with _lock:
                _mem.clear()
                _mem.update({"ts": time.time(), "date": datetime.now().strftime("%Y-%m-%d"), "rates": clean})
                atomic_write_json(CACHE_FILE, dict(_mem))
    except Exception:
        pass
    finally:
        _refreshing = False


def refresh(block: bool = False) -> None:
    """Fetch fresh rates (background thread unless block=True); no-op while one is running."""
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    if block:
        _fetch()
    else:
        threading.Thread(target=_fetch, daemon=True, name="fx-refresh").start()


def _rates() -> dict:
    c = _load_cache()
    hours = float(cfg().get("cache_hours") or 12)
    if not c or time.time() - float(c.get("ts") or 0) > hours * 3600:
        refresh()
    return c.get("rates") or {}


def bdt_per(code: str) -> Optional[float]:
    """BDT for 1 unit of `code`, or None when no rate is known."""
    code = code.upper()
    if code == "BDT":
        return 1.0
    ov = (cfg().get("overrides") or {}).get(code)
    if isinstance(ov, (int, float)) and ov > 0:
        return float(ov)
    r = _rates()
    if r.get("BDT") and r.get(code):
        return r["BDT"] / r[code]
    return None


def rate_info(code: str = "USD") -> dict:
    c = _load_cache()
    return {"currency": code.upper(), "bdt_per_unit": bdt_per(code), "as_of": c.get("date"),
            "source": "override" if (cfg().get("overrides") or {}).get(code.upper()) else DEFAULT_SOURCE}


def convert(amount: float, from_ccy: str, to_ccy: str = "BDT") -> Optional[float]:
    a, b = bdt_per(from_ccy), bdt_per(to_ccy)
    if not a or not b:
        return None
    return amount * a / b


def _group_lakh(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def format_bdt(x: float) -> str:
    """৳12,200 · ৳12,50,000 (lakh grouping) · ৳61.5 crore for 1 crore and up."""
    if x >= 1e7:
        v = x / 1e7
        txt = f"{v:,.2f}".rstrip("0").rstrip(".") if v < 1000 else _group_lakh(round(v))
        return f"৳{txt} crore"
    if x >= 100:
        return f"৳{_group_lakh(round(x))}"
    return "৳" + f"{x:.2f}".rstrip("0").rstrip(".")


def _repl(m: re.Match) -> str:
    before = m.string[max(0, m.start() - 3):m.start()]
    if before.endswith("≈ "):
        return m.group(0)                      # already our own "(≈ $100)" annotation
    if m.group("sym"):
        code, num, sc = _SYMBOLS[m.group("sym")], m.group("n1"), m.group("sc1")
    elif m.group("c1"):
        code, num, sc = m.group("c1"), m.group("n2"), m.group("sc2")
    else:
        code, num, sc = m.group("n3") and m.group("c2"), m.group("n3"), m.group("sc3")
    try:
        amount = float(num.replace(",", "")) * (_SCALE.get((sc or "").lower(), 1.0))
    except ValueError:
        return m.group(0)
    bdt = convert(amount, code)
    if bdt is None:
        return m.group(0)
    return f"{format_bdt(bdt)} (≈ {m.group(0)})"


def convert_text(text: str, in_fence: bool = False) -> tuple[str, bool]:
    """Convert foreign amounts in `text` to BDT, skipping fenced and inline code.

    Returns (new_text, in_fence_after) so a streaming caller can carry the fence state across chunks."""
    if not text or not enabled():
        return text, in_fence
    out = []
    for i, seg in enumerate(text.split(_FENCE)):
        if i:
            in_fence = not in_fence
        if in_fence:
            out.append(seg)
        else:
            out.append("`".join(AMOUNT_RX.sub(_repl, s) if j % 2 == 0 else s
                               for j, s in enumerate(seg.split("`"))))
    return _FENCE.join(out), in_fence
