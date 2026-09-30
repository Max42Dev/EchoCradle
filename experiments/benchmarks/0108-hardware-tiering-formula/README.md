# 0108 — Hardware tiering & model-fit formula

**Status:** draft
**Date:** 2026-09-29
**Author:** agent

## Question

Does the proposed tier score / co-residency formula correctly predict which
models a machine can run, on machines other than the reference L4?

## Hypothesis

VRAM dominates; a simple weighted score plus a `usable_vram / N` residency budget
is enough to pick safe models without OOM.

## Setup

- **OS:** Windows
- **Hardware:** reference L4 24 GB (+ any other GPUs available, else simulate)
- **Runtime:** TBD

## How to run

```powershell
# exact commands added by the experiment
```

## Results

| Config | Predicted tier | Safe? |
| --- | --- | --- |
| L4 24 GB | Tier 3 | |
| 8 GB laptop | Tier 1 | |
| CPU only | Tier 0 | |

## Conclusion

`inconclusive` — pending run.

## Next steps

- [ ] Fill in results
- [ ] Promote finding to `../../ideas/model-orchestrator/modelorchestrator.md`

## Links

- Related idea: `../../ideas/model-orchestrator/modelorchestrator.md`
