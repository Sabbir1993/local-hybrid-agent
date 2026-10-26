"""Picture for a product card (```products block): the model's own image address when a search handed it out,
otherwise the og:image / schema.org picture of the card's page.

The browser asks /media/product-image?u=<page>&i=<image>. Nothing is fetched unless the address came from a web tool
result for THIS user a moment ago (core/image_proxy.py for pictures, core/web_search/citations.py for pages), so a
model-invented address cannot be used to reach a host. Every fetch goes through core.net_guard (public addresses only).
"""

import time
from collections import OrderedDict
from typing import Optional

from . import image_proxy
from .web_search import citations

_PAGE_IMAGE_TTL_S = 3600
_page_image: "OrderedDict[str, tuple]" = OrderedDict()   # page url -> (expiry, image url or "")
_MAX_PAGES = 200


def _meta_image(page_url: str) -> str:
    """og:image of a page (cached, also when it has none, so a failing page is not retried per card)."""
    hit = _page_image.get(page_url)
    if hit and hit[0] > time.time():
        return hit[1]
    from .web_search import extraction
    from .web_search.constants import region
    image = ""
    try:
        r = extraction.fetch_page(page_url, 8, region()["accept"])
        if r.status_code < 400:
            image = extraction.extract_product_meta(
                extraction.decode_html(r.content, r.encoding), str(r.url)).get("image", "")
    except Exception:
        image = ""
    _page_image[page_url] = (time.time() + _PAGE_IMAGE_TTL_S, image)
    while len(_page_image) > _MAX_PAGES:
        _page_image.popitem(last=False)
    return image


def resolve(user_id: Optional[int], page_url: str, image_url: str) -> tuple:
    """(content type, bytes). Blocking: call it from a worker thread. Raises image_proxy.ProxyRefused."""
    page_url, image_url = (page_url or "").strip(), (image_url or "").strip()
    if image_url and image_proxy.is_allowed(user_id, image_url):
        try:
            return image_proxy.fetch(user_id, image_url)
        except image_proxy.ProxyRefused:
            pass                      # blocked hot-linking, expired file ...: try the page's own picture
    if page_url.lower().startswith("https://") and citations.known(user_id, page_url):
        img = _meta_image(page_url)
        if img:
            image_proxy.remember(user_id, [img])
            return image_proxy.fetch(user_id, img)
    raise image_proxy.ProxyRefused("no picture available for this product")
