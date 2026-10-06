# Model Orchestrator

**Status:** exploring
**Last updated:** 2026-09-30
**Related:** [`text.md`](text.md), [`image.md`](image.md), [`three-d.md`](three-d.md),
[`audio.md`](audio.md), [`catalog.md`](catalog.md)

> This is the **main** document for the Model Orchestrator. It covers the shared
> architecture, task model, scheduling, probe and interfaces. Modality-specific model
> choices live in the sibling files linked above.

---

## Summary

A single background service that turns **content requests** ("give me a realistic
axe icon, 512×512", "give me a backstory for a blacksmith NPC", "give me a 3D
model of this snake") into **finished assets**, without the game ever blocking.

It is responsible for:

1. **Capability negotiation** — decide *which* model to use based on the host
   machine's VRAM / RAM and the required quality tier.
2. **Provisioning** — download the chosen models on demand, verify integrity,
   evict unused models when disk runs low.
3. **Scheduling** — own a priority queue of tasks, honour dependencies, and run
   independent tasks in parallel *only when VRAM allows*.
4. **Execution** — drive the actual local runtimes (llama.cpp / diffusers /
   image-to-3D / audio) behind one uniform API.
5. **Lifecycle** — load/unload models, warm them up, retry, cancel, and report
   progress to the game.

The game talks to it over **localhost HTTP** (2-process design). The orchestrator
is a Python service; Unity is a thin client that submits tasks and plays back
results. This keeps heavy ML dependencies out of Unity and lets us use the whole
Python ML ecosystem.

> **Status:** this document was first drafted as a rough plan and has been
> refined with the findings of the experiments in `experiments/`. See
> [Findings from experiments](#findings-from-experiments). Remaining unknowns are
> in [Open Questions](#open-questions).

---

## Goals / Non-goals

### Goals

| # | Goal |
|---|------|
| G1 | One API for every modality: text, image, 3D, animation, music, SFX. |
| G2 | Zero main-thread blocking; results stream in over time with progress events. |
| G3 | Automatically pick model sizes that fit the host GPU; degrade gracefully. |
| G4 | Download models lazily and only once; survive restarts and partial failures. |
| G5 | Reproducible: same request + same seed ⇒ same asset (task 4 golden rule). |
| G6 | Fully local — no cloud calls, ever (Golden Rule 1). |
| G7 | Observable: every task has an id, state, timings and an error reason. |
| G8 | **Open-weight models only** — no gated, closed or licence-restricted weights. |
| G9 | **Quality first, disk second** — model choice is driven by VRAM fit and quality, never by free disk space. |

### Non-goals (for v1)

- Multi-machine / distributed inference.
- Cloud fallback of any kind.
- Training or fine-tuning models at runtime.
- Editing / re-generating an asset *in place* while the player looks at it.
- A GUI. (A debug web page is nice-to-have, not required.)

---

## Constraints

The orchestrator ships **with the game** and must run on whatever machine the
player has — not on any single development box. There is therefore **no fixed
hardware target**; capability is discovered at runtime and the model set adapts.

| Constraint | Assumption | Consequence |
|------------|-----------|-------------|
| GPU | Unknown, possibly none | Probe VRAM at runtime; support CPU-only Tier 0 |
| RAM | Unknown | Do not assume RAM offload is available; measure it |
| Disk | Unknown, possibly small | **Secondary concern.** Never restrict model *choice* by disk; evict unused models instead (see [Disk Policy](catalog.md#disk-policy)) |
| OS | Windows primary; keep it portable | Prefer cross-platform runtimes; avoid Linux-only stacks |
| Engine | Unity shares the GPU | Reserve headroom for the game itself |
| Licence | **Open-weight models only** | No gated/closed weights; prefer permissive licences |

> **No hardcoded hardware.** Any figure in this document marked *(reference)* comes
> from one development machine and is illustrative only. The design must degrade
> gracefully from a GPU-less laptop up to a large workstation, choosing model
> sizes from the probe rather than from a target spec.

> **VRAM budget rule.** The orchestrator must *never* plan more than
> `usable_vram = min(free_vram over the probe window) - reserved`, where
> `reserved` covers the OS, Unity and a safety margin, with an additional
> fragmentation allowance. Models are placed by **measured** footprint, not by
> download size.

> **Disk is not a selection constraint.** Free disk space must **not** cap which
> model is chosen or force a smaller quantization. Disk pressure is handled
> *after the fact* by evicting unused models, always keeping at least one model
> per category. See [Disk Policy](catalog.md#disk-policy).

> **Open-weight only.** Only openly downloadable weights may be used — no gated
> repositories, no closed or licence-restricted weights, and no dependencies on
> models whose terms forbid redistribution or commercial use. Prefer permissive
> licences (Apache-2.0 / MIT); where a model is non-commercial, record it as
> usable for development but **not shippable**, and find an open alternative
> before it reaches the game.

---

## Architecture

```
┌───────────────────────────── Unity (C#) ─────────────────────────────┐
│  Gameplay systems                                                   │
│    NpcDialogueSystem, ShopSystem, LevelLoader, AudioDirector ...    │
│                       │                                             │
│                 ModelOrchestratorClient  (thin, async, event-based) │
│                       │  HTTP + JSON  (localhost)                   │
└───────────────────────┼─────────────────────────────────────────────┘
                        │
┌───────────────────────▼─────────────  Orchestrator service (Python) ─┐
│  API layer      (FastAPI: /tasks, /capabilities, /models, events)    │
│  ─────────────────────────────────────────────────────────────────  │
│  Task Manager   queue, priorities, deps, retries, cancellation      │
│  Scheduler      VRAM-aware placement, parallelism, affinity         │
│  Model Registry catalog, capabilities, download, integrity, evict   │
│  Resource Probe  GPU/RAM/disk detection + tiering formula           │
│  Runtime Hosts  text | image | mesh | anim | audio  (pluggable)     │
│  Cache          content-addressed asset store on disk               │
│  Telemetry      timings, VRAM peaks, failure reasons                │
└─────────────────────────────────────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┬──────────────┐
        ▼               ▼               ▼              ▼
  llama.cpp        diffusers        image-to-3D    MusicGen /
  llama-server     (in-process)     + Blender      AudioGen /
  (router mode)    torch            (SF3D/TripoSR) Kokoro (CPU)
```

### Why two processes?

| Option | Verdict |
|--------|---------|
| Everything in-process in Unity (C#, ONNX) | **Rejected for v1.** C# ML coverage is thin; torch/transformers ecosystem is Python-only. |
| Python subprocess per task | Too heavy to start; no model reuse. |
| **One long-lived Python service + HTTP** | **Chosen.** Keeps models warm, isolates crashes, easy to debug/test from the CLI. |

The HTTP hop is cheap (localhost, small JSON). Large assets are transferred by
**file path**, not by value — Unity loads from a shared cache directory.

> **Caveat from exp. 0102.** `diffusers` runs in the *same* process as the
> orchestrator, so a diffusion crash takes the whole service down. The service
> therefore needs per-task crash containment (run hosts in a supervised child
> process or guard with try/except + restart), and ComfyUI remains the
> crash-isolated escape hatch for authoring-time work.

---

## Findings from experiments

Rough plan refined with the experiments in `experiments/`. Cross-cutting decisions:

| Concern | Decision | Evidence |
|---------|----------|----------|
| Text inference | **llama.cpp `llama-server` in router mode** (Ollama as convenience front-end) | [0101](../../experiments/llm-runtimes/0101-text-runtime-selection/README.md) |
| Image generation | **diffusers in-process** for the hot path; ComfyUI only for authoring | [0102](../../experiments/image-generation/0102-image-runtime-selection/README.md) |
| Image → 3D | **Stable-Fast-3D** primary; **TripoSR** (MIT) fallback; TripoSG (MIT, shape-only) | [0103](../../experiments/three-d-generation/0103-image-to-3d-selection/README.md) |
| Animation | **Auto-rig + retarget** (UniRig MIT or Rigify); **not** generated motion | [0106](../../experiments/three-d-generation/0106-animation-feasibility/README.md) |
| Music | **MusicGen small** (medium as fallback) | [0104](../../experiments/audio-generation/0104-audio-model-selection/README.md) |
| SFX | **AudioGen medium** | [0104](../../experiments/audio-generation/0104-audio-model-selection/README.md) |
| Speech | **Kokoro-82M** (CPU), Piper (MIT, CPU) fallback | [0104](../../experiments/audio-generation/0104-audio-model-selection/README.md) |
| Streaming speech | **sherpa-onnx** (TTS + online ASR); Piper/Kokoro TTS; faster-whisper final STT | [0109](../../experiments/audio-generation/0109-streaming-tts-stt/README.md) |
| VRAM probing | **NVML/`pynvml`** with an `nvidia-smi` fallback; measure **delta of device `used`** | [0105](../../experiments/benchmarks/0105-vram-probe-residency/README.md) |
| Tool calling | **Split persona from extraction**; native `tools` + `--jinja` | [introduction-test-tts-sst](../../experiments/llm-runtimes/introduction-test-tts-sst/README.md) |

Per-modality detail (model families, sizes, licences, fallbacks) lives in the
sibling files: [`text.md`](text.md), [`image.md`](image.md), [`three-d.md`](three-d.md),
[`audio.md`](audio.md).

### Key corrections this forced on the design

1. **Per-process VRAM is unavailable on Windows/WDDM.** NVML and `nvidia-smi`
   return `[N/A]` for every PID. The scheduler must **not** attribute VRAM per
   process; instead it measures a model's footprint as the **delta of device
   `used`** across load/unload, and budgets against the **minimum free across a
   polling window** rather than an instantaneous reading. (exp. 0105)
2. **The budget needs a fragmentation slack term**, not just a reserve:
   `usable_vram = min_free_over_window − reserve − fragmentation_slack`, and
   `resident_ok = Σ peak_footprint ≤ usable_vram`. Peaks (KV cache growth,
   denoise step) — not resident weight size — are what must fit. (exp. 0105)
3. **Multi-model residency eliminated vLLM** as an option and made llama.cpp's
   router mode (`POST /models/load`, `/models/unload`) the natural fit, since it
   manages several resident models in one process. (exp. 0101)
4. **Image runtime is `diffusers`, not ComfyUI**, because ComfyUI's HTTP-level
   VRAM release is all-or-nothing and its second CUDA context is not
   reclaimable — it cannot be bin-packed by the scheduler. (exp. 0102)
5. **The best 3D models fail on licence, not quality.** Hunyuan3D-2 full
   (17 GB w/ texture) and 2.1 (29 GB VRAM) are out; **selective download is
   mandatory** on the big repos. Prefer **MIT** models (TripoSR, TripoSG) and
   treat gated/non-commercial ones as development-only. (exp. 0103)
6. **Animation is a template/retargeting problem, not a generative one.**
   Fully generated motion is blocked by **SMPL licensing**, not hardware; the
   path is auto-rig → auto-skin → retargeted stock clips, and generated meshes
   need a **weight-sanity gate** before they can be skinned. (exp. 0106)
7. **Audio splits into two small models plus a CPU TTS**: MusicGen small
   (~2.6 GB) for music, AudioGen medium (~3.9 GB) for SFX, Kokoro (<0.5 GB, CPU)
   for speech — the CPU TTS deliberately never competes for VRAM. (exp. 0104)
8. **Every model must carry a licence + shipping flag** in the registry, and the
   planner must refuse to ship non-commercial weights. (exp. 0103, 0104)
9. **Speech is a streaming, CPU-only concern.** TTS streams at **sentence**
   granularity (split the LLM token stream, synthesise each sentence as it
   completes) and STT needs a **true streaming** recogniser for live captions —
   Whisper is chunked and lags by a full chunk. Both run on **CPU**, so voice I/O
   costs zero VRAM and works on Tier 0. The LLM→TTS bridge is a **sentence
   splitter**, not a model. (exp. 0109)
10. **Tool calling fails from instruction competition, not model size.** A prompt
    that asks a model to hold a persona, follow a multi-step agenda *and* decide
    when to call a tool makes it do none of them reliably. Splitting the turn
    into an **extraction** call (no persona, no agenda) and a **persona** call
    (no tools) took the same model from 1/3 to 4/4 tool calls. Model choice
    still matters — prefer 2025–2026 models over 2024 ones — but prompt
    structure dominates. (introduction-test-tts-sst)
11. **A host must release its subprocesses.** A leaked `llama-server` holds
    VRAM and makes the planner refuse models that previously fit. Hosts need an
    explicit `unload()` *and* a destructor safety net, and callers must always
    stop the orchestrator. (introduction-test-tts-sst)
12. **Reasoning models need thinking disabled for short turns.** Granite 4.2 and
    Qwen3 spend the whole token budget on hidden reasoning and return empty
    `content` unless `enable_thinking: false` is passed. The catalog records this
    per model. (introduction-test-tts-sst)

---

## Components

### 1. API Layer

Uniform surface for all modalities. One submit call, one status call, one
streaming progress channel.

**Important:** every request returns immediately with a `task_id`; the caller
never blocks. Long jobs (3D, music) stream progress so the game can show a
spinner or keep playing.

### 2. Task Manager

Owns the task graph. A task record:

| Field | Meaning |
|-------|---------|
| `id` | ULID/UUID |
| `kind` | `text` \| `image` \| `mesh3d` \| `animation` \| `music` \| `sfx` \| `tts` \| `stt` \| *(+`embedding`,`upscale` later)* |
| `spec` | modality-specific parameters (prompt, size, seed, refs) |
| `depends_on` | list of task ids whose outputs are inputs to this task |
| `priority` | see below |
| `resource_class` | `gpu-heavy` \| `gpu-light` \| `cpu` — used by the scheduler |
| `state` | `queued → blocked → loading-model → running → post → done` (or `failed`/`cancelled`) |
| `result` | asset path(s), metadata, seed used |
| `attempts` | retry counter |

**Important:** the task graph must be a DAG; a dependency is only satisfiable
once its producer is `done`. Blocked tasks do **not** consume a queue slot.

### 3. Scheduler

The heart of the orchestrator. Responsibilities:

- Pick the next *runnable* task using `(priority, age, resource estimate)`.
- Pack tasks onto available VRAM using a **bin-packing / knapsack** heuristic:
  sum of resident model **peak** footprints + new task peak footprint ≤
  `usable_vram` (measured against the min free over the probe window).
- Run **CPU-only hosts** (speech, Blender normalise/rig) on a parallel lane that
  never consumes the VRAM budget.
- Support **model co-residency**: multiple requests that share a model must not
  load it twice.
- Enforce **affinity**: prefer a model already resident to avoid load/unload cost.
- Handle **eviction**: LRU over resident models, but never evict a model with a
  running task; if nothing fits, serialise (run one at a time).
- Respect **concurrency caps** per runtime (some runtimes are not thread-safe).

Priority bands (highest first):

| Band | Example | Target latency |
|------|---------|----------------|
| `INTERACTIVE` | Text reply to the player mid-dialogue | < 2 s first token |
| `FOREGROUND` | The 3D model of an item the player is about to buy | seconds–minutes |
| `BACKGROUND` | Music for a level the player just entered | may finish after entry |
| `IDLE` | Pre-generate assets for a shop that isn't open yet | whenever free |

> **Design note:** priorities are *bands*, not absolute values, so that
> starvation is avoided by promoting the oldest task in a band.

### 4. Model Registry + Capability Probe

Two halves:

- **Probe** — detect GPU name, total/available VRAM, RAM, free disk, CUDA/ROCm
  support, and compute a machine **tier** (see formula). Runs at startup and
  re-checks before large loads.
- **Registry** — a declarative catalog (JSON/manifest) of known models with:
  modality, family, params, quantization, disk size, measured/published VRAM
  footprint, quality score, licence, download URI + hash.
  See [Model Catalog](catalog.md).

The **planner** joins probe + catalog: for a requested `(kind, quality)`, choose
the **largest model whose footprint fits** the current free VRAM. Disk size is
**not** part of the fit test — see [Disk Policy](catalog.md#disk-policy). If even
the smallest model cannot fit, the task fails with a typed error the game can
fall back from (e.g. use a placeholder).

### 5. Runtime Hosts

Pluggable adapters, one per modality. Each exposes the same internal contract:
`can_load(model) -> footprint`, `load()`, `run(task) -> artifact`, `unload()`.

| Host | Backs onto | Detail |
|------|-----------|--------|
| Text | **llama.cpp `llama-server`** (router mode); Ollama optional | [`text.md`](text.md) |
| Embedding | llama.cpp `/v1/embeddings` | [`text.md`](text.md) |
| Image | **diffusers** (in-process); ComfyUI only for authoring | [`image.md`](image.md) |
| Mesh3D | **Stable-Fast-3D** (primary), **TripoSR** (MIT fallback) | [`three-d.md`](three-d.md) |
| Animation | **Auto-rig + retarget** (UniRig/Rigify) via Blender headless | [`three-d.md`](three-d.md) |
| Music | **MusicGen small** | [`audio.md`](audio.md) |
| SFX | **AudioGen medium** | [`audio.md`](audio.md) |
| Speech | **Kokoro-82M** / **Piper** (CPU) | [`audio.md`](audio.md) |
| Speech (streaming) | **sherpa-onnx** (TTS + online ASR), **faster-whisper** (final STT) | [`audio.md`](audio.md) |

> **Important:** a host must be able to report an **estimated VRAM footprint per
> request**, since diffusion footprints vary with resolution and 3D with input
> size. Schedulers that only know static model size will overcommit.

> **Hosts are not only models.** Some "models" are really multi-step pipelines:
> image-to-3D is *generate → extract → bake texture → Blender normalise/export*,
> and animation is *auto-rig → auto-skin → retarget*. The host contract therefore
> wraps a **pipeline**, and each step may be its own scheduler-visible sub-task
> (e.g. the Blender normalise step is CPU-only and runs on a CPU host). This keeps
> VRAM-heavy and CPU-only work separable so they can run in parallel (exp. 0103,
> 0106).

### 6. Cache

Content-addressed on disk (`sha256(kind+spec+model+seed)`), so identical requests
are free and generation is deterministic. Eviction by LRU with a **byte budget**,
never evicting assets referenced by the current save.

> **Caveat from exp. 0102.** Determinism holds only when the seed **and** the
> device/dtype/offload mode match; a CPU-vs-GPU generator or a different offload
> setting changes the image. The cache key must therefore include the resolved
> device/offload configuration, not just the visible request parameters.

### 7. Telemetry

Per task: queue wait, model-load time, run time, peak VRAM, artifact size, and
failure reason. Needed both for tuning and for the "which model should we
download" formula.

---

## Task Model & Dependency Examples

```
Player opens shop
  └─ FOREGROUND  image   "rusty broadsword icon, 512x512"
       └─ FOREGROUND mesh3d  depends_on=[image]  + description
            └─ BACKGROUND normalise  Blender: recentre, scale, re-export GLB
                 └─ BACKGROUND animation  depends_on=[mesh3d]  ["idle","swing"]

Player enters a level
  ├─ BACKGROUND music   "calm forest, low strings"        (independent)
  └─ INTERACTIVE text   "greet the player by name"        (independent)

Player talks to an NPC (voice)
  ├─ INTERACTIVE stt    mic stream -> partials -> final transcript   (CPU)
  ├─ INTERACTIVE text   depends_on=[stt]  "reply in character"       (GPU)
  └─ INTERACTIVE tts    depends_on=[text]  sentence-by-sentence      (CPU)
```

Runnable now: `image`, `music`, `text` (three hosts, three models — **only if
VRAM permits**). `mesh3d` is `blocked` until `image` is `done`; the Blender
normalise step and animation are blocked until the mesh exists. Speech runs on
**CPU** and so is always runnable, even under full VRAM pressure.

> **Streaming tasks.** `tts` and `stt` are the only kinds that emit **partial
> results while running** rather than a single artifact at the end. The task
> model must therefore support a progress/partial channel, and `tts` consumes
> the *token stream* of its `text` dependency rather than waiting for it to
> finish — so the dependency edge is a **stream**, not a barrier (exp. 0109).

---

## Capability Probe & Tiering Formula

Refined after exp. 0105. The probe reports, per modality, not just a tier but the
**largest model that fits right now**.

```
tier_score = 0.70 * norm(vram_gb)        # dominant factor
           + 0.25 * norm(ram_gb)
           + 0.05 * norm(free_disk_gb)   # minor: disk no longer gates the catalog
```

with `norm(x) = clamp((x - min) / (max - min), 0, 1)` against sensible bounds
(e.g. VRAM 4–48 GB). Then:

- `Tier 0` — CPU only / < 6 GB VRAM → tiny/quantized models, text + small images; CPU-only hosts (Piper/Kokoro) still work.
- `Tier 1` — 6–10 GB → small text, SD1.5-class images.
- `Tier 2` — 10–16 GB → medium text, SDXL-class images, small 3D.
- `Tier 3` — 16–32 GB → large text, best images, 3D + audio concurrently.
- `Tier 4` — 32 GB+ → everything, larger batches, fewer serialisations.

> **Disk is a tie-breaker, not a gate.** The `free_disk_gb` term only nudges the
> tier score; it must never prevent selecting the best model that fits VRAM.
> Disk pressure is resolved by eviction, not by downgrading the model. See
> [Disk Policy](catalog.md#disk-policy).

**Co-residency budget** (corrected by exp. 0105):

```
usable_vram    = min_free_over_window - reserve - fragmentation_slack
resident_ok    = sum(peak_footprint_i) <= usable_vram
per_model_cap  = usable_vram / N          # N = expected hot models
```

- Budget against the **minimum free VRAM over a sampling window**, not an
  instantaneous reading, so transient spikes (Unity, the desktop compositor)
  cannot cause an OOM later.
- Add a **fragmentation slack** (10–15 % of `usable_vram`) on top of the reserve;
  two footprints that exactly sum to `usable_vram` can still fail to find one
  contiguous block.
- Use **peaks**, not resident weight size: an LLM's KV cache grows with context,
  and a diffusion model peaks at the denoise step.
- `N` is the expected number of *hot* models (start `N = 2`: one text + one
  generator), and it is what makes the chosen model size depend on co-residency,
  not just on total VRAM.

> *(reference)* On the development L4 (24 GB total, ~22 GB free idle, 3 GB
> reserve), `N = 2` gave ~9.4 GiB per model — enough for a small text model plus
> SD1.5-class diffusion. This is illustrative only; the shipped game measures.

> **Do not rely on per-process VRAM.** Windows/WDDM returns `[N/A]` for every
> PID via both NVML and `nvidia-smi` (exp. 0105). Footprints are always measured
> as the **delta of device `used`** around load/unload.

---

## Rough Interfaces

### C# (Unity side — thin client)

```csharp
public interface IModelOrchestrator
{
    Task<ProbeReport>         GetCapabilitiesAsync(CancellationToken ct = default);
    Task<TaskHandle>          SubmitAsync(ContentRequest req, CancellationToken ct = default);
    Task<TaskStatus>          GetStatusAsync(string taskId, CancellationToken ct = default);
    Task<bool>                CancelAsync(string taskId, CancellationToken ct = default);
    IAsyncEnumerable<TaskProgress> SubscribeAsync(string taskId, CancellationToken ct = default);
}

public enum TaskKind { Text, Image, Mesh3D, Animation, Music, Sfx }
public enum Priority { Interactive, Foreground, Background, Idle }

public sealed record ContentRequest(
    TaskKind Kind,
    IReadOnlyDictionary<string, object> Spec,
    Priority Priority,
    IReadOnlyList<string> DependsOn = null,
    int? Seed = null);

public sealed record TaskStatus(
    string TaskId, TaskKind Kind, string State, float Progress,
    string[] ArtifactPaths, string Error, ProbeReport Probe);
```

### Python (service side)

```python
class ModelOrchestrator:
    async def capabilities(self) -> ProbeReport: ...
    async def submit(self, req: ContentRequest) -> TaskId: ...
    async def status(self, task_id: TaskId) -> TaskStatus: ...
    async def cancel(self, task_id: TaskId) -> bool: ...
    def events(self, task_id: TaskId) -> AsyncIterator[TaskProgress]: ...

class RuntimeHost(Protocol):
    modality: Modality
    def estimate_vram(self, spec: Spec) -> float: ...
    def load(self, model: ModelDescriptor) -> None: ...
    async def run(self, spec: Spec, deps: list[Artifact]) -> list[Artifact]: ...
    def unload(self) -> None: ...
```

### HTTP surface (draft)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/capabilities` | probe report + chosen tier |
| `GET` | `/models` | catalog + what's installed/loaded |
| `POST` | `/tasks` | submit, returns `{task_id}` |
| `GET` | `/tasks/{id}` | status + result |
| `DELETE` | `/tasks/{id}` | cancel |
| `GET` | `/events` | SSE stream of all task updates |
| `POST` | `/shutdown` | unload everything |

---

## External Libraries

Chosen from the experiments. See [Findings](#findings-from-experiments).

| Concern | Chosen | Notes |
|---------|--------|-------|
| Text inference | **llama.cpp `llama-server`** (router mode) | MIT, single binary, JSON-schema, multi-model residency (0101) |
| Text convenience front-end | **Ollama** (optional) | Same engine; use if model-pull ergonomics matter |
| Embeddings | llama.cpp `/v1/embeddings` | One runtime for text + embeddings |
| Images | **diffusers** + torch, in-process | Only option with per-model VRAM control (0102) |
| Images (authoring only) | ComfyUI (HTTP) | Workflow ecosystem; not on the scheduler's hot path (0102) |
| Image → 3D | **Stable-Fast-3D** primary; **TripoSR**/TripoSG (MIT) fallback | Game-ready GLB vs MIT licence trade-off (0103) |
| 3D post-step | **Blender headless** + `trimesh` | Normalise, rescale, re-export GLB (0103) |
| Animation | **Blender headless** auto-rig + retarget | UniRig (MIT) or Rigify; no generative motion (0106) |
| Music | **MusicGen small** (medium fallback) | Speed driven (0104) |
| SFX | **AudioGen medium** | Text-to-SFX specialist (0104) |
| Speech | **Kokoro-82M** (CPU), **Piper** (MIT, CPU) fallback | Never contends for VRAM (0104) |
| Streaming speech | **sherpa-onnx** (TTS + online ASR); faster-whisper for final STT | CPU-only; sentence-level TTS, true streaming STT (0109) |
| HF downloads | `huggingface_hub` | Range/resume, caching, hashing, selective files |
| Service | **FastAPI + uvicorn** | Async, SSE |
| VRAM probe | `nvidia-ml-py`/`pynvml`, `nvidia-smi` fallback | Per-process data unavailable on Windows (0105) |
| Scheduling | custom (asyncio) | Generic queues (Celery/RQ) don't understand VRAM |
| Content hashing | `blake3`/`sha256` | Deterministic cache keys |

### Rejected

| Candidate | Why |
|-----------|-----|
| **vLLM** | Linux-only, one model per process — fails multi-model residency (0101) |
| **HF transformers + bitsandbytes** (text) | No built-in server; heavy torch footprint (0101) |
| **LM Studio server** | Proprietary, GUI-first (0101) |
| **ComfyUI** as hot path | Coarse, all-or-nothing VRAM release (0102) |
| **Large 3D models** (Hunyuan3D-2 full/2.1, TRELLIS, InstantMesh) | Licence/VRAM, not quality (0103) |
| **MusicGen large** | ~21 GB weights, 60–90 s per 10 s clip (0104) |
| **XTTS-v2** | Only justified by voice cloning; licence-encumbered (0104) |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Disk exhaustion from model downloads | Evict unused models, keeping at least one per category; lazy download; selective file fetch (see [Disk Policy](catalog.md#disk-policy)) |
| VRAM overcommit → OOM / driver TDR | Budget on min-free over a window + fragmentation slack + measured peak footprints + serialise fallback (exp. 0105) |
| Orchestrator crash takes down in-process diffusion | Per-task containment: supervised child process, or try/except + restart; ComfyUI is the isolated authoring path (exp. 0102) |
| Shipping non-open / non-commercial weights | Registry `shippable` flag; planner refuses gated/non-commercial in builds (exp. 0103, 0104) |
| Generated meshes cannot be skinned | Weight-sanity gate (validate non-manifold/zero-weight) before rigging; fallback to unrigged prop (exp. 0106) |
| Cannot attribute VRAM per process (Windows/WDDM) | Never use per-PID memory; always measure device `used` deltas (exp. 0105) |
| Runtimes not thread-safe | Per-host concurrency cap = 1 by default |
| Head-of-line blocking by a huge IDLE task | Priority bands + preemption at safe points |
| Dependency cycles | Validate DAG on submit; reject cycles |
| Non-determinism | Seed every generator; store seed in the task result |
| Long model load stalls | Warm-up at startup for likely-next models |
| Python service crash | Supervisor in Unity restarts it; tasks re-queued from cache |

---

## Milestones (draft)

| M | Scope | Exit criteria |
|---|-------|---------------|
| M0 | Probe + catalog + CLI | `mo probe` prints tier and min-free budget; `mo models` lists catalog with `shippable` flags |
| M1 | Text host (llama.cpp) + task queue + HTTP | Dialogue request round-trips from a C# console; JSON schema enforced |
| M2 | Image host (diffusers) | Icon generated, cached, deterministic with seed; VRAM released after |
| M3 | Unity client + SSE progress | In-editor request → texture applied, UI shows progress |
| M4 | Dependency chains + plugin pipeline host | image → mesh3d → Blender normalise end-to-end |
| M5 | VRAM-aware parallelism | Text + diffusion resident concurrently without OOM, validated by `probe.py` |
| M6 | Audio (music + SFX) | Level music generated after level load; CPU TTS independent of VRAM |
| M6b | Streaming speech | Voice dialogue: live STT partials → LLM → sentence-streamed TTS, all on the CPU lane |
| M7 | Animation | Auto-rig + retarget of an idle/swing clip from a generated mesh, behind a weight gate |

---

## Open Questions

### Resolved

| # | Question | Answer | Experiment |
|---|----------|--------|-----------|
| Q1 | Which text runtime? | **llama.cpp `llama-server` router mode** (Ollama optional) | [0101](../../experiments/llm-runtimes/0101-text-runtime-selection/README.md) |
| Q2 | diffusers vs ComfyUI? | **diffusers** for runtime; ComfyUI for authoring only | [0102](../../experiments/image-generation/0102-image-runtime-selection/README.md) |
| Q3 | Which image-to-3D model? | **Stable-Fast-3D**, TripoSR/TripoSG (MIT) fallbacks | [0103](../../experiments/three-d-generation/0103-image-to-3d-selection/README.md) |
| Q4 | Music/SFX models? | **MusicGen small** + **AudioGen medium** + **Kokoro** (CPU) | [0104](../../experiments/audio-generation/0104-audio-model-selection/README.md) |
| Q5 | How to measure VRAM? | NVML/`pynvml` + `nvidia-smi` fallback; **device `used` deltas**, min-free window | [0105](../../experiments/benchmarks/0105-vram-probe-residency/README.md) |
| Q6 | Is animation generation feasible? | Not locally — **auto-rig + retarget** instead | [0106](../../experiments/three-d-generation/0106-animation-feasibility/README.md) |
| Q12 | Streaming TTS/STT engines? | **sherpa-onnx** (TTS + online ASR), Piper/Kokoro TTS, faster-whisper final STT; CPU-only | [0109](../../experiments/audio-generation/0109-streaming-tts-stt/README.md) |

### Open

| # | Question | Experiment |
|---|----------|-----------|
| Q7 | Unity integration: file handoff, asset import, progress events? | `0107` |
| Q8 | Does the tier formula predict real capability across machines? | `0108` |
| Q9 | Do in-process diffusers and llama.cpp actually co-exist inside the budget? | new |
| Q10 | Should the service be a Windows service, or game-launched? | — |
| Q11 | How to gate generated meshes before rigging (weight sanity)? | new |
| Q13 | Run speech in-process in Unity (sherpa-onnx C# API) instead of the Python host? | new |
| Q14 | What is the right eviction threshold and retention rule for models on a small disk? | new |

---

## Glossary

- **Host** — adapter that runs one modality behind a uniform contract.
- **Resident model** — a model currently loaded in VRAM.
- **Footprint** — VRAM a model actually holds while running a request.
- **Tier** — coarse machine capability bucket (0–4).
- **Band** — coarse priority class (`INTERACTIVE`…`IDLE`).
- **Artifact** — a file produced by a task (image, mesh, audio, text).
- **Category** — a modality bucket (`text`, `image`, `mesh3d`, `audio`, …) used by the disk eviction rule.
