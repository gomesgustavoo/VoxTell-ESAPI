using System;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Marshals work onto the one thread that is allowed to touch a thread-affine object model.
    ///
    /// <see cref="EsapiGate"/> is the real implementation, hopping onto the thread that owns the
    /// Eclipse <c>ScriptContext</c>. The encoder depends on this interface rather than on that
    /// class so the upload path can also run in the console harness, where there is no UI thread
    /// to marshal to and the pass-through implementation is correct.
    /// </summary>
    public interface IThreadGate
    {
        /// <summary>True when the caller is already on the owning thread.</summary>
        bool OnEsapiThread { get; }

        /// <summary>
        /// Throws if the caller is not on the owning thread. <paramref name="what"/> names the
        /// operation, so the message points at the offending call rather than at the gate.
        /// </summary>
        void AssertOnEsapiThread(string what);

        /// <summary>Runs <paramref name="action"/> on the owning thread and waits for it.</summary>
        void Run(Action action);

        /// <summary>Runs <paramref name="func"/> on the owning thread and returns its result.</summary>
        T Run<T>(Func<T> func);
    }

    /// <summary>
    /// A gate with no thread affinity at all: everything runs inline on the caller's thread.
    ///
    /// For the console harness, which has no Eclipse object model to protect. Never use this in
    /// the plugin — it would let ESAPI calls run on a pool thread, which is exactly the
    /// corruption <see cref="EsapiGate"/> exists to prevent.
    /// </summary>
    public sealed class DirectGate : IThreadGate
    {
        public bool OnEsapiThread { get { return true; } }

        public void AssertOnEsapiThread(string what) { }

        public void Run(Action action) { action(); }

        public T Run<T>(Func<T> func) { return func(); }
    }
}
