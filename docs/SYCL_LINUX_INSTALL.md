# Install the llama.cpp SYCL Runtime on Linux

This runbook installs an Intel oneAPI/Level Zero llama.cpp build beside the
existing Vulkan runtime. It does not replace the Python virtual environment or
the Vulkan files.

The application uses exactly one runtime per start:

```text
/var/web/html_new/local_ai/llama-vulkan   existing Vulkan runtime
/var/web/html_new/local_ai/llama-sycl    stable link to the qualified SYCL build
```

The examples assume Ubuntu 22.04 or 24.04, systemd, and an Intel discrete GPU.
For another distribution, use its Intel GPU and oneAPI package instructions but
keep the runtime layout and application configuration shown here.

## 1. Record the working Vulkan baseline

```bash
cd /var/web/html_new/local_ai
cp config/app.json config/app.json.before-sycl
./llama-vulkan/llama-bench --list-devices
```

Keep `/var/web/html_new/local_ai/llama-vulkan` unchanged. Confirm the current
application can still start with `LLAMA_RUNTIME=vulkan` before proceeding.

## 2. Install Intel GPU and oneAPI prerequisites

Configure Intel's package repositories for the exact Ubuntu release by following
Intel's current Linux GPU and oneAPI installation instructions. Then install the
equivalent of:

```bash
sudo apt update
sudo apt install -y \
  git cmake ninja-build build-essential pkg-config clinfo \
  intel-level-zero-gpu level-zero intel-opencl-icd \
  intel-oneapi-compiler-dpcpp-cpp
```

Package names can change between Intel repository generations. Do not substitute
Mesa's OpenCL implementation for the Intel Level Zero runtime used by SYCL.

Set the existing systemd unit name, read its service account, and grant that
account GPU-device access. Replace `local-ai.service` with the real unit name:

```bash
SYSTEMD_UNIT=local-ai.service
APP_USER="$(systemctl show --property=User --value "$SYSTEMD_UNIT")"
test -n "$APP_USER" || {
  echo "ERROR: $SYSTEMD_UNIT has no non-root User=; configure a service account first" >&2
  exit 1
}
APP_GROUP="$(systemctl show --property=Group --value "$SYSTEMD_UNIT")"
APP_GROUP="${APP_GROUP:-$(id -gn "$APP_USER")}"
sudo usermod -aG render,video "$APP_USER"
```

Restart the service login session or reboot after changing group membership.

Verify the devices as the service account:

```bash
ls -l /dev/dri/renderD*
sudo -u "$APP_USER" bash -lc '
  source /opt/intel/oneapi/setvars.sh --force
  sycl-ls
  clinfo -l
'
```

Every GPU intended for tensor splitting must appear through Level Zero. Stop and
fix driver, permissions, or enumeration problems before compiling llama.cpp.

## 3. Build a pinned llama.cpp revision

The initial parity target is `b10840`, matching the documented Vulkan deployment.
Do not build production servers from a moving `master` branch.

```bash
sudo install -d -m 0755 /opt/build
sudo chown "$APP_USER":"$APP_GROUP" /opt/build

sudo -u "$APP_USER" git clone \
  --branch b10840 --depth 1 \
  https://github.com/ggml-org/llama.cpp.git \
  /opt/build/llama.cpp-sycl

sudo -u "$APP_USER" bash -lc '
  set -euo pipefail
  source /opt/intel/oneapi/setvars.sh --force
  cd /opt/build/llama.cpp-sycl
  cmake -S . -B build-sycl -G Ninja \
    -DGGML_SYCL=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=icx \
    -DCMAKE_CXX_COMPILER=icpx
  cmake --build build-sycl \
    --target llama-server llama-bench \
    --parallel
'
```

If `b10840` does not compile with the installed oneAPI version, qualify one newer
llama.cpp revision and pin its exact commit on every SYCL server. Do not silently
use different revisions across machines.

## 4. Deploy beside Vulkan

Deploy the complete `build-sycl/bin` directory because `llama-server` and
`llama-bench` may depend on GGML shared libraries stored beside them.

```bash
APP_ROOT=/var/web/html_new/local_ai
SYCL_RELEASE="$APP_ROOT/llama-sycl-b10840"

sudo install -d "$SYCL_RELEASE"
sudo cp -a /opt/build/llama.cpp-sycl/build-sycl/bin/. "$SYCL_RELEASE/"
sudo chown -R "$APP_USER":"$APP_GROUP" "$SYCL_RELEASE"

sudo -u "$APP_USER" bash -lc '
  source /opt/intel/oneapi/setvars.sh --force
  cd /opt/build/llama.cpp-sycl
  {
    printf "llama.cpp commit: "
    git rev-parse HEAD
    icpx --version | head -n 1
    cmake --version | head -n 1
    printf "%s\n" "GGML_SYCL=ON" "CMAKE_BUILD_TYPE=Release"
  } > /var/web/html_new/local_ai/llama-sycl-b10840/BUILD_INFO.txt
'

ldd "$SYCL_RELEASE/llama-server"
ldd "$SYCL_RELEASE/llama-bench"
```

Neither `ldd` command may contain `not found`.

Create the stable path only after dependency verification:

```bash
sudo ln -sfn llama-sycl-b10840 /var/web/html_new/local_ai/llama-sycl
```

## 5. Create the SYCL cache and service environment

```bash
sudo install -d /var/cache/local-ai/sycl
sudo chown "$APP_USER":"$APP_GROUP" /var/cache/local-ai/sycl
```

The process that starts the existing `.venv` must initialize oneAPI only when
SYCL is selected. A wrapper such as
`/var/web/html_new/local_ai/start-local-ai.sh` can be used:

```bash
#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/var/web/html_new/local_ai
PYTHON="$APP_ROOT/.venv/bin/python"
RUNTIME="${LLAMA_RUNTIME:-}"

if [[ -z "$RUNTIME" ]]; then
  RUNTIME="$("$PYTHON" -c 'import json; print(json.load(open("/var/web/html_new/local_ai/config/app.json", encoding="utf-8")).get("runtime", "vulkan"))')"
fi

case "$RUNTIME" in
  sycl)
    source /opt/intel/oneapi/setvars.sh --force
    export ONEAPI_DEVICE_SELECTOR=level_zero:gpu
    export SYCL_CACHE_PERSISTENT=1
    export SYCL_CACHE_DIR=/var/cache/local-ai/sycl
    ;;
  vulkan|cuda)
    ;;
  *)
    echo "Unknown LLAMA_RUNTIME: $RUNTIME" >&2
    exit 2
    ;;
esac

export LLAMA_RUNTIME="$RUNTIME"
cd "$APP_ROOT"
exec "$PYTHON" server_manager.py
```

```bash
sudo chmod 0755 /var/web/html_new/local_ai/start-local-ai.sh
```

Point the existing systemd unit's `ExecStart` at this wrapper, preserve its
existing `User=`, `Group=`, working directory, restart policy, and limits, then
run:

```bash
sudo systemctl daemon-reload
```

Do not set `ZE_AFFINITY_MASK` to one GPU when multi-GPU inference is required.
The application passes exact llama.cpp device IDs using `-dev`.

## 6. Register the runtime

Add this sibling entry under `runtimes` in `config/app.json`:

```json
"sycl": {
  "llama_bin_dir": "/var/web/html_new/local_ai/llama-sycl",
  "backend": "sycl",
  "gpu_devices": "auto",
  "small_model_gpu": 0,
  "env": {
    "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu",
    "SYCL_CACHE_PERSISTENT": "1",
    "SYCL_CACHE_DIR": "/var/cache/local-ai/sycl"
  }
}
```

Keep the saved top-level runtime as Vulkan during qualification:

```json
"runtime": "vulkan"
```

The preflight section should reserve both device and host memory:

```json
"preflight": {
  "mode": "block",
  "headroom_gb": 1.0,
  "host_headroom_gb": 4.0
}
```

## 7. Verify the installed runtime

Run device discovery through the same environment used by the service:

```bash
sudo -u "$APP_USER" bash -lc '
  source /opt/intel/oneapi/setvars.sh --force
  export ONEAPI_DEVICE_SELECTOR=level_zero:gpu
  /var/web/html_new/local_ai/llama-sycl/llama-bench --list-devices
'
```

Save the output. The IDs shown there, such as `SYCL0` and `SYCL1`, are the IDs
the application must preserve in `-dev` ordering.

Start one qualification run without changing the saved default:

```bash
cd /var/web/html_new/local_ai
LLAMA_RUNTIME=sycl ./start-local-ai.sh
```

Startup must fail rather than fall back to Vulkan if the SYCL binary, libraries,
or configured devices are unavailable.

## 8. Qualify multi-GPU tensor splitting

Test in this order:

1. Each Intel GPU individually.
2. All GPUs using `split_mode: "layer"` and an equal split.
3. An asymmetric split to prove that split entries map to the correct device IDs.
4. The VRAM-proportional split produced by autotune.
5. Main and helper models together.

Example two-GPU profile fields:

```json
"gpu_devices": [0, 1],
"split_mode": "layer",
"runtime_tuned": {
  "sycl": {
    "tensor_split": "9,11",
    "n_gpu_layers": 999
  }
}
```

Do not reuse Vulkan autotune results for SYCL. Intel consumer GPUs may route
cross-device transfers through host memory, so a correct two-GPU launch can be
slower than one GPU. Keep the measured winner.

## 9. Qualify partial offload to DRAM

Dense models use `n_gpu_layers`; lower values keep the remaining layers in
system RAM:

```json
"runtime_tuned": {
  "sycl": {
    "n_gpu_layers": 30,
    "tensor_split": "9,11"
  }
}
```

MoE models can keep expert layers in RAM with `n_cpu_moe`:

```json
"runtime_tuned": {
  "sycl": {
    "n_gpu_layers": 999,
    "n_cpu_moe": 8,
    "tensor_split": "9,11"
  }
}
```

The application checks CPU-resident weights, prompt cache, available RAM, and
host reserve before launching. Swap is never counted as model capacity. Avoid a
production profile that generates sustained swap I/O; it can pass functional
inference while missing health deadlines under concurrency.

Monitor during qualification:

```bash
watch -n 1 'free -h; swapon --show; ps -eo pid,rss,cmd --sort=-rss | head'
```

Also record per-GPU memory, prompt-processing speed, token-generation speed,
and service health for a 24-48 hour concurrency soak.

## 10. Promote and roll back

After qualification, set:

```json
"runtime": "sycl"
```

and restart the service.

Rollback requires no rebuild or Python-package changes:

```bash
LLAMA_RUNTIME=vulkan /var/web/html_new/local_ai/start-local-ai.sh
```

Then restore the saved top-level runtime to `vulkan` and restart the systemd
service. The Vulkan and SYCL directories remain independent.

## Troubleshooting

| Symptom | Check |
|---|---|
| `llama-bench` reports no SYCL devices | Run `sycl-ls`, `clinfo -l`, and check `/dev/dri/renderD*` permissions as the service user. |
| Shared library is missing | Run `ldd llama-server`; ensure the service initializes `/opt/intel/oneapi/setvars.sh`. |
| Only one GPU appears | Remove an accidental single-device `ZE_AFFINITY_MASK`; check Level Zero enumeration. |
| Wrong tensor-split device order | Compare application status IDs with the saved `llama-bench --list-devices` output. |
| VRAM fits but launch is blocked | Review the host-DRAM preflight; lower CPU offload/cache or add RAM. |
| Inference works but is very slow | Check swap activity and benchmark single GPU versus multi-GPU layer split. |
