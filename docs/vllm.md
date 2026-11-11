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
| Parallel slots | `--max-num-seqs` | How many requests run at once |
| Tensor parallel | `--tensor-parallel-size` | Must divide the attention heads: 1, 2 or 4, **not 3**. 0 = auto (2 on 3 GPUs) |
| Pipeline parallel | `--pipeline-parallel-size` | GPUs used = tensor × pipeline. Use pipeline 3 with tensor 1 to use all three GPUs |
| GPU memory share % | `--gpu-memory-utilization` | 0 = `engines.vllm` default |
| Data type / Quantization | `--dtype` / `--quantization` | `fp8` quantizes a 16-bit folder on load |
| Tool parser | `--tool-call-parser` | `hermes` (Qwen), `llama3_json`, `mistral`, ... Empty = no tool calls |
| Reasoning parser | `--reasoning-parser` | `qwen3`, `deepseek_r1`, ... Empty = none |
| Eager mode | `--enforce-eager` | Try it when start-up fails while compiling |
| Tokenizer | `--tokenizer` | GGUF only: the original repo, e.g. `Qwen/Qwen3-8B` |

### GPUs and helper models

vLLM reserves its memory share on every GPU it uses, so a helper model on the same GPU may not fit. On 3 GPUs the
default (tensor parallel 2) uses GPUs 0 and 1, which leaves GPU 2 for the helpers. Set `small_model_gpu` / each helper's
`gpu` to 2. The pre-load check warns when they overlap.

## Notes

- Start-up takes minutes (loading weights and compiling graphs). The load waits up to `health_timeout_s`.
- Stopping the model stops vLLM and all its worker processes. If the app crashes, the next start stops a vLLM server that
  is still running (`logs/main-engine.pid`).
- The speed badge, cached-token counts and context ring work on vLLM too. They are built from vLLM's token usage.
  MTP/draft acceptance is llama.cpp only.
- The raw API proxy still blocks the server's admin endpoints (`/metrics` and similar), so users cannot read or clear
  each other's cached prompts.
