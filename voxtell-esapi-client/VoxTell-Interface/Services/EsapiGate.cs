using System;
using System.Runtime.ExceptionServices;
using System.Threading;
using System.Windows.Threading;

namespace VoxTell_Interface.Services
{
    /// <summary>
    /// Marshals work onto the thread that owns the ESAPI <c>ScriptContext</c>.
    ///
    /// ESAPI objects are thread-affine: every read of an <c>Image</c> and every write to a
    /// <c>Structure</c> must happen on the thread Eclipse called <c>Script.Execute</c> on.
    /// v1 respected that, but only incidentally — geometry was read before the first
    /// <c>await</c>, and voxel/structure work was wrapped in an ad-hoc <c>Control.Invoke</c>
    /// helper. One refactor that moved an <c>await</c> earlier would have broken it silently.
    /// This type makes the rule checkable: <see cref="AssertOnEsapiThread"/> at the top of
    /// anything that touches ESAPI turns a subtle corruption into an immediate exception.
    /// </summary>
    public sealed class EsapiGate : IThreadGate
    {
        private readonly int _esapiThreadId;
        private readonly SynchronizationContext _context;
        private readonly Dispatcher _fallbackDispatcher;

        /// <summary>
        /// The WPF constructor. Must be called ON the ESAPI thread — i.e. from the view's
        /// constructor, which Eclipse reaches synchronously through <c>Script.Execute</c>.
        ///
        /// A <see cref="Dispatcher"/> is a strictly better fallback than a <c>Control</c>: it
        /// needs no window handle, so the "no handle yet" failure the WinForms path has to guard
        /// against cannot arise. Pass <c>Dispatcher.CurrentDispatcher</c>.
        /// </summary>
        public EsapiGate(Dispatcher owner)
        {
            if (owner == null) throw new ArgumentNullException("owner");

            _esapiThreadId = Thread.CurrentThread.ManagedThreadId;
            _fallbackDispatcher = owner;
            _context = SynchronizationContext.Current;
        }

        /// <summary>True when the caller is already on the ESAPI thread.</summary>
        public bool OnEsapiThread
        {
            get { return Thread.CurrentThread.ManagedThreadId == _esapiThreadId; }
        }

        /// <summary>
        /// Guards a method that reads or writes ESAPI objects. <paramref name="what"/> names the
        /// operation so the message points at the offending call rather than at this helper.
        /// </summary>
        public void AssertOnEsapiThread(string what)
        {
            if (!OnEsapiThread)
            {
                throw new InvalidOperationException(string.Format(
                    "{0} touches the Eclipse object model and must run on the ESAPI thread " +
                    "(id {1}), but is running on thread {2}. Wrap the call in EsapiGate.Run.",
                    what, _esapiThreadId, Thread.CurrentThread.ManagedThreadId));
            }
        }

        /// <summary>Runs <paramref name="action"/> on the ESAPI thread and waits for it.</summary>
        public void Run(Action action)
        {
            if (action == null) throw new ArgumentNullException("action");
            Run<object>(() => { action(); return null; });
        }

        /// <summary>Runs <paramref name="func"/> on the ESAPI thread and returns its result.</summary>
        public T Run<T>(Func<T> func)
        {
            if (func == null) throw new ArgumentNullException("func");

            if (OnEsapiThread)
                return func();

            T result = default(T);
            ExceptionDispatchInfo failure = null;

            SendCallback callback = delegate
            {
                try { result = func(); }
                // Capture rather than let it cross the thread boundary raw: Send's own
                // rethrow behaviour differs between the WPF and WinForms contexts, and
                // ExceptionDispatchInfo preserves the original stack either way.
                catch (Exception ex) { failure = ExceptionDispatchInfo.Capture(ex); }
            };

            if (_context != null)
            {
                _context.Send(new SendOrPostCallback(_ => callback()), null);
            }
            else if (_fallbackDispatcher != null)
            {
                // Needs no window handle, which is why it is the only fallback left.
                _fallbackDispatcher.Invoke(new Action(() => callback()));
            }
            else
            {
                // Neither a captured context nor a dispatcher. There is no way to reach
                // the right thread from here, so say so plainly rather than running
                // ESAPI work on whatever thread we happen to be on and corrupting the
                // model. The WinForms Control.Invoke fallback that used to sit here
                // went with MainForm; a Dispatcher needs no window handle, so unlike
                // that path this one is genuinely unreachable in practice.
                throw new InvalidOperationException(
                    "Cannot reach the ESAPI thread: no SynchronizationContext was captured " +
                    "and no dispatcher is available.");
            }

            if (failure != null)
                failure.Throw();

            return result;
        }

        private delegate void SendCallback();
    }
}
