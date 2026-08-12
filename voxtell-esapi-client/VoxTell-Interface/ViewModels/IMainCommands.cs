using System;
using System.ComponentModel;
using System.Threading.Tasks;

namespace VoxTell_Interface.ViewModels
{
    /// <summary>
    /// Everything the view can ask the ViewModel to do, and the one thing it can ask for.
    ///
    /// The other half of the seam described on <see cref="MainViewState"/>: the view holds an
    /// <c>IMainCommands</c> rather than a <c>MainViewModel</c>, so it compiles — and renders —
    /// in a project that has never heard of the Varian assemblies.
    ///
    /// Every signature here already existed on <c>MainViewModel</c> with an exact match, so
    /// implementing this interface cost that class one new member (<see cref="Snapshot"/>) and a
    /// name in its base list. That is deliberate: an interface shaped to fit the code that
    /// already works is far less likely to distort it than one designed in the abstract.
    ///
    /// <see cref="PromptsText"/> is the only two-way member. The free-text inputs cannot bind to
    /// an immutable snapshot, so they push into the ViewModel on edit and are never pushed back —
    /// re-pushing was what forced the WinForms view to guard on whether a box had focus.
    /// </summary>
    public interface IMainCommands : INotifyPropertyChanged, IDisposable
    {
        /// <summary>The prompt box's contents. Pushed by the view as the operator types.</summary>
        string PromptsText { get; set; }

        /// <summary>A fresh, consistent picture of everything the view draws.</summary>
        MainViewState Snapshot();

        Task SignInAsync();
        void SignOut();

        Task RunAsync();
        void Cancel();

        /// <summary>Writes the ticked rows into the structure set. The only patient write.</summary>
        void ImportSelected();

        void ApplyBaseUrl(string baseUrl);
        Task ApplyApiKeyAsync(string apiKey);
    }
}
