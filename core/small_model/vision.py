import base64
import time
from typing import Optional


def image_mime(data: bytes) -> Optional[str]:
    """Sniff the real image type from magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


async def describe_image_bytes(data: bytes, question: str = "Describe this image in detail.",
                                force_local: bool = False) -> str:
    """Send one image through the vision lane (cloud when bound, else local)."""
    mime = image_mime(data)
    if mime is None:
        return "error: not a PNG, JPEG, WEBP or GIF image"
    b64 = base64.b64encode(data).decode()
    payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.1,
    }
    from .. import cloud, lanes
    explicit = "vision" in cloud.role_map()
    try:
        from ..state import state
        main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
        if not explicit and main_ready and bool((state.profile or {}).get("vision_capable")):
            r = await state.client.post("/v1/chat/completions", json=payload, timeout=None)
            state.last_activity = time.time()
            data = r.json()
            return ((data.get("choices") or [{}])[0].get("message", {}).get("content")
                    or "(main vision model returned no text)")
    except Exception as e:
        print(f"[vision] main model vision failed: {e} - falling back to vision lane/model")

    try:
        data, _t = await lanes.post_chat("vision", payload, force_local=force_local)
    except RuntimeError as e:
        return f"error: no image model available ({e})"
    return lanes.message_text(data) or "(vision model returned no text)"
