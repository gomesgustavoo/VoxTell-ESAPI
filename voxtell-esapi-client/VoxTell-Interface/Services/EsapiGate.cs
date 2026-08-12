using System;
using System.Runtime.ExceptionServices;
using System.Threading;
using System.Windows.Forms;
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
        private readonly Control _fallbackControl;
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

        /// <summary>
        /// The WinForms constructor, kept only until <c>MainForm</c> is deleted.
        ///
        /// Must be constructed ON the ESAPI thread — i.e. from the UI control's constructor,
        /// which Eclipse reaches synchronously through <c>Script.Execute</c>.
        /// </summary>
        public EsapiGate(Control owner)
        {
            if (owner == null) throw new ArgumentNullException("owner");

            _esapiThreadId = Thread.CurrentThread.ManagedThreadId;
            _fallbackControl = owner;

            // Prefer the SynchronizationContext over Control.Invoke. Eclipse hosts us in a WPF
            // window, so this is the dispatcher context for the main STA thread and it works
            // before the WinForms handle exists. Control.InvokeRequired returns *false* on a
            // handle-less control, which would run ESAPI work on a pool thread and corrupt it.
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
                // Needs no window handle, so unlike the Control path below this cannot be
                // unreachable.
                _fallbackDispatcher.Invoke(new Action(() => callback()));
            }
            else
            {
                // No captured context. Control.Invoke needs a created handle, and if there
                // isn't one there is no way to reach the right thread — say so plainly
                // instead of running the work here and corrupting the model.
                if (!_fallbackControl.IsHandleCreated)
                {
                    throw new InvalidOperationException(
                        "Cannot reach the ESAPI thread: no SynchronizationContext was captured " +
                        "and the host control has no window handle yet.");
                }
                _fallbackControl.Invoke(new MethodInvoker(() => callback()));
            }

            if (failure != null)
                failure.Throw();

            return result;
        }

        private delegate void SendCallback();
    }
}
