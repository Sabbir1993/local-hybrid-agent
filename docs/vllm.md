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
    "extra_args": [],
    "allow_pickle_weights": false
  }
}
```

- `device`: `xpu` selects GPUs with `ZE_AFFINITY_MASK`; `cuda` uses `CUDA_VISIBLE_DEVICES`.
- `gpu_devices`: `"auto"` uses the llama.cpp runtime's GPU list. vLLM uses the first *tensor × pipeline* GPUs of it.
- `grammar_param`: older vLLM versions (before `structured_outputs`) need `"guided_grammar"`.
- `extra_args`: admin-only extra flags (a list, or one string that is split like a shell would). The app drops these names
  and logs it: `--host`, `--port`, `--api-key`, `--ssl-*`, `--middleware`, `--chat-template`, `--allowed-*`, `--lora*`,
  `--trust*` (so `--trust-remote-code` cannot be turned on at all: it runs Python shipped inside the model folder),
  `--served-model-name`, `--tokenizer*`, `--model`, `--uds`, `--root-path`, `--config`, `--enable-lora` (and shortened forms
  of them such as `--ho`). The app's own `--host 127.0.0.1 --port ... --served-model-name ...` are always put last, so no
  earlier flag can override them. vLLM listens on the loopback address only, and the app's login and card-number masking
  sit in front of it; never expose its port.
- `allow_pickle_weights` (default `false`): a model folder that has `.safetensors` files is always loaded with
  `--load-format safetensors`. A folder with only `.bin` / `.pt` (pickle) weights is refused, because pickle can run code
  when it loads. Set `true` only for a model you trust and cannot convert.
- Privacy: the app always starts vLLM with `VLLM_NO_USAGE_STATS=1`, `DO_NOT_TRACK=1` and `HF_HUB_DISABLE_TELEMETRY=1`, so
  nothing is reported to its developers. For a local model folder (and no Hugging Face tokenizer) it also sets
  `HF_HUB_OFFLINE=1`. It adds the request-logging-off switch your vLLM has (`--disable-log-requests` or
  `--no-enable-log-requests`, found once from `vllm serve --help`), so prompts are not written to the log; if the help
  cannot be read, no switch is added and the log can contain prompts. `engines.vllm.env` can override these variables.

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
| Tokenizer | `--tokenizer` | GGUF only: the original repo (`org/name`, e.g. `Qwen/Qwen3-8B`), or a folder inside the models folder (relative to it, no `..`) |
| Extra arguments | (appended) | For this model only, from the list below, e.g. `--kv-cache-dtype fp8`. Anything else is refused |

### Extra arguments allowed per model

Whoever may edit model settings (`model.local.configure`, which can be delegated) can type these, so only this list is
accepted, and the check runs again when the model loads (a hand-edited `model_configs.json` cannot get around it). Names
are written in full (`--kv_cache_dtype` is read as `--kv-cache-dtype`; abbreviations are refused). A value is plain
letters, digits and `. _ - : ,`; it never starts with `-` and is never a path.

| Kind | Flags |
|---|---|
| on/off (no value) | `--enable-chunked-prefill` `--no-enable-chunked-prefill` `--enable-prefix-caching` `--no-enable-prefix-caching` `--disable-custom-all-reduce` `--enforce-eager` `--disable-log-stats` |
| whole number | `--max-num-batched-tokens` `--block-size` `--max-seq-len-to-capture` `--seed` `--num-scheduler-steps` `--max-num-partial-prefills` `--long-prefill-token-threshold` `--max-logprobs` |
| number | `--swap-space` `--cpu-offload-gb` |
| plain name | `--kv-cache-dtype` `--scheduling-policy` `--dtype` `--quantization` |
| `auto` or `vllm` | `--generation-config` |
| one JSON object (checked and rewritten compactly; text values are plain names, no paths or `..`) | `--speculative-config` `--compilation-config` `--rope-scaling` |

For example `--speculative-config '{"method":"ngram","num_speculative_tokens":3}'`. A flag you need that is not here
can be added by an admin in `config/app.json` (`engines.vllm.extra_args`), except the names listed above.

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
- Stopping the model stops vLLM and all its worker processes, also when the server process had already crashed and only
  its GPU workers were left. If the app itself crashes, the next start stops that process group (`logs/main-engine.pid`
  holds the group id, the process start time and the boot id; a group is only stopped when those still match, so a
  recycled process id of something else is never killed). On Windows this part is unchanged.
- The log lines the Model Config card shows (`/control/log`, and the log tail of `/control/status`) are for super-admins
  only; API tokens never get them. A user who only holds the delegable `model.local.load` permission still sees why a
  load failed: `/control/status` carries `last_load_error`, and the card shows it when `/control/log` answers 403. All of
  it has card numbers masked and server paths, Hugging Face tokens, `Bearer ...` values and keys replaced.
- The speed badge, cached-token counts and context ring work on vLLM too. They are built from vLLM's token usage.
  MTP/draft acceptance is llama.cpp only.
- The raw API proxy still blocks the server's admin endpoints (`/metrics` and similar), so users cannot read or clear
  each other's cached prompts.
