# VoxTell Eclipse 

> **A Project that connects [VoxTell](https://github.com/MIC-DKFZ/VoxTell) — a free-text-prompted 3D medical image segmentation model — to Varian Eclipse via the Eclipse Scripting API (ESAPI).**

This project is a **fork of the original VoxTell model** ([MIC-DKFZ/VoxTell](https://github.com/MIC-DKFZ/VoxTell)), extended with a REST API layer designed to act as a backend for a clinical Eclipse plugin. The companion C# interface script lives in a separate repository: [gomesgustavoo/voxtell-eclipse-interface](https://github.com/gomesgustavoo/voxtell-eclipse-interface).

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [The Data Conversion Pipeline](#the-data-conversion-pipeline)
  - [1. DICOM Geometry → LPS Affine](#1-dicom-geometry--lps-affine)
  - [2. ESAPI Voxels → NIfTI (LPS → RAS)](#2-esapi-voxels--nifti-lps--ras)
  - [3. VoxTell Inference](#3-voxtell-inference)
  - [4. Segmentation Masks → LPS Contour Points](#4-segmentation-masks--lps-contour-points)
- [API Reference](#api-reference)
- [Setup & Running](#setup--running)
- [Configuration](#configuration)
- [Proprietary DLL Notice](#proprietary-dll-notice)
- [License](#license)

---

## Overview

Varian Eclipse is a clinical treatment planning system widely used in radiation oncology. Its ESAPI allows scripting access to patient CT volumes and structure sets. VoxTell is a state-of-the-art vision-language segmentation model capable of segmenting any anatomy from a plain-text prompt (e.g., `"liver"`, `"left kidney"`).

This project bridges the two by:

1. **Accepting raw CT voxel data** streamed slice-by-slice from the Eclipse contouring workspace via HTTP.
2. **Reconstructing the volume** as a NIfTI file, properly converting coordinate systems (DICOM LPS → NIfTI RAS).
3. **Running VoxTell inference** driven by free-text anatomical prompts.
4. **Returning contour points** in DICOM LPS patient coordinates, ready to be drawn as RT Structure Set contours inside Eclipse.

The API exposes a **session-based, async workflow** so that the .NET ESAPI script can stream a full CT volume (often 200+ slices) over HTTP without holding the Eclipse UI thread.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Varian Eclipse (Windows)           │
│                                                 │
│  C# ESAPI Script (VoxTell-Interface)            │
│  ├─ VoxelEncoder   → base64(gzip(int32-LE))     │
│  ├─ VoxTellApiClient → HTTP calls to this API   │
│  └─ EsapiStructureImporter → apply LPS contours │
└───────────────────┬─────────────────────────────┘
                    │ HTTP/JSON (LAN or localhost)
┌───────────────────▼─────────────────────────────┐
│            VoxTell Backend  (this repo)         │
│                                                 │
│  POST  /sessions              ← volume metadata │
│  PUT   /sessions/{id}/slices  ← one slice each  │
│  POST  /sessions/{id}/finalize ← assemble NIfTI │
│  POST  /inference             ← text prompts    │
│  GET   /inference/{job_id}    ← poll results    │
│                                                 │
│  nifti_builder  → coord conversion + .nii.gz    │
│  voxtell_worker → VoxTell model inference       │
│  contour_utils  → masks → LPS contour points    │
└─────────────────────────────────────────────────┘
```

---

## The Data Conversion Pipeline

This is the **technical core** of the project. Moving data between Eclipse's DICOM world and a deep learning model requires careful coordinate system management at every step.

### 1. DICOM Geometry → LPS Affine

Eclipse exposes image geometry through ESAPI properties (`image.Origin`, `image.RowDirection`, `image.ColumnDirection`, `image.XRes`, etc.). The C# script serialises these and sends them to `POST /sessions`.

The server constructs a **4×4 affine matrix** that maps integer voxel indices `(x, y, z)` to millimetre positions in the **DICOM LPS patient coordinate system** (Left, Posterior, Superior):

```
affine_lps[:3, 0] = row_direction    × x_res   # column  (+X) axis
affine_lps[:3, 1] = col_direction    × y_res   # row     (+Y) axis
affine_lps[:3, 2] = slice_direction  × z_res   # slice   (+Z) axis
affine_lps[:3, 3] = origin                     # position of voxel (0,0,0)
```

This affine is stored in the session and re-used both for NIfTI construction and later for back-projecting contour points.

### 2. ESAPI Voxels → NIfTI (LPS → RAS)

**Why this is non-trivial:**

| System | X | Y | Z |
|---|---|---|---|
| DICOM / Eclipse (LPS) | Patient Left | Patient Posterior | Patient Superior |
| NIfTI / VoxTell (RAS) | Patient **Right** | Patient **Anterior** | Patient Superior |

The first two axes are flipped. A naive copy would produce a mirrored, anteroposterior-inverted volume.

**On the C# side** (`VoxelEncoder.cs`): each 2D slice is extracted from the ESAPI `Frame` as `ushort[xSize, ySize]`, widened to `int32`, serialised as **little-endian bytes**, **gzip-compressed**, and **base64-encoded** before being sent via `PUT /sessions/{id}/slices/{z}`. This reduces HTTP payload size by ~4×.

```csharp
// ushort → int32 LE bytes → gzip → base64
ushort[,] voxels = new ushort[xSize, ySize];
frame.GetVoxels(zIndex, voxels);
// ... (see VoxelEncoder.cs)
return Convert.ToBase64String(compressedBytes);
```

**On the Python side** (`nifti_builder.py`): slices are decoded and accumulated into an `int32 (Z, Y, X)` NumPy buffer. At finalisation:

```python
# 1. Transpose (Z,Y,X) → (X,Y,Z) for NIfTI axis convention
arr_xyz = volume.transpose(2, 1, 0).astype(np.float32)

# 2. Flip X and Y to convert LPS → RAS
affine_ras = np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine_lps

# 3. Save NIfTI
nib.Nifti1Image(arr_xyz, affine_ras).to_filename(output_path)
```

The LPS affine is **preserved intact** in the session — it is needed in step 4 to map predictions back to DICOM space.

### 3. VoxTell Inference

The VoxTell worker loads the NIfTI volume and runs the segmentation model against a list of free-text anatomical prompts (e.g., `["liver", "spleen", "right kidney"]`). The text encoder uses **Qwen3-Embedding-4B** to produce prompt embeddings that guide the 3D segmentation head.

Inference runs in a **background `asyncio` task** and results are polled via `GET /inference/{job_id}`, so the Eclipse UI thread is never blocked.

The model's output is a set of **binary 3D masks** in the model's internal RAS orientation — one mask per prompt.

### 4. Segmentation Masks → LPS Contour Points

This step inverts the NIfTI orientation and back-projects predictions into DICOM-compatible coordinates (`contour_utils.py`).

1. **Write back to original DICOM orientation** using `nnUNet`'s `NibabelIOWithReorient.write_seg()`, which reverses whatever reorientation nibabel applied when loading.
2. **Extract 2D contour lines** per z-slice using `skimage.measure.find_contours` (operating on the `(X, Y)` voxel plane).
3. **Convert voxel indices → LPS mm** using the stored affine:

```python
# vox_coords: (N, 4) homogeneous voxel indices [x, y, z, 1]
pts_lps = (vox_coords @ affine_lps.T)[:, :3]
```

The result — `ContourSlice` objects containing `points_lps` — can be passed **directly** to Eclipse's structure API:

```csharp
structure.AddContourOnImagePlane(contour_points_lps, z_index);
```

---

## API Reference

Interactive documentation (Swagger UI) is available at `http://localhost:8000/docs` when the server is running.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Check server and model status |
| `POST` | `/sessions` | Create a session with volume geometry metadata |
| `PUT` | `/sessions/{id}/slices/{z}` | Upload one gzip+base64 encoded CT slice |
| `POST` | `/sessions/{id}/finalize` | Assemble all slices into a NIfTI file |
| `DELETE` | `/sessions/{id}` | Remove session and its NIfTI file |
| `POST` | `/inference` | Submit segmentation job with text prompts |
| `GET` | `/inference/{job_id}` | Poll job status, retrieve LPS contour results |

**Session lifecycle:**

```
POST /sessions  →  PUT /sessions/{id}/slices/{z} (×N slices)
             →  POST /sessions/{id}/finalize
             →  POST /inference  →  GET /inference/{job_id}  (poll)
             →  DELETE /sessions/{id}
```

---

## Setup & Running

### Prerequisites

- Linux with NVIDIA GPU (CUDA 11.8+) — CPU inference is supported but slow for clinical volumes
- [Miniconda](https://docs.anaconda.com/miniconda/) or Anaconda

### 1. Create the environment

```bash
conda create -n voxtell python=3.12 -y
conda activate voxtell
```

### 2. Install PyTorch (CUDA)

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu118
```

### 3. Install this package with API extras

```bash
git clone https://github.com/gomesgustavoo/voxtell-eclipse-extension.git
cd voxtell-eclipse-extension
pip install -e ".[api]"
```

### 4. Download the VoxTell model weights

```bash
python download_model.py
```

Or set `VOXTELL_MODEL_DIR` to point to a directory containing `plans.json` and `fold_0/checkpoint_final.pth`.

### 5. Start the server

```bash
VOXTELL_MODEL_DIR=/path/to/model bash run.sh
```

Or copy `.env.example` to `.env`, fill in the values, and just run `bash run.sh`.

The API will be available at `http://0.0.0.0:8000`.

---

## Configuration

All settings are configured through environment variables (or a `.env` file). See `.env.example` for the full list.

| Variable | Default | Description |
|---|---|---|
| `VOXTELL_MODEL_DIR` | *(required)* | Path to model directory (`plans.json` + `fold_0/`) |
| `VOXTELL_DEVICE` | `cuda` | `cuda` or `cpu` |
| `VOXTELL_GPU_ID` | `0` | GPU index when using CUDA |
| `VOXTELL_TEXT_MODEL` | `Qwen/Qwen3-Embedding-4B` | HuggingFace text encoder model ID |
| `VOXTELL_SESSION_DIR` | `/tmp/voxtell_sessions` | Where NIfTI session files are stored |
| `VOXTELL_SESSION_TTL_SECONDS` | `7200` | Session expiry (seconds) |
| `VOXTELL_HOST` | `0.0.0.0` | Uvicorn bind address |
| `VOXTELL_PORT` | `8000` | Uvicorn port |

---

## Proprietary DLL Notice

The companion C# ESAPI script ([voxtell-eclipse-interface](https://github.com/gomesgustavoo/voxtell-eclipse-interface)) depends on **Varian Medical Systems proprietary DLL files** (`VMS.CA.Scripting.dll` and related assemblies) that are **not redistributable** and **not included** in either repository.

To compile and run the Eclipse interface you must:

1. Have a licensed installation of **Varian Eclipse** (version 16+).
2. Copy the required `.dll` files from your Eclipse installation into the `reference/` directory of the interface project.
3. Build with Visual Studio 2019+ targeting **.NET Framework 4.6.2**.

**This repository (the Python API server) has no such dependency** and runs entirely on open-source libraries.

---

## License

VoxTell is developed by the [Medical Image Computing Lab (MIC)](https://www.dkfz.de/en/mic/index.php) at DKFZ Heidelberg.

- **Paper:** [arXiv:2511.11450](https://arxiv.org/abs/2511.11450)
- **Original repository:** [MIC-DKFZ/VoxTell](https://github.com/MIC-DKFZ/VoxTell)

> **Rokuss et al.** (2025). *VoxTell: Free-Text Promptable Universal 3D Medical Image Segmentation*. arXiv:2511.11450.

```bibtex
@misc{rokuss2025voxtell,
  title={VoxTell: Free-Text Promptable Universal 3D Medical Image Segmentation}, 
  author={Maximilian Rokuss and Moritz Langenberg and Yannick Kirchhoff and Fabian Isensee and Benjamin Hamm and Constantin Ulrich and Sebastian Regnery and Lukas Bauer and Efthimios Katsigiannopulos and Tobias Norajitra and Klaus Maier-Hein},
  year={2025},
  eprint={2511.11450},
  archivePrefix={arXiv}
}
```
Thanks Max and Moritz for developing this amazing work.

If you use this work, please let me know:
📧 https://www.linkedin.com/in/gustavoogomesss/ 

The Python source code in this repository (the `api/` package) is original work and is released under the **Apache 2.0 License** (see [LICENSE](LICENSE)), consistent with the upstream VoxTell project.