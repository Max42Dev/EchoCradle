---
name: unity-csharp-development
description: 'Write, refactor, and debug Unity C# gameplay code. Use when implementing MonoBehaviours, ScriptableObjects, components, coroutines, async/await, serialization, events, or fixing Unity-specific C# issues (null refs on serialized fields, GC spikes, Update loops, physics, input). Triggers: Unity, MonoBehaviour, ScriptableObject, GameObject, prefab, coroutine, UnityEvent, SerializeField, Rigidbody, NavMesh, Addressables.'
---

# Unity C# Development

## When to Use
- Implementing or refactoring gameplay systems in C#
- Creating components, ScriptableObjects, or editor tooling
- Diagnosing Unity-specific runtime issues (GC, frame spikes, lifecycle bugs)
- Wiring input, physics, animation, or UI

## Core Conventions

### Component design
- One responsibility per `MonoBehaviour`. Prefer composition over inheritance.
- Cache component references in `Awake()`; never call `GetComponent` in `Update()`.
- Use `[SerializeField] private` instead of `public` fields — keeps encapsulation while exposing to the Inspector.
- Use `[RequireComponent(typeof(X))]` to enforce dependencies.
- Use `[Tooltip]` and `[Header]` to make the Inspector self-documenting.

### Lifecycle order (memorize)
`Awake` → `OnEnable` → `Start` → `FixedUpdate` (physics) → `Update` → `LateUpdate` → `OnDisable` → `OnDestroy`.
- Physics and `Rigidbody` movement → `FixedUpdate` + `Time.fixedDeltaTime`.
- Camera follow / IK → `LateUpdate`.
- Never do heavy work in `Update`; gate with timers or events.

### Data-driven design
- Put tunable game data in `ScriptableObject` assets (items, enemies, abilities, dialogue).
- Keep runtime state in plain C# classes; keep Unity objects out of save data.
- Use `[CreateAssetMenu]` for designer-friendly assets.

### Events
- Prefer C# `event`/`Action` for internal systems; `UnityEvent` for designer-wired Inspector hooks.
- Always unsubscribe in `OnDisable`/`OnDestroy` to avoid leaks and null-target exceptions.
- For cross-scene/global messaging, use a lightweight event bus or `ScriptableObject`-based channels.

### Async & coroutines
- Coroutines for frame-spread work tied to a GameObject's lifetime.
- `async`/`await` with `UniTask` (recommended) or `Awaitable` (Unity 2023+) for I/O, network, and LLM calls.
- Never `async void` except event handlers; always handle exceptions.

### Performance
- Avoid per-frame allocations: no `new`, LINQ, string concat, or boxing in `Update`.
- Use `StringBuilder`, object pooling (`UnityEngine.Pool`), and `NonAlloc` physics queries.
- Use `Profiler` and `ProfilerMarker` to measure before optimizing.
- Prefer `CompareTag` over `tag ==`, and cache `Transform` references.

## Procedure
1. Identify the system boundary and its data (ScriptableObject vs runtime state).
2. Define interfaces/events between systems before writing implementations.
3. Implement the smallest working component; wire it in a scene or prefab.
4. Add `[SerializeField]` tuning knobs and tooltips.
5. Validate with the Unity MCP (see `unity-mcp-editor-control`) or Unity Test Framework.
6. Profile if the code runs per-frame.

## Common Pitfalls
- `NullReferenceException` on serialized fields → the field was never assigned in the Inspector; add a guard or `[RequireComponent]`.
- Modifying a collection while iterating → iterate a copy or use a reverse `for` loop.
- `Instantiate`/`Destroy` churn → pool instead.
- `FindObjectOfType` in `Update` → cache it.
- Forgetting `Time.deltaTime` → frame-rate-dependent movement.
- Editing assets at runtime → changes don't persist; use editor scripts for that.

## References
- [Unity scripting best practices](https://docs.unity3d.com/Manual/BestPracticeGuides.html)
- [Unity C# style guide](https://unity.com/resources/c-sharp-style-guide-unity-6)
