# Character creation checkpoint — 2026-10-24

## Implemented

- New appearance conversation after the original configuration interview.
- Image review with approval or a new description session after rejection.
- Static GLB and sequential animation review inside Unity using glTFast 6.20.0.
- Cryptographically random generation seeds, recorded per request.
- Text-only production-controller automation and stage screenshots.
- Automatic text/speech weight and llama.cpp runtime provisioning; atomic downloads.
- Experimental visual API, cached 0110 worker integration and diagnostics.

## Verified versus unfinished

- Windows player builds successfully with the real imported preview package.
- Python orchestrator suite: 602 passing at the automatic-provisioning checkpoint.
- Earlier real orchestrator-only run completed all three stages and rendered all five
  clips; evidence remains locally in experiment results, not committed binaries.
- Unity real text-only runs completed configuration, appearance recording, image
  rejection, a changed description, and generated images. An earlier run also
  displayed a static model. Full final animation review is NOT yet verified.
- Randomized reconstruction exposed a UV weld failure. A UV-only fallback is fixed
  and validated on the failing mesh without changing geometry or skin weights.
- A subsequent service-only reconstruction failure did not reproduce through the
  in-process facade with identical input/seed. Service traceback logging is now
  available through trusted `ECHOCRADLE_SERVICE_LOG`; investigation is unfinished.
- Last Unity diagnostic NUnit run: 106 passed, 4 failed, 2 explicit skipped.
  Open failures: cancellation subtype assertion, missing native Silero model, and
  two cancellation endpoint assertions. Do not describe these as a passing suite.
- Standard Unity test-run setup stalls in the transitive Performance Testing global
  prebuild hook. The editor-only diagnostic runner avoids that hook and records XML;
  it does not replace coroutine/PlayMode acceptance.

## Remaining work

1. Resolve the service-only failure using retained local diagnostics.
2. Run the complete real Unity text-only acceptance through all five animations,
   inspect screenshots, and verify the final ready save and process cleanup.
3. Fix remaining regression tests and validate voice continuation on real devices.
4. Add automatic visual dependency preparation only after exact transitive download
   availability/licensing checks. Current visuals still use installed 0110 workers;
   no gated or commercially unverified dependency is silently provisioned.
5. Clean up stale cached-only terminology and refine status/progress UX as necessary.

No offline mode was added. No production fixed seeds were introduced. Model quality,
foot contact, animation semantics and shipping eligibility remain separate from
structural/API success. Test players and owned workers were stopped for this handoff.

Local evidence: `Unity/Logs/character-smoke*`, `character-player*.log`,
`character-service-v3.log`, `project-nunit-tests.xml`, and `uv-repro/`.
Logs, generated binaries and machine-specific configuration are deliberately excluded.