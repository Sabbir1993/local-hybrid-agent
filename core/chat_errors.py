"""What a person sees when a chat request fails.

A model server that crashes mid-request (a llama.cpp assertion, an out-of-memory kill) closes the connection;
httpx then raises an exception whose text is empty, and the chat showed a bare "Chat error:" with nothing to act
on. Transport failures now say what happened and what to do. The raw exception goes to the server log only:
httpx messages can carry local addresses and paths, and this text goes to a browser.
"""

import httpx


def friendly_chat_error(e: BaseException) -> str:
    if isinstance(e, httpx.TimeoutException):
        return ("⚠️ The model took too long to answer and the request timed out. Try again, or pick a smaller "
                "model in Settings → Models & Jobs if this keeps happening.")
    if isinstance(e, httpx.TransportError):
        return ("⚠️ The local model server stopped while answering - it may have crashed. It normally restarts "
                "by itself within a minute; send your message again then. If it keeps happening with this model, "
                "check the llama-server log (logs/llama-server-<date>.log) and the model's settings in "
                "Settings → Models & Jobs.")
    text = str(e).strip()
    return f"⚠️ Chat error: {text or type(e).__name__}"
