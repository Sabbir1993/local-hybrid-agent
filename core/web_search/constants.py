import re
from ..small_model import APP_CONFIG

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEARCH_TIMEOUT_S = 12
FETCH_MAX_BYTES = 5 * 1024 * 1024
MAX_RESULTS = 8
SNIPPET_CHARS = 300
PASSAGE_CHARS = 700
AUTO_FETCH_BUDGET_S = 7.0
CACHE_TTL_S = 600
CACHE_MAX = 256

REGIONS = {
    "bd-en": {"ddg": "wt-wt", "bing_cc": "BD", "bing_lang": "en", "accept": "en-BD,en;q=0.9,bn;q=0.7"},
    "bd-bn": {"ddg": "wt-wt", "bing_cc": "BD", "bing_lang": "bn", "accept": "bn-BD,bn;q=0.9,en;q=0.7"},
    "us-en": {"ddg": "us-en", "bing_cc": "US", "bing_lang": "en", "accept": "en-US,en;q=0.9"},
    "uk-en": {"ddg": "uk-en", "bing_cc": "GB", "bing_lang": "en", "accept": "en-GB,en;q=0.9"},
    "in-en": {"ddg": "in-en", "bing_cc": "IN", "bing_lang": "en", "accept": "en-IN,en;q=0.9"},
    "wt-wt": {"ddg": "wt-wt", "bing_cc": "", "bing_lang": "en", "accept": "en;q=0.9"},
}
RECENCY = {
    "day": ("d", 'ex1:"ez1"'),
    "week": ("w", 'ex1:"ez2"'),
    "month": ("m", 'ex1:"ez3"'),
    "year": ("y", "")
}

_STOP = set("a an and are as at be by for from how i in is it of on or that the this to was what when "
            "where which who why will with do does can you me my your latest current today".split())

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "button", "nav", "header",
              "footer", "aside", "template", "select", "input", "canvas")
_BOILER_RX = re.compile(r"(cookie|consent|gdpr|banner|subscribe|newsletter|sidebar|comment|share|social|"
                        r"advert|\bads?\b|promo|related|breadcrumb|\bmenu\b|navbar|popup|modal|footer|header)", re.I)
_META_CHARSET_RX = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)
_BLOCKS = {"p", "div", "section", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table", "dd", "dt"}
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|msclkid$|ref$|ref_src$)")

_REWRITE_PROMPT = (
    "Rewrite the user's message as ONE concise web search query (keywords, max 12 words). "
    "Keep names, places, product/version numbers and the year if the message implies recent "
    "information. Reply with the query only, no quotes or explanation.")


def cfg() -> dict:
    c = APP_CONFIG.get("web")
    return c if isinstance(c, dict) else {}


def region() -> dict:
    return REGIONS.get(str(cfg().get("region") or "bd-en").lower(), REGIONS["bd-en"])
