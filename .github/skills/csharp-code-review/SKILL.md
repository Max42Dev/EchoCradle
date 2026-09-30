---
name: csharp-code-review
description: 'Review C# and Unity code for correctness, performance, security, and maintainability. Use when reviewing a diff or file, before committing, when asked for a code review, or when checking for Unity-specific anti-patterns, allocations, null handling, and API misuse. Triggers: code review, review my code, check this diff, find bugs, performance review, refactor, best practices, C# review.'
---

# C# / Unity Code Review

## When to Use
- Reviewing a diff, file, or pull request
- Before committing or merging
- Checking for Unity-specific anti-patterns
- Assessing performance, security, or maintainability

## Review Checklist

### Correctness
- Null handling: serialized fields, `GetComponent`, async results, event targets.
- Off-by-one and boundary conditions in loops and indices.
- Correct lifecycle usage (`Awake`/`Start`/`OnEnable`/`OnDisable`).
- Exceptions handled at the right level; no swallowed errors.
- Determinism where required (seeded RNG, stable ordering).

### Unity-specific
- No `GetComponent`/`FindObjectOfType` in `Update`.
- No per-frame allocations (`new`, LINQ, string concat, boxing, closures).
- `Time.deltaTime`/`fixedDeltaTime` used correctly.
- Events unsubscribed in `OnDisable`/`OnDestroy`.
- `[SerializeField] private` over public fields.
- Physics in `FixedUpdate`; camera/IK in `LateUpdate`.
- Prefabs/pooling instead of `Instantiate`/`Destroy` churn.

### Performance
- Hot paths free of allocations and virtual calls where it matters.
- Collections sized appropriately; no repeated `List` growth in loops.
- Async/threading used for I/O; main thread kept free.
- Profiler evidence for any claimed optimization.

### Security
- No secrets, tokens, or keys in code or assets.
- Input validated; no `eval`-style dynamic execution of untrusted data.
- File/network access scoped and sanitized.
- LLM output treated as untrusted (validated, never executed blindly).

### Maintainability
- Single responsibility; clear naming; consistent style.
- No dead code, commented-out blocks, or magic numbers.
- Public APIs documented; complex logic commented.
- Tests cover the behavior, not the implementation.

## Procedure
1. Read the change and its intent (commit message / PR description).
2. Check correctness first, then Unity-specific issues, then performance/security.
3. Cite the exact file and line for each finding.
4. Classify: **blocker**, **should-fix**, **nit**.
5. Suggest a concrete fix, not just a complaint.
6. Confirm the change builds and tests pass.

## Output Format
```
## Review: <file/PR>
### Blockers
- `path:line` — <issue> → <fix>
### Should-fix
- ...
### Nits
- ...
### Verdict
Approve / Request changes
```

## Pitfalls
- Reviewing style before correctness.
- Vague feedback without a concrete fix.
- Missing the Unity context (e.g. flagging `Update` code that runs once).
- Approving without checking that it compiles and tests pass.
