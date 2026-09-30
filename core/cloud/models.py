from copy import deepcopy
import re
from typing import Optional
from urllib.parse import urlparse

from ..config import CLOUD_TIMEOUT_S

_LLAMA_ONLY_KEYS = (
    "repeat_penalty", "grammar", "min_p", "top_k", "typical_p",
    "xtc_probability", "xtc_threshold", "n_keep", "cache_prompt"
)


class CloudModel:
    """One provider/model pair that can serve a lane."""

    def __init__(self, provider: str, arg2, arg3, model_cfg: dict = None):
        if isinstance(arg2, dict):
            provider_cfg = arg2
            model_id = str(arg3)
        else:
            model_id = str(arg2)
            provider_cfg = arg3 if isinstance(arg3, dict) else {}
        self.provider = provider
        self.provider_cfg = provider_cfg or {}
        self.prov_cfg = self.provider_cfg
        self.model_id = model_id
        self.model_cfg = model_cfg or {}
        self.options = self.provider_cfg.get("options") or {}
        self.key = f"{self.provider}/{self.model_id}"

    @property
    def base_url(self) -> str:
        url = str(self.options.get("baseURL") or self.options.get("base_url") or "").strip()
        return url.rstrip("/")

    @property
    def api_key(self) -> str:
        return str(self.options.get("apiKey") or self.options.get("api_key") or "").strip()

    @property
    def chat_path(self) -> str:
        return str(self.options.get("chat_path") or "").strip()

    def endpoint(self) -> str:
        """Absolute chat-completions URL for this provider.

        Handles the two common baseURL shapes:
          https://openrouter.ai/api/v1  -> .../api/v1/chat/completions
          https://api.groq.com/openai   -> .../openai/v1/chat/completions
        """
        base = self.base_url.rstrip("/")
        if self.chat_path:
            return base + "/" + self.chat_path.lstrip("/")
        if not base:
            return ""
        if re.search(r"/v\d+$", base):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    @property
    def timeout_s(self) -> float:
        try:
            return float(self.options.get("timeout_s") or CLOUD_TIMEOUT_S)
        except (TypeError, ValueError):
            return CLOUD_TIMEOUT_S

    @property
    def extra_headers(self) -> dict:
        h = self.options.get("extra_headers")
        return {str(k): str(v) for k, v in h.items()} if isinstance(h, dict) else {}

    @property
    def extra_body(self) -> dict:
        b = self.options.get("extra_body")
        return deepcopy(b) if isinstance(b, dict) else {}

    @property
    def stream_options(self) -> bool:
        return bool(self.options.get("stream_options", False))

    @property
    def context_length(self) -> int:
        """Window in tokens: the model's ctx, else the provider's, else 32768 - deliberately small,
        so a model with no configured window is not given a budget the provider may reject."""
        ctx = (self.model_cfg.get("ctx") or self.model_cfg.get("context_size")
               or self.provider_cfg.get("ctx"))
        try:
            return int(ctx) if ctx else 32768
        except (TypeError, ValueError):
            return 32768

    @property
    def ctx(self) -> int:
        return self.context_length

    @property
    def display_name(self) -> str:
        return str(self.model_cfg.get("name") or self.model_id)

    @property
    def provider_display(self) -> str:
        return str(self.prov_cfg.get("name") or self.provider)

    @property
    def provider_name(self) -> str:
        return self.provider_display


    @property
    def display(self) -> str:
        """The model's own name (the UI groups models under their provider)."""
        return self.display_name

    def label(self) -> str:
        return f"{self.display} ({self.provider_name})"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "provider": self.provider,
            "provider_name": self.provider_display,
            "model_id": self.model_id,
            "name": self.display_name,
            "context_length": self.context_length,
            "endpoint": self.endpoint(),
            "has_key": bool(self.api_key),
        }

    def url(self, path: str) -> str:
        """Absolute URL of another OpenAI-style endpoint (images, videos, audio)
        on this provider, e.g. url("/images/generations")."""
        base = self.base_url.rstrip("/")
        if not base:
            return ""
        if not re.search(r"/v\d+[a-z0-9]*$", base):
            base += "/v1"
        return base + "/" + path.lstrip("/")

    @property
    def is_google(self) -> bool:
        """Gemini API (generativelanguage.googleapis.com): Imagen / Veo use its native API."""
        return (urlparse(self.base_url).hostname or "").endswith("generativelanguage.googleapis.com")

    def info(self, lane: Optional[str] = None) -> dict:
        """Lane descriptor sent to the UI ("lane" SSE event) and used for usage records."""
        out = self._info_base(lane)
        out.update({"role": "Cloud lane", "source": "cloud", "provider": self.provider,
                    "provider_name": self.provider_name, "key": self.key})
        return out

    def _info_base(self, lane: Optional[str] = None) -> dict:
        return {
            "lane": lane,
            "model": self.model_id,
            "display": f"☁️ {self.display}",
            "device": f"{self.provider_name} (cloud)",
            "context_size": self.context_length,
            "max_tokens": None,
            "system_prompt": True,
            "can_stream": True,
            "can_vision": False,
        }

    def __repr__(self) -> str:
        return f"<CloudModel {self.key} -> {self.endpoint()}>"
