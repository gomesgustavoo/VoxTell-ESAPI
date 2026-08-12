"""The volume and job-create wire contract.

Two things are being pinned.

The **hash normalisation**, because C# offers two idiomatic ways to hex-encode a
digest and they disagree on case: ``ToString("x2")`` gives lower, while
``BitConverter.ToString().Replace("-","")`` gives upper. A dedup key that depended
on which one the client happened to use would look fine in testing and then, one
refactor later, silently re-upload every volume — or worse, fail to find one that
is there.

The **job-create union**, because accepting either shape is what lets an
already-approved plugin DLL keep working while a new one uses volumes. Eclipse
approves a DLL by version *and* content hash, so a planner's workstation cannot be
upgraded on our schedule.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from api.schemas import JobCreateRequest, VolumeCreateRequest
from voxtell_cloud.wire import geometry_sha256

GEOM = dict(
    x_size=512, y_size=512, z_size=210,
    x_res=0.9765625, y_res=0.9765625, z_res=3.0,
    origin=[-250.0, -250.0, -100.0],
    row_direction=[1.0, 0.0, 0.0],
    col_direction=[0.0, 1.0, 0.0],
    slice_direction=[0.0, 0.0, 1.0],
    scaling_slope=1.0, scaling_intercept=-1024.0,
)
SHA_LOWER = "9f2c8a" + "0" * 58


def volume_body(**kw):
    body = dict(geometry=GEOM, upload_bytes=35_788_947, content_sha256=SHA_LOWER)
    body.update(kw)
    return VolumeCreateRequest(**body)


# --------------------------------------------------------------------------- #
# content_sha256
# --------------------------------------------------------------------------- #
def test_uppercase_and_lowercase_digests_are_the_same_volume():
    lower = volume_body(content_sha256=SHA_LOWER)
    upper = volume_body(content_sha256=SHA_LOWER.upper())
    assert lower.content_sha256 == upper.content_sha256


def test_digest_is_normalised_to_lowercase():
    assert volume_body(content_sha256=SHA_LOWER.upper()).content_sha256 == SHA_LOWER


@pytest.mark.parametrize("bad", ["", "abc", "9f" * 31, "9f" * 33, "z" * 64, " " + "9f" * 32])
def test_malformed_digests_are_rejected(bad):
    with pytest.raises(ValidationError):
        volume_body(content_sha256=bad)


def test_zero_upload_bytes_is_rejected():
    with pytest.raises(ValidationError):
        volume_body(upload_bytes=0)


# --------------------------------------------------------------------------- #
# geometry identity
# --------------------------------------------------------------------------- #
def test_identical_geometry_hashes_identically():
    assert geometry_sha256(GEOM) == geometry_sha256(dict(GEOM))


def test_hu_rescale_is_part_of_the_identity():
    """The rescale does not appear in the uploaded bytes at all.

    So two series with identical stored voxels but different HU mapping would be
    byte-identical on the wire. Reusing one for the other would feed the model the
    wrong intensities — the exact v1 bug that the rescale fields were added to fix.
    """
    other = dict(GEOM, scaling_intercept=-1000.0)
    assert geometry_sha256(other) != geometry_sha256(GEOM)


def test_shape_is_part_of_the_identity():
    """512x512x210 and 256x1024x210 hold the same voxel count.

    Identical bytes, different shape. Reusing one as the other would place every
    contour incorrectly in a patient.
    """
    reshaped = dict(GEOM, x_size=256, y_size=1024)
    assert geometry_sha256(reshaped) != geometry_sha256(GEOM)


def test_orientation_is_part_of_the_identity():
    flipped = dict(GEOM, slice_direction=[0.0, 0.0, -1.0])
    assert geometry_sha256(flipped) != geometry_sha256(GEOM)


def test_derived_fields_do_not_affect_the_identity():
    """``affine_lps`` and ``content_sha256`` ride along in the stored JSONB.

    They are derived from fields already in the key, so including them would make
    the digest depend on its own inputs twice — and any future addition to the
    stored blob would invalidate every held volume.
    """
    enriched = dict(GEOM, affine_lps=[[1, 0, 0, 0]], content_sha256=SHA_LOWER)
    assert geometry_sha256(enriched) == geometry_sha256(GEOM)


# --------------------------------------------------------------------------- #
# job create union
# --------------------------------------------------------------------------- #
def test_volume_id_shape_is_accepted():
    body = JobCreateRequest(volume_id=uuid.uuid4(), prompts=["liver"])
    assert body.geometry is None and body.upload_bytes is None


def test_legacy_inline_shape_is_accepted():
    body = JobCreateRequest(geometry=GEOM, upload_bytes=1000, prompts=["liver"])
    assert body.volume_id is None


def test_both_shapes_at_once_is_rejected():
    with pytest.raises(ValidationError):
        JobCreateRequest(
            volume_id=uuid.uuid4(), geometry=GEOM, upload_bytes=1000, prompts=["liver"]
        )


def test_neither_shape_is_rejected():
    with pytest.raises(ValidationError):
        JobCreateRequest(prompts=["liver"])


def test_half_the_inline_shape_is_rejected():
    """geometry without upload_bytes used to be impossible by construction.

    Making both optional to allow the union re-opened it, so it is checked here:
    the server would otherwise reach part_count(None).
    """
    with pytest.raises(ValidationError):
        JobCreateRequest(geometry=GEOM, prompts=["liver"])
    with pytest.raises(ValidationError):
        JobCreateRequest(upload_bytes=1000, prompts=["liver"])


def test_prompt_rules_still_apply_to_the_volume_shape():
    """The union must not have loosened validation that already existed."""
    with pytest.raises(ValidationError):
        JobCreateRequest(volume_id=uuid.uuid4(), prompts=[])
    with pytest.raises(ValidationError):
        JobCreateRequest(volume_id=uuid.uuid4(), prompts=["   "])


def test_duplicate_prompts_are_still_collapsed_case_insensitively():
    body = JobCreateRequest(volume_id=uuid.uuid4(), prompts=["Liver", "liver", "spleen"])
    assert body.prompts == ["Liver", "spleen"]
