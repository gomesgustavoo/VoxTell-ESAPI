using VMS.TPS.Common.Model.API;
using VoxTell_Interface.Views;

namespace VMS.TPS
{
    public class Script
    {
        public Script() { }

        public void Execute(ScriptContext context, System.Windows.Window window)
        {
            // Unlock write access — required once before any structure modifications
            context.Patient.BeginModifications();

            // Host the WinForms control inside Eclipse's WPF window via WindowsFormsHost.
            // When Execute() returns, Eclipse unfreezes the workspace while keeping
            // the window open and ScriptContext/write access alive.
            var host = new System.Windows.Forms.Integration.WindowsFormsHost();
            var mainControl = new MainControl(context);
            host.Child = mainControl;
            host.SnapsToDevicePixels = true;

            // Enable per-monitor DPI awareness for crisp text rendering
            System.Windows.Media.RenderOptions.SetBitmapScalingMode(
                host, System.Windows.Media.BitmapScalingMode.HighQuality);

            window.Content = host;
            window.Title = "VoxTell AI Segmentation";
            window.Width = 432;
            window.Height = 500;
            window.UseLayoutRounding = true;
            window.Background = new System.Windows.Media.SolidColorBrush(
                System.Windows.Media.Color.FromRgb(30, 30, 30));
        }
    }
}
