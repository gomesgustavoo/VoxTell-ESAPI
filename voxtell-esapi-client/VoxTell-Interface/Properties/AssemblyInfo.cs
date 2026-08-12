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
[assembly: AssemblyVersion("2.0.1.0")]
[assembly: AssemblyFileVersion("2.0.1.0")]

// REQUIRED, and easy to lose in a refactor: without it Eclipse refuses
// Patient.BeginModifications() on a binary plugin and every structure write fails.
// The attribute lives in VMS.TPS.Common.Model.API, hence the using above.
[assembly: ESAPIScript(IsWriteable = true)]
