# llama.cpp SYCL Runtime Implementation Plan

Design: `docs/superpowers/specs/2026-11-14-llama-cpp-sycl-runtime-design.md`

## 1. Backend contract and strict configuration

- Replace silent backend fallback with a typed Vulkan/CUDA/SYCL registry.
- Add runtime-scoped environment validation and child-environment construction.
- Validate explicit runtime/backend selections during configuration load.
- Preserve the rule that exactly one runtime is active for the process.
- Add unit tests before changing production behavior.

## 2. Device inventory and exact identity

- Extend `llama-bench --list-devices` parsing to SYCL.
- Preserve the exact llama.cpp device ID alongside the numeric config index.
- Resolve configured numeric indices to exact IDs for all launch paths.
- Keep inventory caches isolated by runtime binary directory and environment.
- Add Vulkan/CUDA/SYCL parser and mapping tests.

## 3. Main, helper, and benchmark launches

- Use the backend contract for main-server, helper-server, and benchmark child
  environments.
- Use exact discovered device IDs when available and a validated prefix fallback
  only for compatibility with an unavailable inventory in unit/offline paths.
- Preserve multi-GPU split ordering, zero-share filtering, dense `-ngl`, and MoE
  `-ncmoe` behavior.
- Add SYCL single/multi-GPU command tests and environment-isolation tests.

## 4. Monitoring, VRAM, and host DRAM preflight

- Remove Vulkan-only labels from Linux status and preflight output.
- Carry backend/device identity through normalized device records.
- Add conservative host-memory accounting for CPU-resident dense layers, MoE
  experts, and prompt cache.
- Fail closed when a partial-offload plan exceeds available DRAM; never count
  swap as capacity.
- Add host-memory fit/no-fit and backend-correct diagnostic tests.

## 5. Configuration example and Linux runbook

- Add `/var/web/html_new/local_ai/llama-sycl` beside the existing Vulkan runtime.
- Document pinned oneAPI/Level Zero build, versioned deployment, stable symlink,
  service environment, device checks, multi-GPU qualification, partial offload,
  promotion, and rollback.
- Keep the existing Python `.venv` unchanged.

## 6. Verification and commits

- Run focused backend/config/device/launch/preflight tests after every layer.
- Run the broader runtime and VRAM test groups after integration.
- Document Windows-only baseline failures separately from regressions.
- Commit each coherent layer on branch `sycl_support`.
