---
name: unity-mcp-editor-control
description: 'Drive the Unity Editor through the Unity MCP server. Use when creating scenes, GameObjects, components, materials, prefabs, or assets; editing and compiling C# scripts; running Unity tests; inspecting the scene hierarchy; or automating Unity workflows from chat. Triggers: Unity MCP, create GameObject, scene hierarchy, add component, material, prefab, run Unity tests, Unity Editor automation.'
---

# Unity MCP Editor Control

## When to Use
- Creating or modifying scenes, GameObjects, components, and materials
- Generating and compiling C# scripts inside a live Unity project
- Inspecting the scene hierarchy or project assets
- Running Unity Test Framework tests and reading results
- Automating repetitive editor tasks

## Prerequisites
- Unity Editor installed and the project open (see `docs/SETUP.md`).
- The Unity MCP plugin installed in the project and the MCP server configured in `.vscode/mcp.json`.
- The MCP server connected (Unity → `Window → MCP for Unity` / `AI Game Developer`).

## Procedure
1. **Confirm connection.** List available MCP tools; if Unity tools are missing, the editor or server is not running.
2. **Inspect before mutating.** Read the scene hierarchy and existing assets so you don't duplicate or clobber.
3. **Make one logical change at a time.** Create → configure → verify. Unity state is shared and order matters.
4. **Prefer scripts over manual object edits** for anything reproducible: generate a C# script, then let Unity compile it.
5. **Verify.** Re-read the hierarchy/asset state, or run a test, after each change.
6. **Save the scene** when the change set is complete.

## Working Rules
- Never assume a GameObject/asset exists — query first.
- Use the project's existing naming and folder conventions (`Assets/Scripts`, `Assets/Prefabs`, `Assets/Data`).
- When creating many objects, prefer a single editor script over many individual tool calls.
- After editing C# scripts, wait for compilation and check for errors before proceeding.
- Keep changes small and reversible; commit working states to git.

## Typical Tasks
| Goal | Approach |
|------|----------|
| New gameplay object | Create GameObject → add components → set serialized fields → save as prefab |
| New tunable data | Create a `ScriptableObject` asset under `Assets/Data` |
| New system | Generate C# script → compile → attach to a manager object |
| Verify behavior | Run Unity Test Framework tests via MCP |
| Inspect state | Read hierarchy + component properties |

## Pitfalls
- Editing files while Unity is compiling can cause stale state — wait for the compile to finish.
- Scene changes are not persisted until saved.
- The MCP server and editor must agree on the port; a mismatch shows as "not connected".
- Only run one MCP server instance per Unity project.

## References
- [MCP for Unity docs](https://coplaydev.github.io/unity-mcp/)
- [Unity MCP (IvanMurzak) docs](https://github.com/IvanMurzak/Unity-MCP)
