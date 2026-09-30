# Model Orchestrator — Model Catalog, Download & Disk Policy

**Status:** exploring
**Last updated:** 2026-09-30
**Parent:** [`modelorchestrator.md`](modelorchestrator.md)
**Related:** [`text.md`](text.md), [`image.md`](image.md), [`three-d.md`](three-d.md), [`audio.md`](audio.md)

The declarative catalog of known models, how they are downloaded, and how disk
space is managed.

---

## Catalog

A checked-in `models/catalog.json` (shipped with the game, and updatable) maps
`(modality, tier) → model descriptor`.

```jsonc
{
  "text.default.medium": {
    "modality": "text",
    "family": "qwen2.5-instruct",
    "format": "gguf",
    "quant": "Q4_K_M",
    "disk_gb": 4.7,
    "vram_gb": 5.5,          // measured footprint, not download size
    "quality": 0.82,
    "license": "apache-2.0",
    "shippable": true,       // false for gated / non-commercial weights
    "runtime": "llama.cpp",
    "source": {
      "type": "huggingface",
      "repo": "…", "revision": "…", "files": ["model-Q4_K_M.gguf"]
    }
  }
}
```

Every descriptor carries a **licence id** and a **`shippable` boolean**. Gated or
non-commercial weights may be used for **development** but the planner must
refuse to select them for a shipped build (exp. 0103, 0104).

---

## Download requirements

- **Lazy.** Download a model only when a task actually needs it.
- **Verified.** Check hash + size after download; reject partial files.
- **Resumable.** HTTP range requests so a killed download resumes.
- **Declarative.** The catalog is the single source of truth for what may be
  fetched.
- **Source.** Hugging Face Hub for most models (`huggingface_hub`); Ollama's own
  registry where a model is Ollama-native. **Never** download at build time.
- **Quantized first.** Prefer GGUF/AWQ/FP8 where quality loss is acceptable.
- **Selective.** Large repos ship many variants; download **only the files** a
  descriptor names. Never clone a repo wholesale (exp. 0103: a 24 GB repo yielded
  a 4.4 GB usable subset).
- **Licence-gated.** See `shippable` above.

> **Disk is not a download gate.** Free disk space must **not** block a download
> or force a smaller quantization. If disk is tight, evict unused models first
> (see below) and then download the model the planner actually chose.

---

## Disk Policy

**Disk space is a secondary concern.** Model *choice* is driven by VRAM fit and
quality — never by free disk space. Disk pressure is handled *after the fact* by
eviction.

### Rules

1. **Never restrict model size by disk.** The planner selects the best model that
   fits VRAM; disk size is not part of the fit test.
2. **Evict unused models when disk is scarce.** "Unused" = not resident, not
   referenced by a running/queued task, and not referenced by the current save.
3. **Always keep at least one model per category.** Eviction must retain the
   **most-recently-used** model in each category (`text`, `image`, `mesh3d`,
   `audio`, …), even if it has not been used recently. A category is never left
   empty.
4. **Evict in LRU order** within a category, oldest first, until the free-space
   target is met.
5. **Never evict** a model with a running task, or one pinned by the current
   save.
6. **Re-download is acceptable.** Because downloads are lazy, verified and
   resumable, evicting a model is cheap to undo — this is what makes rule 1 safe.

### Eviction order

```
1. Unused models, LRU, oldest first
2. …but stop before removing the last (most-recently-used) model of any category
3. If still short on space: warn, and surface a typed error to the game
```

### Thresholds

- Trigger eviction when free space drops below a **low-water mark** (e.g. 8 GB).
- Evict until free space reaches a **high-water mark** (e.g. 16 GB), so eviction
  is not triggered on every download.
- Thresholds are configurable and measured against the probe's free-disk reading.

> **Open question Q14.** The exact low/high-water marks and the retention rule
> (keep one per category vs. keep one per `(category, tier)`) need validation on
> a small-disk machine.

---

## Cache vs. models

Two different stores, two different eviction policies:

| Store | Contents | Eviction |
|-------|----------|----------|
| **Model store** | Downloaded weights | LRU, keep ≥ 1 per category (this doc) |
| **Asset cache** | Generated artifacts (`sha256(kind+spec+model+seed)`) | LRU with a byte budget; never evict assets referenced by the current save |

---

## Open questions

| # | Question |
|---|----------|
| Q14 | What is the right eviction threshold and retention rule for models on a small disk? |
