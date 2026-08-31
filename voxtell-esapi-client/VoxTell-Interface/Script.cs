using System.Runtime.CompilerServices;
using VMS.TPS.Common.Model.API;
using VoxTell_Interface.Views;

namespace VMS.TPS
{
    public class Script
    {
        public Script() { }

        /// <summary>
        /// REQUIRED for a plugin that writes, and easy to omit because nothing complains until
        /// it does. Eclipse authorises a modification by walking the stack to find the calling
        /// assembly, so if the JIT inlines this method away the walk finds no script and
        /// <c>BeginModifications()</c> below fails with Varian's own message:
        ///
        ///     "The system did not find a calling assembly while script '{0}' tried to modify
        ///      data. Possible reason is that the script or action pack is not using the
        ///      MethodImplOptions.NoInlining attribute in the Execute method."
        ///
        /// (That string is in VMS.TPS.Common.Model.dll, and Varian's own shipped
        /// esapi\Plugins\Example_Plan.cs carries this attribute for the same reason.)
        ///
        /// Whether the JIT inlines a given method is not ours to control and can change with the
        /// runtime or tiered compilation, so the fact that writes work today is not evidence this
        /// is unnecessary — it is evidence we have been lucky.
        /// </summary>
        [MethodImpl(MethodImplOptions.NoInlining)]
        public void Execute(ScriptContext context, System.Windows.Window window)
        {
            // v1 called BeginModifications() unconditionally, which threw a bare
            // NullReferenceException when no patient was open — an alarming way to learn you
            // forgot to open one.
            if (context == null || context.Patient == null)
            {
                System.Windows.MessageBox.Show(
                    "Open a patient with a CT and a structure set before running VoxTell.",
                    "VoxTell", System.Windows.MessageBoxButton.OK,
                    System.Windows.MessageBoxImage.Information);
                return;
            }

            // Write access is NOT unlocked here.
            //
            // v1 and the first v2 build both called BeginModifications() the moment
            // the panel opened, which unlocks the patient for writing before the
            // planner has chosen anything -- including on the run where they only
            // wanted to look at a QA verdict. Eclipse treats an unlocked patient as
            // modified, so opening the panel and closing it could prompt to save.
            //
            // The unlock now happens immediately before the first contour is
            // written (see MainViewModel.EnsureWritable), gated on
            // Patient.CanModifyData(). Reading the structure set back for QA needs
            // no unlock at all, which is a real safety improvement: run 2 can open
            // read-only.
            //
            // NoInlining on this method stays load-bearing regardless. Eclipse walks
            // the stack to attribute BeginModifications to the calling assembly, and
            // an inlined frame breaks that attribution.

            // A pure WPF panel now. The WindowsFormsHost that used to carry
            // MainControl is gone, and with it the hosted child HWND, the
            // AutoScaleMode/Px() DPI arithmetic and the double-scaling trap it
            // brought. WPF lays out in device-independent units, so DPI is simply
            // not a thing this panel has to think about.
            //
            // When Execute() returns, Eclipse unfreezes the workspace while keeping
            // the window open and ScriptContext alive.
            window.Content = new MainPanel(context);
            window.Title = "VoxTell AI Segmentation";

            // Wider than the WinForms build: the review table's six columns and the
            // 9.75pt type scale need the room.
            //
            // The MINIMUM is what changed in 2.2.0.0, and it is a consequence of the panel
            // no longer having an outer scroller. Nothing scrolls the whole panel any more,
            // so the window must not be resizable below the height at which the header, the
            // context strip, the picker, the action bar and the review card all fit — or
            // the bottom of a card is simply clipped, silently, with no scrollbar to hint
            // that a control is missing. 720 is that height plus a little; the layout
            // harness renders exactly 620x720 to keep it honest.
            window.Width = 760;
            window.Height = 860;
            window.MinWidth = 620;
            window.MinHeight = 720;
            window.UseLayoutRounding = true;
            window.Background = Theme.Void;
        }
    }
}
