# Diagnostic ordinary NUnit runner

Entry point: `EchoCradle.Editor.ProjectTests.Run` via Unity `-executeMethod`
with `-batchmode -nographics -projectPath` (do not use `-runTests`).

This editor-only harness loads Unity's compiled `EchoCradle.Tests` assembly
and executes its real NUnit work-item tree on Unity's main thread. The installed
NUnit fork's default dispatcher creates a worker even with zero test workers;
`InlineTestDispatcher` avoids that engine-thread violation. Runtime assemblies,
test bodies and package sources are not modified or stubbed.

Purpose: bypass UTF's **entire prebuild pipeline**, where
`Unity.PerformanceTesting.Editor.TestRunBuilder` hangs even for ordinary project
tests. This does not selectively patch that package or validate its setup.
NUnit fixtures, setup/teardown and explicit-test semantics remain in effect.
Unity coroutine tests/setup/teardown are rejected, not silently counted as passed.
PlayMode tests and UTF-specific log assertion handling are outside this harness.
Do not use this as a general replacement for the Unity Test Framework.

Results: `Unity/Logs/project-nunit-tests.xml` contains the complete NUnit assembly
result tree, including failure stacks and explicit skip reasons. The editor exits
0 only with passing tests, no failures and no inconclusive results; otherwise 1
(harness errors use 2). Each run overwrites the XML; check the corresponding log
for compilation/harness errors and the XML timestamp before consuming results.

Verified 2026-10-24, Unity 6000.6.3f1:

- 112 discovered: **106 passed, 4 failed, 2 explicit skipped**; editor exit 1.
- Real native GameObject/controller lifecycle test passed.
- Failures: cancellation exact exception type (`TaskCanceledException` versus
  `OperationCanceledException`); unprovisioned native Silero VAD; text cancellation
  endpoint not observed; visual cancellation endpoint not observed.
- Both `RealOrchestratorTests` remain explicit and skipped. No model generation
  was started. Existing player PID 4360 was left running.