using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace VoxTell_Interface.Services.Auth
{
    /// <summary>
    /// Catches the OAuth redirect on 127.0.0.1 and hands back the query parameters.
    ///
    /// A raw <see cref="TcpListener"/>, not <c>HttpListener</c>. Registering an <c>http://</c>
    /// prefix on Windows requires a <c>netsh http add urlacl</c> reservation or an elevated
    /// process, and an Eclipse plugin runs as the ordinary clinical user — <c>HttpListener</c>
    /// would fail with "Access is denied" on exactly the machines this has to work on. A TCP
    /// socket on a loopback port needs no privileges, and the only HTTP we have to speak is one
    /// request line in and one small response out.
    /// </summary>
    internal sealed class LoopbackListener : IDisposable
    {
        private readonly TcpListener _listener;

        private LoopbackListener(TcpListener listener, int port, string redirectUri)
        {
            _listener = listener;
            Port = port;
            RedirectUri = redirectUri;
        }

        public int Port { get; private set; }

        /// <summary>The exact URI to send Keycloak, which must match a registered one verbatim.</summary>
        public string RedirectUri { get; private set; }

        /// <summary>
        /// Binds the first available port from <paramref name="ports"/>.
        ///
        /// The ports are fixed by the Keycloak client, not chosen freely: Keycloak matches
        /// redirect URIs exactly and its wildcard covers only the path, so an ephemeral port is
        /// rejected with "Invalid parameter: redirect_uri". Several candidates exist so a port
        /// already taken on the workstation is not a dead end.
        /// </summary>
        /// <returns>A bound listener, or null when every candidate port is taken.</returns>
        public static LoopbackListener TryBind(IEnumerable<int> ports, string redirectPath)
        {
            if (string.IsNullOrEmpty(redirectPath)) redirectPath = "/callback";

            foreach (int port in ports)
            {
                // Explicitly IPv4 loopback: Keycloak has 127.0.0.1 registered, and binding
                // IPv6Any would also answer as [::1], which is a different registered URI.
                var listener = new TcpListener(IPAddress.Loopback, port);
                try
                {
                    listener.Start();
                    string uri = string.Format("http://127.0.0.1:{0}{1}", port, redirectPath);
                    return new LoopbackListener(listener, port, uri);
                }
                catch (SocketException)
                {
                    // Port in use, or a local policy forbids binding it. Try the next.
                    try { listener.Stop(); } catch { }
                }
            }
            return null;
        }

        /// <summary>
        /// Waits for the browser to arrive and returns the redirect's query parameters.
        ///
        /// Loops rather than accepting once: browsers and security tooling routinely open
        /// speculative connections, and a favicon probe or a dropped preconnect must not be
        /// mistaken for the callback.
        /// </summary>
        public async Task<Dictionary<string, string>> WaitForCallbackAsync(CancellationToken ct)
        {
            using (ct.Register(() => { try { _listener.Stop(); } catch { } }))
            {
                while (true)
                {
                    ct.ThrowIfCancellationRequested();

                    TcpClient client;
                    try
                    {
                        client = await _listener.AcceptTcpClientAsync().ConfigureAwait(false);
                    }
                    catch (ObjectDisposedException)
                    {
                        // Stop() from the cancellation registration above.
                        ct.ThrowIfCancellationRequested();
                        throw;
                    }
                    catch (InvalidOperationException)
                    {
                        ct.ThrowIfCancellationRequested();
                        throw;
                    }

                    using (client)
                    {
                        string requestLine = await ReadRequestLineAsync(client).ConfigureAwait(false);
                        if (string.IsNullOrEmpty(requestLine))
                            continue;

                        // "GET /callback?code=...&state=... HTTP/1.1"
                        string[] parts = requestLine.Split(' ');
                        if (parts.Length < 2 || parts[0] != "GET")
                        {
                            await RespondAsync(client, 405, "Method not allowed", null).ConfigureAwait(false);
                            continue;
                        }

                        string target = parts[1];
                        int q = target.IndexOf('?');
                        if (q < 0)
                        {
                            // The callback always carries a query; anything else is a probe.
                            await RespondAsync(client, 404, "Not found", null).ConfigureAwait(false);
                            continue;
                        }

                        Dictionary<string, string> query = ParseQuery(target.Substring(q + 1));

                        bool isCallback = query.ContainsKey("code") || query.ContainsKey("error");
                        if (!isCallback)
                        {
                            await RespondAsync(client, 404, "Not found", null).ConfigureAwait(false);
                            continue;
                        }

                        string body = query.ContainsKey("error")
                            ? BuildPage("Sign-in failed",
                                "Eclipse could not complete the sign-in. Close this tab and try again.")
                            : BuildPage("Signed in",
                                "You can close this tab and return to Eclipse.");

                        await RespondAsync(client, 200, "OK", body).ConfigureAwait(false);
                        return query;
                    }
                }
            }
        }

        private static async Task<string> ReadRequestLineAsync(TcpClient client)
        {
            var buffer = new byte[2048];
            var line = new StringBuilder();
            NetworkStream stream = client.GetStream();

            // Only the request line is needed, so read until the first CRLF and stop. A cap
            // keeps a client that never sends a newline from growing this unbounded.
            while (line.Length < 8192)
            {
                int read = await stream.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
                if (read <= 0) break;

                line.Append(Encoding.ASCII.GetString(buffer, 0, read));
                int eol = line.ToString().IndexOf("\r\n", StringComparison.Ordinal);
                if (eol >= 0)
                    return line.ToString().Substring(0, eol);
            }

            return line.Length > 0 ? line.ToString() : null;
        }

        private static async Task RespondAsync(TcpClient client, int status, string reason, string html)
        {
            html = html ?? "";
            byte[] body = Encoding.UTF8.GetBytes(html);

            var header = new StringBuilder();
            header.Append("HTTP/1.1 ").Append(status).Append(' ').Append(reason).Append("\r\n");
            header.Append("Content-Type: text/html; charset=utf-8\r\n");
            header.Append("Content-Length: ").Append(body.Length).Append("\r\n");
            // The page carries an authorization code in its URL; keep it out of any cache.
            header.Append("Cache-Control: no-store\r\n");
            header.Append("Connection: close\r\n\r\n");

            byte[] head = Encoding.ASCII.GetBytes(header.ToString());

            NetworkStream stream = client.GetStream();
            await stream.WriteAsync(head, 0, head.Length).ConfigureAwait(false);
            if (body.Length > 0)
                await stream.WriteAsync(body, 0, body.Length).ConfigureAwait(false);
            await stream.FlushAsync().ConfigureAwait(false);
        }

        private static Dictionary<string, string> ParseQuery(string query)
        {
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (string pair in query.Split('&'))
            {
                if (pair.Length == 0) continue;
                int eq = pair.IndexOf('=');
                if (eq < 0)
                    result[Uri.UnescapeDataString(pair)] = "";
                else
                    result[Uri.UnescapeDataString(pair.Substring(0, eq))] =
                        Uri.UnescapeDataString(pair.Substring(eq + 1).Replace('+', ' '));
            }
            return result;
        }

        private static string BuildPage(string title, string message)
        {
            // Self-contained and offline: the workstation may have no route to the internet
            // even when it can reach Keycloak.
            return
                "<!doctype html><html><head><meta charset=\"utf-8\"><title>VoxTell</title>" +
                "<style>body{background:#1e1e1e;color:#f0f0f0;font:15px/1.6 'Segoe UI',sans-serif;" +
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}" +
                "div{text-align:center}h1{font-size:20px;font-weight:600;margin:0 0 8px}" +
                "p{color:#a0a0a0;margin:0}</style></head><body><div><h1>" + title +
                "</h1><p>" + message + "</p></div></body></html>";
        }

        public void Dispose()
        {
            try { _listener.Stop(); } catch { }
        }
    }
}
