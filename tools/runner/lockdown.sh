#!/bin/sh
# Close the runner's network: only the app server may connect (one port), nothing goes out. Run as root on the runner VM.
#
#   APP_IP=10.20.0.5 sh lockdown.sh                 the app server's address (required)
#   APP_IP=10.20.0.5 ADMIN_IP=10.20.0.9 sh lockdown.sh   also allow ssh from one admin address
#
# The virtual switch / cloud security group should say the same thing: this is the second lock, not the only one.
# Written for Alpine with nftables; not run on a real VM by its author - read it first.
set -eu
[ "$(id -u)" = 0 ] || { echo "run this as root"; exit 1; }
: "${APP_IP:?set APP_IP to the app server address}"
PORT="${PORT:-8470}"
apk add --no-cache nftables >/dev/null
ADMIN_RULE=""
[ -n "${ADMIN_IP:-}" ] && ADMIN_RULE="ip saddr $ADMIN_IP tcp dport 22 accept"
cat > /etc/nftables.nft <<EOF
flush ruleset
table inet runner {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ip saddr $APP_IP tcp dport $PORT accept
    $ADMIN_RULE
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output {
    type filter hook output priority 0; policy drop;
    oif lo accept
    ct state established,related accept
  }
}
EOF
nft -f /etc/nftables.nft
rc-update add nftables default >/dev/null 2>&1 || true
echo "locked: inbound only $APP_IP -> tcp/$PORT${ADMIN_IP:+ and ssh from $ADMIN_IP}; outbound nothing new."
echo "check from the VM: 'wget -T 3 -qO- https://example.com' must fail."
