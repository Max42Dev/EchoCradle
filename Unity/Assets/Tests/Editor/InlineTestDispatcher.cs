using NUnit.Framework.Internal.Execution;

namespace EchoCradle.Editor
{
    /// <summary>Keep diagnostic NUnit work items on Unity's calling engine thread.</summary>
    internal sealed class InlineTestDispatcher : IWorkItemDispatcher
    {
        public void Dispatch(WorkItem work) => work.Execute();

        public void CancelRun(bool force)
        {
            throw new System.NotSupportedException("Stop the diagnostic editor process to cancel this inline run.");
        }
    }
}