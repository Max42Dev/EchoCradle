---
name: npc-monster-ai
description: 'Design and implement NPC and monster AI for the RPG, including behavior trees, state machines, utility AI, perception, pathfinding, combat tactics, and LLM-driven dialogue/decision making. Use when building enemy behaviors, NPC schedules, aggro/perception, NavMesh movement, or hybrid scripted+LLM agents. Triggers: NPC AI, monster AI, behavior tree, state machine, utility AI, NavMesh, pathfinding, perception, aggro, combat AI, LLM agent, dialogue.'
---

# NPC & Monster AI

## When to Use
- Implementing enemy combat, patrol, flee, or pack behaviors
- Building NPC schedules, routines, and social reactions
- Adding perception (sight, hearing, memory) and aggro
- Pathfinding and navigation with NavMesh
- Hybrid AI where an LLM drives dialogue or high-level decisions

## Architecture Choices
| Approach | Best for |
|----------|----------|
| Finite State Machine | Simple, predictable behaviors |
| Behavior Tree | Composable, designer-friendly combat AI |
| Utility AI | Many competing goals, emergent choices |
| GOAP / Planner | Multi-step goals with preconditions |
| LLM agent | Dialogue, barks, high-level tactics, quest reactions |

**Recommended split:** deterministic systems (FSM/BT/utility) for moment-to-moment combat and movement; LLM for language and strategic intent. Never let an LLM block the frame.

## Core Systems
- **Perception:** vision cone (angle + range + line-of-sight raycast), hearing radius, last-known-position memory with decay.
- **Navigation:** NavMeshAgent for movement; `SetDestination` sparingly; use `NavMeshPath` for prediction.
- **Senses → Blackboard:** write perceived facts to a shared blackboard the decision layer reads.
- **Decision layer:** BT/utility selects actions; actions are small, testable units.
- **Animation:** drive Animator parameters from the decision layer, not vice versa.

## Procedure
1. Define the agent's **goals** and **states** in plain language.
2. Choose the architecture (FSM for simple, BT/utility for complex).
3. Implement perception and a blackboard.
4. Implement actions (move, attack, flee, idle, interact) as isolated units.
5. Wire the decision layer to select actions from blackboard state.
6. Add NavMesh movement and animation hooks.
7. Tune with debug gizmos (draw vision cones, paths, current state).
8. Test edge cases: no path, target lost, multiple agents, low FPS.

## LLM-Driven NPCs
- Use the LLM for **dialogue, barks, and intent**, not per-frame decisions.
- Send a compact context: personality, relationship, recent events, available actions.
- Request structured output (e.g. `{ "action": "trade", "dialogue": "..." }`).
- Cache and pre-generate common lines; always have scripted fallbacks.
- Rate-limit and run off the main thread; stream dialogue to the UI.

## Performance
- Stagger AI ticks across frames (time-slicing); don't update every agent every frame.
- LOD AI: distant agents use coarse logic and no animation.
- Pool agents; avoid allocations in the decision loop.
- Cap pathfinding requests per frame.

## Pitfalls
- Every agent pathfinding every frame → time-slice and cache paths.
- LLM calls on the main thread → freeze; use async + fallback.
- Perception without memory → agents forget instantly; add decay.
- Overly complex BTs → keep actions small and observable.

## References
- [Unity NavMesh](https://docs.unity3d.com/Manual/Navigation.html)
- [Behavior trees primer](https://www.behaviortree.dev/)
