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
cd /var/web/html_new/local_ai/application
cp config/app.json config/app.json.before-sycl
../llama-vulkan/llama-bench --list-devices
```

Keep `/var/web/html_new/local_ai/llama-vulkan` unchanged. Confirm the current
application can still start with `LLAMA_RUNTIME=vulkan` before proceeding.

## 2. Install Intel GPU and oneAPI prerequisites

Add Intel's apt repositories for your Ubuntu release (the Intel GPU repository and the oneAPI repository, following
Intel's current instructions), then refresh the package lists and check that every package has a candidate:

```bash
sudo apt update
apt-cache policy libze-intel-gpu1 libze-dev intel-oneapi-compiler-dpcpp-cpp \
  intel-oneapi-dnnl-devel intel-oneapi-mkl-devel | grep -E "^[a-z]|Candidate"
```

Install the build tools, the Level Zero driver and headers, the compiler, and the oneDNN and MKL development packages
that llama.cpp's SYCL build requires:

```bash
sudo apt install -y git cmake ninja-build build-essential pkg-config \
  intel-level-zero-gpu libze-dev \
  intel-oneapi-compiler-dpcpp-cpp intel-oneapi-dnnl-devel intel-oneapi-mkl-devel
```

Notes from the first installation (Ubuntu 26.04, oneAPI 2026.x):

- `intel-level-zero-gpu` is an alias for `libze-intel-gpu1`. There is no package called `level-zero`; the loader comes
  with the driver and `libze-dev` adds the headers.
- The oneDNN development package is `intel-oneapi-dnnl-devel` (not `...-dnn-devel`).
- `clinfo` and `intel-opencl-icd` are not needed: SYCL uses Level Zero, not OpenCL. Do not install Mesa's OpenCL as a
  substitute.

Give the account that runs the application access to the GPU devices (the `render` group is enough), then log that
account out and in again, or restart its processes:

```bash
sudo usermod -aG render <app-user>
```

Verify the devices as that account:

```bash
ls -l /dev/dri/renderD*
sudo -u <app-user> bash -lc 'source /opt/intel/oneapi/setvars.sh --force && sycl-ls'
```

Every GPU intended for tensor splitting must appear under `level_zero`. Stop and fix driver, permissions, or
enumeration problems before compiling llama.cpp.

## 3. Build a pinned llama.cpp revision

Build as an administrator account, not as the account that runs the application. The initial parity target is `b10840`,
matching the documented Vulkan deployment. Do not build production servers from a moving `master` branch.

```bash
mkdir -p ~/build && cd ~/build
git clone --branch b10840 --depth 1 https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
source /opt/intel/oneapi/setvars.sh --force

# where the oneDNN and MKL CMake files and libraries are
DNNL_DIR="$(dirname "$(find /opt/intel/oneapi/dnnl -name dnnl-config.cmake | head -n 1)")"
MKL_DIR="$(dirname "$(find /opt/intel/oneapi/mkl -name MKLConfig.cmake | head -n 1)")"
RPATH="$(echo "$LD_LIBRARY_PATH" | tr ':' '\n' | grep /opt/intel/oneapi | paste -sd';' -)"
RPATH="$RPATH;$(dirname "$(dirname "$DNNL_DIR")");$(dirname "$(dirname "$MKL_DIR")")"
echo "DNNL_DIR=$DNNL_DIR"; echo "MKL_DIR=$MKL_DIR"; echo "RPATH=$RPATH"

cmake -S . -B build-sycl -G Ninja \
  -DGGML_SYCL=ON -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx \
  -DDNNL_DIR="$DNNL_DIR" -DMKL_DIR="$MKL_DIR" \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
  -DCMAKE_BUILD_RPATH='$ORIGIN;'"$RPATH" \
  -DCMAKE_INSTALL_RPATH='$ORIGIN;'"$RPATH"
cmake --build build-sycl --target llama-server llama-bench -j
```

The run path (`$ORIGIN` plus the oneAPI library folders) lets the binaries find their own libraries and oneAPI's
without changing the environment of any other process. The configure output must show no `Warning` or `Error` about
Level Zero, DNNL or MKL.

Harmless messages during the build: many `'barrier' is deprecated` warnings, and `UI: download dist.tar.gz ... failed`
(the optional web page bundled with `llama-server`; the application uses the API only).

Check the result from the build folder:

```bash
cd build-sycl/bin
ldd ./llama-server | grep -c "not found"          # must print 0
ONEAPI_DEVICE_SELECTOR=level_zero:gpu \
  LD_LIBRARY_PATH=/opt/intel/oneapi/umf/<ver>/lib:/opt/intel/oneapi/tcm/<ver>/lib ./llama-bench --list-devices
```

`<ver>` is the version folder under `/opt/intel/oneapi/umf` and `/opt/intel/oneapi/tcm` (`1.2` and `1.6` on the first
install). The device list must show every GPU as `SYCL0`, `SYCL1`, and so on.

If `b10840` does not compile with the installed oneAPI version, qualify one newer llama.cpp revision and pin its exact
commit on every SYCL server. Do not silently use different revisions across machines.

## 4. Deploy beside Vulkan

Deploy the complete `build-sycl/bin` directory because `llama-server` and `llama-bench` depend on GGML shared
libraries stored beside them. Keep the Vulkan folder untouched.

```bash
APP_ROOT=/var/web/html_new/local_ai
sudo install -d "$APP_ROOT/llama-sycl"
sudo cp -a ~/build/llama.cpp/build-sycl/bin/. "$APP_ROOT/llama-sycl/"
sudo chown -R <app-user>:<app-group> "$APP_ROOT/llama-sycl"
ldd "$APP_ROOT/llama-sycl/llama-server" | grep -c "not found"       # must print 0
ldd "$APP_ROOT/llama-sycl/llama-bench"  | grep -c "not found"       # must print 0
```

Record what was built next to it:

```bash
cd ~/build/llama.cpp && git rev-parse HEAD > "$APP_ROOT/llama-sycl/BUILD_INFO.txt"
```

## 5. Environment of the process that starts the application

The SYCL runtime needs two things in the environment of the process that starts the application (a terminal, PM2 or
a service definition). Do not create a wrapper script and do not source oneAPI's environment script for the whole application:
its variables would also reach the Vulkan model servers.

1. `LD_LIBRARY_PATH` with oneAPI's memory libraries. Without it, device discovery reports `no SYCL device available`
   even though `sycl-ls` works:

   ```bash
   LD_LIBRARY_PATH=/opt/intel/oneapi/umf/<ver>/lib:/opt/intel/oneapi/tcm/<ver>/lib
   ```

   This cannot go into `config/app.json`: library paths are deliberately not allowed there.

2. Select the runtime for the start: `LLAMA_RUNTIME=sycl`.

Foreground start (shows startup errors directly):

```bash
cd /var/web/html_new/local_ai/application
LLAMA_RUNTIME=sycl LD_LIBRARY_PATH=/opt/intel/oneapi/umf/<ver>/lib:/opt/intel/oneapi/tcm/<ver>/lib \
  .venv/bin/python server_manager.py
```

With PM2, export both variables in the shell and run `pm2 restart Backend --update-env`. On the first install PM2
restarted the backend in a loop while a foreground start ran correctly; the cause was not established, so run in the
foreground to read the error if PM2 does the same. With systemd, put the two values in `Environment=` lines.

Two settings that look useful but must not be used with this build (oneAPI 2026.2, llama.cpp `b10840`, Arc Pro B70):

- `SYCL_CACHE_PERSISTENT=1` with `SYCL_CACHE_DIR`: `llama-server` segfaulted (exit code -11) right after
  `llama threadpool init` while loading a model. Removing the two variables fixed it.
- `ZE_AFFINITY_MASK` set to one GPU when multi-GPU inference is needed. The application passes exact device IDs with
  `-dev`.

## 6. Register the runtime

The application reads `config/app.json` inside the application folder (`.../application/config/app.json`). Add this
sibling entry under `runtimes`:

```json
"sycl": {
  "llama_bin_dir": "/var/web/html_new/local_ai/llama-sycl",
  "backend": "sycl",
  "gpu_devices": "auto",
  "small_model_gpu": 0,
  "env": {
    "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu",
    "GGML_SYCL_ENABLE_VMM": "0"
  }
}
```

`GGML_SYCL_ENABLE_VMM=0` selects llama.cpp's legacy GPU memory pool. With the default pool, a long prompt on a MoE model
aborted `llama-server` with `GGML_ASSERT(ptr == reinterpret_cast<void *>(pool_addr + pool_used)) failed` in
`ggml_sycl_pool_vmm::free` (that pool requires buffers to be freed in reverse order of allocation). With `0` the same
prompt completes. The effect on speed and memory has not been measured.

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

Helper models (embedder, vision, image and speech models) are placed on GPUs by their own `gpu` setting in
`small_models`. Do not put them on a GPU the main model also uses unless the memory budget has been checked: a main
model split over all GPUs shares memory with them.

## 7. Verify the installed runtime

Run device discovery with the same environment the application will use:

```bash
ONEAPI_DEVICE_SELECTOR=level_zero:gpu \
  LD_LIBRARY_PATH=/opt/intel/oneapi/umf/<ver>/lib:/opt/intel/oneapi/tcm/<ver>/lib \
  /var/web/html_new/local_ai/llama-sycl/llama-bench --list-devices
```

Save the output. The IDs shown there, such as `SYCL0` and `SYCL1`, are the IDs the application must preserve in `-dev`
ordering.

Start one qualification run without changing the saved default (see section 5). The startup log must contain
`runtime=sycl backend=sycl` and the list of devices. Startup must fail rather than fall back to Vulkan if the SYCL
binary, libraries, or configured devices are unavailable.

Before loading models through the application, run one by hand with the application's flags and send it a long prompt;
a short "hi" does not exercise the code path that crashed with the default memory pool.

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

and restart the application. The `LD_LIBRARY_PATH` setting from section 5 must be present every time it starts.

Rollback requires no rebuild or Python-package changes: set the saved top-level runtime back to `vulkan`, remove
`LLAMA_RUNTIME=sycl` and the oneAPI `LD_LIBRARY_PATH` from the environment that starts the application, and restart it.
The Vulkan and SYCL directories remain independent.

## Troubleshooting

| Symptom | Check |
|---|---|
| `llama-bench` reports no SYCL devices | Run `sycl-ls` and check `/dev/dri/renderD*` permissions as the application user; see also the `LD_LIBRARY_PATH` row below. |
| Shared library is missing | Run `ldd llama-server`; rebuild with the run path from section 3 (the build embeds the oneAPI library folders). |
| Only one GPU appears | Remove an accidental single-device `ZE_AFFINITY_MASK`; check Level Zero enumeration. |
| Wrong tensor-split device order | Compare application status IDs with the saved `llama-bench --list-devices` output. |
| VRAM fits but launch is blocked | Review the host-DRAM preflight; lower CPU offload/cache or add RAM. |
| Inference works but is very slow | Check swap activity and benchmark single GPU versus multi-GPU layer split. |
| `ggml_sycl_init: no SYCL device available` although `sycl-ls` lists the GPUs | The process lacks `LD_LIBRARY_PATH` for oneAPI's UMF and TCM libraries (section 5). |
| `llama-server` exits with code -11 right after `llama threadpool init` | Remove `SYCL_CACHE_PERSISTENT` and `SYCL_CACHE_DIR`; also check for a leftover `llama-server` holding the same GPU (`ps -eo pid,etime,args`, filter for `llama-server`). |
| `GGML_ASSERT(ptr == ... pool_addr + pool_used)` on a long prompt | Set `GGML_SYCL_ENABLE_VMM` to `0` in the runtime `env` (section 6). |
| `tensor_split '1' has 1 entries but 3 GPUs are in use` | Save one share per GPU in Model Config (`1,0,0` uses only the first GPU). The split is stored per runtime. |
| `libdnnl.so.3` or another library `not found` | Rebuild with the run path from section 3, or check that the oneDNN and MKL folders exist. |
