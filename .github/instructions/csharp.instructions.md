---
description: 'C# coding conventions for the EchoCradle Unity project.'
applyTo: '**/*.cs'
---

# C# Conventions (EchoCradle)

## Style
- `PascalCase` for types, methods, properties, events, constants.
- `camelCase` for locals, parameters, and `[SerializeField] private` fields.
- `_camelCase` for private instance fields.
- One type per file; file name matches the type name.
- Use `var` only when the type is obvious from the right-hand side.
- Prefer expression-bodied members for trivial one-liners.
- Use file-scoped namespaces (`namespace EchoCradle.Combat;`).

## Unity specifics
- `[SerializeField] private` instead of `public` fields.
- Cache component references in `Awake()`; never `GetComponent` in `Update()`.
- Add `[RequireComponent]` for hard dependencies and `[Tooltip]` for Inspector clarity.
- No allocations in `Update`/`FixedUpdate`/`LateUpdate` (no LINQ, `new`, string concat, closures).
- Unsubscribe from events in `OnDisable`/`OnDestroy`.
- Physics in `FixedUpdate`; camera/IK in `LateUpdate`.
- Use `CompareTag` instead of `tag ==`.
- Prefer object pooling over `Instantiate`/`Destroy` churn.

## Async
- Use `UniTask` or `Awaitable` for async; never `async void` except event handlers.
- Always handle exceptions in async methods.
- Never block the main thread on I/O, LLM, or network calls.

## Data
- Tunable data in `ScriptableObject`s under `Assets/Data`.
- Runtime state in plain C# classes; no Unity objects in save data.
- Validate all external/LLM data against a schema before use.

## Naming assets
- `SM_` static mesh, `SK_` skeletal mesh, `M_` material, `T_` texture.
