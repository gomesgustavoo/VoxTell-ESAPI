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

            // Unlock write access — required once before any structure modifications
            context.Patient.BeginModifications();

            // Host the WinForms control inside Eclipse's WPF window via WindowsFormsHost.
            // When Execute() returns, Eclipse unfreezes the workspace while keeping
            // the window open and ScriptContext/write access alive.
            var host = new System.Windows.Forms.Integration.WindowsFormsHost();
            var mainControl = new MainControl(context);
            host.Child = mainControl;
            // Aligns the hosted HWND to whole device pixels, so the card edges and the 1 px
            // section rules do not land on half-pixels and blur.
            host.SnapsToDevicePixels = true;

            // There was a RenderOptions.SetBitmapScalingMode(host, HighQuality) call here,
            // commented as enabling "per-monitor DPI awareness for crisp text rendering". It
            // did neither: WindowsFormsHost hosts a child HWND that WPF composites directly
            // rather than rasterising, so a bitmap scaling mode never applies to it, and
            // nothing about DPI awareness is settable from inside a plugin — that belongs to
            // the host process manifest. DPI is handled properly in MainControl now, via
            // AutoScaleMode.Dpi and UiTheme.Scale.

            window.Content = host;
            window.Title = "VoxTell AI Segmentation";
            // Wider and taller than v1: the results review list needs room, since nothing
            // reaches the patient until the planner has read it. The extra width over the
            // first v2 build is for the 9.75pt type scale — at 8.25pt the grid columns fitted,
            // but only because the text was too small to read.
            window.Width = 680;
            window.Height = 820;
            window.MinWidth = 560;
            window.MinHeight = 620;
            window.UseLayoutRounding = true;
            window.Background = new System.Windows.Media.SolidColorBrush(
                System.Windows.Media.Color.FromRgb(30, 30, 30));
        }
    }
}
