---
name: git-workflow
description: 'Manage version control for the game project with Git. Use when committing, branching, writing commit messages, resolving conflicts, managing large binary assets (LFS), or preparing releases. Triggers: git, commit, branch, merge, conflict, git lfs, large files, .gitignore, release, tag.'
---

# Git Workflow

## When to Use
- Committing changes or writing commit messages
- Creating/merging branches
- Handling large binary assets (models, textures, audio)
- Resolving conflicts or preparing releases

## Commit Conventions
Use Conventional Commits:
```
<type>(<scope>): <subject>

<body>

<footer>
```
Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `chore`, `build`, `ci`.
Examples:
- `feat(combat): add stamina drain on sprint`
- `fix(ai): prevent agents from pathing through walls`
- `chore(assets): import dungeon tile set`

Rules: imperative mood, ≤72-char subject, explain *why* in the body.

## Branching
- `main` — always shippable.
- `feat/<topic>`, `fix/<topic>`, `chore/<topic>` — short-lived.
- Rebase feature branches on `main` before merging; keep history linear.
- Delete branches after merge.

## Large Binary Assets (Unity + Blender)
- Enable **Git LFS** for: `*.psd *.png *.tga *.fbx *.blend *.glb *.wav *.mp3 *.mp4 *.unity *.asset`
- Track via `.gitattributes`:
  ```
  *.fbx filter=lfs diff=lfs merge=lfs -text
  *.blend filter=lfs diff=lfs merge=lfs -text
  *.png filter=lfs diff=lfs merge=lfs -text
  ```
- Never commit `Library/`, `Temp/`, `Obj/`, `Build/`, `Logs/`, `UserSettings/`.
- Commit `.meta` files **with** their assets — never separately or never at all.

## Unity `.gitignore` Essentials
```
[Ll]ibrary/
[Tt]emp/
[Oo]bj/
[Bb]uild/
[Bb]uilds/
[Ll]ogs/
[Uu]serSettings/
*.csproj
*.sln
*.user
```

## Procedure
1. `git status` and `git diff` — review before staging.
2. Stage logically related changes; avoid mixing refactors with features.
3. Write a Conventional Commit message.
4. Run tests/build before committing.
5. Push and open a PR for review.

## Conflict Resolution
- Prefer rebase for feature branches; merge for shared branches.
- For binary conflicts, pick one side deliberately — never hand-merge binaries.
- For `.meta` conflicts, keep the GUID consistent with the asset.

## Pitfalls
- Committing `Library/` bloats the repo massively.
- Missing `.meta` files break references for teammates.
- Large binaries without LFS make clones unusable.
- Force-pushing shared branches loses others' work.

## References
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Git LFS](https://git-lfs.com/)
- [GitHub Unity .gitignore](https://github.com/github/gitignore/blob/main/Unity.gitignore)
