# Experiments

A sandbox for **agents and developers** to try out ideas before committing to them in the
main game. Each experiment is a small, self-contained spike that answers a specific question.

## Purpose

`ideas/` describes *what* we want. `experiments/` proves *whether and how* we can do it.
Nothing here is production code — experiments are expected to be thrown away or promoted.

## Structure

```
experiments/
  README.md
  _template/            # copy this to start a new experiment
    README.md
    src/
  llm-runtimes/         # e.g. Ollama, llama.cpp, LM Studio, vLLM, ONNX Runtime GenAI
  image-generation/     # e.g. Stable Diffusion, ComfyUI, Flux, SDXL, ControlNet
  three-d-generation/   # e.g. text/image-to-3D, TripoSR, Hunyuan3D, Blender scripting
  audio-generation/     # e.g. MusicGen, AudioCraft, Piper/TTS
  npc-ai/               # agent frameworks, memory, planning, behavior trees
  unity-integration/    # Unity <-> local model bridges, IPC, async, editor tooling
  benchmarks/           # latency, VRAM, quality comparisons across the above
```

Each experiment lives in its own folder named `NNNN-short-slug/` (e.g. `0001-ollama-basics/`)
so ordering is stable and folders sort chronologically.

## Experiment README template

Every experiment must contain a `README.md` with:

- **Question** — the single question this experiment answers.
- **Hypothesis** — what we expect to happen.
- **Setup** — OS, hardware, models, versions, install steps.
- **How to run** — exact commands.
- **Results** — measurements, outputs, screenshots, sample assets.
- **Conclusion** — `promising` / `inconclusive` / `rejected`, plus next steps.
- **Links** — related `ideas/` documents and follow-up experiments.

## Rules

- Keep experiments isolated: no shared state, no edits to the main game.
- Pin versions and record hardware — results are meaningless without them.
- Prefer the smallest possible spike that answers the question.
- Record failures too; a documented dead end saves future time.
- When an experiment succeeds, promote the finding into `ideas/` and link both ways.
