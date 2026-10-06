# Git-Backed Save State

**Status:** exploring
**Last updated:** 2026-09-30
**Related:** [`configuration.md`](configuration.md), [`model-orchestrator/modelorchestrator.md`](model-orchestrator/modelorchestrator.md),
[`model-orchestrator/catalog.md`](model-orchestrator/catalog.md),
[`image-generation.md`](image-generation.md), [`audio-generation.md`](audio-generation.md),
[`3d-asset-generation.md`](3d-asset-generation.md), [`modding-and-tools.md`](modding-and-tools.md)

---

## Summary

**The save file is a git repository.** Every player-visible change to game state —
a looted chest, a finished quest, a newly generated level, a synthesised sound
effect, a rewritten NPC — is **one commit on a single, linear timeline**.

| Concept | Implementation |
| --- | --- |
| Save slot | one git repository under `Saves/<slot>/` |
| Save file contents | the repository's **working tree** (the typed JSON from [`configuration.md`](configuration.md)) |
| Autosave | a **commit** — atomic, multi-file, crash-safe |
| Undo / redo | move a **cursor** up and down the commit chain; re-materialise the working tree |
| Player history | the **commit log** |
| Binary content (images, audio, meshes) | committed as git blobs, content-addressed and deduplicated |
| Branching | **none.** No branches, no merges, no remotes, no rebase. A straight line. |

Because the state store is a real git repository, the entire existing toolset
applies to a save for free: `git log`, `git diff`, `git blame`, `git bisect`,
`git show`. A bug report can be "slot 3, commit `a1b2c3d`" and we can inspect the
exact world state the player saw.

> **This document extends [`configuration.md`](configuration.md).** That document
> defines *what* the persistent data is (typed JSON, layers, ids, references).
> This one defines *how it is stored and versioned*. Where they conflict, this
> document wins — it is written later and supersedes the save layout described
> there (Golden Rule 7).

---

## Goals / Non-goals

### Goals

| # | Goal |
|---|------|
| G1 | **Every state change is recoverable.** No mutation is ever unrecoverable; the previous state is always one operation away. |
| G2 | **Atomic saves.** A save is a single atomic operation across *all* files, not a sequence of per-file writes that can tear on crash. |
| G3 | **Recoverable generations.** If a generated level / sound / model is bad, revert the commit and retry — with the seed and prompt recorded so the bad result is auditable. |
| G4 | **Binary-safe.** Images, audio and meshes version alongside the JSON that references them; no separate asset-backup mechanism. |
| G5 | **Strictly linear.** A player-facing action is never a merge, a conflict, or a branch choice. |
| G6 | **No external tooling required.** The player's machine does *not* need `git.exe` installed; the game embeds a git library. |
| G7 | **Inspectable.** A save can be opened with ordinary git tools (for us), and read as plain files (for anyone). |
| G8 | **Bounded growth.** Repository size must not grow without limit; unreachable history is reclaimable. |

### Non-goals

- **Distributed / remote state.** No push, no pull, no server, no cloud sync
  (Golden Rule 1). A "remote" is explicitly out of scope; sharing is done by
  copying a slot directory.
- **Collaborative editing.** One writer, one machine.
- **Branching workflows.** No feature branches, no merges, no rebase, no
  cherry-pick. History rewriting is limited to *truncating* an undone tail.
- **Storing model weights.** The orchestrator's model store is a machine-level
  cache, not game state; it is never committed (`modelorchestrator.md`).
- **Replacing the content layer.** Shipping content in `Assets/Data/**` stays
  read-only in a player; only the save is mutable.
- **A general VCS.** We use git's object store and history; we do not expose git
  concepts (remotes, tags, submodules, hooks) to the player.

---

## Why git

| Option | Verdict |
| --- | --- |
| **Rotating snapshot slots** (`save1`..`save10`) | Rejected. O(n) disk per snapshot, no dedupe, coarse granularity, still needs atomics. |
| **Bespoke append-only event log + periodic snapshot** | Rejected. We would write the object store, hashing and GC ourselves — that is git. |
| **SQLite with a change-log table** | Rejected as the *store*. Good for queries, but it is one opaque binary blob: no blob dedupe, no diff, no standard tooling, and undo means replaying a log we wrote by hand. |
| **Filesystem snapshots** (VSS / btrfs / ZFS) | Rejected. Not portable, requires admin rights, coarse, and gives no per-change granularity. |
| **`git` CLI subprocess** | Rejected. Requires git on the player's machine; process spawn per commit is slow and adds a failure mode. |
| **Embedded git library (LibGit2Sharp)** | **Chosen.** MIT-licensed, in-process, no external dependency, gives the object store, refs, and packfiles without us writing them. |

The decisive argument is **content-addressed storage**. Git already stores blobs
by hash of their content, which gives us, for free:

- **Deduplication** — re-committing an unchanged 4 MB texture costs a pointer,
  not 4 MB. Two save slots that share a generated portrait share the bytes.
- **Immutability** — a blob can never be corrupted in place; verification is a
  hash check.
- **Identity** — a blob's hash *is* its identity, which is exactly the
  `AssetRef` identity model in `configuration.md` (see
  [Content addressing](#content-addressing--assetref)).

---

## The slot repository

```
%persistentDataPath%/Saves/<slot>/
  .git/                            # the store: objects, refs, config
  .gitattributes                   # text/binary handling (see below)
  meta.json                        # slot name, created-at, schemaVersion
  state/
    snapshot.json                  # player + world state (fast load)
    world.json                     # deltas vs. content layers
  content/                         # generated + promoted assets for this save
    levels/region-01/level.json
    audio/gen/amb-forest-a3f2.ogg
    art/gen/portrait-jora-91cb.png
    models/gen/axe-4d7e.glb
  logs/
    session.ndjson                 # in-session event log (see below)
```

Everything under `state/` and `content/` is committed. `logs/` is **not**
committed: it is diagnostic, high-churn, and would produce a commit per frame.
It is listed in the slot's `.gitignore`.

### `.gitattributes`

Binary handling is configuration, not luck. Set it once at repo creation:

| Pattern | Setting | Why |
| --- | --- | --- |
| `*.json` | `text eol=lf` | Stable diffs; no CRLF churn between runs. |
| `*.png *.jpg *.ogg *.wav *.glb *.fbx` | `-text -diff` | Never attempt text conversion; skip binary-diff work. |
| `logs/` | *(in `.gitignore`)* | Per-frame churn must never enter history. |

### Repository config (set at creation)

| Key | Value | Why |
| --- | --- | --- |
| `core.autocrlf` | `false` | CRLF normalisation would rewrite binaries and churn every commit. |
| `core.compression` / `pack.compression` | `1` | Binary blobs do not compress; spend no CPU on them. |
| `gc.auto` | `0` | GC is scheduled by us, not by an unbounded heuristic mid-session. |
| `user.name` / `user.email` | `EchoCradle` / `player@localhost` | Commits need an identity; the player may have none configured. |
| `core.fsmonitor` | `false` | No editor, no watchers. |

---

## The commit model

### What one commit is

A commit is a **gameplay transaction**: one logical, player-visible change, with
all the files it touched, recorded atomically.

| Transaction | Commits |
| --- | --- |
| Open a chest | `state/snapshot.json`, `state/world.json` |
| Finish a dialogue | `state/snapshot.json`, `logs/` (not committed) |
| Generate a new region | `state/world.json`, `content/levels/region-01/level.json`, several `content/models/gen/*.glb` |
| Synthesise an ambience bed | `content/audio/gen/amb-forest-a3f2.ogg`, `world.json` |
| Rewrite an NPC (LLM) | `content/…/npc.smith.jora.json`, `state/world.json` |
| Revert a bad generation | *(reverse of the above — new commit)* |

**Granularity rule:** not every field write. A transaction opens, mutations
accumulate, and the transaction closes on a *boundary* — a completed gameplay
event, a debounce tick, a scene exit, or app pause. This keeps the timeline
readable (hundreds of commits per session, not millions) and amortises commit
cost.

```
StateTransaction  (C#)
  Open("loot: open chest in region-01")
  mutate the object graph freely …
  AddTrailer("Seed", "918273")
  Commit()      -> one git commit
  Rollback()    -> discard, restore working tree from cursor
```

This is the same shape as the autosave machinery in `configuration.md`
(serialise-and-hash poll, debounce, flush on pause/quit) — a commit replaces the
per-file atomic write as the flush target.

### Commit messages

The project already uses Conventional Commits, so the timeline uses the same
grammar. Types are gameplay verbs, not code verbs:

| Type | Meaning |
| --- | --- |
| `new` | new game / new region created |
| `gen` | generated content promoted into the save |
| `loot` | items gained or lost |
| `quest` | quest state changed |
| `npc` | NPC state, relationship, or dialogue history changed |
| `world` | world flags, doors, spawns, time |
| `audio` | music/ambience/SFX binding changed |
| `revert` | a previous commit undone by a *new* commit (revert-by-replay) |
| `system` | schema migration, settings, save maintenance |

Example:

```
gen: region-01 dungeon layout

Seed: 918273
Generator-Version: levelgen-0.4.1
Model: ollama:granite4.2-8b
Prompt-Id: level-layout-v2
```

**Trailers** carry the generation provenance that `configuration.md` already
requires in the file (`_provenance`) up to the commit level, where it is
greppable across the whole timeline:

| Trailer | Purpose |
| --- | --- |
| `Seed:` | the generation seed — mandatory for any `gen` commit (Golden Rule 4). |
| `Generator-Version:` | the code version that produced it — a seed alone is not reproducible (`configuration.md`). |
| `Model:` / `Prompt-Id:` | which local model and which prompt template produced LLM output. |
| `Reverts:` | the commit id this one reverses. |
| `Playtime:` | seconds at commit time, so the history browser can show "2h 14m in". |

---

## Undo and redo

The timeline is a straight line, and the player's position on it is a **cursor**.

```
   C0 ──── C1 ──── C2 ──── C3 ──── C4 ──── C5
   new     gen     loot    quest   gen     loot
   game    region  chest   done    sound   chest
                           ▲
                         cursor          ▲
                                       main (tip)

   Undo    : cursor -> C2, working tree re-materialised from C2
   Redo    : cursor -> C3, ... and so on, up to main
   Commit  : parent = cursor (C3) -> C6; main = C6; C4/C5 abandoned
```

Two refs implement this; both point at commits on the same line:

| Ref | Meaning |
| --- | --- |
| `refs/heads/main` | the newest commit ever made (the tip) |
| `refs/echocradle/cursor` | where the player currently *is* |

Operations:

| Operation | Effect |
| --- | --- |
| `Undo()` | `cursor = parent(cursor)`; hard-materialise the working tree at `cursor`. No commit is created. |
| `Redo()` | `cursor = child(cursor)`; materialise. Available while `cursor != main`. |
| `Commit(tx)` | new commit with `parent = cursor`; then `main = cursor = new`. Any commits that were ahead of the cursor become unreachable. |
| `Load()` | on startup, materialise the working tree at `cursor` (never at `main`). |
| `Revert(id)` | *(alternative to undo)* a **new** commit that undoes `id` — used when history must not be rewritten, e.g. for a commit already "published" inside the session. |
| `Log(n)` | the timeline, newest first, with messages, trailers and playtime. |

### Behaviour on a linear history

- **Undo is bounded by the beginning of the save**, not by an in-memory stack:
  the player can undo back to the new-game commit.
- **Redo survives app restart**, because the cursor is a ref, not RAM.
- **Committing from an undone position discards the redo tail** — standard editor
  semantics, and it is what keeps the history a straight line rather than a fork.
  The discarded commits are not deleted immediately; the git reflog keeps them
  reachable for the session as a safety net.
- **No conflict is possible.** There is one writer, one working tree, and no
  concurrent edits. A merge can only exist if we create one, and we do not.

> **Why not `git reset --hard <id>` for undo?** Because it *destroys* the redo
> tail by moving `main`. Moving a separate cursor ref leaves `main` where it is,
> so redo is free and no data is lost until the player commits a new action.

> **Deterministic content after undo.** Re-materialising a commit restores the
> generated asset *and* the seed that produced it. Undoing a bad generation and
> retrying therefore means "revert, then resubmit with a new seed" — the old
> result stays in history and can be diffed against the new one.

---

## Content addressing & `AssetRef`

`configuration.md` defines `AssetRef { Guid, Placeholder }` — the stable identity
of a Unity asset, stored in JSON instead of a pointer. Git's blob hash is a
better identity than an `AssetDatabase` GUID, because it is derived from the
bytes:

| | `AssetDatabase` GUID | git blob hash (`blob:<sha1>`) |
| --- | --- | --- |
| Derived from content | ✗ (arbitrary, assigned on import) | ✓ |
| Same bytes ⇒ same id | ✗ | ✓ |
| Verifiable | ✗ | ✓ (hash check) |
| Available outside the editor | ✗ (editor-only) | ✓ |
| Survives across save slots | ✗ | ✓ |

So for **generated** content, `AssetRef.Id` is the blob hash. Two consequences:

1. **Cross-slot dedupe.** A shared `objects/info/alternates` file (or a shared
   object directory) pointing at a per-machine store makes generated assets
   shared automatically, because identical bytes hash identically. Ten saves
   containing the same generated portrait cost one blob.
2. **Integrity on load.** A missing or corrupt generated asset is detected by
   re-hashing, and is *regenerable* — the ref carries the seed and the model
   (`AssetRef` gains `Seed`, `Model`, `PromptId` for generated content; the
   placeholder is the fallback render until regeneration completes).

Hand-authored shipped assets keep their `AssetDatabase` GUID, because they are
authored in Unity and must resolve through Addressables at runtime.

---

## Growth and performance

This is the design's main risk and needs an explicit policy. Binaries do not
delta-compress, so **every distinct version of a texture or mesh is stored
whole** in the object store.

### Mitigations

| Lever | Effect |
| --- | --- |
| **Content-addressed dedupe** | Unchanged assets cost nothing on re-commit. Turns "one full copy per save" into "one copy per *distinct* asset". |
| **Transaction granularity** | Commits are events, not ticks. A session produces hundreds of commits, not millions. |
| **`logs/` excluded** | The highest-churn data never enters history. |
| **Low compression level** | Binary blobs barely compress; `compression=1` trades ~nothing in size for real CPU savings on commit. |
| **GC / repack, scheduled** | Loose objects are packed at launch or on quit (never mid-gameplay, never on a timer the player can feel). Unreachable commits — the abandoned redo tails — become reclaimable. |
| **Size budget + eviction** | Beyond a per-slot budget, prune the *oldest* history beyond a retention point (e.g. keep the last N commits plus every `new`/`gen` milestone), or move large content to the CAS offload below. |
| **CAS offload (escape hatch)** | Large binaries live in the orchestrator's content-addressed store; the commit records only the hash. Undo still works (blobs are immutable), but the repo stays text-sized. |

### Reference budget *(illustrative — not a target)*

| Quantity | Estimate |
| --- | --- |
| Text state per commit | 50–300 KB |
| Generated portrait (PNG, 1024²) | ~1–2 MB |
| Generated 3D asset (GLB, textured) | ~5–20 MB |
| Generated ambience loop (OGG) | ~1–3 MB |
| Commits per hour of play | 100–400 |
| **Naive worst case** (a generated model per region) | **hundreds of MB per save** |

The worst case is why the CAS offload lever exists and why G8 is a goal rather
than an afterthought. Marked as [Q1](#open-questions): *do large generated
binaries live in the slot repo, or by hash only?*

### Commit cost

| Concern | Mitigation |
| --- | --- |
| Hashing large blobs on commit | zlib/hash off the main thread; commit on a worker; the game never waits for a commit to finish a frame. |
| Commit latency spikes on a large scene change | Debounce per transaction; coalesce into one commit per boundary. |
| Windows antivirus scanning `.git/` | Document an exclusion for the save directory in setup; measure and confirm. |
| `.git/index.lock` left by a crash | libgit2 recovers on open; a stale lock is detected and cleared (single writer, so it is always stale, never contested). |

> **Non-blocking rule.** Commits happen on a background thread and are queued
> behind gameplay. The main thread only *marks* state dirty and *opens* a
> transaction (Golden Rule 6).

---

## Relationship to `configuration.md`

This design changes four things in that document. Everything else — typed JSON,
ids, references, layers, `_provenance`, validation — stands.

| `configuration.md` said | Now |
| --- | --- |
| Saves live in `Saves/<slot>/{meta,snapshot,world,remap}.json` | Saves live in `Saves/<slot>/`, which is a **git repository**; those files are its working tree. |
| Autosave = per-file temp-write + atomic rename, debounced | Autosave = **one commit** per transaction. Atomic across all files by construction — strictly stronger. |
| `history.ndjson` = append-only player history | The **commit log** is the durable history. `logs/session.ndjson` remains as fine-grained, non-durable detail (per-event, per-tick) that would be too noisy to commit. See [Q2](#open-questions). |
| `world.json` deltas replayed over content layers | Unchanged, but the delta file is now versioned, so a save can be replayed against *any* historical content set — a save's layer order is pinned by its own history. |
| Content files are LLM-editable and diffable | Unchanged, and now the same is true of **the whole timeline**: `git show` on an old commit is a complete, reviewable snapshot. |

One property that improves: `configuration.md` needed a `remap.json` for retired
ids. Git does not remove that need (ids are still not recycled), but a retired id
is now traceable to the commit that retired it.

---

## Player-facing surface

The timeline is not only a safety net; it is content the game is *about* —
local models generating an ever-changing world.

| Surface | Behaviour |
| --- | --- |
| **Undo / Redo** | Bound to keys. Instant, because it is a cursor move plus a file materialisation. |
| **History browser** | "Travel log": the commit list with type icons, playtime, and thumbnails for `gen` commits (the generated portrait, the dungeon map, the region art). Selecting an entry previews that world state read-only. |
| **Restore point** | Promote any commit to a **named milestone** (`git tag`) — a "chapter" the player can return to. Tags are the one git concept worth surfacing, and they are linear-safe. |
| **Crash recovery** | On launch, a slot that failed to commit cleanly is detected and the session is resumed from the cursor; the player is told, not silently moved. |
| **Bug report** | `slot + commit id` identifies an exact world state plus the seed/model/prompt that produced it. |

---

## Design rules

1. **One repository per slot. One branch. No remotes.**
2. **A commit is a transaction, never a tick.**
3. **Any `gen` commit carries `Seed` + `Generator-Version`.** A commit without
   them is a bug.
4. **Never amend or rewrite published history** — with one exception: a commit
   made *ahead of the cursor* is abandoned by design when the player acts from an
   undone position. That is truncation, not rewriting.
5. **Undo moves the cursor; it never deletes.** `Revert(id)` is the non-rewriting
   alternative when a commit must stay in the line.
6. **The working tree at the cursor is the truth.** The game reads files, not
   the object database, on the hot path.
7. **`logs/` is never committed.** Anything that changes per frame does not
   belong in history.
8. **Large binaries are a policy decision, not an accident** — see
   [Q1](#open-questions).
9. **The player never needs git installed, and never sees a merge.**
10. **Commit message types are gameplay verbs**, and `Seed`/`Model`/`Prompt-Id`
    trailers are first-class.

---

## Open questions

| # | Question | Notes |
| --- | --- | --- |
| Q1 | **Do large generated binaries live in the slot repo, or in the CAS store by hash?** | The request is "git stores state, including binary files". Dedupe makes small assets cheap; a textured GLB per region does not. Proposal: commit everything by default, move a category to CAS-offload-by-hash once it exceeds a threshold. Needs measurement (see [exp. 0105](../../experiments/benchmarks/0105-vram-probe-residency/README.md) for the probe style, but a *disk* experiment is needed). |
| Q2 | **Does `history.ndjson` still earn its place** once the commit log is durable history? | Candidate: drop it and rely on commits + `session.ndjson` diagnostics. Golden Rule 7 favours dropping it outright. |
| Q3 | **Repack/GC implementation.** LibGit2Sharp does not expose `git gc` / `repack` directly. | Options: packbuilder interop; a bundled maintenance binary used only at launch (never in gameplay); or accept loose objects and prune only abandoned commits. Must be decided before a save can grow indefinitely. |
| Q4 | **Retention policy default.** Keep-all, keep-N, or milestone-plus-window? | Interacts with Q1. Needs the disk budget from `catalog.md`'s disk policy (disk is *secondary* there — but a save repo has no LRU eviction, so it may need one). |
| Q5 | **Should undo be able to cross a "new game" boundary?** | Today it stops at `C0`. A separate slot per game is the current answer; confirm it stays that way. |
| Q6 | **Cross-slot blob sharing via `alternates`** — always on, or opt-in? | Sharing saves disk but couples slot directories; deleting a slot must not break another's objects. |
| Q7 | **Commit at scale: worker thread + queue, or a dedicated process?** | Leans worker thread; a dedicated "state keeper" process follows the orchestrator pattern if commit contention appears. |
| Q8 | **Does the cursor belong in a ref or in `meta.json`?** | A ref survives a torn working tree better; `meta.json` is simpler and human-readable. Leaning ref. |

---

## Risks

| Risk | Severity | Mitigation |
| --- | --- | --- |
| Save repository grows without bound | **High** | Dedupe + transaction granularity + [Q1](#open-questions)/[Q3](#open-questions)/[Q4](#open-questions). Must be measured before shipping. |
| Commit latency hitches gameplay | Medium | Background commits, debounce, boundary-only commits, atomic-rename-free (git handles it), no mid-frame flush. |
| Embedded libgit2 native binary per platform | Low | ~1–2 MB per architecture; already a small cost next to a model store. |
| Windows AV / OneDrive scanning `.git/` | Medium | Document an exclusion in `docs/SETUP.md`; detect `persistentDataPath` under OneDrive and warn. |
| Corrupt working tree vs. valid commit | Low | The commit is the truth; a torn tree is recovered by materialising the cursor. Detect on launch. |
| `AssetRef` identity change (blob hash vs. GUID) breaks generated refs | Medium | Only *generated* refs move to blob hashes; shipped content keeps GUIDs. Migration is a load-time remap. |
| Player "undoes" and then loses the redo tail without noticing | Low | Discard the tail only on a *new commit*, never on undo; the reflog keeps it for the session. |
| Serialising a save for a bug report is now a repo | Low | `git bundle` produces a single-file, complete, compressed save export. |

---

## Implementation sketch

- **Library:** LibGit2Sharp (MIT). No `git.exe` dependency (G6).
- **Ownership:** a `StateRepo` per slot, owned by the same `ConfigHost` that
  drives autosave in `configuration.md` — it already knows which documents are
  dirty; it now also knows the open transaction.
- **Surface (C#):**

```
StateRepo
  StateRepo Open(slotPath)                    // init if absent, set config, attributes
  void      OpenTransaction(string message)
  void      AddTrailer(string key, string value)
  CommitId  Commit()                          // parent = cursor; main = cursor = new
  void      Rollback()
  bool      CanUndo / bool CanRedo
  void      Undo() / void Redo()               // move cursor, re-materialise tree
  CommitId  Revert(CommitId id)                // new commit undoing id
  IReadOnlyList<CommitInfo> Log(int max)
  void      Tag(string name, CommitId id)      // milestone / restore point
  void      Materialise(CommitId id)           // write the tree to disk
  Task      MaintainAsync()                    // pack + prune (Q3), off the hot path
```

- **Threading:** one background worker owns the repository; gameplay posts
  transactions to it. No git call ever happens on the main thread (Golden Rule 6).
- **Tests** (`experiments/`-style, then PlayMode/EditMode): a committed
  transaction restores byte-identically after undo + redo; N identical commits
  produce zero new objects; a `gen` commit always carries `Seed` and
  `Generator-Version`; a crash mid-transaction resumes at the cursor.

---

## Verification plan

Small, cheap, disk-focused experiments to run before this leaves `exploring`:

| # | Question | Method |
| --- | --- | --- |
| 1 | Does content-addressed dedupe actually hold for generated assets? | Generate the same asset twice with the same seed; assert blob count is unchanged. |
| 2 | What is real repo growth per hour of play (text-only vs. with binaries)? | Scripted session; measure `.git` size, loose vs. packed. Answers [Q1](#open-questions). |
| 3 | Commit latency for a large transaction (a region's worth of meshes). | Time commit on a worker thread; assert no frame over budget. |
| 4 | Does a save remain adoptable by the git CLI for inspection? | `git log`/`git show`/`git bundle` on a real slot repo. |
| 5 | Repack/prune from LibGit2Sharp or an external binary. | Answers [Q3](#open-questions). |

Result pages go in `experiments/` under a `state-versioning/` category, and the
conclusions fold back into this document.

---

## Rejected alternatives (kept for history)

| Rejected | Reason |
| --- | --- |
| Branch-per-timeline, merge to reconcile | The user requirement is explicitly *linear*; branching moves a conflict decision onto the player, and there is only ever one writer. |
| One commit per mutation (per field, per frame) | Unreadable history and unbounded commit cost. Transactions are the unit. |
| `history.ndjson` as the *only* history | An event log can be replayed but not *snapshotted or diffed*; git gives both. |
| Reimplementing a content-addressed store in C# | That is reproducing git's object store, hashing, packing and GC. Use git. |
| Cloud remote for backup / cross-device | Violates Golden Rule 1 (local only). |
