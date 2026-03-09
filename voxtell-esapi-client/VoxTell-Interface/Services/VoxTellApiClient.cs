using System;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using VoxTell_Interface.Models;

namespace VoxTell_Interface.Services
{
    public class VoxTellApiClient : IDisposable
    {
        private readonly HttpClient _client;
        private string _baseUrl;

        public VoxTellApiClient(string baseUrl)
        {
            _baseUrl = baseUrl.TrimEnd('/');
            _client = new HttpClient
            {
                Timeout = TimeSpan.FromMinutes(5)
            };
        }

        public void UpdateBaseUrl(string baseUrl)
        {
            _baseUrl = baseUrl.TrimEnd('/');
        }

        private async Task EnsureSuccessOrThrowDetailedAsync(HttpResponseMessage response)
        {
            if (!response.IsSuccessStatusCode)
            {
                string detail = "";
                try
                {
                    detail = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                }
                catch { }

                throw new HttpRequestException(string.Format(
                    "HTTP {0} ({1}): {2}",
                    (int)response.StatusCode, response.ReasonPhrase, detail));
            }
        }

        public async Task<HealthResponse> CheckHealthAsync(CancellationToken ct = default)
        {
            var response = await _client.GetAsync(_baseUrl + "/health", ct).ConfigureAwait(false);
            await EnsureSuccessOrThrowDetailedAsync(response);
            var json = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return JsonConvert.DeserializeObject<HealthResponse>(json);
        }

        public async Task<CreateSessionResponse> CreateSessionAsync(
            CreateSessionRequest request, CancellationToken ct = default)
        {
            var json = JsonConvert.SerializeObject(request);
            var content = new StringContent(json, Encoding.UTF8, "application/json");
            var response = await _client.PostAsync(_baseUrl + "/sessions", content, ct).ConfigureAwait(false);
            await EnsureSuccessOrThrowDetailedAsync(response);
            var responseJson = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return JsonConvert.DeserializeObject<CreateSessionResponse>(responseJson);
        }

        public async Task<SliceUploadResponse> UploadSliceAsync(
            string sessionId, int zIndex, string voxelDataB64,
            CancellationToken ct = default)
        {
            var req = new SliceUploadRequest { VoxelDataB64 = voxelDataB64 };
            var json = JsonConvert.SerializeObject(req);
            var content = new StringContent(json, Encoding.UTF8, "application/json");

            var httpRequest = new HttpRequestMessage(HttpMethod.Put,
                string.Format("{0}/sessions/{1}/slices/{2}", _baseUrl, sessionId, zIndex))
            {
                Content = content
            };

            var response = await _client.SendAsync(httpRequest, ct).ConfigureAwait(false);
            await EnsureSuccessOrThrowDetailedAsync(response);
            var responseJson = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return JsonConvert.DeserializeObject<SliceUploadResponse>(responseJson);
        }

        public async Task<FinalizeResponse> FinalizeSessionAsync(
            string sessionId, CancellationToken ct = default)
        {
            var response = await _client.PostAsync(
                string.Format("{0}/sessions/{1}/finalize", _baseUrl, sessionId),
                new StringContent("{}", Encoding.UTF8, "application/json"), ct).ConfigureAwait(false);
            await EnsureSuccessOrThrowDetailedAsync(response);
            var json = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return JsonConvert.DeserializeObject<FinalizeResponse>(json);
        }

        public async Task<InferenceStartResponse> StartInferenceAsync(
            InferenceRequest request, CancellationToken ct = default)
        {
            var json = JsonConvert.SerializeObject(request);
            var content = new StringContent(json, Encoding.UTF8, "application/json");
            var response = await _client.PostAsync(_baseUrl + "/inference", content, ct).ConfigureAwait(false);
            if ((int)response.StatusCode != 202)
                await EnsureSuccessOrThrowDetailedAsync(response);
            var responseJson = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return JsonConvert.DeserializeObject<InferenceStartResponse>(responseJson);
        }

        public async Task<InferenceStatusResponse> GetInferenceStatusAsync(
            string jobId, CancellationToken ct = default)
        {
            var response = await _client.GetAsync(
                string.Format("{0}/inference/{1}", _baseUrl, jobId), ct).ConfigureAwait(false);
            await EnsureSuccessOrThrowDetailedAsync(response);
            var json = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return JsonConvert.DeserializeObject<InferenceStatusResponse>(json);
        }

        public async Task DeleteSessionAsync(
            string sessionId, CancellationToken ct = default)
        {
            var response = await _client.DeleteAsync(
                string.Format("{0}/sessions/{1}", _baseUrl, sessionId), ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode && (int)response.StatusCode != 204)
                await EnsureSuccessOrThrowDetailedAsync(response);
        }

        public void Dispose()
        {
            _client?.Dispose();
        }
    }
}
