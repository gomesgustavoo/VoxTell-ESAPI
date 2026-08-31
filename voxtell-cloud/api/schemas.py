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

from voxtell_cloud.catalog import DEFAULT_MODEL, catalog

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
        default_factory=list,
        description='Free-text anatomy for a prompt model, e.g. ["liver", "left kidney"]',
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

    # --- model addressing ------------------------------------------------- #
    # Omitted means VoxTell, so every plugin already approved in a clinic keeps
    # working untouched -- Eclipse approves a DLL by version and content hash, so
    # we cannot roll workstations forward on our own schedule.
    model: str | None = Field(
        None,
        description=f"Prompt-model key. Omit for the default ({DEFAULT_MODEL}). "
                    "Mutually exclusive with structure_ids.",
    )
    structure_ids: list[str] = Field(
        default_factory=list,
        description="Catalog structure ids, e.g. [\"cads_556.rectum\"]. The model set "
                    "is derived from these, never named by the client.",
    )

    # --- QA lineage (all opaque, all optional) ---------------------------- #
    # HMAC-SHA256 hex under an org-scoped secret held on the workstation. The
    # plugin never sends a DICOM UID, a patient name or a patient id; these keys
    # are what let run 2 find run 1 without us ever holding an identifier.
    series_key: str | None = Field(
        None, min_length=64, max_length=64,
        description="HMAC of the DICOM series UID. Opaque; not an identifier.",
    )
    for_key: str | None = Field(
        None, min_length=64, max_length=64,
        description="HMAC of the frame-of-reference UID.",
    )
    scanner_key: str | None = Field(
        None, min_length=64, max_length=64,
        description="HMAC of the imaging device manufacturer/model/serial triple, "
                    "for detecting an acquisition-protocol change.",
    )
    baseline: bool = Field(
        False,
        description="Record this job's contours as the QA baseline for series_key.",
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

    @model_validator(mode="after")
    def _exactly_one_target(self) -> JobCreateRequest:
        """Either free-text prompts or catalog structure ids, never both.

        Models are *derived* from structure ids rather than named alongside them,
        the same rule DicomSegVR's router uses: asking for one brain structure and
        one liver structure should load two networks, and letting the client also
        name a model invites a request whose model and structures disagree.
        """
        has_prompts = bool(self.prompts)
        has_structures = bool(self.structure_ids)

        if has_prompts and has_structures:
            raise ValueError("send either prompts or structure_ids, not both")
        if not has_prompts and not has_structures:
            raise ValueError("one of prompts or structure_ids is required")

        cat = catalog()

        if has_structures:
            if self.model is not None:
                raise ValueError(
                    "do not send model with structure_ids; the model set is derived"
                )
            if len(self.structure_ids) > settings.VOXTELL_MAX_STRUCTURES:
                raise ValueError(
                    f"at most {settings.VOXTELL_MAX_STRUCTURES} structure_ids per job"
                )
            unknown = cat.unknown_structures(self.structure_ids)
            if unknown:
                shown = ", ".join(unknown[:5])
                more = f" (+{len(unknown) - 5} more)" if len(unknown) > 5 else ""
                raise ValueError(f"unknown structure_ids: {shown}{more}")
            return self

        model_key = self.model or DEFAULT_MODEL
        model = cat.model(model_key)
        if model is None:
            raise ValueError(f"unknown model: {model_key}")
        if not model.takes_prompts:
            raise ValueError(
                f"model {model_key} is addressed by structure_ids, not prompts"
            )
        return self

    @property
    def resolved_model(self) -> str | None:
        """The prompt model to run, or ``None`` when the job is structure-addressed."""
        return None if self.structure_ids else (self.model or DEFAULT_MODEL)

    @property
    def resolved_models(self) -> list[str]:
        """Every model this job needs, in catalog order."""
        if self.structure_ids:
            return catalog().models_for_structures(self.structure_ids)
        return [self.model or DEFAULT_MODEL]

    @field_validator("structure_ids")
    @classmethod
    def _dedup_structure_ids(cls, v: list[str]) -> list[str]:
        seen, out = set(), []
        for i in (x.strip() for x in v):
            if i and i not in seen:
                seen.add(i)
                out.append(i)
        return out

    @field_validator("series_key", "for_key", "scanner_key")
    @classmethod
    def _lower_hex(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not all(c in "0123456789abcdef" for c in v):
            raise ValueError("must be lowercase hex")
        return v

    @field_validator("prompts")
    @classmethod
    def _clean_prompts(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v if p and p.strip()]
        if not cleaned:
            return []
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


# --------------------------------------------------------------------------- #
# QA baselines
# --------------------------------------------------------------------------- #
# The snapshot the plugin uploads so a later run can be scored against it.
#
# There is deliberately no patient name, id, accession number or DICOM instance
# UID in any model below. Series identity arrives only as opaque HMACs. If a
# field that could identify a patient is ever added here, the product's central
# privacy claim stops being true, so the absence is the design.
class SnapshotContour(BaseModel):
    z_index: int = Field(..., ge=0)
    points_lps: list[list[float]] = Field(
        ..., description="LPS millimetres, as ESAPI returned them"
    )


class SnapshotStructure(BaseModel):
    id: str = Field(..., max_length=64, description="ESAPI Structure.Id, as the clinic wrote it")
    name: str | None = Field(None, max_length=256)
    dicom_type: str | None = Field(None, max_length=32)
    roi_number: int = 0
    structure_id: str | None = Field(
        None, description="Catalog structure id, or null when the name was not recognised"
    )
    volume_cc: float | None = None
    is_empty: bool = False
    is_high_resolution: bool = False
    separate_parts: int | None = None
    is_approved: bool = False
    last_modified_by: str | None = Field(None, max_length=256)
    last_modified_at: datetime | None = None
    structure_codes: list[str] | None = None
    contours: list[SnapshotContour] = Field(default_factory=list)


class SnapshotRequest(BaseModel):
    schema_version: int = Field(1, alias="schema")
    series_key: str = Field(..., min_length=64, max_length=64)
    for_key: str | None = Field(None, min_length=64, max_length=64)
    scanner_key: str | None = Field(None, min_length=64, max_length=64)
    structure_set_uid: str | None = Field(None, max_length=128)
    structure_set_sha256: str = Field(..., min_length=64, max_length=64)
    role: Literal["baseline", "clinical"] = "baseline"
    geometry: Geometry | None = None
    structures: list[SnapshotStructure] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("series_key", "for_key", "scanner_key", "structure_set_sha256")
    @classmethod
    def _lower_hex(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not all(c in "0123456789abcdef" for c in v):
            raise ValueError("must be lowercase hex")
        return v


class BaselineResponse(BaseModel):
    baseline_id: uuid.UUID
    state: str
    created: bool = Field(
        ...,
        description="False when an identical structure set was already recorded. "
                    "Not an error: reopening a patient before editing is normal, "
                    "and it must not create a second baseline or bill twice.",
    )
    superseded: bool = Field(
        False, description="True when this snapshot replaced an earlier, different one"
    )
    structure_count: int
    web_url: str | None = None
    message: str | None = None


# --------------------------------------------------------------------------- #
# Model catalog
# --------------------------------------------------------------------------- #
# What this deployment can be asked to segment. The plugin fetches it once per
# session and builds its model picker from it, so adding a model is a server-side
# change -- no new DLL, and therefore no re-approval on every workstation.
class CatalogModel(BaseModel):
    key: str = Field(..., description="Address this model by this key")
    display_name: str
    kind: str = Field(..., description='"prompt" takes prompts; others take structure_ids')
    region: str
    modality: str
    count: int | None = Field(None, description="Structures this model produces")
    task: str | None = Field(None, description="Upstream task id, e.g. CADS 556")
    weights_variant: str | None = Field(
        None, description="Which published weights variant is deployed"
    )
    weights_licence: str = Field(
        ..., description="Licence of the deployed weights, shown to the planner"
    )
    code_licence: str


class CatalogStructure(BaseModel):
    id: str = Field(..., description="Namespaced {model}.{class_name}")
    display_name: str
    group: str
    modality: str
    source_model: str
    aliases: list[str] = Field(
        default_factory=list,
        description="Normalised match keys for an existing structure name. "
                    "Lowercase, alphanumerics only.",
    )


class CatalogPreset(BaseModel):
    key: str
    display_name: str
    structure_ids: list[str]
    models: list[str]


class CatalogProtocolEntry(BaseModel):
    structure_id: str
    write_as: str = Field(
        ...,
        max_length=16,
        description="Eclipse structure Id to write. Eclipse allows 16 characters.",
    )
    dicom_type: str = Field("CONTROL", description="Applied only when created")
    colour: str | None = Field(None, description="#RRGGBB; null lets the palette choose")
    required: bool = True


class CatalogProtocol(BaseModel):
    key: str
    display_name: str
    site: str = Field("", description="Groups the protocol list; display only")
    modality: str = "CT"
    models: list[str]
    entries: list[CatalogProtocolEntry]


class CatalogResponse(BaseModel):
    version: int
    group_order: list[str] = Field(
        ..., description="Render structure groups in this order"
    )
    models: list[CatalogModel]
    structures: list[CatalogStructure]
    # Presets stay in the response for as long as a 2.1.x plugin is approved anywhere:
    # that build reads presets and knows nothing about protocols. Additive only.
    presets: list[CatalogPreset]
    protocols: list[CatalogProtocol] = Field(
        default_factory=list,
        description="Clinic protocols: a named structure set with the clinic's naming, "
                    "DICOM types and colours. An entry may name a structure no model "
                    "produces — a site protocol lists what the plan needs, including "
                    "structures a human contours — and the plugin shows those as "
                    "unavailable rather than dropping them.",
    )


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
    structure_ids: list[str] = Field(
        default_factory=list,
        description="Catalog structure ids, for a catalog-addressed job",
    )
    models: list[str] = Field(
        default_factory=list, description="Models this job needs, in catalog order"
    )
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

    # ---- Additive, for the dashboard. Every one of these is an existing column.
    #
    # SAFE BECAUSE THE APPROVED PLUGIN USES NEWTONSOFT, which ignores unknown JSON
    # fields — so 2.0.1.0 keeps deserialising this unchanged. Adding is safe;
    # renaming or removing anything above is not, and needs a DLL re-approval on
    # every workstation.
    queued_at: datetime | None = Field(
        None, description="When the job entered the queue; null if it never did"
    )
    duration_seconds: float | None = Field(
        None,
        description=(
            "Wall-clock seconds on the worker (finished_at - started_at). Server-"
            "computed so every client agrees, and so a client cannot get it wrong "
            "by subtracting timestamps in the browser's local timezone."
        ),
    )
    gpu_seconds: float | None = Field(None, description="Measured GPU time, when recorded")
    voxels: int | None = None
    bytes_in: int | None = Field(None, description="Compressed upload size")
    attempts: int = Field(0, description="Claim attempts; >1 means it was requeued")
    failure_class: str | None = Field(
        None,
        description=(
            "Bounded label for a failure: transient | permanent | stalled | timeout. "
            "Null unless the job failed."
        ),
    )
    volume_id: uuid.UUID | None = Field(
        None, description="The uploaded volume this job segmented; null for a legacy inline job"
    )


class JobListResponse(BaseModel):
    jobs: list[JobStatusResponse]
    # Additive. `total` is the count matching the filter, ignoring limit/offset, so
    # a client can page without guessing when it has reached the end.
    total: int = Field(0, description="Jobs matching the filter, before paging")
    limit: int = Field(50)
    offset: int = Field(0)


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
    lineage_secret: str | None = Field(
        None,
        description="Hex keying material for the QA lineage HMACs, scoped to this "
                    "tenant. Null when the deployment has not enabled QA lineage.",
    )
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

    # ---- Additive, for the dashboard. All three already exist on QuotaState and
    # were being collapsed into `outstanding` before reaching the wire, which is
    # why the console could only ever say "1/2 in flight" without saying whether
    # the job was waiting for the GPU or on it.
    queued: int = Field(0, description="Caller's jobs waiting for the GPU")
    running: int = Field(0, description="Caller's jobs on the GPU now")
    remaining: int | None = Field(
        None, description="Jobs left this month; null when the quota is unlimited"
    )


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
class UsageDay(BaseModel):
    """One UTC calendar day of usage.

    UTC deliberately, matching ``quota.month_start()``. Bucketing in a local
    timezone would make the daily columns disagree with the monthly quota the
    same page shows above them, which is the kind of off-by-one nobody
    investigates and everybody distrusts.
    """

    day: str = Field(..., description="ISO date, YYYY-MM-DD, in UTC")
    jobs: int
    prompts: int
    gpu_seconds: float
    voxels: int


class UsageResponse(BaseModel):
    days: list[UsageDay] = Field(
        ...,
        description=(
            "One entry per day in the window, oldest first, INCLUDING days with no "
            "activity. Zero-filled server-side so a chart can plot the array "
            "directly instead of reconstructing a calendar."
        ),
    )
    window_days: int
    since: str = Field(..., description="First day in the window, ISO date, UTC")
    # Totals over the window, so the summary numbers and the chart cannot disagree.
    total_jobs: int
    total_prompts: int
    total_gpu_seconds: float


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
class SystemResponse(BaseModel):
    """Shared-service state, so a queued job's wait is explainable.

    Served from the cached snapshot ``api/metrics.py`` already refreshes for the
    Prometheus scrape — not from fresh queries. A dashboard polling this every few
    seconds must not be able to add load to the database.
    """

    queue_depth: int = Field(..., description="Queued jobs across every user")
    running: int = Field(..., description="Jobs on the GPU across every user")
    worker_online: bool = Field(
        ..., description="A worker has heartbeat a running job recently, or is idle and healthy"
    )
    estimated_wait_seconds: int = Field(
        ..., description="Rough seconds for a job joining the back of the queue now"
    )
    snapshot_age_seconds: float = Field(
        ..., description="How stale these numbers are; the snapshot is cached"
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
