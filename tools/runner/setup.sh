#!/bin/sh
# Install the a770 code runner on a fresh Alpine Linux VM (run as root, while the VM still has internet).
#
#   sh setup.sh                  install, start, print the certificate fingerprint and a pairing code
#   PORT=8470 sh setup.sh        another port
#
# Afterwards close the network with lockdown.sh: the runner needs no internet and nothing may reach it but the app server.
# This script was written for Alpine 3.19+ and has not been run on a real VM by its author: read it, then run it.
set -eu

[ "$(id -u)" = 0 ] || { echo "run this as root"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR=/opt/a770-runner
STATE_DIR=/var/lib/a770-runner
PORT="${PORT:-8470}"
USER_NAME=a770runner

echo "== packages"
apk update
apk add --no-cache python3 py3-pip bubblewrap openssl py3-lxml py3-pillow py3-numpy py3-matplotlib py3-openpyxl
apk add --no-cache font-noto-bengali font-dejavu || echo "(no Bengali font package here: charts with Bengali labels will show boxes)"
pip install --no-cache-dir --break-system-packages -r "$HERE/requirements.txt"

echo "== user, folders"
id "$USER_NAME" >/dev/null 2>&1 || adduser -S -D -H -s /sbin/nologin "$USER_NAME"
mkdir -p "$INSTALL_DIR" "$STATE_DIR"
cp "$HERE/runner_server.py" "$INSTALL_DIR/runner_server.py"
chown -R root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR" && chmod 644 "$INSTALL_DIR/runner_server.py"
chown "$USER_NAME" "$STATE_DIR" && chmod 700 "$STATE_DIR"

echo "== certificate (self-signed; the app pins its fingerprint)"
if [ ! -f "$STATE_DIR/cert.pem" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=a770-runner" -keyout "$STATE_DIR/key.pem" -out "$STATE_DIR/cert.pem" 2>/dev/null
fi
chown "$USER_NAME" "$STATE_DIR"/*.pem && chmod 600 "$STATE_DIR/key.pem"

echo "== can bubblewrap sandbox as $USER_NAME?"
if su -s /bin/sh "$USER_NAME" -c 'bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev true' 2>/dev/null; then
  echo "yes"
else
  echo "NO: unprivileged user namespaces are off. Try: sysctl -w user.max_user_namespaces=15000"
  echo "    (and persist it in /etc/sysctl.d/). The runner refuses to run code until bubblewrap works."
fi

echo "== service"
cat > /etc/init.d/a770-runner <<EOF
#!/sbin/openrc-run
name="a770-runner"
command="/usr/bin/python3"
command_args="$INSTALL_DIR/runner_server.py serve --state-dir $STATE_DIR --port $PORT"
command_user="$USER_NAME"
command_background=true
pidfile="/run/a770-runner.pid"
output_log="/var/log/a770-runner.log"
error_log="/var/log/a770-runner.log"
depend() { need net; }
EOF
chmod 755 /etc/init.d/a770-runner
touch /var/log/a770-runner.log && chown "$USER_NAME" /var/log/a770-runner.log
rc-update add a770-runner default >/dev/null
rc-service a770-runner restart

sleep 2
echo
echo "Runner address : https://$(hostname -i 2>/dev/null | awk '{print $1}'):$PORT"
echo "Certificate    : $(su -s /bin/sh "$USER_NAME" -c "python3 $INSTALL_DIR/runner_server.py fingerprint --state-dir $STATE_DIR")"
echo "Pairing code   : $(su -s /bin/sh "$USER_NAME" -c "python3 $INSTALL_DIR/runner_server.py pair-code --state-dir $STATE_DIR")   (valid 10 minutes; make a new one with the 'pair-code' command)"
echo
echo "Next: sh lockdown.sh  (APP_IP=<the app server's address>), then pair from the app: Settings -> Code Runner."
