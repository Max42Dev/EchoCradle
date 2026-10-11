# Implemented orchestrator API review

Reviewed: 2026-10-07. Scope: existing REST, JSON WebSocket, artifact transport,
remote tools, and Python client. Missing modalities, binary audio streaming,
history, provisioning, and other explicitly unsupported features are not defects
in this review. No implementation changes were made.

## Resolution annotation — 2026-10-10

This review's findings, line references and **253-test baseline below are retained
as historical evidence**. Subsequent implementation supports generated JSON
array/scalar roots, range-checks numeric values before conversion, normalizes
bounded framework/application errors and canonical job snapshots, and preserves
client error correlation/retryability/phase metadata. Verified TTS survives
cleanup failure; unconsumed STT uploads are cleaned after definite admission
rejection, with ambiguous transport/5xx ownership left to TTL. Python also
classifies validated `503 NOT_READY` as definite rejection, matching Unity.

Removed `dialogue` and `delivery_mode`; schema-plus-tools fails explicitly.
Python now uses typed `TextResult`/`ToolOutcome`, Event cancellation and explicit
owned/attached lifecycle. Tools remain arbitrary validated client declarations.
Public speculative task exports and the unused `tasks.py` module were removed
without shims.
The current
Unity client and experiment consumers are updated and offline boundary suites
pass. See [current usage](SERVICE_USAGE.md),
[the later review follow-up](ORCHESTRATOR_REVIEW_2026-10-10.md#implementation-follow-up--2026-10-10)
and [Unity validation](../Unity/README.md#validation).

Service module decomposition remains optional maintainability work, not a
completed correctness fix. Blocked synchronous callbacks/tools can still exceed
deadlines; cancellation does not undo side effects. No real-model/audio/player
acceptance was rerun for this cleanup. Final offline validation: **369 orchestrator
tests passed; 122 interview tests passed, 1 live-model test deselected**. No C#
changes in the final audit required a Unity rerun. The original evidence below
remains unchanged.

## Overall assessment

**A sound local-service API with a clear transport split, but some inconsistent
contracts and unnecessary interface surface.** It is suitable for the current
slice; tightening the existing contract is more valuable than expanding it.

REST handles resource creation, job submission/polling/cancellation, and complete
PCM artifacts. WebSocket handles text deltas, state events, heartbeat, and tool
round trips. Keeping submission in REST rather than duplicating it over the
socket is a good simplification. These are native-client interfaces, not a
browser-ready API: upgrades require bearer headers, and origins are denied by
default.

## Findings, in priority order

### 1. P2 — Accepted response schemas can reject valid model output

Evidence: `orchestrator/orchestrator/service_protocol.py:186–207` accepts a valid
array response schema. `orchestrator/orchestrator/service.py:777–780` subsequently
decodes the generated value through an object-only JSON parser.

Reproduced with `{"type":"array","items":{"type":"string"}}`: the specification
is accepted, but valid output `["valid"]` raises `INVALID_JSON` before schema
validation. The same structural mismatch applies to scalar output schemas.

**Recommendation:** either constrain admission explicitly to object-root output
schemas, or use bounded JSON-value decoding for generated responses. Request and
WebSocket envelopes should remain object-only. The advertised contract and
actual output validator must agree.

### 2. P2 — Invalid numeric input escapes the validation/error contract

Evidence: `orchestrator/orchestrator/service_protocol.py:178–181` evaluates
`math.isfinite(temperature)` before checking its allowed numeric range.

A 451-byte JSON request containing a 401-digit integer temperature reproduces
`OverflowError`, yielding HTTP 500 with plain `Internal Server Error`, instead
of a structured client validation error. This is well below the request limit.

**Recommendation:** reject out-of-range numbers before float conversion, or
translate conversion overflow into `INVALID_SPEC`. Add a regression test at the
HTTP boundary, not just the specification parser.

### 3. P2 — Artifact cleanup is not separated from inference outcome

Evidence: `orchestrator/orchestrator/client.py:620–627` uploads STT PCM without
deleting it when job admission fails. Admission rejects busy/quota requests before
consuming the artifact (`orchestrator/orchestrator/service.py:593–601`). Repeated
rejected submissions retain uploads until expiry and pressure the artifact quota.

Conversely, `orchestrator/orchestrator/client.py:595–606` requires deletion of an
already downloaded, verified TTS artifact before returning successful audio.
A cleanup failure therefore turns usable inference output into a caller-visible
failure.

**Recommendation:** clean up uploads after definite admission rejection; do not
discard verified TTS output because deletion failed. Treat ambiguous submission
transport failures separately: the server may already have accepted the job.
These two cleanup paths were established by source inspection, not fault injection.

### 4. P2 — REST errors have multiple incompatible shapes

Evidence: `orchestrator/orchestrator/service.py:966–968` handles `ServiceError`,
but framework routing errors keep their default format. Actual requests returned:

- Unknown route: HTTP 404, `{"detail":"Not Found"}`.
- Wrong method: HTTP 405, `{"detail":"Method Not Allowed"}`.
- Numeric validation escape above: HTTP 500, plain text.
- Application errors: the custom `error` envelope.

The Python client assumes bounded object JSON and the custom error structure
(`orchestrator/orchestrator/client.py:346–355`). It consequently loses error
classification on some responses.

**Recommendation:** normalize framework HTTP errors and sanitized unexpected
exceptions into the same envelope, preserving status and appropriate headers.
Keep arbitrary internal exception text out of responses.

### 5. Interface issue — Accepted options do not consistently express behavior

- `delivery_mode` is accepted and echoed but does not control delivery
  (`orchestrator/orchestrator/service.py:275–281,990–1001`). The usage document
  acknowledges the limitation, but the option still suggests a behavioral choice.
- `dialogue` executes the same text/tool lane as `text`; it is not a voice pipeline
  (`orchestrator/orchestrator/service.py:744–747`). Current usage documentation is
  clear, but exposing two names adds a distinction clients must learn without
  different execution semantics.
- A text session can declare tools and a job can request `json_schema`, yet the
  schema branch uses `stream_chat`, not the tool-calling path
  (`orchestrator/orchestrator/service.py:751–798`). Such a request still needs a
  negotiated socket because of the session tools, even when non-streaming.

**Recommendation:** remove inert options or state their metadata-only role
unambiguously. Clarify or reject the schema-plus-tools combination instead of
silently bypassing tools. Do not implement extra functionality merely to justify
an existing field.

## Cleanness and interface recommendations

1. **Preserve useful error metadata in the client.** The wire includes `retryable`
   and `job_id`, but client exceptions generally retain only the error code and a
   generic message (`client.py:350–355,438–442`). Expose validated status,
   retryability, and correlation fields without exposing arbitrary server text.
2. **Use one canonical job response representation.** Submission and GET include
   service identity; cancellation returns only the snapshot. GET alone adds
   selected-model diagnostics (`service.py:1015–1033`). The client compensates
   with an additional GET after cancellation (`client.py:430–434`). Prefer a
   shared serializer and consistent identity fields.
3. **Document tool execution semantics prominently.** Tools run synchronously in
   the client, and a result exceeding the five-second budget is discarded
   (`client.py:953–965`). Cancellation or timeout does not undo a side effect.
   Existing documentation correctly calls the outcome unknown; tool authors need
   that requirement at the client method boundary too.
4. **Separate transport, scheduling, and adapters modestly.** `service.py` combines
   HTTP middleware/routes, socket protocol, quotas, lifecycle, and inference in
   1,248 lines. Extracting routing and socket handling from `_Runtime` would make
   contract testing easier without introducing a new framework or abstraction
   hierarchy. This is maintainability work, not a correctness blocker.
5. **Keep documentation authoritative.** `docs/ORCHESTRATOR_SERVICE.md:49–51`
   still describes a compiled `config_set`-only restriction, while implementation
   and `SERVICE_USAGE.md` support arbitrary validated declarations. Also explain
   the REST default deadline of 30 seconds versus the Python client's 120 seconds.

## What is already clean

- Explicit versioning, instance/session/job/turn identities, connection-local
  sequencing, and a negotiated socket before streaming submission.
- Strict JSON decoding, unknown-field rejection, finite-value checks, bounded
  input/output, reference-free schemas, and validation of generated JSON.
- Independent socket reader/writer, bounded backpressure, heartbeat, late-result
  rejection, and cancellation checks around remote tool continuation.
- Tool implementations stay client-side; declarations and arguments are validated
  before execution. No game-specific tool implementation is required in the server.
- Logical cancellation is distinguished from outstanding native work through
  `host_pending`; worker lanes are not prematurely released.
- Raw audio format is explicit. The client verifies length, sample rate/count,
  and SHA-256. Unsupported binary WebSocket audio is explicitly rejected.
- Capabilities distinguish implemented and unsupported behavior. Startup remains
  asynchronous and provisioning is excluded from the serving path.

## Verification and limits

- Existing focused API tests: **15 passed**.
- Entire offline orchestrator suite: **253 passed**.
- Reproduced numeric overflow and 404/405 response shapes using FastAPI TestClient,
  without running the lifespan or loading models.
- Reproduced array-schema rejection using an isolated inference stub.
- Existing service tests mainly exercise parsers and stubbed inference, not real
  REST/WebSocket round trips. Prioritize boundary tests for these findings plus
  successful tool correlation, late results, cancellation, and socket loss.
- No live model inference, audio-device testing, or real-network WebSocket stress
  testing was performed. Runtime concurrency behavior beyond the inspected source
  remains unverified.

Suggested order: fix schema/output agreement and validation escape; normalize
errors and cleanup; then simplify accepted options and response representations.