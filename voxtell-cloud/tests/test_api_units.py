"""API logic that needs no database: key minting and request validation."""

from __future__ import annotations

import hashlib

import pytest

from api.auth import API_KEY_PREFIX, generate_api_key, hash_api_key
from api.config import settings
from api.routes.health import auth_config
from api.schemas import Geometry, JobCreateRequest


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
def test_generated_key_is_prefixed_and_high_entropy():
    token, prefix, digest = generate_api_key()
    assert token.startswith(API_KEY_PREFIX)
    assert token.startswith(prefix)
    # 32 bytes of token_urlsafe is ~43 base64 characters.
    assert len(token) - len(API_KEY_PREFIX) >= 40
    assert digest == hashlib.sha256(token.encode("ascii")).hexdigest()


def test_keys_are_unique():
    tokens = {generate_api_key()[0] for _ in range(200)}
    assert len(tokens) == 200


def test_only_the_hash_identifies_a_key():
    """The stored digest must not be reversible to, or shared with, another key."""
    a, _, digest_a = generate_api_key()
    b, _, digest_b = generate_api_key()
    assert digest_a != digest_b
    assert hash_api_key(a) == digest_a
    assert hash_api_key(b) != digest_a
    # The plaintext must not appear anywhere in what we persist.
    assert a not in digest_a


def test_api_key_prefix_cannot_collide_with_a_jwt():
    """The dual-auth dependency branches on this prefix, so it must be unambiguous.

    A JWT is three base64url segments separated by dots; base64url has no
    underscore-after-alpha pattern at position 3 and never contains 'vxt_'
    at the start because the header always begins with '{"' base64url-encoded
    as 'eyJ'.
    """
    token, _, _ = generate_api_key()
    assert not token.startswith("eyJ")
    assert "." not in token[: len(API_KEY_PREFIX) + 1]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #
def valid_geometry(**overrides) -> dict:
    base = {
        "x_size": 512, "y_size": 512, "z_size": 100,
        "x_res": 0.98, "y_res": 0.98, "z_res": 2.5,
        "origin": [-243.7, -211.5, -88.25],
        "row_direction": [1, 0, 0],
        "col_direction": [0, 1, 0],
        "slice_direction": [0, 0, 1],
    }
    base.update(overrides)
    return base


def test_geometry_voxel_count():
    geom = Geometry(**valid_geometry(x_size=4, y_size=5, z_size=6))
    assert geom.voxels == 120


def test_geometry_defaults_to_an_identity_rescale():
    """A client that omits the ESAPI scaling values must not have data altered."""
    geom = Geometry(**valid_geometry())
    assert geom.scaling_slope == 1.0
    assert geom.scaling_intercept == 0.0


@pytest.mark.parametrize(
    "override",
    [
        {"x_size": 0},
        {"z_res": 0},
        {"origin": [1, 2]},
        {"row_direction": [1, 2, 3, 4]},
    ],
)
def test_geometry_rejects_impossible_values(override):
    with pytest.raises(Exception):
        Geometry(**valid_geometry(**override))


def job_request(**overrides) -> JobCreateRequest:
    body = {
        "geometry": valid_geometry(),
        "prompts": ["liver"],
        "upload_bytes": 1024,
    }
    body.update(overrides)
    return JobCreateRequest(**body)


def test_prompts_are_trimmed_and_blank_ones_dropped():
    assert job_request(prompts=["  liver ", "", "  ", "spleen"]).prompts == [
        "liver",
        "spleen",
    ]


def test_case_insensitive_duplicate_prompts_are_collapsed():
    """The model lowercases prompts, so a duplicate would waste a logits channel."""
    assert job_request(prompts=["Liver", "liver", "LIVER", "spleen"]).prompts == [
        "Liver",
        "spleen",
    ]


def test_all_blank_prompts_is_an_error():
    """Blank prompts still fail, now via the prompts-xor-structure_ids rule.

    Once ``prompts`` became optional (a CADS job is addressed by structure ids
    instead), an all-blank list normalises to empty and is caught by the
    exactly-one-target validator rather than by a prompts-specific message. What
    matters is that a job with nothing to segment is still refused.
    """
    with pytest.raises(Exception, match="one of prompts or structure_ids"):
        job_request(prompts=["", "   "])


def test_too_many_prompts_rejected():
    with pytest.raises(Exception, match="at most"):
        job_request(prompts=[f"structure {i}" for i in range(64)])


def test_overlong_prompt_rejected():
    with pytest.raises(Exception, match="characters"):
        job_request(prompts=["x" * 500])


def test_upload_bytes_must_be_positive():
    with pytest.raises(Exception):
        job_request(upload_bytes=0)


# --------------------------------------------------------------------------- #
# GET /v1/auth/config — the ESAPI plugin's only bootstrap call
# --------------------------------------------------------------------------- #
def test_auth_config_advertises_both_grants():
    """The plugin compiles in no realm URL, so every endpoint must come from here."""
    cfg = auth_config()
    base = settings.OIDC_ISSUER.rstrip("/")

    # Device code flow.
    assert cfg.device_client_id == settings.OIDC_DEVICE_CLIENT_ID
    assert cfg.device_authorization_endpoint == (
        f"{base}/protocol/openid-connect/auth/device"
    )
    assert cfg.token_endpoint == f"{base}/protocol/openid-connect/token"

    # Authorization Code + PKCE. Note the authorization endpoint is /auth, and
    # must not accidentally be the /auth/device one.
    assert cfg.authorization_endpoint == f"{base}/protocol/openid-connect/auth"
    assert not cfg.authorization_endpoint.endswith("/device")
    assert cfg.pkce_method == "S256"
    assert cfg.audience == settings.OIDC_AUDIENCE


def test_pkce_method_is_advertised_once_for_both_grants():
    """The client attribute enforces PKCE realm-side on the device grant too.

    There is deliberately no separate `device_pkce_method`: a single value keeps
    the client from concluding PKCE is redirect-only and sending a device
    authorization request without a challenge, which Keycloak rejects with
    "Missing parameter: code_challenge_method".
    """
    cfg = auth_config()
    assert not hasattr(cfg, "device_pkce_method")
    assert cfg.pkce_method in ("S256", "plain")


def test_auth_config_requests_an_offline_refresh_token():
    """Access tokens live 300 s and Eclipse relaunches the plugin every run."""
    scopes = auth_config().scopes.split()
    assert "openid" in scopes
    assert "offline_access" in scopes


def test_auth_config_redirect_ports_match_what_keycloak_registers():
    """Keycloak's redirect wildcard is path-only, so ports are registered verbatim.

    If this list ever drifts from the client's redirectUris, PKCE breaks with an
    "Invalid parameter: redirect_uri" that looks like a plugin bug.
    """
    cfg = auth_config()
    assert cfg.redirect_ports == [47653, 47654, 47655]
    assert cfg.redirect_path.startswith("/")
    # Loopback only — a redirect to a routable host would leak the code.
    for port in cfg.redirect_ports:
        assert 1024 < port < 65536
