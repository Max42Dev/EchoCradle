# Visual generation API

Status: initial implementation, 2026-10-24. Experimental cached-only extraction of
`experiments/three-d-generation/0110-text-to-animated-character`.

## Contract

Use the existing asynchronous `POST /v1/jobs`, polling and cancellation routes.
Stages are independent; callers await success and pass the returned artifact ID
to the next stage. No implicit dependency graph or automatic visual-stack downloads.
Text/voice startup separately provisions missing selected text/speech weights and
llama.cpp; its provisioning capability flag does not cover these visual stages.

| Job kind | Input | Output |
| --- | --- | --- |
| `image` | `text`, required `seed`, optional `negative_prompt` | PNG |
| `image_to_3d` | `input_artifact_id` (PNG), required `seed` | static GLB |
| `animate_3d` | `input_artifact_id` (static GLB), required `seed` | rigged, textured GLB with the experiment's five animation clips |

Visual jobs accept `deadline_ms` (1,000–3,600,000; default 600,000) and
`idempotency_key`. They reject text-chat fields, sessions and socket streaming.
Resolution, model paths, worker programs and motion presets are server-owned in
this first slice. Animation means auto-rigging plus motion generation/retargeting,
not merely adding a canned animation to arbitrary geometry. Only the experiment's
humanoid reconstruction output is supported initially.

The result contains `artifact_id`, `media_type`, `byte_length`, `sha256`, `seed`
and `experimental: true`. Artifacts are reusable across stages. Upload PNG/GLB
through `/v1/artifacts` using their media type; existing PCM upload behavior is
unchanged. Download returns matching content type and digest. Clients verify
length and digest before exposing bytes. Visual payloads are bounded to 32 MiB
each and share the existing aggregate artifact quota and expiry policy.

## Execution and ownership

The service opts in through server-owned visual configuration. Missing workers
or weights report unavailable capabilities without preventing text/voice startup.
Use trusted configured worker paths, offline environment variables, argument lists
and isolated temporary output directories. Never accept paths or commands from a
job. Validate PNG/GLB container structure before running native importers. Generated
GLBs must be self-contained (no external file/network references).

One visual lane serializes all three stages. Serialize visual inference against
text inference and unload the resident text host before GPU workers run; lazily
reload text for the next text request. CPU speech remains independent. Cancellation
terminates owned worker process trees; native completion still owns capacity and
inputs until cleanup finishes. Do not publish outputs from cancelled/deadline jobs.
Inputs are copied into job-owned bounded storage at admission so expiry/deletion
cannot invalidate queued/running work. Publication occurs only after validation.

## Public Python surface

`ModelOrchestrator.generate_image`, `generate_3d` and `animate_3d` expose the same
stages in-process with path inputs/outputs and cancellation callbacks. A configured
backend executes the proven experiment workers without importing GPU dependencies
into the service interpreter. Python service-client helpers submit/poll/download
the corresponding stages and return typed immutable artifact results.

## Eligibility and initial limits

No automatic visual-stack provisioning is implemented.
The installed 0110 stack has completed unattended runs, but the full transitive
license/anonymous-download audit is not complete (notably MoMask rights and
SkinTokens dependencies). Reject visual work in `shippable_only` mode. Do not mark
these models commercially cleared, substitute gated DINOv3/TRELLIS.2, or use the
EU-blocked Hunyuan3D-2 route. These constraints do not prevent cached experimental
execution explicitly configured by a developer.

The initial backend depends on the existing experiment's pinned configuration
and workers; extracting portable workers/provisioning into the package is follow-up.
Seeded execution records provenance but is not a claim of bitwise GPU determinism.
Quality, animation loops and Unity playback are not certified by structural tests.

### Automatic provisioning review (2026-10-24; research only)

The new service provisioning path uses `ModelStore.ensure` and `TextHost` for
selected text/speech/runtime assets, not the visual scripts below. The service's
global `provisioning=true` flag is not evidence that **all** missing models are
handled. Client-side Silero VAD and visual worker runtimes also remain separate.
No models were downloaded or installed for this review; anonymous checks used
HEAD requests following redirects, without authorization headers or HF tokens.

| Component / existing script | Concrete automatic path and remaining gate |
| --- | --- |
| DreamShaper 8 / `provision_stylized.py` | Keep HF revision `a7e52b98680b1ba8ff7bce97c7f9f2e2e5337917`, with only the worker's FP16 UNet, VAE, text encoder and exact configs/tokenizer files. All three weight HEADs returned 200; HF reports ungated. [Model card](https://huggingface.co/Lykon/dreamshaper-8/blob/a7e52b98680b1ba8ff7bce97c7f9f2e2e5337917/README.md) declares CreativeML OpenRAIL-M, not MIT: worldwide commercial use is conditional on use restrictions and downstream license/notices. |
| TripoSR / `provision_baseline.py` | Replace dynamically resolved latest revision with `5b521936b01fbe1890f6f9baed0254ab6351c04a`; fetch only `config.yaml` and `model.ckpt` (both anonymous HEAD 200). [Weights](https://huggingface.co/stabilityai/TripoSR/blob/5b521936b01fbe1890f6f9baed0254ab6351c04a/README.md) and pinned code are MIT. Add `facebook/dino-vitb16` **config.json only**, revision `f205d5d8e640a89a2b8ef0369670dfc37cc07fc2` (Apache-2.0, ungated, HEAD 200) in the expected HF cache layout; TripoSR constructs the architecture and supplies its own weights. No DINOv3 download. Do not provision the script's unused SD1.5 baseline. |
| U2netp / no dedicated script | Fetch `https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx` into `foreground_model/u2netp.onnx` and set `U2NET_HOME`; anonymous HEAD 200. Installed rembg expects MD5 `8e83ca70e441ab06c318d82300c84806`. Preserve [U-2-Net Apache-2.0](https://github.com/xuebinqin/U-2-Net/blob/master/LICENSE) and [rembg MIT](https://github.com/danielgatis/rembg/blob/main/LICENSE.txt) notices; record the converted artifact's provenance rather than equating rembg's code license with all model licenses. |
| SkinTokens / `provision_rig.py` | Both exact checkpoints at `79736cad0fd84de384d5eede659b4ebd24effe33` returned anonymous HEAD 200; [weight card](https://huggingface.co/VAST-AI/SkinTokens/blob/79736cad0fd84de384d5eede659b4ebd24effe33/README.md) and pinned source declare MIT. Qwen3-0.6B config at `c1899de289a04d12100db370d81485cdf75e47ca` is Apache-2.0/ungated/HEAD 200; no Qwen weights needed for the observed architecture-loading path. Provision both rig checkpoints and materialize the decoder checkpoint and Qwen config at the checkout-relative paths actually read by the worker. **Shipping gate:** vendored Michelangelo origin/license audit (upstream is GPL-3.0), compiled attention dependency packaging and full dependency notices; top-level MIT does not clear copied code. |
| MoMask / `provision_motion.py` | Required HumanML3D package: Drive ID `1vXS7SHJBgWPt59wupQ5UUzhFObrnGkQ0`; three selected checkpoint/option sets plus RVQ mean/std, pinned code/BVH template, and CLIP ViT-B/32. CLIP's existing SHA-256 URL returned anonymous HEAD 200; [CLIP license](https://github.com/openai/CLIP/blob/main/LICENSE) is MIT. Drive HEAD returned **200 text/html**, not a verified anonymous ZIP: confirmation/quota behavior remains unresolved. [MoMask license section](https://github.com/EricGuo5513/momask-codes/blob/94a6636c9c463b7a9414c3401a6f1b67e6c51824/README.md#license) explicitly licenses code and warns about separate library/dataset terms; the weight grant, HumanML3D/AMASS-related rights and BVH/template provenance are not cleared. Do not enable automatic product provisioning until these gates are resolved. No training datasets, evaluators, GloVe or SMPL body weights are needed by the intended joint/BVH-only path. |

Implementation proposal, **not implemented**: one trusted pinned visual manifest
using `model_store_dir()` (honoring `ECHOCRADLE_HOME`), explicit anonymous HF
requests (`token=False` if using the HF client), allowlisted files and reviewed
license records. Before stage readiness, automatically fetch **every missing
required dependency** off the event loop under the store writer lock; publish
only complete verified staging directories. Verify available publisher digests,
reject HTML/login responses and unsafe archive members, bound archive expansion,
and clean partials on failure. The existing scripts instead write directly to
machine-specific final paths; MoMask treats any existing `t2m` directory as complete.
Do not invoke these scripts unchanged as a provisioning solution. Package pinned,
license-reviewed worker environments/checkouts and Blender/BVH support too;
existing broad/unpinned requirements are not a reproducible end-user deployment.
Inference workers can remain network-isolated **after** automatic preparation;
this is not an offline-only product mode or a manual-cache workaround.

## Python usage

The existing experiment's `automatic.local.json` configures the cached worker
interpreter, Blender, checkouts and weights. The `src` directory must be beside
that config. Do not copy the machine-local paths into shipping configuration.
The normal text profile automatically provisions missing selected text weights and
runtime at startup; Unity's `--text-only` uses this profile without speech/audio.
Use service `--profile visual` for visual-only execution without text/speech loading;
this still requires the cached visual deployment and is not automatic provisioning.
Visual readiness is inspected off the event loop at startup and cached until
restart; each stage rechecks its own deployment before executing.

```python
import asyncio
from pathlib import Path

from orchestrator.client import ServiceClient


async def create_character() -> None:
	config = Path(
		"experiments/three-d-generation/0110-text-to-animated-character/automatic.local.json"
	).resolve()
	# Startup and close are blocking; run this coordinator off a UI thread.
	with ServiceClient.start_owned(profile="text", visual_config=config) as client:
		image = await client.generate_image(
			"A full-body fantasy courier in a neutral pose, plain background", seed=42,
			negative_prompt="cropped, text, watermark",
		)
		mesh = await client.generate_3d(image, seed=42)
		animated = await client.animate_3d(mesh, seed=42, deadline_ms=1_800_000)
		Path("SK_courier.glb").write_bytes(animated.payload)


asyncio.run(create_character())
```

Passing a `VisualResult` to the next helper reuses its server artifact ID without
uploading another copy. Alternatively pass PNG/GLB `bytes`; the helper uploads and
deletes its temporary upload after confirmed admission. Results are immutable and
contain verified bytes in `payload`. Retained IDs belong to that service instance
and expire after the service TTL (600 seconds); keep calls sequential or upload
the saved bytes again after expiry. Explicit cancellation of an awaiting helper
cancels the job; temporary native files are removed after the worker tree exits.

For a separately launched service, configure `--visual-config` at service startup,
then use `ServiceClient.attach(...)`. Attached clients cannot change deployment
policy. A REST image request is:

```json
{"kind":"image","text":"A full-body fantasy courier, plain background","seed":42,"deadline_ms":600000}
```

Poll the returned job ID until terminal. Submit `image_to_3d` with its successful
result's `artifact_id` as `input_artifact_id`; then submit `animate_3d` with the mesh
artifact ID. GET the final artifact using the existing bearer-authenticated route.
No paths, arbitrary scripts or model selections are accepted in these jobs.

The server worker budget is `timeout_seconds` in the experiment config (default
600 seconds for an entire API stage, maximum 1,800). Increasing `deadline_ms` does
not override that budget. The five-clip animation stage includes all rigging,
texture baking, motion generation, retargeting and independent playback checks.
PNG input and embedded PNG textures are bounded/validated. Embedded JPEG textures
are not accepted in this slice. Artifacts and job-owned input copies share the
64 MiB aggregate budget; release retained artifacts when no longer needed.

## Verification

Offline tests cover strict requests, wrong artifact types, malformed containers,
reusable handoffs, idempotency, cancellation, output integrity, missing deployment,
shippable rejection and worker argument construction. Existing text/voice suites
must remain green. A real GPU end-to-end run is a separate explicit smoke check;
mocked tests must never be reported as generated-model quality validation.