# Official MCP SDK tool compiler

Local .NET 10 utility using `ModelContextProtocol.Core` 2.2.0. The Unity player
does not reference the SDK or these tooling assemblies.

- Source: `Unity/Assets/Scripts/Interview/InterviewState.cs`.
- Metadata: official `[McpServerTool]`, standard `[Description]`, typed parameters.
- Schema: official `McpServerTool.Create(...).ProtocolTool.InputSchema`.
- Output: committed `InterviewToolBindings.g.cs` with declarations/direct dispatch.
- Invocation: Unity's existing authenticated orchestrator session callback.

From the repository root, `dotnet run --project tools/ToolDefinitions/ToolDefinitions.csproj`
regenerates the artifact. Append `-- --check` for a read-only CI freshness check,
or `-- --print` to inspect the generated source. `dotnet restore
tools/ToolDefinitions/ToolDefinitions.csproj --locked-mode` enforces the dependency lock.

The generator never invokes tools. It rejects signatures other than synchronous
`JObject` results with required string arguments. To support other types, extend
the bounded generator/binder and add tests rather than silently converting input.
Renaming a method or parameters regenerates both metadata and typed dispatch.
Domain field constraints remain in the interview schema, validated at invocation.

Unity rejects Play/build when the source fingerprint changes without regeneration.
CI should additionally run `--check`, which checks the full generated artifact.
Tool source is linked into the generator; `ECHOCRADLE_TOOL_GENERATOR` enables SDK
attributes only there. No custom replacement MCP attributes are introduced.

This is MCP **tool-definition** integration. It does not implement `tools/list`,
`tools/call`, or an MCP transport endpoint. Model/audio jobs continue to use the
existing REST/WebSocket service; an adapter wraps the SDK metadata in its function
declaration format. No cloud inference, server process or SDK runtime player
dependency is added. Unity IL2CPP compatibility of the SDK itself is unverified.