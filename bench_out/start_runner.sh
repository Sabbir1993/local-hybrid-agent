set -e
S=$HOME/a770-runner-state
mkdir -p $S && chmod 700 $S
[ -f $S/cert.pem ] || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 30 -subj "/CN=a770-runner" -keyout $S/key.pem -out $S/cert.pem 2>/dev/null
chmod 600 $S/key.pem
exec python3 /mnt/e/AI/vulkan-arc/a770-dual-runtime/tools/runner/runner_server.py serve --state-dir $S --port 8470 --max-jobs 2 --host 0.0.0.0
