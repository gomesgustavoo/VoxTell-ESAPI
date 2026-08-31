using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using VMS.TPS.Common.Model.API;

[assembly: AssemblyTitle("VoxTell-Interface")]
[assembly: AssemblyDescription("AI segmentation interface for Varian Eclipse TPS External Beam Planning")]
[assembly: AssemblyConfiguration("")]
[assembly: AssemblyCompany("RT Medical Systems")]
[assembly: AssemblyProduct("VoxTell-Interface")]
[assembly: AssemblyCopyright("Copyright © RT Medical Systems 2026")]
[assembly: AssemblyTrademark("")]
[assembly: AssemblyCulture("")]
[assembly: ComVisible(false)]
[assembly: Guid("0400cd83-84d5-431e-9ad4-5ca31954e46d")]

// 2.x tracks the v2 wire protocol (voxtell-cloud/PROTOCOL.md); 1.x spoke v1.
//
// ############  BUMP THE REVISION ON EVERY BUILD YOU HAND OVER  ############
//
// Eclipse approves a script by (version + content hash) and re-checks both before every run.
// Republishing changed bytes under a version it has already approved fails with:
//
//     UnauthorizedScriptingAPIAccessException:
//     The script 'VoxTell-Interface (x.y.z.w)' has been modifed.        [sic]
//     If this is a new version of the script, the version number must be changed.
//
// It reads like a tampering or permissions error, but it only means "same version, different
// bytes". A new number makes Eclipse treat it as a new script — which then has to be approved
// again in the UI.
//
// A `2.0.*` wildcard would automate this, but it is rejected while <Deterministic>true</...>
// is set, and reproducible builds are worth more here than the convenience: for a plugin that
// writes structures into a patient, being able to rebuild the exact approved binary and
// compare hashes is the thing that makes an approval meaningful. So it is bumped by hand, and
// the install step on the build box refuses to publish a version that is already published.
//
// History:
//   2.0.0.0  first v2 build (2026-08-05)
//   2.0.1.0  5 MiB parts / 408+499 retry / typography + DPI pass
//   2.1.0.0  WPF flip (MainForm/UiTheme deleted), model addressing + CADS catalog,
//            structure read-back and the QA baseline recording. First build with no
//            WinForms reference at all.
//   2.1.1.0  NEVER DEPLOYED (Eclipse held the DLL when it was built). Its fixes are
//            included in 2.2.0.0, which is why there is no approved 2.1.1.0 anywhere.
//            Fixes from 2.1.0.0's first run in Eclipse:
//              * PropertyChanged is marshalled to the dispatcher again. 2.1.0.0
//                asserted the view model always raises on the UI thread and dropped
//                the marshalling the WinForms view had; it threw "The calling thread
//                cannot access this object because a different thread owns it".
//              * StackPanels filled after construction (the step rail, the preset
//                row) got no gap, so their children sat flush against each other.
//              * Sync() now renders the review list too, instead of binding it only
//                from a "Plans" notification.
//              * Review columns are fixed-width, so they align across rows.
//              * The Structures tab says WHY it is unavailable, and offers a retry.
//   2.2.0.0  UI pass, clinic protocols, and the components the panel was missing:
//              * One header: product mark, the two-run tagline, the step rail, and the
//                account reduced from a 20 px email address in its own card to a 12 px
//                chip whose menu holds sign-out and the server/API-key pane. VR is now a
//                link rather than a word.
//              * The review table is ONE grid, so its columns align across rows by
//                construction; 2.1.1.0's fixed widths clipped at the 620 px minimum.
//                DICOM type is a real dropdown instead of a button that silently cycled
//                four values.
//              * "replaces X" is recomputed as the write-as id is edited. It used to be
//                fixed at plan time while the write path resolved the edited id, so a
//                rename onto an existing structure said "will create" and then replaced
//                that structure's contours. Two ticked rows writing one id are flagged.
//              * Catalog-addressed results are named through the catalog and paired back
//                to their result by an explicit key. BuildPlan read result.Prompt, which
//                is null for every CADS result, and Import keyed a Dictionary on it.
//              * Clinic protocols from GET /v1/models: per-structure write-as id, DICOM
//                type and colour, with entries no model produces listed rather than
//                dropped.
//              * Structure list is filterable, collapsible and virtualised; the prompt
//                box is two lines with its instruction inside it.
//              * No outer ScrollViewer: the header stays put and the region that owns the
//                leftover height follows the workflow.
//              * The API key field is a PasswordBox, which it always claimed to be. The
//                progress fill is a grid weight, so it survives a resize.
[assembly: AssemblyVersion("2.2.0.0")]
[assembly: AssemblyFileVersion("2.2.0.0")]

// REQUIRED, and easy to lose in a refactor: without it Eclipse refuses
// Patient.BeginModifications() on a binary plugin and every structure write fails.
// The attribute lives in VMS.TPS.Common.Model.API, hence the using above.
[assembly: ESAPIScript(IsWriteable = true)]
