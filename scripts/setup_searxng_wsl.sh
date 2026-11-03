#!/usr/bin/env bash
# Install and run SearXNG (meta-search: Google, Brave, Startpage, Mojeek, Wikipedia, Bing, DuckDuckGo) inside WSL2 or on
# any Linux box. No Docker. The app reads it through "web.searxng_url" in config/app.json and keeps DuckDuckGo/Bing as
# fallback.
#
#   bash scripts/setup_searxng_wsl.sh install   # once: clone, venv, settings.yml (new random secret)
#   bash scripts/setup_searxng_wsl.sh start     # run in the foreground (Ctrl+C stops it)
#   bash scripts/setup_searxng_wsl.sh check     # ask it a question and show which engines answered
#
# From Windows: powershell -File scripts\searxng_wsl.ps1 [install|start|check]
# Env: SEARXNG_DIR (default ~/searxng), SEARXNG_PORT (default 8888), SEARXNG_BIND (default 127.0.0.1;
# use 0.0.0.0 only on a trusted LAN box that another machine's app will query, and firewall it to that host).
set -euo pipefail

DIR="${SEARXNG_DIR:-$HOME/searxng}"
PORT="${SEARXNG_PORT:-8888}"
BIND="${SEARXNG_BIND:-127.0.0.1}"
SETTINGS="$DIR/settings.yml"

install_searxng() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3-venv python3-dev build-essential git libxslt-dev zlib1g-dev libffi-dev libssl-dev curl
  fi
  [ -d "$DIR/.git" ] || git clone --depth 1 https://github.com/searxng/searxng "$DIR"
  cd "$DIR"
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -U pip setuptools wheel pyyaml msgspec typing_extensions
  .venv/bin/pip install --use-pep517 --no-build-isolation -e .
  if [ ! -f "$SETTINGS" ]; then
    SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "$SETTINGS" <<EOF
use_default_settings: true
server:
  bind_address: "$BIND"
  port: $PORT
  secret_key: "$SECRET"
  limiter: false
  image_proxy: false
search:
  formats: [html, json]
outgoing:
  request_timeout: 5.0
  max_request_timeout: 8.0
engines:
  - {name: google,     disabled: false, weight: 1.4}
  - {name: brave,      disabled: false, weight: 1.2}
  - {name: startpage,  disabled: false, weight: 1.1}
  - {name: mojeek,     disabled: false, weight: 0.8}
  - {name: wikipedia,  disabled: false, weight: 1.3}
  - {name: bing,       disabled: false, weight: 1.0}
  - {name: duckduckgo, disabled: false, weight: 1.0}
EOF
    chmod 600 "$SETTINGS"
    echo "wrote $SETTINGS (secret generated; do not commit it)"
  else
    echo "$SETTINGS already exists - left unchanged"
  fi
  echo "installed. Start it with: bash scripts/setup_searxng_wsl.sh start"
}

start_searxng() {
  [ -x "$DIR/.venv/bin/python" ] || { echo "not installed - run: bash $0 install" >&2; exit 1; }
  cd "$DIR"
  echo "SearXNG on http://$BIND:$PORT (Ctrl+C to stop)"
  SEARXNG_SETTINGS_PATH="$SETTINGS" exec .venv/bin/python -m searx.webapp
}

check_searxng() {
  local url="http://127.0.0.1:$PORT/search?q=bangladesh+bank+exchange+rate&format=json"
  curl -sf --max-time 20 "$url" | python3 -c '
import json, sys
d = json.load(sys.stdin)
res = d.get("results", [])
print("results:", len(res))
for r in res[:5]:
    print(" -", ",".join(r.get("engines", [])), "|", r.get("url"))
print("unresponsive engines:", d.get("unresponsive_engines") or "none")
' || { echo "no answer from $url - is it running (start)?  JSON enabled in settings.yml?" >&2; exit 1; }
}

case "${1:-}" in
  install) install_searxng ;;
  start) start_searxng ;;
  check) check_searxng ;;
  *) echo "usage: $0 install|start|check" >&2; exit 2 ;;
esac
