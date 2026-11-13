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
- Claiming SYCL multi-GPU, flash-attention, or tensor-split support without
  validation against the pinned llama.cpp revision and target Intel hardware.
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
      "llama_bin_dir": "/opt/local-agent/runtimes/llama-vulkan/bin",
      "gpu_devices": "auto"
    },
    "sycl": {
      "backend": "sycl",
      "llama_bin_dir": "/opt/local-agent/runtimes/llama-sycl/bin",
      "gpu_devices": "auto",
      "env": {
        "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu"
      }
    },
    "cuda": {
      "backend": "cuda",
      "llama_bin_dir": "/opt/local-agent/runtimes/llama-cuda/bin",
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

The first production release enables:

- Level Zero GPU discovery;
- single-GPU main-model launch;
- single-GPU helper-model launch;
- backend-aware status and errors;
- startup selection and clean rollback to Vulkan or CUDA.

SYCL multi-GPU and tensor split are capability-gated. They remain disabled
until the target Intel hardware and pinned llama.cpp revision pass correctness,
memory-accounting, and soak tests. Unsupported settings fail before process
launch with a clear message.

Flash attention and other backend-sensitive flags follow the same rule: retain
them only when the active build advertises or integration tests prove support.

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

## Linux Packaging and Installation

Vulkan and SYCL use separate pinned llama.cpp installations, for example:

```text
/opt/local-agent/runtimes/llama-vulkan/
/opt/local-agent/runtimes/llama-sycl/
/opt/local-agent/runtimes/llama-cuda/
```

The SYCL production path builds a pinned llama.cpp revision from source using
the installed Intel oneAPI DPC++ compiler and `GGML_SYCL=ON`. The build produces
at least `llama-server` and `llama-bench`. Build provenance records the git
revision, compiler version, CMake options, and build date.

The existing Python `.venv` is retained. The service environment initializes
oneAPI/Level Zero before starting Python, and Python launches binaries from the
selected runtime directory.

The existing Vulkan runtime remains installed and configured throughout the
SYCL rollout. Rollback consists of selecting `vulkan` and restarting the
application.

Exact Intel driver and oneAPI package commands are documented per supported
Linux distribution because package repositories and kernel requirements differ.

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
7. Validate single-GPU SYCL on the target server.
8. Evaluate multi-GPU and tensor split separately; enable only after passing its
   own acceptance gate.

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

Integration tests on Linux cover:

- `llama-bench --list-devices` under the service account;
- one main-model request on SYCL;
- executor/helper request on SYCL;
- restart from SYCL to Vulkan and back;
- missing oneAPI library and permission-error diagnostics;
- process crash and supervised restart;
- concurrent requests and VRAM cleanup.

Production promotion requires a 24-48 hour soak with representative models and
concurrency. Multi-GPU, tensor split, and flash attention require separate
acceptance results and are not implied by single-GPU success.

## Acceptance Criteria

- `LLAMA_RUNTIME=sycl` starts every llama.cpp process with the SYCL runtime.
- `LLAMA_RUNTIME=vulkan` and `LLAMA_RUNTIME=cuda` preserve existing behavior.
- Only one backend is active in a given application process.
- Invalid runtime/backend selection stops startup with an actionable error.
- Device discovery and status identify the correct backend and hardware.
- Main and helper models pass single-GPU SYCL inference tests.
- Switching back to Vulkan needs no Python environment rebuild or model changes.
- Existing backend-focused unit tests remain green, excluding documented
  platform-incompatible baseline tests.
