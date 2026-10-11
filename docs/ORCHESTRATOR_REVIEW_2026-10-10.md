# Model orchestrator review

Reviewed: 2026-10-10. Scope: Python host facade, service, wire protocol, Python
client, public exports, documentation, and offline tests. No implementation
changes, model downloads, or live inference were performed. The Unity client was
not independently audited in this review.

## Implementation follow-up — 2026-10-10

The findings, line references, reproductions and **277-test review baseline below
are historical evidence**, not a description of the subsequently changed source.
Current contracts are documented in [SERVICE_USAGE.md](SERVICE_USAGE.md) and
[the package README](../orchestrator/README.md).

- Correctness 1–2: socket cleanup detaches immediately with a bounded close;
  lanes/reaper continue. Failed startup rolls back only after native loading ends;
  unload failure deliberately retains ownership. Offline lifecycle regressions
  cover these boundaries, including cancellation while native work is pending.
- Correctness 3–5: complete REST/event success representations are byte-bounded;
  generated JSON uses value decoding (array/scalar roots supported), while request
  envelopes remain objects. Numeric range checks precede float conversion.
- Correctness 6: verified TTS survives failed deletion; Python cleans unconsumed
  STT on definite validated 4xx, `503 NOT_READY` or local admission rejection,
  matching Unity. Arbitrary 5xx, malformed errors and transport failures remain
  ambiguous and leave ownership to TTL.
- Interface A/F: application entry is `orchestrator.client.ServiceClient` with
  explicit `start_owned`/`attach`; package imports do not force client extras.
  Removed speculative task exports and physically deleted unused `tasks.py`
  without a shim. Repository-wide import audit found no remaining executable
  consumers. Package docs now describe the synchronous host API, not an
  unimplemented scheduler.
- Interface B/C: immutable `TextResult`/`ToolOutcome`, separate model/dispatch IDs,
  final server attempts and local execution evidence; Event cancellation and
  caller-thread callback semantics. Total operation budgets and bounded transport
  waits are documented; blocked synchronous user code still cannot be interrupted
  and can overrun the budget. Cancellation never rolls back side effects.
  Final integration fixes accept delivered rejection results and independent
  argument/result byte budgets; failed dispatch emission remains unknown, not
  falsely rejected with a dispatch ID. Model delivery is recorded only after a response
  to a continuation containing the result, not registry receipt or host return.
  A cancellation snapshot taken while native work is pending can lack attempts;
  Python merges local call evidence and Unity retains local dispatch IDs separately.
- Interface D/E: canonical bounded submit/get/cancel/event snapshots, sanitized
  errors and validated correlation/retryability metadata; typed messages,
  capabilities and registry protocol. Removed `dialogue`/`delivery_mode`, rejected
  schema-plus-tools, and documented arbitrary validated client tools.
- Unity consumers now use typed canonical jobs/tool outcomes and tools-only
  sessions. Current offline EditMode transport/domain tests pass (see
  [Unity validation](../Unity/README.md#validation)); experiment consumers have
  been adapted separately from direct-host diagnostics.

Final integration validation: **369 orchestrator tests passed; 122 interview
tests passed, 1 live-model test deselected** (Python 3.13.15). `git diff --check`
passed. No C# was changed by the final audit, so Unity was not rerun; the preceding
53-pass EditMode result remains separate evidence.

Validation for the follow-up is offline: fake hosts plus loopback HTTP/WebSocket
boundary tests, not real model/native reliability, GPU, microphone, audible
playback or a new player-build acceptance run. Earlier real-model/audio evidence
is preserved in the usage/Unity documents and is not a rerun of these changes.
The historical review baseline below is unchanged. No downloads or provisioning
were performed here.

## Verdict

**A solid local-service foundation, but not yet a polished public interface.**
The next investment should be contract clarity and failure-boundary reliability,
not additional modalities or a general-purpose scheduling framework.

Good foundations worth preserving:

- REST owns submission, polling, cancellation and complete artifacts; WebSocket
  owns incremental events and remote tool round trips.
- Cached-only service startup is distinct from provisioning.
- Input and model output are treated as untrusted, with strict JSON envelopes,
  schema validation, identity checks, quotas and output limits.
- Tools execute in the client, not as arbitrary code on the server.
- Native work stays accounted for after logical cancellation through
  `host_pending`; cancellation does not falsely free host capacity.
- Catalog preferences and runtime deployments are data-driven and configurable.

## Prioritized correctness findings

### 1. High — Socket cleanup can block unrelated work

`orchestrator/orchestrator/service.py:678–689` bounds event enqueueing but awaits
socket closure without a timeout after enqueue failure. Worker lanes and the
reaper await this notification path (`:851–855`, `:874–893`).

A stalled close can prevent the lane from processing unrelated jobs and prevent
housekeeping from enforcing subsequent deadlines. In a fake-socket reproduction,
the first job was cancelled with `host_pending=True` before inference began;
the next sessionless job remained queued until close was released.

**Fix:** bound close and keep connection cleanup out of critical scheduling and
housekeeping paths. Test that unrelated jobs and deadline processing continue.

### 2. Medium — Failed startup does not roll back partial ownership

`service.py:436–506` acquires the store lock and loads hosts sequentially. On
failure it records a failed state without stopping loaded hosts or releasing the
lock; shutdown eventually performs cleanup (`:926–938`).

A standalone service whose text host loaded before a later speech failure can
remain alive holding model resources and exclusive store ownership. A fake
startup failure confirmed no immediate stop or lock-release calls; native
resource retention was not exercised.

**Fix:** roll back completed startup work after the loading operation ends,
without racing outstanding native work. Preserve sanitized diagnostics.

### 3. Medium — A successful STT result can exceed the wire limit

`service.py:836–837` bounds STT output by characters, not serialized bytes.
REST snapshot serialization bypasses bounded `encode()` (`:1029–1035`).

With fake recognition output containing 16,384 supplementary Unicode characters,
the job succeeded and GET returned 65,823 bytes, exceeding the 65,536-byte JSON
limit. The Python client then rejects the successful result; socket publication
can also fail.

**Fix:** validate the complete external representation before marking success,
including envelope overhead, consistently across REST and WebSocket.

### 4. Medium — Schema admission and output decoding disagree

`orchestrator/orchestrator/service_protocol.py:187–204` accepts array/scalar
response schemas, but `service.py:787–789` uses object-only `decode()`.
Valid output such as `["valid"]` fails before schema validation.

**Fix:** support bounded JSON-value decoding for generated output, or explicitly
restrict admitted schemas to object roots. Keep request envelopes object-only.

### 5. Medium — Numeric validation escapes the public error contract

`service_protocol.py:179–182` calls `math.isfinite()` before range checking.
A 401-digit integer temperature raises `OverflowError`. An authenticated HTTP
reproduction returned plain HTTP 500 rather than a structured invalid request.

**Fix:** range-check before conversion or translate overflow into validation.

### 6. Medium — Artifact cleanup changes inference outcomes

`orchestrator/orchestrator/client.py:620–626` leaves STT uploads behind after
definite admission rejection. Busy rejection precedes consumption in
`service.py:605–628`. Conversely, `client.py:595–605` requires deletion of an
already downloaded, verified TTS artifact before returning usable audio.

**Fix:** clean up uploads after definite rejection; distinguish ambiguous
submission failures. Make cleanup nonfatal after verified inference success.
These paths were inspected, not fault-injected.

## Interface findings

### A. Establish one honest public entry point

`orchestrator/orchestrator/__init__.py:1–15` describes a nonblocking scheduler and
names `ModelOrchestrator` as the public entry point. That facade is synchronous;
the application-facing remote interface is `ServiceClient`, which is not
exported there. Its dependencies are optional, so an unconditional client import
would also need care.

The package exports `ContentRequest`, `Priority`, `TaskStatus` and `Artifact`,
but no implemented submission API consumes these request/status types. Their
states (`done`, `loading-model`, etc.) differ from service job states. Within the
orchestrator package, request/status/priority usage is confined to definitions
and public exports. `tasks.py` describes submission and dependency scheduling
that are not implemented.

**Recommendation:** make `orchestrator.client.ServiceClient` the documented
application entry point, retain `ModelOrchestrator` as an explicitly low-level
host API, and remove speculative exports/types rather than preserving shims.
Do not implement a scheduler merely to justify unused types.

### B. Make completion and tool outcomes explicit

`client.py:1011–1017` returns ordinary partial text/calls on cancellation;
`chat_with_tools()` uses the same path (`:492–499`). Non-streaming polling instead
raises on nonsuccess (`:438–442`). Callers cannot reliably distinguish a cancelled
turn from a successful short or empty answer.

The same return path exposes the locally dispatched call list rather than the
authoritative final job list. Locally recorded IDs are dispatch UUIDs
(`:952–955`), while final host call IDs come from the model. Server-rejected tool
attempts never dispatch to the client and disappear from its returned list
(`service.py:366–382`, `:807–809`).

**Recommendation:** use a typed result with terminal state, partial/final text,
job/turn identity and explicit tool outcomes. Keep model-call and dispatch IDs
distinct. Separate attempted, executed and result-delivered facts; do not imply
cancellation undoes a side effect.

### C. Specify cancellation threading and deadline scope

`ServiceClient` says callbacks run synchronously on the caller (`client.py:139–142`),
but `should_stop` runs both on a cancellation thread (`:905–910`) and the consumer
thread (`:949–950`). It can therefore be called concurrently.

HTTPX's five-second timeout is not a total operation deadline (`:204`, `:327–330`).
TTS transport/cleanup can outlive the documented total budget (`:580–606`). A
failed cancellation request can replace a primary deadline error (`:457–459`).
Already-running synchronous tools cannot be interrupted (`:954`).

**Recommendation:** prefer a cancellation event/token over a concurrent arbitrary
predicate. Document caller-thread callbacks separately. Distinguish job deadline,
total call budget and transport timeout; propagate remaining budgets and preserve
the primary outcome. Clearly describe cooperative cancellation of user code.

### D. Preserve machine-readable errors and canonical snapshots

Wire errors contain retryability and job correlation (`service_protocol.py:24–37`),
but `ServiceClientError` keeps only code/message (`client.py:40–45`, `:350–355`).
Framework 404/405 and unexpected errors have different response shapes because
only `ServiceError` is normalized (`service.py:975–977`).

Cancellation returns a different snapshot shape from submission/GET
(`service.py:1024–1043`), requiring a second GET in the client (`client.py:432–435`).

**Recommendation:** expose validated status, retryability, job ID and failure
phase; normalize sanitized application/framework errors. Handle nonapplication
server/transport failures explicitly. Use one snapshot serializer everywhere.

### E. Remove distinctions that do not change behavior

- `delivery_mode` is accepted/echoed, not a delivery policy.
- `dialogue` is the same text/tool lane, not a voice pipeline.
- A schema job in a tool-enabled session requires a socket, then bypasses tools
  entirely (`service.py:602–604`, `:769–807`).
- `registry: Any` and loosely typed dictionaries do not express supported caller
  contracts; capability/result structures are similarly hard to discover.

**Recommendation:** remove inert options and redundant names. Reject unsupported
schema/tool combinations explicitly. Add small message/result types and a tool
registry protocol, not a large abstraction hierarchy.

### F. Bring documentation back into agreement

`orchestrator/README.md` and `docs/SERVICE_USAGE.md` still say there is no Unity
client although one now exists. README also says remote tools are `config_set`
only, despite generic validated declarations. Package documentation promises
nonblocking behavior while actual Python APIs are deliberately synchronous.

Documentation should clearly separate shipped features, unsupported features,
operator provisioning, application calls, and blocking/threading requirements.

## Recommended interface direction

A narrow, typed interface is sufficient:

1. **Connection/lifecycle:** explicit owned-service startup versus attachment;
   inspect capabilities without accidental provisioning.
2. **Convenience operations:** typed text, speech and transcription results.
   Keep synchronous methods explicitly blocking; any future async variant must
   preserve cancellation and cleanup semantics, not merely rename methods.
3. **Advanced job operations:** one submit/snapshot/cancel contract, an explicit
   event stream and canonical terminal outcomes.
4. **Tools:** declarations plus a typed local implementation protocol; complete
   attempt/execution/delivery records and documented unknown outcomes.
5. **Errors:** stable machine-readable categories and correlation metadata.

Keep model/runtime selection as deployment policy unless a genuine application
requirement calls for per-job selection. Do not expose catalog internals merely
to make the API look generic. Add new modalities through bounded request/result
contracts when their adapters actually exist.

## Verification and next steps

- Entire offline orchestrator suite: **277 passed** (Python 3.13.15).
- Review reproductions used in-memory HTTP and fake hosts/sockets, not native
  inference. No live-network stress, GPU, audio-device, or Unity tests were run.
- Prior findings in `ORCHESTRATOR_API_REVIEW.md` remain unresolved in the reviewed
  source; this is a current assessment, not evidence that previous fixes regressed.
- Current service tests principally validate parsers and stubbed inference. They
  do not establish complete REST/WebSocket/client lifecycle guarantees.

Suggested implementation order:

1. Bound socket cleanup; roll back partial startup; enforce complete output budgets.
2. Fix schema/numeric validation and artifact ownership; unify error/snapshot shapes.
3. Define typed completion/tool/cancellation contracts and add client boundary tests.
4. Remove speculative/inert API surface and correct documentation.

Acceptance tests should cover stalled close, partial startup, multibyte output,
array/scalar schemas, huge integers, admission rejection, failed cleanup,
remote tool correlation, cancellation after side effects, socket loss, deadlines,
idempotent replay, retention quotas and expiry. These tests can remain offline.