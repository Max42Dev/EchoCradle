# ModelOrchestrator startup configuration plan

Status: proposed — 2026-10-25. Implementation plan only; no runtime changes.

## Goal

Choose one backend/model pipeline for every enabled capability when the
orchestrator starts. Load user choices from a local JSON file; automatically
resolve unspecified choices against hardware and product constraints. Keep the
resolved choices stable for that process. Provision missing assets automatically,
with prioritized background startup downloads and demand-driven promotion.

Provide a **very small, independent configuration GUI**, runnable without Unity
or a running game. It loads, automatically configures, manually edits, and saves
the same configuration through ModelOrchestrator functionality. The orchestrator
is the sole source of hardware information, available choices, resource estimates,
validation, automatic selection, and configuration persistence; the GUI only
presents these operations.

The requested default game VRAM reserve is **5 GiB**, configurable. Use GiB
consistently in new configuration and diagnostics; verify the units of existing
`*_gb` measurements before integrating them.

## Current implementation and reuse

- `orchestrator/orchestrator/catalog.py`: model descriptors already include peak
  `vram_gb`, RAM, quality, download size, modality, and a `shippable` flag.
  Modalities currently cover text, TTS, and STT.
- `planner.py`: selects a fitting model independently for each request, using
  catalog preferences before quality ranking. It does not select or admit a
  combined portfolio.
- `probe.py`: currently reserves 3 GB with additional 12% headroom; sums free
  VRAM across devices and uses total system RAM. These are insufficient for
  placement-aware combined planning.
- `orchestrator.py`: in-process calls lazily select/provision/load models;
  `start()` warms text. Startup and service readiness semantics differ.
- `service.py`: constructor/CLI configuration supports profiles and selected
  text/speech IDs, not one unified policy file. Startup runs off the event loop,
  holds the store writer lock, and provisions profile components sequentially.
- `store.py`: automatic model downloads and staged publication exist, but there
  is no automatic disk-space preflight or eviction.
- `visual.py`: uses a separate experimental, cached-only deployment configuration
  with fixed workers. Text is unloaded during visual work and reloaded lazily.
  This is useful scheduling behavior, not a generic backend-selection system.
- `runtimes.py`: pinned native deployment metadata can be reused rather than
  introducing a second runtime installer.

## 1. One startup configuration contract

Introduce a strictly validated `OrchestratorConfig`, shared by service and
in-process construction. The constructor primarily takes a config path; support
an already-validated object for tests/embedding, not another parallel set of
model-selection keyword arguments.

Recommended file location: `%LOCALAPPDATA%/EchoCradle/orchestrator.json`, respecting
the existing application-home override through `paths.py`. Resolve config path
from explicit argument, then a documented environment override, then this path.

Recommended fields:

| Field | Meaning/default |
|---|---|
| `schema_version` | Current schema; reject unsupported versions, no migration layer |
| `resources.game_vram_reserve_gib` | 5.0 |
| `resources.vram_safety_margin_gib` | Separate explicit fragmentation/estimation headroom; calibrate from measurements |
| `resources.game_ram_reserve_gib` | Configurable system-memory headroom |
| `resources.device` | Auto or one identified GPU; do not pool unrelated GPUs |
| `resources.residency` | `resident` by default; optional explicit `staged` policy |
| `pipelines` | Capability -> `auto`, exact pipeline ID, or `disabled` |
| `provisioning.startup_prefetch` | True: enqueue all selected assets asynchronously |
| `provisioning.priority` | Agentic, TTS, STT, image, image-to-3D, animation, remaining capabilities |
| `storage.min_free_gib` | Free-space floor after installation; configurable |
| `storage.evict_unselected` | True; only orchestrator-owned cache assets |

Rules:

- Missing file, blank/whitespace file, and `{}` mean automatic defaults. A supplied
  but missing explicit path should instead fail as a likely typo.
- Missing capability entries mean `auto`; `disabled` must be explicit.
- All shipped/enabled capabilities require one selection. Do not invent pipelines
  for unimplemented capabilities; report unsupported/no-eligible-pipeline clearly.
- Explicit selections are pins, not preferences. Reject ineligible/over-budget
  pins with actionable diagnostics; never silently substitute another model.
- Reject unknown fields/IDs, wrong capability assignments, negative/non-finite
  costs, incompatible settings, and malformed JSON before downloads start.
- Remove superseded model-selection flags/paths once entrypoints use this contract;
  do not retain compatibility shims. Deployment details belong in catalog/adapter
  metadata, not player-specific experiment paths.
- Do not overwrite user input with generated selections. Atomically save a separate
  `orchestrator.resolved.json` for inspection, containing hardware, catalog revision,
  selections, estimates, and reasons. Re-resolve auto entries on next startup;
  deliberate persistent pinning uses an exported config.

## 2. Select pipelines, not just checkpoints

Extend the catalog to describe capability pipelines: backend adapter, model IDs,
exact revisions/quantization, runtime deployment, auxiliary dependencies, supported
devices, quality rank, and resource profiles. Keep installation assets separate
from pipeline identity so shared dependencies are downloaded/accounted for once.

Initial capability keys: `agentic`, `tts`, `stt`, `image`, `image_to_3d`, `animation`.
Map agentic calls to the selected tool-capable text pipeline. Add other text/audio
roles only when implemented. STT includes recognizer/endpointing dependencies;
animation may be a rigging/retargeting algorithm plus clips rather than a neural
model. Model-free CPU pipelines can legitimately have zero VRAM cost.

Every pipeline has a floating-point **peak VRAM cost in GiB** as requested. Also
record resident VRAM, peak RAM, artifact sizes, stage peaks, and relevant workload
limits (LLM context/KV cache, image size/batch, mesh resolution). Estimate the full
pipeline, not merely checkpoint file size. Unknown cost is not zero.

Hard eligibility gate before selection/provisioning: the exact complete stack must
be EU/commercially suitable and anonymously downloadable without accounts, tokens,
manual license acceptance, or publisher approval. Check auxiliary encoders,
background removal, VAD, native libraries, and motion assets as well as primary
weights. A `shippable` boolean or HTTP 200 for one checkpoint is not sufficient.
Unverified pipelines remain ineligible for product selection; configuration cannot
bypass this requirement. Completing visual eligibility and automatic provisioning
is a real implementation dependency, not already solved by current workers.

## 3. Resolve a resource-feasible portfolio

Selection is a constrained quality optimization, not “smallest model wins.”
VRAM cost determines feasibility; a quality/preference rank determines which
feasible portfolio is best. Define capability weights and deterministic tie-breaks
explicitly, so one large agentic choice cannot greedily starve all other roles.

1. Probe supported devices, sampled free VRAM, available RAM, and the actual store
   volume. Filter candidates by complete-stack eligibility, adapter availability,
   device support, and workload requirements.
2. Apply pins first. Construct feasible combinations for remaining enabled roles
   using bounded enumeration/branch-and-bound for the small curated catalog.
3. Enforce VRAM/RAM constraints, deduplicate actual shared resident assets, and
   maximize weighted quality. Break ties deterministically by lower resource cost
   then stable pipeline ID. Disk/cache state must not override hardware/quality
   selection; eviction addresses disk pressure instead.
4. Record every selection/rejection reason and final resource accounting. Fail
   resolution before provisioning if no complete feasible combination exists.

### Residency decision — important

**Default recommendation: `resident`.** This matches the requested interpretation
that one selected model per section fits alongside the game. Conservatively require
the sum of selected peak costs to fit the AI budget, or use verified resident plus
simultaneous-workspace accounting where available. Do not select a portfolio that
fits only by secretly unloading other sections.

**Optional `staged` policy:** allow agentic and visual workloads to swap, keeping
latency-critical speech resident where possible. Account for each permitted
concurrency group and pipeline stage; the maximum scheduled peak, not the sum of
all installed pipelines, must fit. This enables stronger visual models but causes
load delays and pauses in shared-device inference. Make that tradeoff explicit.

Startup selection alone cannot enforce safety: runtime admission must use the same
residency/concurrency contract, with leases for loaded assets and exclusive GPU
stages where needed. Recheck memory before loading; queue/unload/retry safely on
pressure, and return a structured resource error rather than silently reselecting.

### Reserve accounting

If probing before the game allocates memory, subtract the 5 GiB game reservation
and safety margin from free GPU memory. If the game is already running, subtract
only the unconsumed part of that reservation when reliable attribution exists;
otherwise use a conservative documented policy. Do not unintentionally subtract
the full game reserve twice. Startup is still an estimate, not a GPU reservation.

Windows shared GPU memory is spillover into system RAM, **not** extra equivalent
dedicated VRAM. Independently attached GPUs are not one additive VRAM pool. CPU
fallbacks must meet available-RAM and latency requirements, not merely report zero
VRAM. No requirement to enable every capability on hardware that cannot support it;
report the limitation and require explicit disabling or another residency policy.

## 4. Provision asynchronously, in likely-use order

Resolve and validate the complete portfolio first, then start a single prioritized
provisioning queue. Downloading is distinct from loading weights into VRAM.

Default roots: **agentic -> TTS -> STT -> image -> image-to-3D -> animation -> rest**.
Schedule shared/runtime/auxiliary dependencies before their parent pipeline, with
their parent's priority. A requested capability promotes its missing dependencies
ahead of speculative prefetch. Deduplicate concurrent requests for the same asset;
default to one download/install at a time to limit staging space and contention.

This reconciles “download on demand” and “download at startup”: startup performs
background prefetch; demand uses that same queue with higher priority. Setting
startup prefetch false makes it strictly demand-driven. A blocking full-install
startup mode is not the recommended default.

Expose separate service-live, configuration-resolved, and per-capability states:
`queued`, `downloading`, `installing`, `ready`, `failed`. Here `ready` means all
dependencies are installed/verified; report loaded/warm status separately.
The game may begin once its required capabilities are ready; optional visual work
must not delay text/speech readiness. Surface bytes/progress, retryable errors,
cancellation, and the capability blocking a request. Keep all I/O off Unity and
service event-loop threads. Centralize ownership/cleanup across both entrypoints.

Use pinned manifests and verified hashes where published/provided; never fabricate
hashes. Validate sizes and required files, stage on the destination volume, then
publish atomically. Retain useful partials for resumable downloads only when safe
and bounded; clean abandoned staging on recovery. Pin runtime/environment assets
too: model weights alone do not make a backend usable.

## 5. Evict only safe, unselected cache assets

Maintain a store manifest with asset ownership, actual bytes, last use,
complete/partial status, dependency references, and active download/job/load leases.
The protected set is the transitive closure of current selections plus active
leases and explicitly pinned assets. Old resolved-report files do not protect
assets indefinitely; live processes/configurations do.

Before each installation calculate additional download bytes, retained archive,
extracted bytes, temporary conversion/build space, and the free-space floor on
each relevant volume. Account for existing resumable data and already-cached
dependencies. Reserve capacity under the store lock so installers cannot race.

If short on space, remove stale abandoned staging, then evict unselected,
unleased, orchestrator-owned assets in LRU order until sufficient space exists.
Shared dependencies are removable only with no protected references. Recheck
actual free space after deletions and handle concurrent external disk use and
failed/locked-file deletion. Never delete arbitrary external model directories,
experiment results, user data, generated game assets, or files outside the store.
Do not follow symlinks/junctions out of managed directories.

If the selected portfolio plus installation overhead cannot fit even after every
eligible eviction, stop with `INSUFFICIENT_DISK_SPACE`, required/free/reclaimable
bytes, and affected capability. Do not loop, delete selected models, or silently
downgrade quality. Full startup prefetch necessarily requires disk capacity for
the whole selection. Dropping the old “keep one old model per category” design
rule is intentional: current selections/dependencies, not historical categories,
define protection under this proposal.

## 6. Small independent configuration GUI

### Scope and layout

Ship a lightweight standalone desktop configuration utility, not a Unity scene,
model-management dashboard, or bundled browser/Electron application. Prefer a
minimal native toolkit available with the chosen distribution (for example,
Tkinter if Tcl/Tk is included); confirm packaging availability before choosing.
It must open before any model is installed and must not start inference hosts.

- **System summary:** selected GPU/device, total dedicated VRAM, sampled free
  VRAM, game reserve (default 5 GiB), safety margin, and resulting AI budget.
  Distinguish hardware capacity, currently free memory, and estimated consumption.
  Provide hardware refresh through the orchestrator; do not silently change pins.
- **Capability rows:** selection menus for agentic/text, TTS, STT, image,
  image-to-3D, animation, and other capabilities advertised by the orchestrator.
  Labels may say “3D” and “Speech recognition (STT)” while retaining canonical
  capability keys in configuration.
- **Choice labels:** backend/model name, actual size/quantization variant, and
  estimated peak VRAM in GiB; identify CPU-only choices. Show only real catalog
  alternatives—one available option is expected for many capabilities initially.
  Include `Auto` and explicit `Disabled` where the capability contract permits.
  Unsupported/no-eligible-choice rows explain why; do not invent size tiers or
  treat an unavailable pipeline as implemented. Catalog-provided exclusion reasons
  can be shown as disabled choices or contextual details.
- **Budget preview:** update after each selection or resource-setting change.
  Display estimated selected AI usage versus available AI budget, plus game reserve
  and safety headroom. Include per-capability costs and remaining/over-budget
  amount. In staged mode, clearly label scheduled peak versus summed selection
  costs; all shared-dependency/concurrency arithmetic comes from the orchestrator.
  Unknown costs appear as unknown, never zero; estimates are not live allocations.
- **Actions:** `Load configuration`, `Auto configure`, and `Save configuration`.
  Display the target path, unsaved-change state, validation errors, and a clear
  success/failure result. Expose game reserve and residency in a compact settings
  area; preserve other valid settings when editing or saving.

### Orchestrator-owned configuration operations

Add a configuration-only facade to ModelOrchestrator, backed by the same code used
at runtime. It must be callable without constructing GPU hosts, acquiring a
long-lived inference-store writer lock, downloading assets, or loading models.
The standalone GUI can call this local facade directly; a running inference
service must not be required. Any service exposure is an adapter over the same
operations, not a second implementation.

Proposed operations (names are illustrative):

1. `describe_configuration_options`: return capabilities, pipeline IDs, labels,
   variants, eligibility reasons, costs/workload limits, and config schema/defaults.
2. `probe_configuration_hardware`: return per-device memory and the budget inputs
   used by the planner, including probe timestamp and accounting assumptions.
3. `load_configuration(path)`: parse/validate user input without provisioning;
   retain missing/auto semantics and report unresolved obsolete/unknown IDs.
4. `preview_configuration(draft, hardware_snapshot)`: validate pins, resolve auto
   entries, and return selections, per-role and portfolio resource accounting,
   warnings/errors, and feasibility. An invalid draft remains editable.
5. `auto_configure(draft, hardware_snapshot)`: explicitly recompute all enabled
   capability selections, replacing manual model pins in the draft while preserving
   disabled capabilities, resource policy, and unrelated settings. Return concrete
   selected IDs and reasons; the UI makes this replacement behavior clear.
6. `save_configuration(path, draft)`: revalidate and atomically write the canonical
   startup config. Save concrete selections from Auto configure as pins, or retain
   `Auto` for rows the user deliberately leaves automatic. Preserve the separate
   resolved-report contract; saving never starts provisioning or applies a live
   runtime configuration change.

Use structured responses, not UI parsing of log messages. Catalog/hardware revision
tokens identify the preview inputs; refresh/revalidate before save if they change
and show any changed result rather than silently replacing a user's choices.
Syntactic/eligibility errors or an infeasible complete selection block normal save,
with actionable diagnostics. A missing installed checkpoint alone is not an error:
valid uncached selections are saved for subsequent automatic provisioning.

### Interaction and lifecycle

- On opening, load the default or chosen file through the facade, obtain options
  and hardware, and preview the selection. An empty config previews automatic
  choices without modifying the file until Save is pressed.
- Manual changes only update an in-memory draft and its preview. Auto configure
  also updates only that draft; neither action writes files, downloads, or evicts.
- Keep probing/planning/file I/O off the GUI event thread. Ignore outdated preview
  responses if the draft has changed; show pending state and keep controls usable.
- Save uses the orchestrator's validated atomic writer; surface permission/path
  errors and detect an externally changed target before overwriting it. Prompt
  before discarding unsaved changes on Load or exit.
- If an orchestrator/game is already running, show that saved changes apply on its
  next startup. Never hot-swap models, stop the game, or trigger startup prefetch
  merely because a user opened or saved in the utility.

## 7. Implementation sequence

1. **Config and catalog contracts:** add validated startup config, capability and
   pipeline descriptors, dependency manifests, and one shared resolution path.
   Inventory existing product-eligible backends; explicitly list unresolved ones.
2. **Resource planner:** update probing/reserves; implement deterministic joint
   selection, pins, residency groups, workload limits, and resolved diagnostics.
3. **Safe storage:** add manifest/leases, disk preflight, transactional capacity
   reservations, dependency-aware LRU eviction, recovery, and deletion boundaries.
4. **Provisioning coordinator:** prioritized deduplicated queue, demand promotion,
   per-capability readiness, cancellation/retry, and staged verified publication.
5. **Adapters and integration:** route all requests through startup selections;
   bring visual adapters/dependencies into the same catalog/provisioning contract.
   Replace existing profile/model-selection configuration instead of retaining
   parallel legacy paths. Preserve cached-only worker execution after provisioning.
6. **Admission and clients:** enforce runtime residency, update service status and
   owned-client startup configuration, expose progress to Unity without blocking.
7. **Configuration facade and GUI:** expose side-effect-free options, probe,
  load/preview/auto-configure and explicit atomic save operations. Build the small
  standalone utility as a thin client; verify packaging without Unity or models.
8. **Validation/documentation:** examples, failure diagnostics, mocked integration
   tests, then opt-in hardware verification using already cached eligible assets.
   No large downloads are needed to validate the configuration/planner/store work.

## 8. Tests and acceptance criteria

- Extend `test_catalog.py`, `test_orchestrator_defaults.py`, and new config tests:
  empty/missing/partial files, precedence, pins, disabled roles, invalid/non-finite
  values, unknown fields, complete-stack eligibility, and entrypoint parity.
- Extend `test_probe.py` / `test_planner.py`: 5 GiB default and overrides, CPU-only,
  low-VRAM/no-solution machines, multi-GPU non-pooling, workload limits, shared
  dependencies, joint rather than greedy selection, deterministic ties, and
  resident versus staged feasibility. Include game-already-running accounting.
- Extend `test_store.py`: fake disk capacities, staging overhead, LRU order,
  selected/shared/active protections, stale partial recovery, locked files,
  interrupted installs, out-of-store paths, and competing requests/processes.
- Extend service provisioning/lifecycle tests: no download before complete
  validation, exact priority/dependency order, request promotion, deduplication,
  partial readiness, cancellation, restart recovery, and store lock ownership.
- Extend visual/service/client contracts: requests use only resolved pipelines;
  status identifies pending/failed capability; no event-loop blocking; resource
  admission actually enforces the advertised plan.
- Add configuration-facade and GUI tests: options mirror the catalog, single-choice
  and unsupported rows, manual budget updates, CPU/unknown-cost display, auto
  replacement semantics, disabled-role preservation, invalid/over-budget saves,
  load/save round trips, external file conflicts, stale preview rejection, and
  responsive error handling. Assert opening, previewing, auto-configuring, and
  saving never download/evict assets, start inference hosts, or alter a live plan.
- New automated tests use fake probes, local fixtures, and fake downloaders; no
  network/GPU/model downloads. Hardware tests are separate and opt-in.

Acceptance: `{}` resolves reproducibly on fixed hardware/catalog; every enabled
role has exactly one eligible selection or an explicit resolution error; resident
costs fit the configured game reserve; provisioning follows priority and promotes
demand; only unselected/unleased owned assets can be evicted; disk exhaustion
terminates clearly; configuration changes take effect on restart, not mid-request.

GUI acceptance: without Unity, a running inference service, or installed weights,
the utility displays system VRAM and catalog-provided choices, loads a config,
previews manual/automatic selections against the same budget as runtime, and saves
a valid configuration usable unchanged at the next ModelOrchestrator startup.
No selection, budget, validation, or persistence rules are duplicated in the GUI.

## Decisions to confirm before implementation

1. Use **resident** fit by default, with explicit staged swapping as an option?
   This is the largest quality/latency tradeoff and differs from current visuals.
2. Accept **background startup prefetch**, with game readiness based on required
   capabilities rather than waiting for every visual dependency?
3. Which capabilities are required for game startup versus merely enabled for
   later use? What are the minimum acceptable latency/quality/workload limits?
4. What RAM reserve, GPU safety margin, and disk free-space floor should ship?
   The requested game VRAM reserve is already defined as 5 GiB.
5. On unsupported hardware, fail required capability resolution or permit a
   deliberately configured reduced-capability game mode? Never silently disable.

The principal missing inputs are trustworthy full-pipeline resource profiles,
quality ranks and acceptable latency limits, and verified distributable complete
visual stacks. A single float per checkpoint cannot supply these by itself.