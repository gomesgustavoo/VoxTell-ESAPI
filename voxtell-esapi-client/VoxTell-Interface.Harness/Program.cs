using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services;
using VoxTell_Interface.Services.Auth;

namespace VoxTell_Interface.Harness
{
    /// <summary>
    /// Drives the whole v2 protocol from the command line, using the plugin's own code.
    ///
    /// Exit codes: 0 all good, 1 a failure worth investigating, 2 cancelled or timed out.
    /// </summary>
    public static class Program
    {
        public static int Main(string[] args)
        {
            try
            {
                return RunAsync(args).GetAwaiter().GetResult();
            }
            catch (OperationCanceledException)
            {
                Console.Error.WriteLine("cancelled");
                return 2;
            }
            catch (Exception ex)
            {
                // The whole chain, not just the top: HttpClient's own message is always
                // "An error occurred while sending the request." and the cause -- a DNS
                // failure, a TLS failure, a refused connection -- is one or two levels down.
                Console.Error.WriteLine("FAILED:");
                for (Exception e = ex; e != null; e = e.InnerException)
                    Console.Error.WriteLine("  {0}: {1}", e.GetType().Name, e.Message);
                return 1;
            }
        }

        private static async Task<int> RunAsync(string[] args)
        {
            var options = Options.Parse(args);
            if (options.ShowHelp)
            {
                Options.PrintUsage();
                return 0;
            }

            var gate = new DirectGate();

            // The client and the auth service are mutually dependent: the client needs a token
            // from the service, and the service reads its Keycloak endpoints from the config the
            // client caches. A closure over the not-yet-assigned local breaks the cycle without
            // either side having to fetch anything eagerly — the same wiring MainViewModel uses.
            VoxTellApiClient api = null;
            var auth = new AuthService(() => api == null ? null : api.CachedAuthConfig);
            api = new VoxTellApiClient(options.BaseUrl, auth) { HostHeaderOverride = options.HostHeader };

            try
            {
                using (var cts = new CancellationTokenSource())
                {
                    Console.CancelKeyPress += (s, e) => { e.Cancel = true; cts.Cancel(); };
                    return await RunProtocolAsync(api, auth, options, gate, cts.Token);
                }
            }
            finally
            {
                api.Dispose();
                auth.Dispose();
            }
        }

        private static async Task<int> RunProtocolAsync(
            VoxTellApiClient api, AuthService auth, Options options,
            IThreadGate gate, CancellationToken ct)
        {
            var stopwatch = Stopwatch.StartNew();

            Step("health");
            HealthResponse health = await api.GetHealthAsync(ct);
            Console.WriteLine("  status {0}, database {1}, api {2}",
                health.Status, health.Database, health.Version);

            Step("auth/config");
            AuthConfigResponse cfg = await api.GetAuthConfigAsync(ct);
            Console.WriteLine("  issuer          {0}", cfg.Issuer);
            Console.WriteLine("  client          {0}", cfg.ClientId);
            Console.WriteLine("  pkce            {0}", cfg.PkceMethod);
            Console.WriteLine("  scopes          {0}", cfg.Scopes);
            Console.WriteLine("  redirect ports  {0}",
                cfg.RedirectPorts == null ? "(none)" : string.Join(", ", cfg.RedirectPorts));

            if (string.IsNullOrEmpty(cfg.AuthorizationEndpoint))
            {
                Console.Error.WriteLine(
                    "  the server advertises no authorization_endpoint, so PKCE is unavailable -- " +
                    "is the API older than the client?");
                return 1;
            }

            Step("sign in");
            if (!string.IsNullOrEmpty(options.ApiKey))
            {
                auth.SetApiKey(options.ApiKey);
                Console.WriteLine("  using the supplied API key");
            }
            else
            {
                if (options.ForceDeviceFlow)
                {
                    Console.WriteLine(
                        "  --device given: occupying the loopback ports so the PKCE path " +
                        "cannot bind and the fallback is exercised");
                }

                using (PortBlocker blocker = options.ForceDeviceFlow
                           ? PortBlocker.Occupy(cfg.RedirectPorts)
                           : null)
                {
                    await auth.SignInAsync(prompt =>
                    {
                        if (!string.IsNullOrEmpty(prompt.Message))
                            Console.WriteLine("  " + prompt.Message);
                        if (!string.IsNullOrEmpty(prompt.VerificationUri))
                            Console.WriteLine("  open  {0}", prompt.VerificationUri);
                        if (!string.IsNullOrEmpty(prompt.UserCode))
                            Console.WriteLine("  code  {0}", prompt.UserCode);
                    }, ct);
                }

                Console.WriteLine("  signed in via {0}", auth.Kind);
            }

            Step("me");
            MeResponse me = await api.GetMeAsync(ct);
            Console.WriteLine("  {0}  |  {1} used this month  |  {2}/{3} in flight",
                me.DisplayName, me.UsedThisMonth, me.Outstanding, me.MaxOutstanding);

            return await RunJobAsync(api, options, gate, stopwatch, ct);
        }

        private static async Task<int> RunJobAsync(
            VoxTellApiClient api, Options options, IThreadGate gate,
            Stopwatch stopwatch, CancellationToken ct)
        {
            Step("encode");
            var volume = new SyntheticVolumeSource(options.XSize, options.YSize, options.ZSize);
            Geometry geometry = Geometry.FromVolumeSource(volume);

            // Check our own arithmetic before using it to judge the server's.
            AffineCheck.SelfTest(geometry);
            Console.WriteLine("  affine inverse self-test passed");

            byte[] blob = VoxelEncoder.BuildVolumeBlob(volume, gate, null, ct);
            VoxelEncoder.ValidateBlob(blob, volume);

            long raw = geometry.VoxelCount * 2;
            Console.WriteLine("  {0}x{1}x{2}  |  {3:N1} MB raw -> {4:N1} MB gzip  |  HU = stored x {5} + {6}",
                volume.XSize, volume.YSize, volume.ZSize,
                raw / 1e6, blob.Length / 1e6, volume.ScalingSlope, volume.ScalingIntercept);

            Step("create job");
            var request = new JobCreateRequest
            {
                Geometry = geometry,
                Prompts = options.Prompts,
                UploadBytes = blob.LongLength,
                KeepLargest = false,
                WantMask = false,
            };

            JobCreatedResponse created;
            try
            {
                created = await api.CreateJobAsync(request, ct);
            }
            catch (VoxTellApiException ex) when (ex.IsTooManyOutstanding)
            {
                Console.Error.WriteLine("  {0}", ex.Message);
                Console.Error.WriteLine("  (this is the 429 cap working as designed, not a bug)");
                return 2;
            }

            Console.WriteLine("  job {0}, state {1}, {2} part(s) of {3:N0} bytes, urls valid {4}s",
                created.JobId, created.State, created.Upload.Count,
                created.PartSize, created.ExpiresIn);

            Step("upload");
            List<SubmitPart> parts = await api.UploadPartsAsync(blob, created,
                (part, total, sent, all) =>
                    Console.WriteLine("  part {0}/{1}  {2:N1}/{3:N1} MB", part, total, sent / 1e6, all / 1e6),
                ct);

            Step("submit");
            JobStatusResponse status = await api.SubmitJobAsync(created.JobId, parts, ct);
            Console.WriteLine("  state {0}", status.State);

            Step("poll");
            string lastLine = null;
            DateTime deadline = DateTime.UtcNow.AddSeconds(options.TimeoutSeconds);

            while (!status.IsTerminal)
            {
                if (DateTime.UtcNow > deadline)
                {
                    Console.Error.WriteLine(
                        "  still {0} after {1}s -- giving up on waiting. The job keeps running.",
                        status.State, options.TimeoutSeconds);
                    return 2;
                }

                await Task.Delay(TimeSpan.FromSeconds(status.PollAfter > 0 ? status.PollAfter : 5), ct);
                status = await api.GetJobAsync(created.JobId, ct);

                string line = string.Format("  [{0}] {1,5:P0}  {2}{3}",
                    status.State, status.Progress, status.Message,
                    status.QueuePosition.HasValue
                        ? string.Format(" ({0} ahead)", status.QueuePosition.Value) : "");

                if (line != lastLine)
                {
                    Console.WriteLine(line);
                    lastLine = line;
                }
            }

            Console.WriteLine("  finished in {0:N1}s: {1}", stopwatch.Elapsed.TotalSeconds, status.State);

            if (status.State != JobState.Done)
            {
                if (!string.IsNullOrEmpty(status.Error))
                    Console.Error.WriteLine("  error: {0}", status.Error);
                return status.State == JobState.Cancelled ? 2 : 1;
            }

            Step("download + verify");
            ResultEnvelope envelope = await api.GetResultAsync(created.JobId, ct);
            Console.WriteLine("  schema {0}, model {1}, {2} result(s)",
                envelope.Schema, envelope.Model,
                envelope.Results == null ? 0 : envelope.Results.Count);

            List<AffineCheckResult> checks = AffineCheck.Verify(envelope, geometry);

            int failures = 0;
            int withContours = 0;

            foreach (AffineCheckResult c in checks)
            {
                if (c.ContourCount == 0)
                {
                    Console.WriteLine("  {0,-28} EMPTY", Truncate(c.Prompt, 28));
                    continue;
                }

                withContours++;
                string verdict = c.Passed
                    ? "ok"
                    : c.OutOfBounds > 0
                        ? string.Format("{0} POINT(S) OUT OF BOUNDS", c.OutOfBounds)
                        : string.Format("Z MISMATCH {0:E2}", c.MaxZError);

                if (!c.Passed) failures++;

                Console.WriteLine("  {0,-28} {1,9:N0} vox  {2,4} contours  {3,7:N0} pts  z {4}-{5}  {6}",
                    Truncate(c.Prompt, 28), c.VoxelCount, c.ContourCount, c.PointCount,
                    c.FirstSlice, c.LastSlice, verdict);
            }

            if (options.Cleanup)
            {
                Step("delete job");
                await api.DeleteJobAsync(created.JobId, ct);
                Console.WriteLine("  purged");
            }

            Console.WriteLine();
            if (failures > 0)
            {
                Console.Error.WriteLine("{0} structure(s) failed geometric verification", failures);
                return 1;
            }

            if (withContours == 0)
            {
                // Not a failure. The phantom is not anatomy, so a vision-language model finding
                // nothing in it is the expected outcome — auth, the wire format and the job
                // lifecycle were all still exercised end to end. Use real DICOM through
                // scripts/e2e_client.py, or the plugin in Eclipse, to exercise the geometry.
                Console.WriteLine(
                    "protocol verified end to end; no contours came back, which is expected for a " +
                    "synthetic phantom -- the geometry check had nothing to assert on");
                return 0;
            }

            Console.WriteLine("protocol and geometry verified against the source grid");
            return 0;
        }

        private static void Step(string name)
        {
            Console.WriteLine();
            Console.WriteLine("== " + name + " ==");
        }

        private static string Truncate(string s, int max)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Length <= max ? s : s.Substring(0, max - 3) + "...";
        }

        /// <summary>
        /// Holds the loopback ports so the PKCE listener cannot bind, forcing the device-code
        /// fallback. That path is the one a locked-down clinical workstation will actually take,
        /// and it is otherwise very hard to reach deliberately.
        /// </summary>
        private sealed class PortBlocker : IDisposable
        {
            private readonly List<System.Net.Sockets.TcpListener> _held =
                new List<System.Net.Sockets.TcpListener>();

            public static PortBlocker Occupy(IEnumerable<int> ports)
            {
                var blocker = new PortBlocker();
                foreach (int port in ports ?? Enumerable.Empty<int>())
                {
                    try
                    {
                        var listener = new System.Net.Sockets.TcpListener(
                            System.Net.IPAddress.Loopback, port);
                        listener.Start();
                        blocker._held.Add(listener);
                    }
                    catch
                    {
                        // Already taken by something else, which serves the same purpose.
                    }
                }
                return blocker;
            }

            public void Dispose()
            {
                foreach (var listener in _held)
                {
                    try { listener.Stop(); } catch { }
                }
            }
        }

        private sealed class Options
        {
            public string BaseUrl = VoxTellApiClient.DefaultBaseUrl;
            public string HostHeader;
            public string ApiKey;
            public string[] Prompts = { "liver", "spleen" };
            public int XSize = 128, YSize = 128, ZSize = 64;
            public int TimeoutSeconds = 900;
            public bool ForceDeviceFlow;
            public bool Cleanup = true;
            public bool ShowHelp;

            public static Options Parse(string[] args)
            {
                var o = new Options();

                for (int i = 0; i < args.Length; i++)
                {
                    string a = args[i];
                    Func<string> next = () =>
                    {
                        if (i + 1 >= args.Length)
                            throw new ArgumentException(a + " needs a value");
                        return args[++i];
                    };

                    switch (a)
                    {
                        case "--base": o.BaseUrl = next(); break;
                        case "--host-header": o.HostHeader = next(); break;
                        case "--api-key": o.ApiKey = next(); break;
                        case "--prompts":
                            o.Prompts = next().Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)
                                              .Select(p => p.Trim()).Where(p => p.Length > 0).ToArray();
                            break;
                        case "--size":
                            string[] dims = next().Split('x');
                            if (dims.Length != 3) throw new ArgumentException("--size wants XxYxZ");
                            o.XSize = int.Parse(dims[0]);
                            o.YSize = int.Parse(dims[1]);
                            o.ZSize = int.Parse(dims[2]);
                            break;
                        case "--timeout": o.TimeoutSeconds = int.Parse(next()); break;
                        case "--device": o.ForceDeviceFlow = true; break;
                        case "--keep": o.Cleanup = false; break;
                        case "-h":
                        case "--help": o.ShowHelp = true; break;
                        default:
                            throw new ArgumentException("unknown argument: " + a);
                    }
                }

                return o;
            }

            public static void PrintUsage()
            {
                Console.WriteLine("VoxTell ESAPI client harness -- drives the v2 protocol without Eclipse.");
                Console.WriteLine();
                Console.WriteLine("  --base URL          API base, default " + VoxTellApiClient.DefaultBaseUrl);
                Console.WriteLine("  --host-header HOST  Host header for API calls, to reach Traefik before DNS exists");
                Console.WriteLine("  --api-key KEY       Use a vxt_ key instead of signing in");
                Console.WriteLine("  --prompts a,b,c     Prompts to send, default \"liver,spleen\"");
                Console.WriteLine("  --size XxYxZ        Phantom dimensions, default 128x128x64");
                Console.WriteLine("  --timeout SECONDS   How long to wait for the job, default 900");
                Console.WriteLine("  --device            Block the loopback ports to force the device-code flow");
                Console.WriteLine("  --keep              Do not delete the job afterwards");
            }
        }
    }
}
