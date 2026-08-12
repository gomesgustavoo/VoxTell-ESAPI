"""Pydantic request/response models — the wire contract for the ESAPI client.

Field names are snake_case to match the v1 protocol the existing C# models
already use (``[JsonProperty("x_size")]`` etc.), so porting the plugin is a
matter of changing endpoints, not re-annotating every DTO.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import settings


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
class Geometry(BaseModel):
    """Volume shape, spacing and DICOM orientation, straight from ESAPI.

    Everything here comes off the ESAPI ``Image``: ``XSize``/``YSize``/``ZSize``,
    ``XRes``/``YRes``/``ZRes``, ``Origin``, ``RowDirection``,
    ``ColumnDirection``, ``SliceDirection``.
    """

    x_size: int = Field(..., gt=0, description="Columns (image.XSize)")
    y_size: int = Field(..., gt=0, description="Rows (image.YSize)")
    z_size: int = Field(..., gt=0, description="Slices (image.ZSize)")

    x_res: float = Field(..., gt=0, description="mm per column (image.XRes)")
    y_res: float = Field(..., gt=0, description="mm per row (image.YRes)")
    z_res: float = Field(..., gt=0, description="mm per slice (image.ZRes)")

    origin: list[float] = Field(
        ..., min_length=3, max_length=3,
        description="LPS mm position of voxel (0,0,0) (image.Origin)",
    )
    row_direction: list[float] = Field(
        ..., min_length=3, max_length=3, description="Unit vector along +x (image.RowDirection)"
    )
    col_direction: list[float] = Field(
        ..., min_length=3, max_length=3, description="Unit vector along +y (image.ColumnDirection)"
    )
    slice_direction: list[float] = Field(
        ..., min_length=3, max_length=3, description="Unit vector along +z (image.SliceDirection)"
    )

    # ESAPI's GetVoxels returns stored values; VoxelToDisplayValue applies this
    # linear map to reach Hounsfield units. Send them and the server rescales --
    # see voxtell_cloud/geometry.py for why it matters (crop_to_nonzero).
    scaling_slope: float = Field(1.0, description="image.VoxelToDisplayValue slope")
    scaling_intercept: float = Field(0.0, description="image.VoxelToDisplayValue intercept")

    @property
    def voxels(self) -> int:
        return self.x_size * self.y_size * self.z_size


# --------------------------------------------------------------------------- #
# Multipart upload primitives
# --------------------------------------------------------------------------- #
# Shared by volumes and by the legacy inline job upload, deliberately: the two
# flows use the identical part/ETag exchange, so the C# client reuses one DTO for
# both rather than carrying a near-duplicate.
class UploadPart(BaseModel):
    part_number: int = Field(..., ge=1, description="1-based, matches the presigned URL order")
    url: str


class SubmitPart(BaseModel):
    part_number: int = Field(..., ge=1)
    etag: str = Field(..., description="ETag header returned by the part PUT, quotes included")


# --------------------------------------------------------------------------- #
# Volumes
# --------------------------------------------------------------------------- #
SHA256_HEX = r"^[0-9a-fA-F]{64}$"


def _normalise_sha256(v: str) -> str:
    """Lower-case a hex digest.

    Normalise rather than reject: C#'s ``ToString("x2")`` produces lower case but
    ``BitConverter.ToString().Replace("-","")`` produces upper, and a dedup key
    that silently depends on which one the client happened to use is exactly the
    kind of thing that bites at 2 a.m. Both spellings must map to one key.
    """
    return v.lower()


class VolumeCreateRequest(BaseModel):
    geometry: Geometry
    upload_bytes: int = Field(
        ..., gt=0,
        description="Exact byte length of the gzip-compressed volume you are about to upload",
    )
    content_sha256: str = Field(
        ..., pattern=SHA256_HEX,
        description=(
            "sha256 of the UNCOMPRESSED int16 little-endian (Z,Y,X) voxel stream — "
            "not of the gzip output, which is not canonical"
        ),
    )

    _norm_sha = field_validator("content_sha256")(_normalise_sha256)


class VolumeCompleteRequest(BaseModel):
    parts: list[SubmitPart] = Field(..., min_length=1)


class VolumeCreatedResponse(BaseModel):
    volume_id: uuid.UUID
    state: Literal["uploading", "ready", "failed"]
    content_sha256: str
    upload: list[UploadPart] = Field(
        ...,
        description=(
            "PUT each part's bytes to its URL, then call /complete. "
            "EMPTY means there is nothing to upload — the server already holds this series."
        ),
    )
    part_size: int = Field(..., description="Bytes per part; the final part may be shorter")
    expires_in: int = Field(..., description="Seconds until the presigned URLs expire")
    expires_at: datetime = Field(..., description="When the volume itself will be deleted")
    reused: bool = Field(
        False, description="True when this returned an existing volume rather than creating one"
    )


class VolumeResponse(BaseModel):
    volume_id: uuid.UUID
    state: Literal["uploading", "ready", "failed"]
    content_sha256: str
    bytes: int
    voxels: int
    x_size: int
    y_size: int
    z_size: int
    jobs_run: int
    created_at: datetime
    expires_at: datetime


class VolumeListResponse(BaseModel):
    volumes: list[VolumeResponse]


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class JobCreateRequest(BaseModel):
    """Either shape is accepted, and both stay supported.

    * ``volume_id`` — segment a series already uploaded via ``POST /v1/volumes``.
      The job is created straight into ``queued``; there is no ``/submit`` step.
    * ``geometry`` + ``upload_bytes`` — the original inline upload, byte-for-byte
      the behaviour that shipped.

    The legacy shape is not deprecated on a timetable. Eclipse approves a plugin
    DLL by version *and* content hash, so a planner's workstation cannot be
    upgraded on our schedule; an API that stopped accepting the inline shape would
    break a clinical session mid-plan with a 422. The cost of keeping it is one
    branch in ``create_job``.
    """

    volume_id: uuid.UUID | None = Field(
        None, description="An existing volume to segment. Mutually exclusive with geometry."
    )
    geometry: Geometry | None = Field(
        None, description="Legacy inline upload. Mutually exclusive with volume_id."
    )
    prompts: list[str] = Field(
        ..., min_length=1,
        description='Free-text anatomy, e.g. ["liver", "left kidney"]',
    )
    upload_bytes: int | None = Field(
        None, gt=0,
        description="Legacy inline upload: exact byte length of the gzip-compressed volume",
    )
    keep_largest: bool = Field(
        False, description="Reduce each mask to its largest connected component"
    )
    want_mask: bool = Field(
        False, description="Also produce mask.bin.gz alongside the contours"
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> JobCreateRequest:
        has_volume = self.volume_id is not None
        has_inline = self.geometry is not None or self.upload_bytes is not None

        if has_volume and has_inline:
            raise ValueError(
                "send either volume_id or geometry+upload_bytes, not both"
            )
        if not has_volume and not has_inline:
            raise ValueError("one of volume_id or geometry+upload_bytes is required")
        if has_inline and (self.geometry is None or self.upload_bytes is None):
            raise ValueError("the inline shape needs both geometry and upload_bytes")
        return self

    @field_validator("prompts")
    @classmethod
    def _clean_prompts(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v if p and p.strip()]
        if not cleaned:
            raise ValueError("at least one non-empty prompt is required")
        if len(cleaned) > settings.VOXTELL_MAX_PROMPTS:
            raise ValueError(f"at most {settings.VOXTELL_MAX_PROMPTS} prompts per job")
        for p in cleaned:
            if len(p) > settings.VOXTELL_PROMPT_MAX_CHARS:
                raise ValueError(
                    f"prompt exceeds {settings.VOXTELL_PROMPT_MAX_CHARS} characters: {p[:40]}..."
                )
        # Preserve order, drop case-insensitive duplicates: the model lowercases
        # prompts anyway, so duplicates would waste a whole logits channel.
        seen, out = set(), []
        for p in cleaned:
            if p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
        return out


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    state: str
    upload: list[UploadPart] = Field(
        ..., description="PUT each part's bytes to its URL, in order, then call /submit"
    )
    part_size: int = Field(..., description="Bytes per part; the final part may be shorter")
    expires_in: int = Field(..., description="Seconds until the presigned URLs expire")


class JobSubmitRequest(BaseModel):
    parts: list[SubmitPart] = Field(..., min_length=1)


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    state: Literal[
        "awaiting_upload", "queued", "running", "done", "failed", "cancelled", "expired"
    ]
    progress: float = Field(..., ge=0, le=1)
    message: str | None = None
    error: str | None = None
    prompts: list[str]
    queue_position: int | None = Field(
        None, description="Jobs ahead of this one; null unless queued"
    )
    # Additive: Newtonsoft ignores unknown fields, so the already-approved 2.0.1.0
    # plugin is unaffected and a later build can surface it without a server change.
    estimated_wait_seconds: int | None = Field(
        None,
        description=(
            "Rough seconds until this job starts, from measured throughput; null "
            "unless queued. Prefer showing this over queue_position — it is the "
            "number a user can act on."
        ),
    )
    poll_after: int = Field(..., description="Suggested seconds before polling again")
    has_mask: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobListResponse(BaseModel):
    jobs: list[JobStatusResponse]


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    expires_in_days: int | None = Field(
        None, ge=1, le=3650, description="Null means the key never expires"
    )


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreatedResponse(ApiKeyResponse):
    token: str = Field(..., description="Shown once and never again — store it now")


# --------------------------------------------------------------------------- #
# Account / system
# --------------------------------------------------------------------------- #
class MeResponse(BaseModel):
    id: uuid.UUID
    email: str | None
    username: str | None
    monthly_quota: int | None
    used_this_month: int
    outstanding: int
    max_outstanding: int
    capabilities: list[str] = Field(
        default_factory=list,
        description=(
            'Optional features this deployment has enabled, e.g. ["volumes"]. '
            "Clients must treat an absent name as unsupported and fall back."
        ),
    )
    # Advertised so the client can show the operator what it is about to hold and
    # for how long, without a second round trip.
    volume_ttl_minutes: int | None = Field(
        None, description="Idle expiry of an uploaded volume; null when volumes are disabled"
    )


class AuthConfigResponse(BaseModel):
    """Everything the ESAPI plugin needs to sign a planner in.

    Covers both grants the plugin uses: Authorization Code + PKCE against a
    loopback redirect (the normal path), and the device code flow as the
    fallback for a workstation where no port binds or no browser is registered.
    One public Keycloak client, ``voxtell-esapi``, serves both.

    ``pkce_method`` applies to **both** grants, not just the redirect one.
    Setting ``pkce.code.challenge.method`` on a Keycloak client enforces PKCE
    across every authorization request it makes, so the device-authorization
    call must also send ``code_challenge``/``code_challenge_method`` and the
    token call must send ``code_verifier``. Omitting them fails the *device*
    flow with ``invalid_request: Missing parameter: code_challenge_method``,
    which reads like a plugin bug rather than a client-config consequence.
    Verified against the live realm on 2026-08-04.
    """

    issuer: str
    device_client_id: str
    device_authorization_endpoint: str
    token_endpoint: str
    audience: str
    # --- Authorization Code + PKCE ---
    authorization_endpoint: str
    pkce_method: str = "S256"
    scopes: str
    redirect_ports: list[int] = Field(
        ...,
        description=(
            "Loopback ports registered on the client, in preference order. The "
            "plugin listens on the first one it can bind and redirects to "
            "http://127.0.0.1:<port>/callback."
        ),
    )
    redirect_path: str = "/callback"


class HealthResponse(BaseModel):
    status: str
    database: bool
    version: str
