# 0107 — Unity ↔ orchestrator integration

**Status:** draft
**Date:** 2026-09-29
**Author:** agent

## Question

What is the best bridge between the Unity game and the Python orchestrator
service: HTTP+JSON with file-path handoff, or a message bus?

## Hypothesis

Localhost HTTP with a shared asset cache directory is simplest and robust; SSE (or
a small poll loop) carries progress. Large payloads are **paths**, not bytes.

## Setup

- **OS:** Windows Server 2022
- **Runtime:** Unity 6000.0.84f1, Python 3.13 + FastAPI, `UnityWebRequest`
- **Models:** n/a (uses a stub host)

## How to run

```powershell
# exact commands added by the experiment
```

## Results

| Metric | Value |
| --- | --- |
| Submit round-trip | |
| SSE reliability | |
| Texture/mesh import path | |
| Crash recovery | |
| Editor vs player build parity | |

## Conclusion

`inconclusive` — pending run.

## Next steps

- [ ] Fill in results
- [ ] Promote finding to `../../ideas/model-orchestrator/modelorchestrator.md`

## Links

- Related idea: `../../ideas/model-orchestrator/modelorchestrator.md`
