using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Xml;
using NUnit.Framework.Api;
using NUnit.Framework.Interfaces;
using NUnit.Framework.Internal;
using NUnit.Framework.Internal.Execution;
using UnityEditor;
using UnityEngine;

namespace EchoCradle.Editor
{
    /// <summary>
    /// Diagnostic runner for ordinary project NUnit tests inside the real engine.
    /// Deliberately does not enter UTF's package-global prebuild pipeline (the
    /// performance package hangs there). Not a replacement for UTF/PlayMode runs.
    /// Explicit model tests retain NUnit's normal skipped results. Unity coroutine
    /// tests/setups are rejected rather than silently omitted or reported as passed.
    /// </summary>
    public static class ProjectTests
    {
        public static void Run()
        {
            int exitCode = 2;
            SynchronizationContext previousContext = SynchronizationContext.Current;
            try
            {
                Assembly assembly = AppDomain.CurrentDomain.GetAssemblies()
                    .Single(candidate => candidate.GetName().Name == "EchoCradle.Tests");
                foreach (Type type in assembly.GetTypes())
                foreach (MethodInfo method in type.GetMethods(BindingFlags.Instance | BindingFlags.Static |
                    BindingFlags.Public | BindingFlags.NonPublic))
                {
                    if (method.GetCustomAttributes(true).Any(attribute =>
                        attribute.GetType().Name == "UnityTestAttribute" ||
                        attribute.GetType().Name == "UnitySetUpAttribute" ||
                        attribute.GetType().Name == "UnityTearDownAttribute") ||
                        (typeof(IEnumerator).IsAssignableFrom(method.ReturnType) &&
                         method.GetCustomAttributes(true).Any(attribute => attribute is NUnit.Framework.TestAttribute)))
                        throw new NotSupportedException("Use UTF for coroutine tests: " + type.FullName + "." + method.Name);
                }

                string output = Path.GetFullPath(Path.Combine(Application.dataPath,
                    "../Logs/project-nunit-tests.xml"));
                Directory.CreateDirectory(Path.GetDirectoryName(output));
                var runner = new NUnitTestAssemblyRunner(new DefaultTestAssemblyBuilder());
                runner.Load(assembly, new Dictionary<string, object>
                {
                    ["NumberOfTestWorkers"] = 0,
                    ["SynchronousEvents"] = true,
                    // NUnit enforces timeouts using a separate thread, which
                    // cannot call native Unity lifecycle APIs. No default timeout.
                    ["WorkDirectory"] = Path.GetDirectoryName(output)
                });
                // NUnit's own single-threaded async pump must own continuations;
                // blocking on UnitySynchronizationContext would deadlock the editor.
                SynchronizationContext.SetSynchronizationContext(null);
                var context = new TestExecutionContext
                {
                    IsSingleThreaded = true,
                    Dispatcher = new InlineTestDispatcher(),
                    WorkDirectory = Path.GetDirectoryName(output)
                };
                Debug.Log("ProjectTests: actual Unity engine; assembly=" + assembly.Location +
                    "; discovered=" + runner.LoadedTest.TestCaseCount +
                    "; bypass=all UTF prebuild hooks; explicit models not selected.");
                // This Unity NUnit fork's SimpleWorkItemDispatcher always creates
                // a thread, even with zero workers. Execute its real work-item
                // tree inline instead; no test bodies or results are substituted.
                WorkItem work = WorkItem.CreateWorkItem(runner.LoadedTest, TestFilter.Empty);
                work.InitializeContext(context);
                work.Execute();
                ITestResult result = work.Result;
                using (XmlWriter writer = XmlWriter.Create(output, new XmlWriterSettings { Indent = true }))
                    result.ToXml(true).WriteTo(writer);
                Debug.Log("ProjectTests: passed=" + result.PassCount + "; failed=" + result.FailCount +
                    "; skipped=" + result.SkipCount + "; inconclusive=" + result.InconclusiveCount +
                    "; XML=" + output);
                exitCode = result.FailCount == 0 && result.PassCount > 0 && result.InconclusiveCount == 0 ? 0 : 1;
            }
            catch (Exception error) { Debug.LogException(error); }
            finally
            {
                SynchronizationContext.SetSynchronizationContext(previousContext);
                EditorApplication.Exit(exitCode);
            }
        }
    }
}