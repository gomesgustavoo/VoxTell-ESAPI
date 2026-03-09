using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using VMS.TPS.Common.Model.API;
using VMS.TPS.Common.Model.Types;
using VoxTell_Interface.Models;
using VoxTell_Interface.Services;

namespace VoxTell_Interface.ViewModels
{
    public class MainViewModel : INotifyPropertyChanged, IDisposable
    {
        private readonly ScriptContext _context;
        private readonly Control _uiControl;
        private readonly VoxTellApiClient _apiClient;
        private CancellationTokenSource _cts;

        private string _sessionId;
        private string _jobId;
        private InferenceStatusResponse _lastInferenceResult;

        // TPS Image reference for voxel extraction
        private readonly Image _image;
        private readonly StructureSet _structureSet;
        private readonly int _xSize;
        private readonly int _ySize;
        private readonly int _zSize;

        private string _backendUrl = "http://localhost:8000";
        public string BackendUrl
        {
            get { return _backendUrl; }
            set { _backendUrl = value; OnPropertyChanged(); UpdateApiClient(); }
        }

        private string _healthStatus = "Unknown";
        public string HealthStatus
        {
            get { return _healthStatus; }
            set { _healthStatus = value; OnPropertyChanged(); }
        }

        private bool _isHealthy;
        public bool IsHealthy
        {
            get { return _isHealthy; }
            set
            {
                _isHealthy = value;
                OnPropertyChanged();
                OnPropertyChanged("CanStartSession");
            }
        }

        private bool _isSessionActive;
        public bool IsSessionActive
        {
            get { return _isSessionActive; }
            set
            {
                _isSessionActive = value;
                OnPropertyChanged();
                OnPropertyChanged("CanStartSession");
                OnPropertyChanged("CanRunInference");
                OnPropertyChanged("CanImportContours");
            }
        }

        private double _uploadProgress;
        public double UploadProgress
        {
            get { return _uploadProgress; }
            set { _uploadProgress = value; OnPropertyChanged(); }
        }

        private string _uploadStatusText = "";
        public string UploadStatusText
        {
            get { return _uploadStatusText; }
            set { _uploadStatusText = value; OnPropertyChanged(); }
        }

        private string _promptsText = "";
        public string PromptsText
        {
            get { return _promptsText; }
            set
            {
                _promptsText = value;
                OnPropertyChanged();
                OnPropertyChanged("CanRunInference");
            }
        }

        private string _inferenceStatus = "";
        public string InferenceStatus
        {
            get { return _inferenceStatus; }
            set
            {
                _inferenceStatus = value;
                OnPropertyChanged();
                OnPropertyChanged("CanImportContours");
            }
        }

        private string _resultsDisplay = "";
        public string ResultsDisplay
        {
            get { return _resultsDisplay; }
            set { _resultsDisplay = value; OnPropertyChanged(); }
        }

        private bool _isBusy;
        public bool IsBusy
        {
            get { return _isBusy; }
            set
            {
                _isBusy = value;
                OnPropertyChanged();
                OnPropertyChanged("CanStartSession");
                OnPropertyChanged("CanRunInference");
                OnPropertyChanged("CanImportContours");
                OnPropertyChanged("CanCheckHealth");
            }
        }

        private string _statusMessage = "Ready.";
        public string StatusMessage
        {
            get { return _statusMessage; }
            set { _statusMessage = value; OnPropertyChanged(); }
        }

        public bool CanStartSession
        {
            get { return IsHealthy && !IsSessionActive && !IsBusy && _image != null; }
        }

        public bool CanRunInference
        {
            get { return IsSessionActive && !IsBusy && !string.IsNullOrWhiteSpace(PromptsText); }
        }

        public bool CanImportContours
        {
            get
            {
                return _lastInferenceResult != null
                    && _lastInferenceResult.Results != null
                    && _lastInferenceResult.Results.Count > 0
                    && !IsBusy;
            }
        }

        public bool CanCheckHealth
        {
            get { return !IsBusy; }
        }

        public string ImageInfo { get; private set; }

        public MainViewModel(ScriptContext context, Control uiControl)
        {
            _context = context;
            _uiControl = uiControl;
            _apiClient = new VoxTellApiClient(_backendUrl);

            _image = context.Image;
            _structureSet = context.StructureSet;

            if (_image != null)
            {
                _xSize = _image.XSize;
                _ySize = _image.YSize;
                _zSize = _image.ZSize;

                ImageInfo = string.Format(
                    "{0}x{1}x{2}  |  {3:F1}mm res",
                    _xSize, _ySize, _zSize, _image.XRes);

                if (_structureSet != null)
                {
                    ImageInfo += string.Format("  |  SS: {0}", _structureSet.Id);
                }
            }
            else
            {
                _xSize = 0;
                _ySize = 0;
                _zSize = 0;
                ImageInfo = "No image available. Open a plan with an image before running.";
                StatusMessage = "No image found. Session upload disabled.";
            }
        }

        private void InvokeOnUI(Action action)
        {
            if (_uiControl?.InvokeRequired == true)
                _uiControl.Invoke(action);
            else
                action();
        }

        private void UpdateApiClient()
        {
            _apiClient?.UpdateBaseUrl(_backendUrl);
        }

        public async void CheckHealthAsync()
        {
            IsBusy = true;
            StatusMessage = "Checking backend health...";
            try
            {
                _cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                var health = await Task.Run(() => _apiClient.CheckHealthAsync(_cts.Token));
                InvokeOnUI(() =>
                {
                    IsHealthy = health.Status == "ok" || health.Status == "healthy";
                    HealthStatus = IsHealthy
                        ? string.Format("Connected (model: {0})", health.ModelLoaded)
                        : string.Format("Unhealthy: {0}", health.Status);
                    StatusMessage = IsHealthy
                        ? "Connected. Ready to upload."
                        : "Backend reported unhealthy status.";
                });
            }
            catch (Exception ex)
            {
                InvokeOnUI(() =>
                {
                    IsHealthy = false;
                    HealthStatus = string.Format("Error: {0}", ex.Message);
                    StatusMessage = string.Format("Health check failed: {0}", ex.Message);
                });
            }
            finally
            {
                InvokeOnUI(() => IsBusy = false);
            }
        }

        public async void StartSessionAndUploadAsync()
        {
            if (_image == null)
            {
                StatusMessage = "Cannot start session: no image available.";
                return;
            }

            IsBusy = true;
            _cts = new CancellationTokenSource();
            StatusMessage = "Creating session...";
            UploadProgress = 0;

            try
            {
                int xSize = _image.XSize;
                int ySize = _image.YSize;
                int zSize = _image.ZSize;

                var sessionRequest = new CreateSessionRequest
                {
                    XSize = xSize,
                    YSize = ySize,
                    ZSize = zSize,
                    XRes = _image.XRes,
                    YRes = _image.YRes,
                    ZRes = _image.ZRes,
                    Origin = new[] { _image.Origin.x, _image.Origin.y, _image.Origin.z },
                    RowDirection = new[] { _image.XDirection.x, _image.XDirection.y, _image.XDirection.z },
                    ColDirection = new[] { _image.YDirection.x, _image.YDirection.y, _image.YDirection.z },
                    SliceDirection = new[] { _image.ZDirection.x, _image.ZDirection.y, _image.ZDirection.z }
                };

                var sessionResponse = await Task.Run(
                    () => _apiClient.CreateSessionAsync(sessionRequest, _cts.Token));

                _sessionId = sessionResponse.SessionId;
                int totalSlices = sessionResponse.SlicesTotal;

                InvokeOnUI(() =>
                {
                    StatusMessage = string.Format(
                        "Uploading {0} slices...", totalSlices);
                    UploadStatusText = string.Format("0 / {0}", totalSlices);
                });

                for (int z = 0; z < totalSlices; z++)
                {
                    _cts.Token.ThrowIfCancellationRequested();

                    string encodedSlice = null;
                    int currentZ = z;
                    InvokeOnUI(() =>
                    {
                        encodedSlice = VoxelEncoder.ExtractAndEncodeSlice(_image, currentZ);
                    });

                    await Task.Run(() =>
                        _apiClient.UploadSliceAsync(_sessionId, currentZ, encodedSlice, _cts.Token));

                    InvokeOnUI(() =>
                    {
                        UploadProgress = (double)(currentZ + 1) / totalSlices;
                        UploadStatusText = string.Format("{0} / {1}", currentZ + 1, totalSlices);
                        StatusMessage = string.Format(
                            "Uploading slice {0} of {1}...", currentZ + 1, totalSlices);
                    });
                }

                InvokeOnUI(() => StatusMessage = "All slices uploaded. Finalizing...");

                await Task.Run(() => _apiClient.FinalizeSessionAsync(_sessionId, _cts.Token));

                InvokeOnUI(() =>
                {
                    IsSessionActive = true;
                    StatusMessage = "Session ready. Enter prompts and run inference.";
                    UploadStatusText = string.Format("{0} / {0}", totalSlices);
                });
            }
            catch (OperationCanceledException)
            {
                InvokeOnUI(() => StatusMessage = "Operation cancelled.");
                await CleanupSessionAsync();
            }
            catch (Exception ex)
            {
                InvokeOnUI(() =>
                {
                    var errorMsg = ex.Message;
                    if (errorMsg.Length > 300)
                        errorMsg = errorMsg.Substring(0, 300) + "...";
                    StatusMessage = string.Format("Upload error: {0}", errorMsg);
                    UploadStatusText = "Error";
                });
                await CleanupSessionAsync();
            }
            finally
            {
                InvokeOnUI(() => IsBusy = false);
            }
        }

        public async void RunInferenceAsync()
        {
            IsBusy = true;
            _cts = new CancellationTokenSource();
            StatusMessage = "Starting inference...";
            InferenceStatus = "Submitting...";
            ResultsDisplay = "";
            _lastInferenceResult = null;

            try
            {
                var prompts = PromptsText
                    .Split(new[] { ',', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries)
                    .Select(p => p.Trim())
                    .Where(p => !string.IsNullOrEmpty(p))
                    .ToArray();

                if (prompts.Length == 0)
                {
                    InvokeOnUI(() =>
                    {
                        StatusMessage = "No valid prompts entered.";
                        IsBusy = false;
                    });
                    return;
                }

                var inferenceRequest = new InferenceRequest
                {
                    SessionId = _sessionId,
                    Prompts = prompts
                };

                var startResponse = await Task.Run(
                    () => _apiClient.StartInferenceAsync(inferenceRequest, _cts.Token));

                _jobId = startResponse.JobId;

                InvokeOnUI(() =>
                {
                    InferenceStatus = "Pending...";
                    StatusMessage = string.Format("Inference job submitted: {0}", _jobId);
                });

                bool completed = false;
                while (!completed)
                {
                    _cts.Token.ThrowIfCancellationRequested();
                    await Task.Delay(2000, _cts.Token);

                    var status = await Task.Run(
                        () => _apiClient.GetInferenceStatusAsync(_jobId, _cts.Token));

                    InvokeOnUI(() =>
                    {
                        InferenceStatus = string.Format("Status: {0}", status.Status);
                    });

                    switch (status.Status)
                    {
                        case "completed":
                            completed = true;
                            _lastInferenceResult = status;
                            InvokeOnUI(() =>
                            {
                                InferenceStatus = "Completed";
                                ResultsDisplay = FormatResults(status.Results);
                                StatusMessage = "Importing structures...";
                                ProcessContourResults();
                            });
                            break;

                        case "failed":
                            completed = true;
                            InvokeOnUI(() =>
                            {
                                StatusMessage = string.Format("Inference failed: {0}",
                                    status.Error ?? "Unknown error");
                                InferenceStatus = "Failed";
                            });
                            break;
                    }
                }
            }
            catch (OperationCanceledException)
            {
                InvokeOnUI(() =>
                {
                    StatusMessage = "Inference cancelled.";
                    InferenceStatus = "Cancelled";
                });
            }
            catch (Exception ex)
            {
                InvokeOnUI(() =>
                {
                    StatusMessage = string.Format("Inference error: {0}", ex.Message);
                    InferenceStatus = "Error";
                });
            }
            finally
            {
                InvokeOnUI(() => IsBusy = false);
            }
        }

        public void ValidateStructures()
        {
            if (_lastInferenceResult == null || _lastInferenceResult.Results == null)
                return;

            try
            {
                var importer = new EsapiStructureImporter(_context);
                var validation = importer.ValidateStructures(
                    _lastInferenceResult.Results, out var warnings);

                var sb = new System.Text.StringBuilder();
                sb.AppendLine();
                sb.AppendLine("--- Structure Validation ---");

                foreach (var name in validation.Available)
                {
                    string eclipseId = validation.MatchMap.ContainsKey(name)
                        ? validation.MatchMap[name] : name;
                    if (string.Equals(name, eclipseId, StringComparison.OrdinalIgnoreCase))
                        sb.AppendLine(string.Format("  [x] {0}", name));
                    else
                        sb.AppendLine(string.Format("  [x] {0} -> \"{1}\"", name, eclipseId));
                }

                foreach (var name in validation.Missing)
                {
                    sb.AppendLine(string.Format("  [+] {0} (will be created)", name));
                }

                if (validation.Missing.Count == 0)
                    sb.AppendLine(string.Format("All {0} structure(s) found.", validation.Available.Count));
                else
                    sb.AppendLine(string.Format("{0} existing, {1} will be auto-created.",
                        validation.Available.Count, validation.Missing.Count));

                ResultsDisplay += sb.ToString().TrimEnd();
                StatusMessage = string.Format("Ready to import {0} structure(s).",
                    validation.Available.Count + validation.Missing.Count);
            }
            catch (Exception ex)
            {
                StatusMessage = string.Format("Validation error: {0}", ex.Message);
            }
        }

        public void ProcessContourResults()
        {
            if (_lastInferenceResult == null || _lastInferenceResult.Results == null)
                return;

            IsBusy = true;
            StatusMessage = "Importing structures into Eclipse...";

            try
            {
                var importer = new EsapiStructureImporter(_context);
                var imported = importer.ProcessResults(
                    _lastInferenceResult.Results, out var warnings);

                StatusMessage = string.Format(
                    "Import complete: {0} structure(s) written.",
                    imported.Count);

                var sb = new System.Text.StringBuilder();
                sb.AppendLine("--- Import Results ---");

                if (imported.Count > 0)
                {
                    sb.AppendLine(string.Format("Imported {0} structure(s):", imported.Count));
                    foreach (var item in imported)
                        sb.AppendLine(string.Format("  + {0}", item));
                }
                else
                {
                    sb.AppendLine("No structures were imported.");
                }

                if (warnings.Count > 0)
                {
                    sb.AppendLine();
                    sb.AppendLine("Notes:");
                    foreach (var w in warnings)
                        sb.AppendLine(string.Format("  {0}", w));
                }

                ResultsDisplay += "\n" + sb.ToString().TrimEnd();
            }
            catch (Exception ex)
            {
                StatusMessage = string.Format("Import error: {0}", ex.Message);
                MessageBox.Show(
                    string.Format(
                        "Failed to import structures:\n{0}", ex.Message),
                    "Import Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally
            {
                IsBusy = false;
            }
        }

        public void Cancel()
        {
            _cts?.Cancel();
            StatusMessage = "Cancelling...";
        }

        private string FormatResults(List<InferenceResult> results)
        {
            if (results == null || results.Count == 0)
                return "No results returned.";

            var lines = new List<string>();
            foreach (var r in results)
            {
                int totalPoints = r.Contours != null
                    ? r.Contours.Sum(c => c.PointsLps != null ? c.PointsLps.Count : 0)
                    : 0;
                int sliceCount = r.Contours != null ? r.Contours.Count : 0;
                lines.Add(string.Format("\"{0}\" -> {1} slices, {2} pts",
                    r.Prompt, sliceCount, totalPoints));
            }
            return string.Join("\n", lines);
        }

        private async Task CleanupSessionAsync()
        {
            if (!string.IsNullOrEmpty(_sessionId))
            {
                try
                {
                    await Task.Run(() => _apiClient.DeleteSessionAsync(_sessionId));
                }
                catch { }
                _sessionId = null;
            }
        }

        public event PropertyChangedEventHandler PropertyChanged;

        protected void OnPropertyChanged([CallerMemberName] string name = null)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }

        public void Dispose()
        {
            _cts?.Cancel();
            _cts?.Dispose();
            if (!string.IsNullOrEmpty(_sessionId))
            {
                try { _apiClient.DeleteSessionAsync(_sessionId).Wait(3000); }
                catch { }
            }
            _apiClient?.Dispose();
        }
    }
}
