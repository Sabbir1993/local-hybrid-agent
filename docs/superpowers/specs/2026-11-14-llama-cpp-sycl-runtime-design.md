# llama.cpp SYCL Runtime Design

Date: 2026-11-14
Branch: `sycl_support`

## Goal

Add Intel oneAPI/Level Zero SYCL as a production runtime for every llama.cpp
process launched by the application while preserving the existing Vulkan and
CUDA runtimes.

Exactly one runtime is active in an application process. The operator selects
`vulkan`, `sycl`, or `cuda` before startup through `config/app.json` or the
`LLAMA_RUNTIME` environment override. Changing runtime requires a controlled
application restart. The application never loads, mixes, or hot-switches
multiple llama.cpp backends in one process.

## Non-goals

- Running Vulkan and SYCL concurrently in one application process.
- Building a single llama.cpp binary containing every GPU backend.
- Hot runtime switching from the administration UI.
- Assuming that SYCL multi-GPU, flash attention, tensor splitting, or partial
  CPU offload has Vulkan-equivalent performance without target-server tests.
- Replacing the existing Python virtual environment. llama.cpp remains an
  external native runtime launched by Python.

## Current Constraints

The repository already supports named runtime presets, but backend-specific
behavior is distributed across several modules:

- `core/backend.py` knows only Vulkan and CUDA and silently treats unknown
  backends as Vulkan.
- Launch code constructs llama.cpp device IDs from a backend prefix and a
  numeric index.
- Device discovery accepts only known Vulkan/CUDA prefixes.
- Linux status code labels devices as Vulkan regardless of the active runtime.
- Benchmark visibility and several preflight messages are backend-specific.
- Binary download logic has no reliable pinned Linux SYCL artifact path.

Silent Vulkan fallback is unacceptable for SYCL because it can launch the
wrong binary with the wrong environment and misleading device diagnostics.

## Chosen Architecture

Introduce a small backend registry used by configuration, discovery, launch,
benchmarking, and diagnostics. This is preferred over adding scattered SYCL
conditionals and over introducing a separate inference service.

Each backend definition declares:

- canonical backend name;
- accepted llama.cpp device-list prefixes;
- optional device-visibility environment strategy;
- validated runtime environment keys;
- capabilities such as explicit device selection, tensor split, multi-GPU,
  flash attention, and memory reporting;
- human-readable prerequisite and diagnostic hints.

The initial registry contains Vulkan, CUDA, and SYCL. Backend lookup raises a
configuration error for an unknown name; it never falls back to Vulkan.

Backend definitions describe behavior only. They do not contain machine paths,
selected devices, or mutable process state.

## Runtime Selection and Configuration

The existing selection order remains:

1. `LLAMA_RUNTIME` environment variable;
2. `runtime` in `config/app.json`;
3. the existing default only when neither setting is present.

If an explicitly selected runtime is missing or invalid, startup fails with an
actionable message. It must not continue with another runtime.

Example configuration:

```json
{
  "runtime": "vulkan",
  "runtimes": {
    "vulkan": {
      "backend": "vulkan",
      "llama_bin_dir": "/var/web/html_new/local_ai/llama-vulkan",
      "gpu_devices": "auto"
    },
    "sycl": {
      "backend": "sycl",
      "llama_bin_dir": "/var/web/html_new/local_ai/llama-sycl",
      "gpu_devices": "auto",
      "env": {
        "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu"
      }
    },
    "cuda": {
      "backend": "cuda",
      "llama_bin_dir": "/var/web/html_new/local_ai/llama-cuda",
      "gpu_devices": "auto"
    }
  }
}
```

Runtime environment overrides are copied only into child-process environments;
the application does not mutate global `os.environ`. Configuration accepts a
small allowlist of backend-related keys. Secrets and unrelated arbitrary
environment variables are out of scope.

oneAPI library paths should normally be configured by the Linux service or
system package loader. Device policy such as `ONEAPI_DEVICE_SELECTOR` or a
validated Level Zero affinity setting may live in the SYCL runtime preset.

## Device Discovery and Identity

The active runtime's `llama-bench --list-devices` remains the authoritative
device inventory.

Discovery produces a normalized record containing:

- numeric configuration index;
- exact llama.cpp device ID, such as the complete token printed before `:`;
- backend;
- device name;
- total and free memory when reported;
- integrated/discrete classification where available.

Launch code passes the exact discovered llama.cpp device ID to `-dev`. It does
not synthesize `SYCL0`, `Vulkan0`, or `CUDA0` from an assumed prefix. Existing
numeric `gpu_devices` configuration remains supported and is resolved against
the active inventory at startup.

Device discovery caches remain keyed by the active runtime binary directory so
switching runtime after a restart cannot reuse another backend's inventory.

If the device output is unparseable, startup/preflight reports the command,
runtime name, backend, binary path, and a bounded output excerpt. It does not
interpret an unparseable result as zero GPUs.

## Process Launch

All llama.cpp consumers use one launch path for backend resolution and child
environment construction:

- main model server;
- executor/small model;
- vision and embedding helpers;
- benchmark and autotune commands.

The launch path:

1. resolves the active runtime and backend definition;
2. validates required binaries;
3. resolves configured numeric devices to discovered llama.cpp IDs;
4. validates requested features against backend capabilities;
5. constructs a fresh child environment;
6. launches the process with explicit device arguments.

No component may choose a backend independently of `ACTIVE_RUNTIME`.

## SYCL Capabilities

The production implementation supports the same application-level model
placement controls for SYCL that are already available for Vulkan:

- Level Zero GPU discovery;
- single- and multi-GPU main-model launch;
- single-GPU helper-model launch, with multi-GPU helpers only when explicitly
  configured;
- `layer` and `row` split modes accepted by the pinned llama.cpp build;
- explicit `--tensor-split` values and proportional auto-generated splits;
- partial dense-model offload with `-ngl`;
- partial MoE expert offload with `-ncmoe`;
- backend-aware status, preflight, autotuning, and errors;
- startup selection and clean rollback to Vulkan or CUDA.

The adapter must not disable multi-GPU merely because the backend is SYCL. It
must validate that the active inventory contains every requested device and
that the selected build accepts the requested split mode. If the runtime or
hardware rejects a capability, launch fails before serving requests and reports
the exact unsupported setting.

Multi-GPU device order must be stable across discovery, tensor-split values,
launch arguments, status output, and autotune results. A split such as `9,11`
must always map to the same two exact llama.cpp device IDs. Zero-share handling
continues to remove the corresponding GPU before constructing `-dev`.

The default multi-GPU mode is `layer`. `row` is available only after a direct
benchmark proves it is valid and beneficial on the target Intel GPUs. Consumer
Intel GPUs may transfer between devices through host memory rather than fast
peer-to-peer links, so multi-GPU correctness does not imply better throughput.
Autotune compares candidate splits and records the winner for the SYCL runtime;
Vulkan tuning results are never reused for SYCL.

Flash attention and other backend-sensitive flags follow the same rule: retain
them only when the active build accepts them in an integration test.

## Partial Offload to System DRAM

Partial offload is a required SYCL path, not a fallback added later.

For dense models, `n_gpu_layers`/`-ngl` controls how many layers reside on the
selected GPUs. Remaining model weights stay in system DRAM and execute on the
CPU. For MoE models, `n_cpu_moe`/`-ncmoe` keeps the selected expert layers in
DRAM while the remainder follows the GPU placement. The existing
`--no-op-offload` safety behavior remains active when CPU-resident experts or
layers would otherwise be temporarily copied back to a GPU during prompt
processing.

Tensor split applies only to the GPU-resident portion of the model. The
CPU-resident portion is held once in host memory; it is not multiplied by the
number of GPUs.

Preflight is extended with a host-memory verdict. It estimates CPU-resident
weights, MoE experts, configured prompt cache, mmap/page-cache pressure, and a
service reserve, then compares them with currently available RAM. A partial
offload that fits VRAM but cannot fit DRAM must fail closed. Swap is not counted
as usable model memory; detected swap pressure produces a warning because it
causes severe latency and can make health checks fail.

The suggestion engine may reduce `-ngl` or increase `-ncmoe` only when both the
resulting VRAM and DRAM plans fit. For MoE models it prefers expert offload over
arbitrarily lowering dense layers because that usually preserves better GPU
utilization. Autotune records SYCL-specific `tensor_split`, `n_gpu_layers`, and
`n_cpu_moe` results separately from other runtimes.

## Monitoring and Preflight

Status responses use backend-neutral device IDs and label the active runtime
and backend accurately. Linux monitoring must not emit `vulkanN` for a SYCL or
CUDA runtime.

Preflight distinguishes:

- runtime configuration failure;
- missing executable or shared library;
- Level Zero/oneAPI initialization failure;
- no compatible GPU;
- unparseable device inventory;
- requested capability not supported;
- model VRAM capacity failure.

SYCL install hints mention `sycl-ls`, `clinfo -l`, `/dev/dri/renderD*`, and
membership in the Linux `render`/`video` groups. Vulkan and CUDA retain their
own hints.

## Runtime Placement

The server runtime root is `/var/web/html_new/local_ai/`. SYCL is installed as
a sibling of the existing Vulkan runtime:

```text
/var/web/html_new/local_ai/
├── .venv/                       # existing Python environment; unchanged
├── llama-vulkan/                # existing Vulkan runtime; unchanged
├── llama-sycl -> llama-sycl-b10840/
└── llama-sycl-b10840/
    ├── llama-server
    ├── llama-bench
    ├── libggml*.so
    └── BUILD_INFO.txt
```

`/var/web/html_new/local_ai/llama-sycl` is a stable symlink used by
`config/app.json`. The versioned target makes upgrades and rollback atomic. If
the deployed llama.cpp revision differs from `b10840`, the directory name uses
that pinned revision instead. The entire contents of llama.cpp's `build/bin`
directory are deployed together so required GGML shared libraries are not
omitted.

Vulkan, SYCL, and CUDA remain separate native builds. Only the selected runtime
directory is used during a given application start.

## Linux Installation Process

The production runbook targets an Intel-GPU Linux server with systemd. Package
repository setup is performed from Intel's instructions for the server's exact
distribution release; repository URLs are not hardcoded into the application.

### 1. Preserve the working Vulkan deployment

Back up `config/app.json`, record the current llama.cpp/Vulkan version, and
confirm that the application starts with `LLAMA_RUNTIME=vulkan`. Do not rename,
overwrite, or rebuild `/var/web/html_new/local_ai/llama-vulkan` during SYCL
installation.

### 2. Install native prerequisites

Install the distribution's build tools (`git`, CMake, Ninja, a C/C++ toolchain,
and `pkg-config`), Intel Level Zero GPU userspace runtime, Intel OpenCL runtime,
`clinfo`, and the oneAPI DPC++/C++ compiler. The service user must belong to the
`render` and `video` groups and have read/write access to `/dev/dri/renderD*`.
A logout or service restart is required after changing group membership.

Before building, the following must succeed as the same Unix user that runs the
application:

```bash
ls -l /dev/dri/renderD*
sycl-ls
clinfo -l
```

Every intended Intel GPU must appear through the Level Zero backend. Resolve
driver, permission, and device-enumeration problems before compiling llama.cpp.

### 3. Build a pinned llama.cpp SYCL runtime

Build the same pinned llama.cpp revision used for qualification. `b10840` is the
initial parity target because the current Vulkan deployment documentation uses
that revision; if it does not pass SYCL qualification, choose and record one
newer revision for all SYCL servers rather than following upstream `master`.

```bash
cd /opt/build
git clone https://github.com/ggml-org/llama.cpp.git llama.cpp-sycl
cd llama.cpp-sycl
git checkout b10840

source /opt/intel/oneapi/setvars.sh --force

cmake -S . -B build-sycl -G Ninja \
  -DGGML_SYCL=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=icx \
  -DCMAKE_CXX_COMPILER=icpx

cmake --build build-sycl --target llama-server llama-bench --parallel
```

Record `git rev-parse HEAD`, `icpx --version`, `cmake --version`, and the CMake
options in `BUILD_INFO.txt`.

### 4. Deploy without touching Vulkan

Create the versioned deployment directory under the existing application root,
copy the complete `build-sycl/bin/` contents into it, set ownership to the
application service account, and verify dependencies before changing the stable
symlink:

```bash
sudo install -d /var/web/html_new/local_ai/llama-sycl-b10840
sudo cp -a build-sycl/bin/. /var/web/html_new/local_ai/llama-sycl-b10840/
sudo cp BUILD_INFO.txt /var/web/html_new/local_ai/llama-sycl-b10840/
sudo chown -R --reference=/var/web/html_new/local_ai/llama-vulkan \
  /var/web/html_new/local_ai/llama-sycl-b10840

ldd /var/web/html_new/local_ai/llama-sycl-b10840/llama-server
ldd /var/web/html_new/local_ai/llama-sycl-b10840/llama-bench

sudo ln -sfn llama-sycl-b10840 /var/web/html_new/local_ai/llama-sycl
```

The operator must confirm that neither `ldd` command reports `not found`.

### 5. Provide the oneAPI service environment

Use a service wrapper that initializes oneAPI and then executes the existing
Python `.venv`; do not create another virtual environment:

```bash
#!/usr/bin/env bash
set -euo pipefail
source /opt/intel/oneapi/setvars.sh --force
export ONEAPI_DEVICE_SELECTOR=level_zero:gpu
export SYCL_CACHE_PERSISTENT=1
export SYCL_CACHE_DIR=/var/cache/local-ai/sycl
exec /var/web/html_new/local_ai/.venv/bin/python \
  /var/web/html_new/local_ai/server_manager.py
```

The cache directory is created for and owned by the service account. Do not set
`ZE_AFFINITY_MASK` to one GPU when multi-GPU operation is required. Device
selection is controlled by the application's exact `-dev` list; affinity masks
are used only when an administrator intentionally hides devices from the whole
service.

```bash
sudo install -d /var/cache/local-ai/sycl
sudo chown --reference=/var/web/html_new/local_ai/llama-vulkan \
  /var/cache/local-ai/sycl
```

### 6. Register the SYCL runtime

Add `runtimes.sycl` to `config/app.json` with:

```json
{
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

Keep `runtime` set to `vulkan` until the SYCL smoke tests pass. Operators can
test one start with `LLAMA_RUNTIME=sycl` without changing the saved default.

### 7. Qualify single GPU, multi-GPU, and partial offload

Run `llama-bench --list-devices` as the service account and save its exact
output as the deployment inventory. Then test, in order:

1. one GPU with a model that fits entirely in VRAM;
2. each GPU individually to confirm stable device IDs;
3. all selected GPUs with `split_mode=layer` and a proportional tensor split;
4. an asymmetric split to confirm split-to-device ordering;
5. a dense model with reduced `n_gpu_layers` and measurable DRAM residency;
6. an MoE model with `n_cpu_moe` and `--no-op-offload` behavior;
7. main and helper lanes together;
8. concurrent requests, restart recovery, and a 24-48 hour soak.

Monitor process RSS, available RAM, swap activity, per-GPU memory, tokens per
second, prompt-processing latency, and health-check stability. Reject a tuning
candidate that swaps or repeatedly reloads model pages even if it technically
completes inference.

### 8. Promote or roll back

Promote by setting `runtime` to `sycl` and restarting the application. Roll back
by setting it to `vulkan` or starting once with `LLAMA_RUNTIME=vulkan`, then
restart. No Python package reinstall or model conversion is required.

## Security and Operational Rules

- Runtime names and backend names are allowlisted.
- Runtime binary directories must exist and contain the required executables.
- Child environment overrides are allowlisted and string-valued.
- Diagnostic output is bounded and must not dump the full service environment.
- Runtime changes require a controlled restart.
- A failed selected runtime never falls through to a different backend.

## Implementation Sequence

1. Add backend registry, strict validation, and unit tests.
2. Add exact device identity to discovery and keep numeric config compatibility.
3. Route all main/helper/benchmark launches through backend-aware resolution.
4. Add runtime-scoped child environments and SYCL prerequisite errors.
5. Make Linux monitoring and preflight backend-neutral.
6. Add the SYCL runtime example and Linux installation/runbook documentation.
7. Add host-DRAM preflight for dense and MoE partial offload.
8. Validate single-GPU, multi-GPU tensor split, and partial offload on the target
   server before making SYCL the saved default.

## Test Strategy

Unit tests cover:

- valid Vulkan, CUDA, and SYCL backend lookup;
- rejection of unknown backends and missing selected runtimes;
- environment allowlisting and lack of cross-runtime leakage;
- parsing representative device-list output for every backend;
- numeric configuration index to exact llama.cpp ID resolution;
- main and helper launch commands using the active runtime only;
- rejection of unsupported SYCL capabilities;
- backend-correct status and diagnostics;
- cache isolation by runtime binary directory.
- host-RAM fit/no-fit verdicts for dense and MoE partial offload;
- tensor-split ordering across exact SYCL device IDs;
- runtime-specific autotune results that cannot leak between Vulkan and SYCL.

Integration tests on Linux cover:

- `llama-bench --list-devices` under the service account;
- one main-model request on each SYCL device;
- multi-GPU layer split with symmetric and asymmetric tensor splits;
- dense `-ngl` and MoE `-ncmoe` partial-offload cases;
- executor/helper request on SYCL;
- restart from SYCL to Vulkan and back;
- missing oneAPI library and permission-error diagnostics;
- process crash and supervised restart;
- concurrent requests and VRAM cleanup.

Production promotion requires a 24-48 hour soak with representative models,
multi-GPU tensor splitting, partial DRAM offload, and expected concurrency.
Flash attention and `row` split require separate acceptance results and are not
implied by layer-split success.

## Acceptance Criteria

- `LLAMA_RUNTIME=sycl` starts every llama.cpp process with the SYCL runtime.
- `LLAMA_RUNTIME=vulkan` and `LLAMA_RUNTIME=cuda` preserve existing behavior.
- Only one backend is active in a given application process.
- Invalid runtime/backend selection stops startup with an actionable error.
- Device discovery and status identify the correct backend and hardware.
- Main and helper models pass single-GPU SYCL inference tests.
- Multi-GPU layer splitting preserves configured device order and passes
  symmetric and asymmetric tensor-split tests.
- Dense and MoE partial offload pass both VRAM and host-DRAM preflight without
  sustained swap activity.
- Switching back to Vulkan needs no Python environment rebuild or model changes.
- Existing backend-focused unit tests remain green, excluding documented
  platform-incompatible baseline tests.
