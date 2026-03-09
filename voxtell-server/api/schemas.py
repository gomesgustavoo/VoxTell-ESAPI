from typing import Literal

from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    """
    Metadata for a new session — dimensions, spacing, and DICOM geometry.

    No voxel data is included; slices are uploaded individually via
    PUT /sessions/{id}/slices/{z}.
    """

    # Image dimensions
    x_size: int = Field(..., gt=0, description="Number of columns  (image.XSize)")
    y_size: int = Field(..., gt=0, description="Number of rows     (image.YSize)")
    z_size: int = Field(..., gt=0, description="Number of slices   (image.ZSize)")

    # Voxel spacing (mm)
    x_res: float = Field(..., gt=0, description="mm per column  (image.XRes)")
    y_res: float = Field(..., gt=0, description="mm per row     (image.YRes)")
    z_res: float = Field(..., gt=0, description="mm per slice   (image.ZRes)")

    # DICOM geometry in LPS patient coordinates (mm)
    origin: list[float] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="Position of voxel (0,0,0) in LPS mm (image.Origin)",
    )
    row_direction: list[float] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="Unit vector along increasing x (image.RowDirection)",
    )
    col_direction: list[float] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="Unit vector along increasing y (image.ColumnDirection)",
    )
    slice_direction: list[float] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="Unit vector along increasing z (image.SliceDirection)",
    )


class SessionCreatedResponse(BaseModel):
    session_id: str
    slices_total: int


class SliceUploadRequest(BaseModel):
    """Single 2D slice: base64(gzip(int32-LE flat)), shape y_size x x_size."""

    voxel_data_b64: str = Field(
        ...,
        description="base64( gzip( int32-LE bytes ) ) for one (Y, X) row-major slice",
    )


class SliceUploadResponse(BaseModel):
    z_index: int
    slices_received: int
    slices_total: int


class InferenceRequest(BaseModel):
    session_id: str
    prompts: list[str] = Field(
        ...,
        min_length=1,
        description='Anatomical text prompts, e.g. ["liver", "left kidney"]',
    )


class InferenceAcceptedResponse(BaseModel):
    job_id: str
    message: str = "Inference started. Poll GET /inference/{job_id} for status."


class ContourSlice(BaseModel):
    """Contour boundary for a single DICOM z-slice."""

    z_index: int = Field(
        ..., description="DICOM slice index for AddContourOnImagePlane"
    )
    points_lps: list[list[float]] = Field(
        ...,
        description="Contour points in LPS patient coordinates (mm), shape [[x,y,z], ...]",
    )


class SegmentationResult(BaseModel):
    prompt: str
    contours: list[ContourSlice] = Field(
        ...,
        description="One ContourSlice per occupied DICOM z-slice",
    )
    mask_b64: str | None = Field(
        None,
        description=(
            "Optional binary mask: base64( gzip( uint8 flat (Z,Y,X) row-major ) ), "
            "same shape as the uploaded volume"
        ),
    )


class InferenceStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    error: str | None = None
    results: list[SegmentationResult] | None = None


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
