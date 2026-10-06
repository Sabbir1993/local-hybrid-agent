"""tests/test_auth_invariant.py - every non-public route resolves an authenticated principal.

Run: python -m unittest tests.test_auth_invariant -v

Auth here is per-router, declared at include time in server_manager.py, with per-route
Depends layered on. That works, but it fails OPEN: add one include_router without the
list (or one handler without its Depends) and the endpoint is reachable with no session.
This test is the control that makes that structural risk visible. It walks the REAL app
object - include-time lists, decorator deps, and signature-level Depends via the
dependant tree - and asserts every route either resolves a principal or is on an
explicit, reviewed public list.

Adding a public endpoint means adding it to PUBLIC_HTTP/PUBLIC_WS below. That friction
is the point: going public must be a conscious, reviewed decision, not an omission.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.deps import get_current_user

# (method, path) reachable with no session, by design. Everything else must resolve
# a principal through get_current_user or require_permission.
PUBLIC_HTTP = {
    # static assets carry no sensitive data
    ("GET", "/static/{file_path:path}"),
    ("GET", "/favicon.ico"),
    # the login page itself
    ("GET", "/login"),
    # shell pages that check the session inside the handler (redirect to /login)
    ("GET", "/"),
    ("GET", "/settings"),
    # docs that check the session inside the handler
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    # the auth endpoints themselves (credential/cookie in, session out)
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    # MFA second step: the short-lived ticket (not a session) is the only
    # credential these accept -- reviewed with the MFA build
    ("POST", "/auth/mfa/verify"),
    ("POST", "/auth/mfa/ticket/enroll"),
    ("POST", "/auth/mfa/ticket/confirm"),
}

# WebSockets do their own auth inside the handler (Depends doesn't compose with the
# HTTP session flow on sockets).
PUBLIC_WS = {
    "/ws/companion",
}

# A few load-bearing routes pin their permission level, not just "some auth".
# (method, path) -> required permission key; None means get_current_user suffices.
PERMISSION_PINS = {
    ("POST", "/agent/run"): "chat.use",
    ("POST", "/chat/run"): "chat.use",
    ("GET", "/{path:path}"): "chat.use",
    ("POST", "/{path:path}"): "chat.use",
}


def _iter_routes():
    """(methods, path, is_websocket, dep_callables) for every effective route."""
    from server_manager import app

    def walk(router, inherited):
        for r in router.routes:
            t = type(r).__name__
            if t == "_IncludedRouter":
                ctx = getattr(r, "include_context", None)
                extra = [getattr(d, "dependency", d)
                         for d in (getattr(ctx, "dependencies", None) or [])]
                yield from walk(getattr(r, "original_router"), inherited + extra)
                continue
            if t not in ("APIRoute", "APIWebSocketRoute"):
                continue
            fns = list(inherited)
            seen = {id(f) for f in fns}
            stack = [getattr(r, "dependant", None)]
            seen_dep = set()
            while stack:
                cur = stack.pop()
                if cur is None or id(cur) in seen_dep:
                    continue
                seen_dep.add(id(cur))
                for sub in (getattr(cur, "dependencies", None) or []):
                    fn = getattr(sub, "call", None)
                    if fn is None or id(fn) in seen:
                        continue
                    seen.add(id(fn))
                    fns.append(fn)
                    stack.append(sub)
            methods = ",".join(sorted(getattr(r, "methods", None) or []))
            yield methods, r.path, t == "APIWebSocketRoute", fns

    for inc in app.routes:
        ctx = getattr(inc, "include_context", None)
        if ctx is None:
            continue
        base = [getattr(d, "dependency", d)
                for d in (getattr(ctx, "dependencies", None) or [])]
        yield from walk(getattr(inc, "original_router"), base)


def _is_authed(fns) -> bool:
    return any(f is get_current_user or
               getattr(f, "_require_permission_key", None) is not None
               for f in fns)


def _permission_keys(fns) -> set:
    return {getattr(f, "_require_permission_key")
            for f in fns if getattr(f, "_require_permission_key", None) is not None}


class AuthInvariant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = list(_iter_routes())
        assert cls.rows, "route walk found nothing - the walker is broken, not the app"

    def test_every_non_public_http_route_resolves_a_principal(self):
        open_routes = []
        for methods, path, is_ws, fns in self.rows:
            if is_ws:
                continue
            if not _is_authed(fns):
                for m in methods.split(","):
                    if (m, path) not in PUBLIC_HTTP:
                        open_routes.append(f"{m} {path}")
        self.assertEqual(open_routes, [],
                         "routes reachable with no session that are not on the reviewed "
                         "public list (add Depends or list them in PUBLIC_HTTP deliberately):\n"
                         + "\n".join(open_routes))

    def test_every_websocket_is_reviewed(self):
        for methods, path, is_ws, fns in self.rows:
            if not is_ws:
                continue
            self.assertIn(path, PUBLIC_WS,
                          f"websocket {path} is neither authenticated via Depends nor on "
                          f"the reviewed PUBLIC_WS list")

    def test_public_list_has_no_dead_entries(self):
        """Each PUBLIC entry must match a real route, or the list is stale camouflage."""
        seen = {(m, p) for methods, p, w, _ in self.rows if not w for m in methods.split(",")}
        for entry in PUBLIC_HTTP:
            self.assertIn(entry, seen, f"PUBLIC_HTTP entry {entry} matches no route - remove it")

    def test_sensitive_routes_carry_their_permission(self):
        by_route = {}
        for methods, path, is_ws, fns in self.rows:
            if is_ws:
                continue
            for m in methods.split(","):
                by_route.setdefault((m, path), set()).update(_permission_keys(fns))
        for (method, path), key in PERMISSION_PINS.items():
            self.assertIn((method, path), by_route, f"pinned route {method} {path} not found")
            self.assertIn(key, by_route[(method, path)],
                          f"{method} {path} lost its require_permission({key!r}) gate")

    def test_manual_check_pages_still_check(self):
        """Pages/docs without Depends authenticate inside the handler. If someone deletes
        that call, the route silently goes public - so pin the call itself."""
        root = Path(__file__).resolve().parent.parent
        pages = (root / "routes" / "pages.py").read_text(encoding="utf-8")
        docs = (root / "routes" / "api_docs.py").read_text(encoding="utf-8")
        for name in ("settings_page", "ui_root"):
            body = pages.split(f"async def {name}")[1].split("\nasync def ")[0]
            self.assertIn("get_current_user", body,
                          f"{name} no longer checks the session in-handler")
        for name in ("openapi_schema", "swagger_ui"):
            body = docs.split(f"async def {name}")[1].split("\nasync def ")[0]
            self.assertIn("get_current_user", body,
                          f"{name} no longer checks the session in-handler")

    def test_companion_socket_authenticates(self):
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "core" / "companion_bridge"
               / "routes.py").read_text(encoding="utf-8")
        body = src.split("async def companion_socket")[1].split("\nasync def ")[0]
        self.assertIn("_authenticate", body,
                      "companion websocket must authenticate the handshake in-handler")


if __name__ == "__main__":
    unittest.main()
