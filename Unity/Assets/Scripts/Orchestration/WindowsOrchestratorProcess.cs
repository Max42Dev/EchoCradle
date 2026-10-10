using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    /// <summary>Fail-closed Windows containment: suspended creation, assign job, then resume.</summary>
    internal sealed class WindowsOrchestratorProcess : IDisposable
    {
        private readonly object _sync = new object();
        private SafeFileHandle _job, _process;
        private FileStream _stdout;
        private bool _disposed;

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int Size;
            public IntPtr Descriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool Inherit;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits
        {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, Scheduling;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits
        {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            public int Size;
            public string Reserved, Desktop, Title;
            public uint X, Y, Width, Height, XChars, YChars, Fill, Flags;
            public ushort Show, ReservedSize;
            public IntPtr ReservedPointer, Input, Output, Error;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct StartupInfoEx
        {
            public StartupInfo Startup;
            public IntPtr Attributes;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInfo
        {
            public IntPtr Process, Thread;
            public uint ProcessId, ThreadId;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(SafeFileHandle job, int information,
            ref ExtendedLimits limits, uint size);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(SafeFileHandle job, SafeFileHandle process);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreatePipe(out SafeFileHandle read, out SafeFileHandle write,
            ref SecurityAttributes attributes, uint size);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetHandleInformation(SafeFileHandle handle, uint mask, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string name, uint access, uint share,
            ref SecurityAttributes attributes, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool InitializeProcThreadAttributeList(IntPtr list, int count,
            uint flags, ref IntPtr size);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags,
            IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")]
        private static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateProcess(string application, StringBuilder command,
            IntPtr processAttributes, IntPtr threadAttributes, [MarshalAs(UnmanagedType.Bool)] bool inherit,
            uint flags, IntPtr environment, string directory, ref StartupInfoEx startup, out ProcessInfo info);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(SafeFileHandle thread);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateProcess(SafeFileHandle process, uint exitCode);
        [DllImport("kernel32.dll")]
        private static extern uint WaitForSingleObject(SafeFileHandle handle, uint milliseconds);

        internal bool Exited
        {
            get
            {
                lock (_sync) return _disposed || _process == null || WaitForSingleObject(_process, 0) == 0;
            }
        }

        internal static WindowsOrchestratorProcess Launch(string python, string root, string token)
        {
            if (Environment.OSVersion.Platform != PlatformID.Win32NT)
                throw new OrchestratorException("WINDOWS_REQUIRED");
            var owner = new WindowsOrchestratorProcess();
            try { owner.Create(python, root, token); return owner; }
            catch (Exception)
            {
                owner.Dispose();
                throw new OrchestratorException("OWNERSHIP_FAILED");
            }
        }

        private void Create(string python, string root, string token)
        {
            if (!Path.IsPathRooted(python) || !File.Exists(python) ||
                !Path.IsPathRooted(root) || !File.Exists(Path.Combine(root, "orchestrator", "service.py")) ||
                python.IndexOf('"') >= 0)
                throw new OrchestratorException("INVALID_PROCESS_CONFIG");
            _job = CreateJobObject(IntPtr.Zero, null);
            var limits = new ExtendedLimits { Basic = new BasicLimits { Flags = 0x2000 } };
            if (_job.IsInvalid || !SetInformationJobObject(_job, 9, ref limits,
                (uint)Marshal.SizeOf(typeof(ExtendedLimits))))
                throw new OrchestratorException("OWNERSHIP_FAILED");
            var security = new SecurityAttributes
            {
                Size = Marshal.SizeOf(typeof(SecurityAttributes)), Inherit = true
            };
            SafeFileHandle read = null, write = null, nul = null;
            IntPtr list = IntPtr.Zero, handles = IntPtr.Zero, environment = IntPtr.Zero;
            bool initialized = false;
            try
            {
                if (!CreatePipe(out read, out write, ref security, 4096) ||
                    !SetHandleInformation(read, 1, 0)) throw new OrchestratorException("OWNERSHIP_FAILED");
                // Suppress native stderr entirely: never forward arbitrary secret-bearing output to Unity logs.
                nul = CreateFile("NUL", 0xC0000000, 3, ref security, 3, 0, IntPtr.Zero);
                if (nul.IsInvalid) throw new OrchestratorException("OWNERSHIP_FAILED");
                IntPtr size = IntPtr.Zero;
                InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
                if (size == IntPtr.Zero) throw new OrchestratorException("OWNERSHIP_FAILED");
                list = Marshal.AllocHGlobal(size);
                if (!InitializeProcThreadAttributeList(list, 1, 0, ref size))
                    throw new OrchestratorException("OWNERSHIP_FAILED");
                initialized = true;
                handles = Marshal.AllocHGlobal(2 * IntPtr.Size);
                Marshal.WriteIntPtr(handles, 0, write.DangerousGetHandle());
                Marshal.WriteIntPtr(handles, IntPtr.Size, nul.DangerousGetHandle());
                // Inherit ONLY stdout and NUL, never Unity's other inheritable handles.
                if (!UpdateProcThreadAttribute(list, 0, new IntPtr(0x20002), handles,
                    new IntPtr(2 * IntPtr.Size), IntPtr.Zero, IntPtr.Zero))
                    throw new OrchestratorException("OWNERSHIP_FAILED");
                var variables = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
                {
                    string key = (string)entry.Key;
                    if (key.IndexOf('=') < 0) variables[key] = (string)entry.Value;
                }
                variables["ECHOCRADLE_SERVICE_TOKEN"] = token;
                variables["PYTHONUNBUFFERED"] = "1";
                variables["PYTHONPATH"] = root;
                var block = new StringBuilder();
                foreach (KeyValuePair<string, string> pair in variables)
                    block.Append(pair.Key).Append('=').Append(pair.Value).Append('\0');
                block.Append('\0');
                environment = Marshal.StringToHGlobalUni(block.ToString());
                var startup = new StartupInfoEx
                {
                    Attributes = list,
                    Startup = new StartupInfo
                    {
                        Size = Marshal.SizeOf(typeof(StartupInfoEx)), Flags = 0x100,
                        Input = nul.DangerousGetHandle(), Error = nul.DangerousGetHandle(),
                        Output = write.DangerousGetHandle()
                    }
                };
                // CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW.
                var command = new StringBuilder("\"" + python + "\" -m orchestrator.service --profile voice --port 0");
                if (!CreateProcess(python, command, IntPtr.Zero, IntPtr.Zero, true,
                    0x08080404, environment, root, ref startup, out ProcessInfo info))
                    throw new OrchestratorException("OWNERSHIP_FAILED");
                _process = new SafeFileHandle(info.Process, true);
                using (var thread = new SafeFileHandle(info.Thread, true))
                {
                    // No service instruction can run or spawn llama before job assignment succeeds.
                    if (!AssignProcessToJobObject(_job, _process) || ResumeThread(thread) == uint.MaxValue)
                        throw new OrchestratorException("OWNERSHIP_FAILED");
                }
                _stdout = new FileStream(read, FileAccess.Read, 4096, false);
                read = null; // Stream owns this handle.
            }
            finally
            {
                read?.Dispose();
                write?.Dispose();
                nul?.Dispose();
                if (initialized) DeleteProcThreadAttributeList(list);
                if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
                if (handles != IntPtr.Zero) Marshal.FreeHGlobal(handles);
                if (environment != IntPtr.Zero)
                {
                    // Clear the unmanaged environment block containing the token before freeing it.
                    int length = 0;
                    while (Marshal.ReadInt16(environment, length * 2) != 0 ||
                        Marshal.ReadInt16(environment, (length + 1) * 2) != 0) length++;
                    for (int i = 0; i < length + 2; i++) Marshal.WriteInt16(environment, i * 2, 0);
                    Marshal.FreeHGlobal(environment);
                }
            }
        }

        internal async Task<JObject> BootstrapAsync(CancellationToken token)
        {
            Task<JObject> read = Task.Run(() =>
            {
                try
                {
                    using (var bytes = new MemoryStream())
                    {
                        while (bytes.Length <= OrchestratorWire.JsonLimit)
                        {
                            int value = _stdout.ReadByte();
                            if (value < 0) throw new OrchestratorException("BOOTSTRAP_FAILED");
                            if (value == '\n') return OrchestratorWire.Decode(bytes.ToArray());
                            bytes.WriteByte((byte)value);
                        }
                        throw new OrchestratorException("PAYLOAD_LIMIT");
                    }
                }
                catch (Exception) { throw new OrchestratorException("BOOTSTRAP_FAILED"); }
            });
            var cancelled = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(token))
            {
                timeout.CancelAfter(TimeSpan.FromSeconds(90));
                using (timeout.Token.Register(() => cancelled.TrySetResult(true)))
                {
                    if (await Task.WhenAny(read, cancelled.Task).ConfigureAwait(false) != read)
                    {
                        Dispose(); // Close job first: kills writers and unblocks the bounded pipe reader.
                        try { await read.ConfigureAwait(false); } catch (Exception) { }
                        token.ThrowIfCancellationRequested();
                        throw new OrchestratorException("BOOTSTRAP_TIMEOUT");
                    }
                    JObject bootstrap = await read.ConfigureAwait(false);
                    _stdout.Dispose();
                    _stdout = null;
                    return bootstrap;
                }
            }
        }

        public void Dispose()
        {
            lock (_sync)
            {
                if (_disposed) return;
                _disposed = true;
                _job?.Dispose(); // Kills descendants even if the service parent already exited.
                if (_process != null && !_process.IsInvalid)
                {
                    // Also handles an assignment failure while the child is still suspended.
                    TerminateProcess(_process, 1);
                    _process.Dispose();
                }
                // Do not close the stream while a synchronous read holds its lock; process death
                // unblocks it. The bootstrap reader/owner performs final stream disposal.
            }
        }

        internal void CloseOutput()
        {
            _stdout?.Dispose();
            _stdout = null;
        }
    }
}