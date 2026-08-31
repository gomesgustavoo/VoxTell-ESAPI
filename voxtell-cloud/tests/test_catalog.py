"""The model catalog, and the invariants that keep model addressing safe.

Three things here are worth a test rather than a code review:

* **Label indices and alias uniqueness.** A label index maps a network's output
  channel onto an anatomical name. An off-by-one, or one alias claimed by two
  structures, mislabels a structure *inside a patient* and nothing downstream can
  notice.
* **Backward compatibility.** Eclipse approves a plugin DLL by version and content
  hash on every workstation, so a clinic cannot be rolled forward on our
  schedule. A request shaped exactly like the one the already-approved plugin
  sends must keep working, forever.
* **Volume dedup identity.** Adding a field to the job request must not perturb
  ``geometry_sha256``. See :func:`test_new_job_fields_do_not_touch_geometry_identity`.
"""

from __future__ import annotations

import collections

import pytest

from api.schemas import JobCreateRequest
from voxtell_cloud.catalog import (
    DEFAULT_MODEL,
    KIND_PROMPT,
    MAX_STRUCTURE_ID,
    CatalogError,
    catalog,
    load_catalog,
    normalise,
)
from voxtell_cloud.wire import (
    GEOMETRY_IDENTITY_FIELDS,
    RESULT_SCHEMA_VERSION,
    geometry_sha256,
    model_identity,
    result_envelope,
)

GEOMETRY = {
    "x_size": 512, "y_size": 512, "z_size": 120,
    "x_res": 0.9765625, "y_res": 0.9765625, "z_res": 3.0,
    "origin": [-250.0, -250.0, -300.0],
    "row_direction": [1.0, 0.0, 0.0],
    "col_direction": [0.0, 1.0, 0.0],
    "slice_direction": [0.0, 0.0, 1.0],
    "scaling_slope": 1.0,
    "scaling_intercept": -1024.0,
}


def job(**overrides):
    body = {"geometry": GEOMETRY, "upload_bytes": 4096}
    body.update(overrides)
    return JobCreateRequest(**body)


# --------------------------------------------------------------------------- #
# Catalog integrity
# --------------------------------------------------------------------------- #
def test_catalog_loads_and_defines_the_default_model():
    cat = catalog()
    assert cat.model(DEFAULT_MODEL) is not None
    assert cat.model(DEFAULT_MODEL).kind == KIND_PROMPT


def test_structure_ids_are_unique():
    cat = catalog()
    ids = [s.id for s in cat.structures]
    assert len(ids) == len(set(ids))


def test_every_structure_names_a_declared_model():
    cat = catalog()
    keys = {m.key for m in cat.models}
    assert all(s.source_model in keys for s in cat.structures)


def test_label_indices_are_contiguous_from_one_within_each_model():
    """Off-by-one here mislabels an organ, so the indices are pinned as a set.

    nnU-Net emits channel 0 as background and 1..N as the labels in
    ``dataset.json`` order; a gap or a duplicate means the catalog and the network
    disagree about which channel is which organ.
    """
    cat = catalog()
    by_model = collections.defaultdict(list)
    for s in cat.structures:
        by_model[s.source_model].append(s.label_index)
    for model, indices in by_model.items():
        assert sorted(indices) == list(range(1, len(indices) + 1)), model


def test_no_alias_is_claimed_by_two_structures():
    """An ambiguous alias would make auto-detect pick a structure at random."""
    cat = catalog()
    owner: dict[str, str] = {}
    for s in cat.structures:
        for alias in s.aliases:
            assert owner.setdefault(alias, s.id) == s.id, alias


def test_aliases_are_already_normalised():
    """The stored keys must be in match form, or lookups silently miss."""
    cat = catalog()
    for s in cat.structures:
        for alias in s.aliases:
            assert alias == normalise(alias), (s.id, alias)


# These exact pairs are asserted by the C# plugin's `--selftest` (see
# VoxTell-Interface.Harness/SelfTest.cs, "Normalise - the cross-language match
# contract") and produced by scripts/gen_catalog.py. All three implementations of
# this rule must agree, and when they do not, nothing raises: auto-detect just
# reports "0 recognised" on a series full of contours. Change one, change all three.
CROSS_LANGUAGE_NORMALISE_CASES = [
    ("Kidney_R", "kidneyr"),
    ("Kidney R", "kidneyr"),
    ("kidney-r", "kidneyr"),
    ("  Kidney . R  ", "kidneyr"),
    ("PTV_7000", "ptv7000"),
    ("Vertebra L5", "vertebral5"),
    ("", ""),
    ("Rib-12 L", "rib12l"),
    # Non-ASCII is dropped rather than transliterated, so a clinic using accented
    # names gets no match instead of a wrong one.
    ("Oesophage\u00e9", "oesophage"),
]


@pytest.mark.parametrize("source,expected", CROSS_LANGUAGE_NORMALISE_CASES)
def test_normalise_matches_the_csharp_plugin(source, expected):
    assert normalise(source) == expected


def test_generator_normalise_matches_this_one():
    """The third implementation, in scripts/gen_catalog.py, must agree too."""
    import importlib.util
    import pathlib as _pathlib

    path = _pathlib.Path(__file__).resolve().parent.parent / "scripts" / "gen_catalog.py"
    spec = importlib.util.spec_from_file_location("gen_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for source, expected in CROSS_LANGUAGE_NORMALISE_CASES:
        assert module.norm(source) == expected == normalise(source), source


def test_presets_only_reference_known_structures():
    cat = catalog()
    for preset in cat.presets:
        assert preset.structure_ids
        assert not cat.unknown_structures(preset.structure_ids)


def test_every_group_appears_in_group_order():
    cat = catalog()
    assert {s.group for s in cat.structures} <= set(cat.group_order)


def test_deployed_cads_weights_permit_commercial_use():
    """CADS's default weights variant does not. Ours must.

    Upstream defaults to ``-license reference``, and ``research`` is
    CC BY-NC-SA 4.0 -- non-commercial. Only ``open`` (CC BY-SA 4.0) can ship in a
    paid product, and the flag is easy to omit, so the catalog asserts it.
    """
    cat = catalog()
    for model in cat.models:
        if model.kind == "cads":
            assert model.weights_variant == "open", model.key
            assert model.weights_licence == "CC-BY-SA-4.0", model.key


# --------------------------------------------------------------------------- #
# Alias resolution
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Kidney_R", "cads_551.kidney_r"),
        ("kidney r", "cads_551.kidney_r"),
        ("R Kidney", "cads_551.kidney_r"),
        ("right kidney", "cads_551.kidney_r"),
        ("Breast_L", "cads_556.mammary_gland_l"),
        ("Oesophagus", "cads_553.esophagus"),   # British spelling
        ("Bladder", "cads_553.urinary_bladder"),
        ("L5", "cads_552.vertebra_l5"),
        ("Parotid_L", "cads_558.parotid_gland_l"),
    ],
)
def test_alias_resolution(name, expected):
    assert catalog().resolve_alias(name).id == expected


def test_spinal_cord_and_spinal_canal_stay_distinct():
    """Different structures in different CADS tasks; conflating them would score
    a clinician's cord contour against a canal."""
    cat = catalog()
    cord = cat.resolve_alias("SpinalCord")
    canal = cat.resolve_alias("SpinalCanal")
    assert cord.id == "cads_559.spinal_cord"
    assert canal.id == "cads_556.spinal_canal"
    assert cord.id != canal.id


def test_lung_lobes_are_not_aliased_to_whole_lung():
    """Eclipse's ``Lung_L`` is the whole lung; CADS 551 produces lobes."""
    assert catalog().resolve_alias("Lung_L") is None


def test_unmatched_names_return_none_rather_than_guessing():
    cat = catalog()
    for name in ("PTV_7000", "CTV_low", "zzz_scratch", ""):
        assert cat.resolve_alias(name) is None


def test_minimal_model_set_is_derived_not_everything():
    cat = catalog()
    models = cat.models_for_structures(["cads_551.liver", "cads_557.white_matter"])
    assert models == ["cads_551", "cads_557"]


# --------------------------------------------------------------------------- #
# Job addressing
# --------------------------------------------------------------------------- #
def test_the_already_approved_plugin_request_still_works():
    """Byte-for-byte the shape the field DLL sends: prompts, no model."""
    request = job(prompts=["liver", "spleen"])
    assert request.prompts == ["liver", "spleen"]
    assert request.model is None
    assert request.resolved_model == DEFAULT_MODEL
    assert request.resolved_models == [DEFAULT_MODEL]
    assert request.structure_ids == []


def test_structure_addressed_job_derives_its_models():
    request = job(structure_ids=["cads_556.rectum", "cads_551.liver"])
    assert request.resolved_model is None
    assert request.resolved_models == ["cads_551", "cads_556"]


def test_structure_ids_are_deduplicated_preserving_order():
    request = job(structure_ids=["cads_556.rectum", "cads_551.liver", "cads_556.rectum"])
    assert request.structure_ids == ["cads_556.rectum", "cads_551.liver"]


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({}, "one of prompts or structure_ids"),
        ({"prompts": ["liver"], "structure_ids": ["cads_551.liver"]}, "not both"),
        ({"structure_ids": ["cads_551.no_such_organ"]}, "unknown structure_ids"),
        ({"structure_ids": ["cads_551.liver"], "model": "cads_551"}, "the model set is derived"),
        ({"prompts": ["liver"], "model": "cads_556"}, "addressed by structure_ids"),
        ({"prompts": ["liver"], "model": "no_such_model"}, "unknown model"),
        ({"prompts": ["liver"], "series_key": "ZZ" + "0" * 62}, "lowercase hex"),
    ],
)
def test_rejected_job_shapes(overrides, message):
    with pytest.raises(Exception, match=message):
        job(**overrides)


def test_lineage_keys_are_optional_and_lowercased():
    key = "AB" + "0" * 62
    request = job(prompts=["liver"], series_key=key, for_key=key, scanner_key=key)
    assert request.series_key == key.lower()
    assert request.baseline is False

    bare = job(prompts=["liver"])
    assert bare.series_key is None and bare.for_key is None and bare.scanner_key is None


# --------------------------------------------------------------------------- #
# The trap
# --------------------------------------------------------------------------- #
def test_new_job_fields_do_not_touch_geometry_identity():
    """Volume dedup is keyed on ``geometry_sha256``. If a new request field ever
    leaks into ``GEOMETRY_IDENTITY_FIELDS``, every volume already cached changes
    identity at once and the whole dedup cache silently invalidates.

    Pinned as a literal digest so the failure is loud rather than a drift nobody
    notices.
    """
    baseline = geometry_sha256(GEOMETRY)

    polluted = dict(GEOMETRY)
    polluted.update(
        series_key="a" * 64,
        for_key="b" * 64,
        scanner_key="c" * 64,
        model="cads_556",
        structure_ids=["cads_556.rectum"],
        baseline=True,
    )
    assert geometry_sha256(polluted) == baseline

    for field in ("series_key", "for_key", "scanner_key", "model",
                  "structure_ids", "baseline"):
        assert field not in GEOMETRY_IDENTITY_FIELDS


def test_geometry_identity_still_reacts_to_real_geometry_changes():
    """The other half: the digest must not be inert."""
    baseline = geometry_sha256(GEOMETRY)
    for field, value in [
        ("x_size", 256), ("z_res", 2.5), ("origin", [0.0, 0.0, 0.0]),
        ("scaling_intercept", 0.0), ("slice_direction", [0.0, 0.0, -1.0]),
    ]:
        changed = dict(GEOMETRY)
        changed[field] = value
        assert geometry_sha256(changed) != baseline, field


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #
def test_envelope_keeps_model_a_bare_string_for_approved_plugins():
    env = result_envelope("job-1", "voxtell_v1.1", ["liver"], [])
    assert env["schema"] == RESULT_SCHEMA_VERSION == 3
    assert isinstance(env["model"], str)
    assert env["model"] == "voxtell_v1.1"
    # and the new keys are additive
    assert env["models"][0]["key"] == "voxtell_v1.1"
    assert env["structure_ids"] == []


def test_envelope_carries_multi_model_identity_and_licence():
    models = [
        model_identity("cads_551", kind="cads", task="551",
                       weights_variant="open", weights_licence="CC-BY-SA-4.0"),
        model_identity("cads_556", kind="cads", task="556",
                       weights_variant="open", weights_licence="CC-BY-SA-4.0"),
    ]
    env = result_envelope(
        "job-2", "cads_551", [], [], models=models,
        structure_ids=["cads_551.liver", "cads_556.rectum"],
    )
    assert [m["key"] for m in env["models"]] == ["cads_551", "cads_556"]
    assert {m["weights_licence"] for m in env["models"]} == {"CC-BY-SA-4.0"}
    assert env["structure_ids"] == ["cads_551.liver", "cads_556.rectum"]


def test_catalog_error_is_a_value_error():
    """Startup failure, so it must be catchable by the generic handler."""
    assert issubclass(CatalogError, ValueError)


# --------------------------------------------------------------------------- #
# Clinic protocols
# --------------------------------------------------------------------------- #
# A protocol decides the Id a structure is WRITTEN under, so everything checked
# here fails inside a patient's structure set if it is wrong: an id Eclipse
# refuses, two entries writing the same id, or a type Eclipse does not know.


def test_shipped_protocols_are_usable():
    cat = catalog()
    assert cat.protocols, "the deployment ships no protocols"

    for protocol in cat.protocols:
        assert protocol.entries, f"protocol {protocol.key} is empty"
        for model_key in protocol.models:
            assert cat.model(model_key), f"{protocol.key} names unknown {model_key}"


def test_protocol_write_as_ids_fit_eclipse_and_are_unique():
    for protocol in catalog().protocols:
        seen = collections.Counter(e.write_as.lower() for e in protocol.entries)
        assert not [k for k, n in seen.items() if n > 1], protocol.key
        for entry in protocol.entries:
            assert 0 < len(entry.write_as) <= MAX_STRUCTURE_ID, (
                f"{protocol.key}: {entry.write_as!r}"
            )


def test_shipped_protocol_entries_are_all_producible():
    """Unavailable entries are *allowed*; shipping one would be a typo.

    A site protocol may legitimately name a structure no model produces — a
    planner contours it by hand — and the plugin shows those as unavailable rather
    than dropping them. But nothing we ship should be in that state, so this is
    the test that catches a mistyped id in PROTOCOLS.
    """
    cat = catalog()
    for protocol in cat.protocols:
        producible = set(cat.protocol_structure_ids(protocol.key))
        missing = [e.structure_id for e in protocol.entries
                   if e.structure_id not in producible]
        assert not missing, f"{protocol.key}: {missing}"


def test_presets_survive_for_the_approved_plugin():
    """2.1.0.0 is approved on a workstation and reads `presets`, not `protocols`.

    Eclipse approves a DLL by version and content hash, so a clinic cannot be
    rolled forward on our schedule. Removing presets from the payload would leave
    that build with no structure selection at all.
    """
    assert catalog().presets


def _write(tmp_path, protocol):
    """A minimal but valid catalog carrying one protocol."""
    import json

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "version": 2,
        "group_order": ["Pelvic organs"],
        "models": [
            {"key": DEFAULT_MODEL, "kind": KIND_PROMPT, "display_name": "VoxTell"},
            {"key": "cads_556", "kind": "cads", "display_name": "CADS 556"},
        ],
        "structures": [
            {
                "id": "cads_556.rectum", "display_name": "Rectum",
                "group": "Pelvic organs", "source_model": "cads_556",
                "label_index": 1, "class_name": "rectum", "aliases": ["rectum"],
            },
            {
                "id": "cads_556.prostate", "display_name": "Prostate",
                "group": "Pelvic organs", "source_model": "cads_556",
                "label_index": 2, "class_name": "prostate", "aliases": ["prostate"],
            },
        ],
        "presets": [],
        "protocols": [protocol],
    }), encoding="utf-8")
    return path


def _entry(**overrides):
    entry = {
        "structure_id": "cads_556.rectum",
        "write_as": "Rectum",
        "dicom_type": "ORGAN",
        "colour": "#A9714B",
        "required": True,
    }
    entry.update(overrides)
    return entry


def _protocol(entries, **overrides):
    protocol = {
        "key": "pelvis",
        "display_name": "Prostate",
        "site": "Pelvis",
        "modality": "CT",
        "models": ["cads_556"],
        "entries": entries,
    }
    protocol.update(overrides)
    return protocol


def test_a_valid_protocol_loads(tmp_path):
    cat = load_catalog(_write(tmp_path, _protocol([_entry()])))
    assert cat.protocol("pelvis").entries[0].write_as == "Rectum"
    assert cat.protocol_structure_ids("pelvis") == ["cads_556.rectum"]


def test_an_entry_naming_no_producible_structure_loads_but_is_not_selected(tmp_path):
    """The case the plugin renders as "no model produces this"."""
    cat = load_catalog(_write(tmp_path, _protocol([
        _entry(),
        _entry(structure_id="cads_556.femur_l", write_as="Femur_L"),
    ])))
    assert len(cat.protocol("pelvis").entries) == 2
    assert cat.protocol_structure_ids("pelvis") == ["cads_556.rectum"]


@pytest.mark.parametrize("protocol, message", [
    (_protocol([_entry(write_as="ThisIsFarTooLongForEclipse")]), "longer than"),
    (_protocol([_entry(write_as="   ")]), "empty write_as"),
    (_protocol([_entry(), _entry(structure_id="cads_556.prostate")]), "write as"),
    (_protocol([_entry(), _entry()]), "twice"),
    (_protocol([_entry(dicom_type="ORGANN")]), "unknown dicom_type"),
    (_protocol([_entry(colour="octarine")]), "malformed colour"),
    (_protocol([_entry()], models=["cads_999"]), "unknown model"),
])
def test_refused_protocols(tmp_path, protocol, message):
    with pytest.raises(CatalogError, match=message):
        load_catalog(_write(tmp_path, protocol))


def test_duplicate_protocol_keys_are_refused(tmp_path):
    import json

    path = _write(tmp_path, _protocol([_entry()]))
    raw = json.loads(path.read_text())
    raw["protocols"].append(_protocol([_entry()]))
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CatalogError, match="duplicate protocol keys"):
        load_catalog(path)


def test_unknown_protocol_key_is_none_not_a_guess():
    assert catalog().protocol("no_such_protocol") is None
    assert catalog().protocol_structure_ids("no_such_protocol") == []


# --------------------------------------------------------------------------- #
# Serving a catalog the worker cannot run
# --------------------------------------------------------------------------- #


def test_catalog_models_can_be_advertised_but_refused():
    """The API may be rolled forward before the weights are.

    The catalog is served whether or not the models are deployed, because the plugin
    needs it to render its picker at all. VOXTELL_CATALOG_MODELS_ENABLED=false is
    what stops a structure-addressed job being accepted, queued and completed with
    nothing in it — a refusal naming the models is worth more than an empty result
    two minutes later. Default stays true so a provisioned deployment is unaffected.
    """
    from api.config import Settings
    from api.errors import service_unavailable

    assert Settings().VOXTELL_CATALOG_MODELS_ENABLED is True

    exc = service_unavailable("catalog_models_unavailable", "cads_556 is not deployed")
    assert exc.status_code == 503
    assert exc.detail["error"] == "catalog_models_unavailable"
