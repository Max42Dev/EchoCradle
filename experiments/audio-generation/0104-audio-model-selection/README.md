# 0104 — Audio model selection (music + SFX)

**Status:** done (paper survey; no weights downloaded)
**Date:** 2026-09-29
**Author:** agent

## Question

Which local models can generate level background music and short event SFX
(hurt/death/attack) — plus any voiced lines — within the disk and VRAM budget of
the reference box?

## Hypothesis

MusicGen (small) and AudioGen/stable-audio cover both music and SFX; disk size is
the main constraint, so smaller checkpoints win. A tiny CPU TTS covers any voiced
lines. MusicGen and AudioGen may even be co-resident because they are small.

## Setup

- **OS:** Windows Server 2022 (build 20348)
- **Hardware:** NVIDIA L4 24 GB (23034 MiB, driver 596.86), 16 GB RAM, ~34 GB free disk
- **Runtime:** Python 3.13, PyTorch (CUDA), `transformers`, `audiocraft`, `diffusers`
- **Models:** MusicGen small/medium/large, AudioGen medium, stable-audio-open-1.0,
  Kokoro-82M, Piper, XTTS-v2
- **Method:** this is a **paper/datasheet survey**. No weights were downloaded —
  disk is the hard constraint, so sizes are taken from the Hugging Face Hub blob
  metadata, which is exact, rather than by downloading.

> **Latency caveat.** The L4 has no FP8 and no Tensor Core FP16 dense throughput
> advantage over the A100-class cards the published figures use, and on Windows
> the WDDM driver caps how much VRAM a process may hold. Treat the latency column
> as *indicative*: it is a sanity range for a 24 GB Ada-class card, to be
> confirmed by a real run once disk allows. Qualitative ordering (small < medium
> < large) is reliable; absolute seconds are not.

## How to run

```powershell
# Smoke-test the harness path (stdlib only, writes a placeholder WAV):
python src\audio_bench.py --seconds 10 --sample-rate 32000

# Measure a real model's peak VRAM while it runs (experiment 0105):
python ..\..\benchmarks\0105-vram-probe-residency\src\probe.py --samples 30 --interval 0.5
```

## Results

### Option comparison

| Option | Purpose | Output rate / length limit | ~10 s gen time (L4-class) | Peak VRAM | Model disk size | Licence | Quality notes | Scriptability |
|---|---|---|---|---|---|---|---|---|
| **MusicGen small** (300M, 2.4 GB fp32) | Music | 32 kHz mono, ~30 s cap | ~6–12 s | ~2–3 GB | **~2.6 GB** (safetensors 2.36 GB + EnCodec 236 MB) | CC-BY-NC-4.0 (non-commercial) | Lowest of the three; fine for loops/ambience | `transformers` pipeline or `audiocraft`; batch several prompts per call |
| **MusicGen medium** (1.5B, 6 GB fp32) | Music | 32 kHz mono, ~30 s cap | ~20–35 s | ~4–6 GB | **~8.9 GB** (6.0 GB + 236 MB + alt pth 8.0 GB) | CC-BY-NC-4.0 | Noticeably better melody/structure | same as small |
| **MusicGen large** (3.3B, 13 GB fp32) | Music | 32 kHz mono, ~30 s cap | ~60–90 s | ~10–13 GB | **~20.9 GB** (13.7 GB + 236 MB + pth 6.5 GB) | CC-BY-NC-4.0 | Best FAD, but see blocker below | same, but slow |
| **AudioGen medium** (1.5B, 3.7 GB) | **SFX** | 16 kHz mono, ~10 s practical | ~15–25 s | ~3–4 GB | **~3.9 GB** (3.68 GB + EnCodec 236 MB) | CC-BY-NC-4.0 | Best-in-class text-to-SFX; separate sound per prompt | `audiocraft` only (no `transformers`); batch prompts into one call |
| **stable-audio-open-1.0** (1.1B, ~4.9 GB) | Music **and** SFX | **44.1 kHz stereo, up to 47 s** | ~20–40 s (100 steps) | ~5–7 GB | **~5 GB** (+ gated, needs HF login) | Stability Community (non-commercial; paid licence for commercial) | **One model for both needs**; strong for SFX/field recordings, weaker for full songs | `diffusers.StableAudioPipeline` or `stable-audio-tools`; `num_inference_steps` trades quality/time |
| **Kokoro-82M** (0.08B) | Speech | 24 kHz mono, arbitrary length | **< 1 s (runs on CPU)** | ~0 (CPU) / <0.5 GB GPU | **~0.35 GB** (+ ~0.5 MB/voice) | **Apache-2.0** | Best quality/price of the TTS options; English + several langs | `pip install kokoro soundfile`; needs `espeak-ng` |
| **Piper** (VITS, per-voice ONNX) | Speech | 16–22 kHz mono, any length | **< 0.3 s (CPU)** | ~0 (CPU) | **~0.06–0.11 GB per voice** | **MIT** | Robotic but perfectly usable for barks/short lines | Standalone `piper` binary or `piper-tts`; trivially offline |
| **XTTS-v2** (Coqui) | Speech + cloning | 24 kHz mono, 17 languages | ~2–4 s (GPU) | ~2–4 GB | ~1.8 GB | Coqui Public Model License | Only pick this if you need **voice cloning**; heavier and more licence-restrictive | `TTS.api`; awkward on Windows |

Notes on the numbers:

- **Disk sizes** are exact Hub blob sums (weights + the shared EnCodec
  `compression_state_dict.bin` + tokenizer). Repos also carry duplicate
  `.bin`/`.safetensors` (or `.pth`) copies; with `transformers` you only fetch
  `model.safetensors`, so the "Model disk size" column already scopes to the
  minimal download. Disabling Hugging Face's blob-cache duplicate halves it again.
- **MusicGen length cap** is ~30 s per generation (1500 audio tokens at 50 Hz);
  longer tracks must be stitched with cross-fades.
- **AudioGen** is 16 kHz; upsample if the engine needs 44.1 kHz, or accept the
  lo-fi character (often desirable for retro SFX).

### Fit against the budget

| Constraint | Value | Consequence |
|---|---|---|
| Free disk | ~34 GB | MusicGen small + AudioGen medium + Kokoro ≈ **7 GB** — comfortable. Stable Audio instead of the two ≈ **5 GB**. |
| Free VRAM (idle) | ~22 GB free of 23 GB | Plenty for one audio model; MusicGen small (~3 GB) + AudioGen (~4 GB) co-reside easily. |
| RAM | 16 GB | fp32 checkpoints plus PyTorch overhead are the binding limit when disk is full; keep one audio process, unload between jobs. |

## Conclusions

Per option:

- **MusicGen small — recommended for level music.** It is the only music model
  whose weights (~2.6 GB) leave disk headroom for the rest of the pipeline. The
  quality gap to medium is real but acceptable for looping background beds. Its
  CC-BY-NC licence is fine while the game is not sold; revisit if it is.
- **MusicGen medium — the fallback if small sounds too flat.** It fits disk
  (~8.9 GB) and VRAM, at ~3× the latency. Use it only if a level's music is
  prominent.
- **MusicGen large — rejected.** ~21 GB of weights alone would consume most of
  the 34 GB free disk, and ~60–90 s per 10 s clip is far too slow for a
  background task that may run alongside other generation. Quality gain does not
  justify the blocker.
- **AudioGen medium — recommended for event SFX.** Purpose-built for
  text-to-sound-effect ("monster hurt", "wet claw attack"), small at ~3.9 GB, and
  it generates several sounds per call. It is the right tool for the
  hurt/death/attack clips. Downside: `audiocraft`-only, no `transformers` path.
- **stable-audio-open-1.0 — strong single-model alternative.** It covers music
  *and* SFX, outputs 44.1 kHz stereo up to 47 s (no stitching), and is small
  (~5 GB). It is, however, **gated** (HF account + licence acceptance) and its
  Community licence forbids commercial use. If we want one audio model instead of
  two, this is it; if we want the smallest total footprint and the best SFX
  specificity, keep MusicGen small + AudioGen.
- **Kokoro-82M — recommended for voicing.** Apache-2.0, ~0.35 GB, runs on CPU in
  under a second, so it never competes for VRAM. Best default for any NPC line.
- **Piper — recommended as the always-available fallback.** MIT, ~60–110 MB per
  voice, pure CPU, no PyTorch. Ship it as the deterministic fallback when the GPU
  is busy or a model is missing.
- **XTTS-v2 — rejected for v1.** Only justified by voice cloning, which the game
  does not need; heavier and licence-encumbered.

**Bottom line:** adopt **MusicGen small** (music) + **AudioGen medium** (SFX) +
**Kokoro-82M** (speech, CPU), with **Piper** as the CPU fallback. Combined disk
≈ 7 GB. Keep **stable-audio-open-1.0** on the shortlist as a one-model
consolidation if disk or complexity becomes a problem. **Do not** download
MusicGen medium/large until small is proven insufficient.

## Next steps

- [ ] Once disk allows, download only `musicgen-small` and `audiogen-medium` and
      record real latency + peak VRAM with `probe.py`; replace the indicative
      latency column.
- [ ] Verify `audiocraft` installs on Windows Server 2022 / Python 3.13 (it is
      the only SFX path and historically the most fragile dependency).
- [ ] Decide music length strategy: 30 s × N with cross-fade vs. one stitched bed.
- [ ] Promote finding to `../../ideas/model-orchestrator/audio.md` (audio runtime host).

## Links

- Related idea: `../../ideas/model-orchestrator/audio.md`
- VRAM measurement for this experiment: `../../benchmarks/0105-vram-probe-residency/`
- Helper: [`src/audio_bench.py`](src/audio_bench.py)

### Sources

- MusicGen paper — <https://arxiv.org/abs/2306.05284>
- MusicGen small — <https://huggingface.co/facebook/musicgen-small>
- MusicGen medium — <https://huggingface.co/facebook/musicgen-medium>
- MusicGen large — <https://huggingface.co/facebook/musicgen-large>
- AudioGen medium — <https://huggingface.co/facebook/audiogen-medium>
- AudioGen paper — <https://arxiv.org/abs/2209.15352>
- Stable Audio Open 1.0 — <https://huggingface.co/stabilityai/stable-audio-open-1.0>
- Stable Audio Open paper — <https://arxiv.org/abs/2407.14358>
- Kokoro-82M — <https://huggingface.co/hexgrad/Kokoro-82M>
- Piper voices — <https://huggingface.co/rhasspy/piper-voices>
- Piper project — <https://github.com/rhasspy/piper>
- XTTS-v2 — <https://huggingface.co/coqui/XTTS-v2>
- Transformers MusicGen docs — <https://huggingface.co/docs/transformers/model_doc/musicgen>
