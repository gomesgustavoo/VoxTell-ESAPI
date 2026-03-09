using System.Collections.Generic;
using Newtonsoft.Json;

namespace VoxTell_Interface.Models
{
    public class CreateSessionRequest
    {
        [JsonProperty("x_size")] public int XSize { get; set; }
        [JsonProperty("y_size")] public int YSize { get; set; }
        [JsonProperty("z_size")] public int ZSize { get; set; }
        [JsonProperty("x_res")] public double XRes { get; set; }
        [JsonProperty("y_res")] public double YRes { get; set; }
        [JsonProperty("z_res")] public double ZRes { get; set; }
        [JsonProperty("origin")] public double[] Origin { get; set; }
        [JsonProperty("row_direction")] public double[] RowDirection { get; set; }
        [JsonProperty("col_direction")] public double[] ColDirection { get; set; }
        [JsonProperty("slice_direction")] public double[] SliceDirection { get; set; }
    }

    public class SliceUploadRequest
    {
        [JsonProperty("voxel_data_b64")] public string VoxelDataB64 { get; set; }
    }

    public class InferenceRequest
    {
        [JsonProperty("session_id")] public string SessionId { get; set; }
        [JsonProperty("prompts")] public string[] Prompts { get; set; }
    }

    public class HealthResponse
    {
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("model_loaded")] public bool ModelLoaded { get; set; }
    }

    public class CreateSessionResponse
    {
        [JsonProperty("session_id")] public string SessionId { get; set; }
        [JsonProperty("slices_total")] public int SlicesTotal { get; set; }
    }

    public class SliceUploadResponse
    {
        [JsonProperty("z_index")] public int ZIndex { get; set; }
        [JsonProperty("slices_received")] public int SlicesReceived { get; set; }
        [JsonProperty("slices_total")] public int SlicesTotal { get; set; }
    }

    public class FinalizeResponse
    {
        [JsonProperty("session_id")] public string SessionId { get; set; }
        [JsonProperty("slices_total")] public int SlicesTotal { get; set; }
    }

    public class InferenceStartResponse
    {
        [JsonProperty("job_id")] public string JobId { get; set; }
        [JsonProperty("message")] public string Message { get; set; }
    }

    public class InferenceStatusResponse
    {
        [JsonProperty("job_id")] public string JobId { get; set; }
        [JsonProperty("status")] public string Status { get; set; }
        [JsonProperty("error")] public string Error { get; set; }
        [JsonProperty("results")] public List<InferenceResult> Results { get; set; }
    }

    public class InferenceResult
    {
        [JsonProperty("prompt")] public string Prompt { get; set; }
        [JsonProperty("contours")] public List<ContourSlice> Contours { get; set; }
        [JsonProperty("mask_b64")] public string MaskB64 { get; set; }
    }

    public class ContourSlice
    {
        [JsonProperty("z_index")] public int ZIndex { get; set; }
        [JsonProperty("points_lps")] public List<double[]> PointsLps { get; set; }
    }
}
