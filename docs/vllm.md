# vLLM engine (optional)

The main model can run on **llama.cpp** (the default, `llama-server`) or on **vLLM**. An admin picks the engine per model
in **Settings → Model Config → Runtime engine**, then loads the model as usual. The choice is saved per model in
`config/model_configs.json`.

- vLLM serves the same OpenAI API on the same port (127.0.0.1:8090), so chat, agents and tools work the same way.
- Helper models (executor, vision, embedder, whisper, image) always stay on llama.cpp.
- Only the main model can use vLLM.

## Which models

| Model files | llama.cpp | vLLM |
|---|---|---|
| Hugging Face folder (`config.json` + `*.safetensors`): BF16/FP16, FP8, AWQ, GPTQ | – | yes (best) |
| Single-file `.gguf` | yes | experimental: slower, fewer model types, needs **Tokenizer** set |
| Split `.gguf` (`-00001-of-0000N`) | yes | no. Merge it first: `llama-gguf-split --merge part-00001.gguf merged.gguf` |

### Formats on 3× Arc Pro B70 (32 GB each)

You do not need full 16-bit models. Weights for a 32B model take about:
- **BF16:** ~64 GB. Needs 2–3 GPUs, with little room left for context.
- **FP8:** ~32 GB. Set *Quantization = fp8* to convert a BF16 folder while loading, or download an FP8 folder.
- **INT4 AWQ/GPTQ:** ~18 GB. Fits on one GPU.

INT4 AWQ/GPTQ or FP8 is the recommended choice. On Intel GPUs these and BF16/FP16 are the well-supported formats; check
the vLLM release notes of the version you install for others.

## 1. Install vLLM (separately from the app)

vLLM brings its own PyTorch and oneAPI stack, so keep it out of the app's `.venv`. Use one of:

- **Intel's vLLM container** for Arc Pro (the "llm-scaler" vLLM image). Run it with `/dev/dri` passed through, and set
  `bin` to a small wrapper script that runs `vllm` inside the container.
- **Its own virtual environment**, following vLLM's "GPU → Intel XPU" install guide. For example:

```bash
python3 -m venv /opt/vllm/.venv
```

Then install the XPU build as the guide for your vLLM version describes. Check that it works:

```bash
/opt/vllm/.venv/bin/vllm --help
```

The user that runs the app needs access to the GPUs (`render` and `video` groups), the same as for llama.cpp.

## 2. Configure the app

Add this block to `config/app.json`. A full example is in `config/app.example.json`:

```json
"engines": {
  "vllm": {
    "bin": "/opt/vllm/.venv/bin/vllm",
    "device": "xpu",
    "gpu_devices": "auto",
    "gpu_memory_utilization": 0.9,
    "health_timeout_s": 900,
    "tool_call_parser": "hermes",
    "reasoning_parser": "",
    "grammar_param": "structured_outputs",
    "env": {},
    "extra_args": []
  }
}
```

- `device`: `xpu` selects GPUs with `ZE_AFFINITY_MASK`; `cuda` uses `CUDA_VISIBLE_DEVICES`.
- `gpu_devices`: `"auto"` uses the llama.cpp runtime's GPU list. vLLM uses the first *tensor × pipeline* GPUs of it.
- `grammar_param`: older vLLM versions (before `structured_outputs`) need `"guided_grammar"`.
- `extra_args`: added at the end of the command line. `--trust-remote-code` is never added for you, because it runs Python
  code shipped inside the model folder. Only add it for a model you trust.
- Usage statistics: the app always starts vLLM with `VLLM_NO_USAGE_STATS=1` and `DO_NOT_TRACK=1`, so vLLM does not
  report usage data to its developers.

Restart the backend after editing (pm2: activate `.venv` first, then `pm2 restart <name> --update-env`).

## 3. Download a model

The app has no download page. Download Hugging Face models with the `hf` command (from `pip install -U huggingface_hub`)
straight into the models folder (`models_dir` in `config/app.json`):

```bash
hf download Qwen/Qwen3-32B-AWQ --local-dir /path/to/models/Qwen3-32B-AWQ
```

- For gated models (for example Llama), accept the licence on huggingface.co, then pass your token through the
  environment only: `HF_TOKEN=[PLACEHOLDER] hf download ...`. Never put the token in `config/app.json` or in git.
- Downloads come from a public source. Prefer official or well-known publishers, and keep `--trust-remote-code` off.
- The folder appears in **Settings → Model Config** on the next refresh, tagged *vLLM folder*. It can sit directly in the
  models folder or one level down (`models/qwen/Qwen3-32B-AWQ`).

## 4. Settings per model

Shown when *Runtime engine = vLLM*. llama.cpp-only fields (GPU layers, tensor split, KV type, MTP, ...) are hidden.

| Field | vLLM flag | Notes |
|---|---|---|
| Context size | `--max-model-len` | |
| Parallel slots | `--max-num-seqs` | How many requests run at once. 0 or 1 = the `engines.vllm.max_num_seqs` default (8), because vLLM batches requests |
| Tensor parallel | `--tensor-parallel-size` | Must divide the attention heads: 1, 2 or 4, **not 3**. 0 = auto (2 on 3 GPUs) |
| Pipeline parallel | `--pipeline-parallel-size` | GPUs used = tensor × pipeline. Use pipeline 3 with tensor 1 to use all three GPUs |
| GPU memory share % | `--gpu-memory-utilization` | 0 = `engines.vllm` default |
| Data type / Quantization | `--dtype` / `--quantization` | `fp8` quantizes a 16-bit folder on load |
| Tool parser | `--tool-call-parser` | `hermes` (Qwen), `llama3_json`, `mistral`, ... Empty = no tool calls |
| Reasoning parser | `--reasoning-parser` | `qwen3`, `deepseek_r1`, ... Empty = none |
| Eager mode | `--enforce-eager` | Try it when start-up fails while compiling |
| Tokenizer | `--tokenizer` | GGUF only: the original repo, e.g. `Qwen/Qwen3-8B` |
| Extra arguments | (appended) | For this model only, e.g. `--kv-cache-dtype fp8`. `--trust-remote-code` is refused here |

### GPUs and helper models

vLLM reserves its memory share on every GPU it uses, so a helper model on the same GPU may not fit. On 3 GPUs the
default (tensor parallel 2) uses GPUs 0 and 1, which leaves GPU 2 for the helpers. Set `small_model_gpu` / each helper's
`gpu` to 2. The pre-load check warns when they overlap.

## Tuning for speed

- **One GPU is fastest when the model fits.** Use tensor parallel 1 and pipeline parallel 1 with an INT4 or FP8 model.
- **Two GPUs:** tensor parallel 2 gives the best speed for one large model.
- **Three GPUs:** tensor parallel 1 with pipeline parallel 3 uses all three for a model too large for two. It adds
  capacity, not speed.
- **Many users at once:** raise Parallel slots. vLLM runs them as one batch, so total tokens per second rise with load.
- **Less memory per context:** add `--kv-cache-dtype fp8` under Extra arguments if your vLLM build supports it on Intel.
- **Start-up fails while compiling:** turn on Eager mode.

## Troubleshooting

### "vLLM could not find a GPU" / `RuntimeError: Failed to infer device type`

vLLM started but found no GPU backend. A plain `pip install vllm` gives the NVIDIA (CUDA) build, which cannot use
Intel Arc cards. Check the vLLM environment (the one `engines.vllm.bin` points at), as the same user that runs the app:

```bash
/opt/vllm/.venv/bin/python -c "import torch; print(torch.__version__, 'xpu:', torch.xpu.is_available(), torch.xpu.device_count())"
```

- `xpu: True 3` — PyTorch sees the three cards. If vLLM still fails, the vLLM package itself is the CUDA build:
  reinstall vLLM from source with its XPU requirements, or use Intel's vLLM container.
- `xpu: False 0` or an error — the PyTorch in that environment has no Intel GPU support, or the Intel GPU runtime is
  missing. Install PyTorch's XPU build and the Intel compute runtime (Level Zero), then check again:

```bash
ls /dev/dri
```

```bash
clinfo -l
```

  `clinfo -l` (or `sycl-ls`) should list the three Arc cards for this user; if it only works with `sudo`, add the
  user to the `render` and `video` groups, log in again and restart the backend.
- Running inside pm2: the vLLM process gets the backend's environment. If the Intel runtime needs variables (for
  example from `setvars.sh`), put them in `engines.vllm.env` in `config/app.json`.

### Intel GPUs: the checks that found the problems on a real install (vLLM 0.33.0, torch 2.14.0+xpu, Arc Pro)

Work through these in order, inside the vLLM environment. Each one fixed a failure that looked like "vLLM cannot start".

1. **PyTorch sees the cards** (needs the system Level Zero runtime, see above): `torch.xpu.device_count()` must be 3, not 0.
2. **One `triton` only.** `uv pip list | grep -i triton` must show a single package (`triton-xpu`, at the version PyTorch
   requires: `python -c "from importlib.metadata import requires; print([r for r in requires('torch') if 'triton' in r])"`).
   A second build (for example `tokenspeed-triton`) installs into the same `triton/` folder and breaks it with
   `ImportError: cannot import name '_load_shared_fp4_repacked'`. Fix: `uv pip uninstall tokenspeed-triton triton-xpu`, then
   `uv pip install --no-deps "triton-xpu==<version PyTorch asked for>" --index-url https://download.pytorch.org/whl/xpu`.
3. **vLLM's Intel kernels.** vLLM's Intel module imports `vllm_xpu_kernels`. Without it vLLM silently reports no GPU
   (`UnspecifiedPlatform`, "Failed to infer device type"). Install the version in vLLM's own `requirements/xpu.txt` for your
   vLLM release (0.33.0 asks for `0.1.15.4`): `uv pip install --no-deps "vllm-xpu-kernels==0.1.15.4"`.
4. **Check:** `python -c "from vllm.platforms import current_platform; print(repr(current_platform))"` must print
   `XPUPlatform`, and `vllm --version` must print the version. The `FlashInfer kernels are disabled` and
   `No module named 'vllm._C'` lines are expected on Intel.
5. Later `uv pip install` of other packages can bring the second triton back. Re-run step 2's list after any install.

If a load fails, Settings > Model Config shows what the server printed under the model picker.

## Notes

- Start-up takes minutes (loading weights and compiling graphs). The load waits up to `health_timeout_s`.
- Stopping the model stops vLLM and all its worker processes. If the app crashes, the next start stops a vLLM server that
  is still running (`logs/main-engine.pid`).
- The speed badge, cached-token counts and context ring work on vLLM too. They are built from vLLM's token usage.
  MTP/draft acceptance is llama.cpp only.
- The raw API proxy still blocks the server's admin endpoints (`/metrics` and similar), so users cannot read or clear
  each other's cached prompts.
