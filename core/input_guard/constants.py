# Regex size cap: guards against pathological admin input at config time.
_MAX_PATTERN_LEN = 500
_CACHE_TTL_S = 30.0   # re-read APP_CONFIG at most this often

DEFAULT_MESSAGE = (
    "Your prompt was blocked by an administrator-defined input policy "
    "({rule_name})."
)

SEMANTIC_TIMEOUT_S = 20   # local classifier budget per rule
_MAX_SEM_TEXT = 8000      # chars of scanned text sent to the classifier
_NULL_WORDS = {"", "null", "all", "none", "everyone"}
