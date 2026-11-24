"""Optional local Cross-Encoder reranker for Knowledge Base retrieval.

Supports:
1. Local ONNX model (via onnxruntime + tokenizers) configured in app.json:
   knowledge.reranker = {"onnx_path": "...", "tokenizer_path": "..."}
2. Local HTTP rerank endpoint (e.g. llama-server /v1/rerank):
   knowledge.reranker = {"endpoint": "http://127.0.0.1:8094/v1/rerank", "model": "..."}

Returns None when unconfigured or unavailable so reranker.py falls back to
calculate_cross_features() cleanly.
"""

import math
import sys
import threading
from pathlib import Path
from typing import List, Optional

_lock = threading.Lock()
_ort_session = None
_tokenizer = None
_loaded_key = None


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _reranker_cfg() -> dict:
    try:
        from ..small_model import APP_CONFIG
        cfg = (APP_CONFIG.get("knowledge") or {}).get("reranker")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _load_onnx(onnx_path: str, tok_path: str):
    global _ort_session, _tokenizer, _loaded_key
    key = (onnx_path, tok_path)
    if _loaded_key == key and _ort_session is not None and _tokenizer is not None:
        return _ort_session, _tokenizer
    with _lock:
        if _loaded_key == key and _ort_session is not None and _tokenizer is not None:
            return _ort_session, _tokenizer
        import onnxruntime as ort
        from tokenizers import Tokenizer
        sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        tok_file = Path(tok_path) / "tokenizer.json" if Path(tok_path).is_dir() else Path(tok_path)
        tok = Tokenizer.from_file(str(tok_file))
        tok.enable_truncation(max_length=512)
        tok.enable_padding(length=None)
        _ort_session, _tokenizer, _loaded_key = sess, tok, key
        return sess, tok


def _score_onnx(query: str, texts: List[str], cfg: dict) -> Optional[List[float]]:
    onnx_path = str(cfg.get("onnx_path") or "").strip()
    if not onnx_path or not Path(onnx_path).is_file():
        return None
    tok_path = str(cfg.get("tokenizer_path") or Path(onnx_path).parent)
    try:
        import numpy as np
        sess, tok = _load_onnx(onnx_path, tok_path)
        enc = tok.encode_batch([(query, t) for t in texts])
        input_ids = np.array([e.ids for e in enc], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
        input_names = {i.name for i in sess.get_inputs()}
        if "token_type_ids" in input_names:
            inputs["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        logits = sess.run(None, inputs)[0]
        flat = logits.reshape(len(texts), -1)[:, 0].tolist()
        return [_sigmoid(float(v)) for v in flat]
    except Exception as e:
        print(f"[cross_encoder] onnx scoring failed: {e}", file=sys.stderr)
        return None


def _score_http(query: str, texts: List[str], cfg: dict) -> Optional[List[float]]:
    endpoint = str(cfg.get("endpoint") or "").strip()
    if not endpoint:
        return None
    try:
        import httpx
        payload = {"query": query, "documents": texts, "top_n": len(texts)}
        if cfg.get("model"):
            payload["model"] = cfg["model"]
        with httpx.Client(timeout=float(cfg.get("timeout_s", 5.0))) as client:
            r = client.post(endpoint, json=payload)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or data.get("data") or []
        if not results:
            return None
        scores = [0.0] * len(texts)
        for item in results:
            idx = int(item.get("index", -1))
            if 0 <= idx < len(texts):
                raw = float(item.get("relevance_score", item.get("score", 0.0)))
                scores[idx] = raw if 0.0 <= raw <= 1.0 else _sigmoid(raw)
        return scores
    except Exception as e:
        print(f"[cross_encoder] http scoring failed: {e}", file=sys.stderr)
        return None


def score_pairs(query: str, texts: List[str]) -> Optional[List[float]]:
    """Score (query, text) pairs in [0.0, 1.0]. Returns None if unconfigured or unavailable."""
    if not query or not texts:
        return None
    cfg = _reranker_cfg()
    if not cfg or cfg.get("enabled") is False:
        return None
    if cfg.get("onnx_path"):
        res = _score_onnx(query, texts, cfg)
        if res is not None:
            return res
    if cfg.get("endpoint"):
        return _score_http(query, texts, cfg)
    return None
