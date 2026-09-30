---
name: unity-testing
description: 'Write and run automated tests for Unity projects using the Unity Test Framework (EditMode and PlayMode) and NUnit. Use when adding unit tests, integration tests, or play-mode tests; setting up test assemblies; or validating gameplay systems and generated content. Triggers: Unity Test Framework, NUnit, EditMode test, PlayMode test, unit test, integration test, test assembly, asmdef, run tests.'
---

# Unity Testing

## When to Use
- Adding unit tests for pure C# logic
- Adding PlayMode tests for runtime behavior
- Setting up test assemblies (`asmdef`)
- Validating generated content (levels, AI decisions, data schemas)

## Test Types
| Type | Runs | Use for |
|------|------|---------|
| EditMode | In editor, no play | Pure logic, data, editor tools |
| PlayMode | In play mode | Components, physics, coroutines, scenes |

## Setup
1. Install **Test Framework** via Package Manager (usually present by default).
2. Create test assemblies:
   - `Assets/Tests/EditMode/` with an `.asmdef` referencing `UnityEngine.TestRunner`, `UnityEditor.TestRunner`, and your runtime asmdef.
   - `Assets/Tests/PlayMode/` similarly, with `"includePlatforms": []`.
3. Keep runtime code in its own asmdef so tests can reference it.

## Writing Tests
```csharp
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;
using System.Collections;

public class HealthTests
{
    [Test]
    public void TakeDamage_ReducesHealth()
    {
        var health = new Health(max: 100);
        health.TakeDamage(30);
        Assert.AreEqual(70, health.Current);
    }

    [UnityTest]
    public IEnumerator Mover_ReachesTarget()
    {
        var go = new GameObject();
        var mover = go.AddComponent<Mover>();
        mover.Target = new Vector3(1, 0, 0);
        yield return new WaitForSeconds(1f);
        Assert.Less(Vector3.Distance(go.transform.position, mover.Target), 0.1f);
        Object.Destroy(go);
    }
}
```

## Conventions
- **Arrange / Act / Assert** structure.
- One behavior per test; descriptive names (`Method_Scenario_Expected`).
- Test pure logic in EditMode (fast); reserve PlayMode for integration.
- Use `[SetUp]`/`[TearDown]` to create/destroy objects; never leak GameObjects.
- Avoid `Thread.Sleep`; use `yield return null` / `WaitForSeconds`.
- Deterministic: seed RNG, avoid real time where possible.

## Running
- **Editor:** `Window → General → Test Runner` → Run All.
- **CLI:** `Unity.exe -batchmode -runTests -testPlatform EditMode -projectPath <path> -testResults results.xml`
- **Via MCP:** ask the Unity MCP to run tests and report results.

## Testing Generated Content
- Validate schemas of LLM output (JSON shape, required fields, ranges).
- Assert level generators produce connected, valid layouts for many seeds.
- Snapshot-test AI decisions for fixed inputs.

## Pitfalls
- PlayMode tests are slow — keep them few and focused.
- Forgetting to destroy created objects → cross-test contamination.
- Referencing editor-only APIs from PlayMode tests → compile errors.
- Flaky timing tests → assert ranges, not exact frames.

## References
- [Unity Test Framework](https://docs.unity3d.com/Packages/com.unity.test-framework@latest)
- [NUnit docs](https://docs.nunit.org/)
