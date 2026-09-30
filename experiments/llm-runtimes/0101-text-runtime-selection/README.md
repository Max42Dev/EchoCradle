# 0101 — Text runtime selection (llama.cpp vs Ollama vs vLLM)

**Status:** done
**Date:** 2026-09-29
**Author:** agent

## Question

Which local text-inference runtime should the Model Orchestrator use on Windows
for many short, low-latency, structured requests **while other GPU models are
resident**?

## Hypothesis

An OpenAI-compatible llama.cpp server (or Ollama, which wraps it) gives the best
size/latency/control trade-off on Windows; vLLM is Linux-oriented and will not
run acceptably here.

## Setup

- **OS:** Windows Server 2022 Datacenter (build 20348), AWS EC2
- **Hardware:** NVIDIA L4 24 GB (23 034 MiB total, ~567 MiB used at idle),
  16 GB RAM, ~34 GB free disk (disk is the hard constraint)
- **Runtime:** none installed on `PATH` in this environment — `ollama` is **not**
  on `PATH` and no `ollama` service is registered, so no live numbers were
  captured here. This experiment is a **documentation + tooling** spike: it
  selects the runtime from primary sources and ships a benchmark harness to
  measure it once a runtime is installed.
- **Models:** none downloaded (disk budget). The harness defaults to the
  already-known `llama3.1:8b` (4.9 GB) from the reference box.
- **Tooling:** Python 3.13.15 (`C:\Program Files\Python313`). Neither `httpx`
  nor `requests` is installed, so the harness uses the stdlib `urllib` path.

### Environment probe (this box)

```text
ollama --version         -> not recognized (not on PATH, no service)
python --version         -> Python 3.13.15
nvidia-smi               -> NVIDIA L4, 23034 MiB total, 567 MiB used
python -c "import httpx" -> ModuleNotFoundError
```

## How to run

The harness talks to any OpenAI-compatible `/v1/chat/completions` endpoint, so
the same command works for llama-server, Ollama, vLLM and LM Studio.

```powershell
# 1. Start a runtime (pick one). Examples:

# llama.cpp — single model, explicit VRAM control
llama-server.exe -m models\llama-3.1-8b-Q4_K_M.gguf -ngl 99 -c 4096 --port 8080

# llama.cpp — router mode: multiple models resident, load/unload over HTTP
llama-server.exe --models-dir .\models --models-max 3 --port 8080

# Ollama (if installed) — /v1 shim on 11434
ollama serve

# 2. Benchmark it (defaults to Ollama's /v1 on 11434)
cd experiments\llm-runtimes\0101-text-runtime-selection\src
python bench_text_runtime.py --base-url http://127.0.0.1:8080/v1 --model local
python bench_text_runtime.py --base-url http://127.0.0.1:11434/v1 --model llama3.1:8b
python bench_text_runtime.py --runs 5 --max-tokens 128 --no-schema
```

The script prints per-run TTFT, tokens/sec, total time and whether the reply
parsed as JSON, then a median summary. It needs no API key and no third-party
packages.

## Results

No live latency numbers were captured (no runtime installed, no models
downloaded — see Setup). The deliverable is the runtime comparison below,
sourced from each project's own documentation.

### Runtime comparison

| Attribute | **llama.cpp** (`llama-server`) | **Ollama** | **vLLM** | **HF transformers + bitsandbytes** | **LM Studio server** |
| --- | --- | --- | --- | --- | --- |
| **Windows support** | ✅ Native `llama-server.exe` (CUDA build) | ✅ Native installer (`OllamaSetup.exe`) | ❌ Linux only; WSL or community fork required | ✅ Native (bitsandbytes lists Windows 11 / Server 2022+) | ✅ Native app |
| **OpenAI-compatible API** | ✅ `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/responses` (+ Anthropic `/v1/messages`) | ✅ `/v1` shim over the native `/api/*` | ✅ `/v1` (its primary interface) | ❌ None built in — you write the FastAPI/HTTP layer | ✅ `/v1/chat/completions`, `/v1/responses`, `/v1/embeddings` |
| **GGUF / quant support** | ✅ GGUF is native; full quant range (Q4_K_M, IQ*, etc.) | ✅ GGUF native (imports GGUF, safetensors, MLX) | ⚠️ Primarily safetensors; GGUF is experimental | ⚠️ safetensors + bitsandbytes 4/8-bit (not GGUF) | ✅ GGUF native |
| **Per-model VRAM control** | ✅ Excellent: `-ngl/--gpu-layers`, `--fit`, `-ts/--tensor-split`, `-ctk/-ctv` KV quant, `--override-tensor` | ⚠️ Coarse: `num_gpu` option, `OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS` | ✅ `--gpu-memory-utilization`, `--max-model-len`, `--kv-cache-dtype` | ✅ `device_map`, `load_in_4bit/8bit`, `max_memory` | ⚠️ GUI/API GPU-offload setting per model |
| **Multi-model residency** | ✅ **Router mode**: `--models-dir` + `--models-max` (default 4) keeps several models loaded and routes by `model` field | ✅ `OLLAMA_MAX_LOADED_MODELS` (default 3×GPU, 1 on low VRAM) | ❌ One model per server process; no native multi-model | ⚠️ Manual — you hold several `model` objects and manage VRAM yourself | ✅ JIT loading + Idle TTL / auto-evict |
| **Model load/unload control** | ✅ `POST /models/load`, `POST /models/unload`, `GET /models/sse` events, `--sleep-idle-seconds` | ✅ `keep_alive: 0` unloads; `GET /api/ps` lists resident models | ⚠️ Process restart / `sleep` mode only | ⚠️ Manual (`del model; torch.cuda.empty_cache()`) | ✅ `POST /load`, `POST /unload`, TTL auto-evict |
| **Structured JSON / grammar** | ✅ Best-in-class: `response_format` `json_schema`, `-j/--json-schema`, GBNF `--grammar` | ✅ `format` = `json` or a JSON schema | ✅ Guided decoding (xgrammar/outlines) | ⚠️ Via `outlines`/`xgrammar` or prompt-only | ✅ Structured Output support |
| **Disk footprint of runtime** | ✅ Smallest: single self-contained binary (tens of MB; CUDA build a few hundred MB) | ⚠️ Installer + service, few hundred MB (models separate) | ❌ Largest: PyTorch + CUDA + kernels, several GB | ❌ Large: PyTorch + transformers + bitsandbytes, several GB | ⚠️ App install, few hundred MB |
| **Ease of embedding** | ✅ Excellent: one exe, no Python, HTTP; spawn per model or use router | ✅ Excellent: one service, HTTP, model pull built in | ⚠️ Heavy Python stack; awkward on Windows | ⚠️ In-process Python; you own the server, batching and lifecycle | ⚠️ Good API but GUI-first, proprietary |
| **Licensing** | ✅ MIT | ✅ MIT | ✅ Apache-2.0 | ✅ Apache-2.0 (transformers) / MIT (bitsandbytes) | ❌ Proprietary EULA (free to use, not open source / not redistributable) |

### Notes on the decisive rows

- **Multi-model residency is the requirement that eliminates vLLM.** vLLM serves
  one model per process; co-residency means running several servers and
  hand-partitioning VRAM. llama.cpp's router mode and Ollama's
  `OLLAMA_MAX_LOADED_MODELS` both do this natively.
- **Disk is the second filter.** vLLM and the transformers+bitsandbytes stack
  each pull in a multi-GB PyTorch/CUDA dependency tree before a single model is
  downloaded. On a ~34 GB budget that is a large fixed cost; llama.cpp is a
  single binary.
- **Structured output is a first-class need** (Golden Rule 5: LLM output is
  untrusted). llama.cpp's JSON-schema-constrained decoding is the strongest and
  is exposed directly through the OpenAI-compatible `response_format`.
- **Ollama is llama.cpp underneath** (its README lists llama.cpp as its only
  backend), so it inherits GGUF and grammar support while adding model
  management — at the cost of coarser VRAM control.

## Conclusion

- **llama.cpp (`llama-server`) — `promising` (recommended).** Native Windows
  binary, MIT, smallest disk footprint, the finest per-model VRAM control, and
  the only candidate with first-class **multi-model residency + HTTP
  load/unload** (router mode) plus schema-constrained JSON. It maps directly
  onto the Model Orchestrator's "keep several models resident, load/unload
  dynamically" requirement.
- **Ollama — `promising` (fallback / convenience).** Same engine, easier model
  management, MIT, native Windows. Choose it if model pull/registry ergonomics
  matter more than fine VRAM control. Note it was **not on `PATH`** on this box.
- **vLLM — `rejected` for this project.** Linux-only (WSL on Windows), one model
  per process, and a multi-GB dependency tree. Its throughput advantage is
  irrelevant for short, low-latency, single-user requests.
- **HF transformers + bitsandbytes — `rejected` for the text runtime.** No
  OpenAI-compatible server out of the box, manual lifecycle/VRAM management, and
  a heavy PyTorch footprint. (It remains the right choice for the *diffusion*
  and image-to-3D hosts, which are Python-native anyway.)
- **LM Studio server — `rejected`.** Capable and easy, but proprietary and
  GUI-first; not something to embed in a shipped orchestrator.

**Recommendation:** standardise the text runtime on **llama.cpp `llama-server`
in router mode**, with **Ollama** as an optional convenience front-end. Keep the
orchestrator's text host behind the OpenAI-compatible interface so either can be
swapped without code changes.

## Next steps

- [ ] Install a llama.cpp CUDA build and run `bench_text_runtime.py` against it
      to capture real TTFT / tokens-per-sec on the L4.
- [ ] Measure co-residency: load a text model + a diffusion model and confirm
      the VRAM budget rule (`usable = total − 3 GB`) holds.
- [ ] Verify router-mode `POST /models/load` / `/models/unload` latency and
      whether `--sleep-idle-seconds` is a better fit than explicit unload.
- [ ] Promote finding to `../../ideas/model-orchestrator/text.md`.

## Links

- llama.cpp server docs (OpenAI API, router mode, JSON schema, load/unload):
  <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>
- llama.cpp license (MIT): <https://github.com/ggml-org/llama.cpp/blob/master/LICENSE>
- Ollama API (structured outputs, `keep_alive`, `/api/ps`):
  <https://github.com/ollama/ollama/blob/main/docs/api.md>
- Ollama README (Windows install, llama.cpp backend):
  <https://github.com/ollama/ollama/blob/main/README.md>
- vLLM installation (Linux-only requirement):
  <https://docs.vllm.ai/en/latest/getting_started/installation/gpu.html>
- vLLM OpenAI-compatible server:
  <https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/>
- bitsandbytes system requirements (Windows support, license):
  <https://github.com/bitsandbytes-foundation/bitsandbytes/blob/main/README.md>
- LM Studio OpenAI-compatible endpoints:
  <https://lmstudio.ai/docs/app/api/endpoints/openai>
- LM Studio REST API (load/unload, TTL/auto-evict):
  <https://lmstudio.ai/docs/developer/rest/endpoints>
- Related idea: `../../ideas/model-orchestrator/text.md`
- Harness: [`src/bench_text_runtime.py`](src/bench_text_runtime.py)
