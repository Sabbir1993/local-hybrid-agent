import ipaddress
import re
from typing import Optional
from urllib.parse import urlparse
from core.config import BASE_DIR
from core.net_guard import BlockedURLError, check_url
from core.small_model import APP_CONFIG

from .models import McpServerReq


_NAME_RX = re.compile(r"^[a-z0-9_-]{1,32}$")


_ENV_KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


_PKG_RX = re.compile(r"^(@[a-z0-9._-]+/)?[a-z0-9._-]{1,100}$")


DEFAULT_ALLOWED_COMMANDS = ["npx", "node", "python", "uvx"]


# node/python are only accepted to run a script file shipped inside this repo
# (e.g. core/echo_mcp_server.py) -- never -c / -e / -m or a path outside BASE_DIR.
_SCRIPT_COMMANDS = ("node", "python")


_USER_COMMANDS = ("npx", "uvx")


# env vars that change how npx/uvx/node/python resolve or load code - a non-admin could
# otherwise point the approved package at another registry or preload their own script
_USER_ENV_DENY_PREFIXES = ("NODE_", "NPM_", "NPX_", "UV_", "PIP_", "PYTHON", "LD_", "DYLD_", "SSL_")


_USER_ENV_DENY = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "APPDATA",
                  "LOCALAPPDATA", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                  "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}


def _allowed_commands() -> list:
    return APP_CONFIG.get("capabilities", {}).get("mcp_allowed_commands") or DEFAULT_ALLOWED_COMMANDS


def _user_packages() -> list:
    """Packages a non-admin may run as a personal stdio server (capabilities.mcp_user_allowed_packages)."""
    return [str(p).strip().lower() for p in
            (APP_CONFIG.get("capabilities", {}).get("mcp_user_allowed_packages") or []) if str(p).strip()]


# name, optional version: '@scope/pkg', 'pkg@1.2.3', 'pkg@latest', 'pkg==1.0', 'pkg[extra]'.
# No aliases (npm:), URLs, git+/file: specs, paths, or whitespace -- all of those make npx/uvx
# install something other than the approved name.
_SPEC_RX = re.compile(
    r"^(@[a-z0-9._-]+/)?[a-z0-9._-]{1,100}(\[[a-z0-9,_-]+\])?"
    r"((@|==)[a-z0-9.+^~*-]{1,64})?$")


def _package_base(spec: str) -> str:
    """'@scope/pkg@1.2' / 'pkg@latest' / 'pkg==1.0' / 'pkg[extra]' -> the bare package name."""
    spec = spec.strip().lower()
    if spec.startswith("@"):
        at = spec.find("@", 1)
        return spec if at == -1 else spec[:at]
    return re.split(r"[@=<>!~\[ ]", spec, maxsplit=1)[0]


def _validate_user_stdio(req: McpServerReq) -> Optional[str]:
    cmd = (req.command or "").strip()
    if cmd not in _USER_COMMANDS:
        return f"personal stdio servers must use {' or '.join(_USER_COMMANDS)} with an approved package"
    args = [str(a) for a in req.args if str(a) != ""]
    i = 0
    # only npx's -y/--yes may precede the package: --package, --from, --with, --index ...
    # would pull in something other than the approved package
    while i < len(args) and cmd == "npx" and args[i] in ("-y", "--yes"):
        i += 1
    if i >= len(args) or args[i].startswith("-"):
        return f"the first {cmd} argument must be the package name (only -y may come before it for npx)"
    if not _SPEC_RX.match(args[i].strip().lower()):
        return f"'{args[i]}' is not a plain package spec (use name or name@version)"
    pkg = _package_base(args[i])
    allowed = _user_packages()
    if pkg not in allowed:
        return (f"package '{pkg}' is not approved for personal servers; approved: "
                f"{', '.join(allowed) or '(none - ask an admin)'}")
    return None


def _validate_script_command(cmd: str, args: list) -> Optional[str]:
    args = [str(a) for a in args if str(a) != ""]
    if not args or args[0].startswith("-"):
        return f"'{cmd}' may only run a script file from this app's folder (no -c/-e/-m)"
    script = (BASE_DIR / args[0]).resolve()
    if not script.is_file() or not script.is_relative_to(BASE_DIR):
        return f"'{cmd}' script must be an existing file inside {BASE_DIR}"
    return None


def _public_url_error(url: str) -> Optional[str]:
    u = urlparse(url or "")
    if not (u.scheme == "https" and u.hostname) or _is_internal_host(u.hostname):
        return "url must be a public https:// address"
    try:
        check_url(url)   # resolves DNS: 127.0.0.1.nip.io and friends are caught here
    except BlockedURLError as e:
        return f"url must be a public https:// address ({e})"
    return None


def _is_internal_host(host: str) -> bool:
    """localhost and loopback / private / link-local IP literals - a personal server must not
    make this host call its own internal services."""
    h = (host or "").strip("[]").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def _is_preset_command(req: McpServerReq, preset: Optional[dict]) -> bool:
    if not preset or preset.get("transport") != "stdio":
        return False
    return (req.command, [str(a) for a in req.args]) == (preset.get("command"), [str(a) for a in preset.get("args", [])])


def _validate(req: McpServerReq, restricted: bool = False, preset: Optional[dict] = None) -> Optional[str]:
    """`preset`: the reviewed catalog entry this request was built from. A restricted user may then run that
    entry's exact command without the package being on the personal allow-list; nothing else is relaxed."""
    if not _NAME_RX.match(req.name or ""):
        return "name must be 1-32 chars of a-z, 0-9, _ or -"
    if req.scope not in ("global", "user"):
        return "scope must be 'global' or 'user'"
    if req.transport not in ("stdio", "http"):
        return "transport must be 'stdio' or 'http'"
    if req.transport == "stdio":
        if restricted and _is_preset_command(req, preset):
            pass     # exactly the reviewed catalog command
        elif restricted:
            err = _validate_user_stdio(req)
            if err:
                return err
        else:
            cmd = (req.command or "").strip()
            if cmd not in _allowed_commands():
                return (f"command '{cmd}' is not allowed; allowed: {', '.join(_allowed_commands())} "
                        f"(capabilities.mcp_allowed_commands in config/app.json)")
            if cmd in _SCRIPT_COMMANDS:
                err = _validate_script_command(cmd, req.args)
                if err:
                    return err
    else:
        u = urlparse(req.url or "")
        if restricted:
            err = _public_url_error(req.url)
            if err:
                return err
        else:
            local = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
            if not (u.scheme == "https" and u.hostname) and not local:
                return "url must be https:// (or http://127.0.0.1 / http://localhost)"
    if req.transport == "http":
        err = _validate_auth(req, restricted)
        if err:
            return err
    elif req.auth or req.headers:
        return "auth/headers only apply to http servers"
    for k in list(req.env) + list(req.secret_env):
        if not _ENV_KEY_RX.match(str(k)):
            return f"invalid env var name '{k}'"
        ku = str(k).upper()
        if restricted and (ku in _USER_ENV_DENY or ku.startswith(_USER_ENV_DENY_PREFIXES)):
            return f"env var '{k}' is not allowed on a personal server"
    return None


_HEADER_RX = re.compile(r"^[A-Za-z0-9-]{1,64}$")


# secrets belong in secret_env (keychain), never in plain headers stored in the config
_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}


def _validate_auth(req: McpServerReq, restricted: bool) -> Optional[str]:
    for k, v in (req.headers or {}).items():
        if not _HEADER_RX.match(str(k)):
            return f"invalid header name '{k}'"
        if str(k).lower() in _SECRET_HEADERS or str(k).lower().startswith("mcp-"):
            return f"header '{k}' can't be set here (put secrets in secret env, e.g. AUTHORIZATION)"
        if len(str(v)) > 512:
            return f"header '{k}' is too long"
    a = req.auth
    if not a:
        return None
    if not isinstance(a, dict) or a.get("type") != "oauth":
        return "auth.type must be 'oauth'"
    if "client_secret" in a:
        return "client_secret goes in secret env as OAUTH_CLIENT_SECRET"
    unknown = set(a) - {"type", "client_id", "scopes", "auth_url", "token_url", "redirect_port",
                        "extra_params", "resource_param"}
    if unknown:
        return f"unknown auth field(s): {', '.join(sorted(unknown))}"
    for k in ("auth_url", "token_url"):
        if a.get(k):
            err = _public_url_error(str(a[k])) if restricted else (
                None if urlparse(str(a[k])).scheme == "https" else f"auth.{k} must be https://")
            if err:
                return f"auth.{k}: {err}"
    port = a.get("redirect_port") or 0
    if not isinstance(port, int) or not (port == 0 or 1024 <= port <= 65535):
        return "auth.redirect_port must be 0 or 1024-65535"
    scopes = a.get("scopes") or []
    if not isinstance(scopes, list) or not all(isinstance(x, str) and len(x) < 200 for x in scopes):
        return "auth.scopes must be a list of strings"
    if not isinstance(a.get("extra_params") or {}, dict):
        return "auth.extra_params must be an object"
    return None
