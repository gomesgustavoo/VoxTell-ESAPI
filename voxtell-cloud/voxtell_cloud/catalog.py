"""The model and structure catalog: what this deployment can be asked to segment.

Why a catalog exists at all
---------------------------
Until now a job carried only ``prompts`` and there was exactly one model, so
"which model" was not a question the wire could express. ``result.json`` reported
``model`` as the *basename of a mount path*, which is a deployment detail, not an
identity. With CADS arriving as a second kind -- and custom nnU-Nets after it --
a job has to name what it wants and a result has to say what produced it.

Structure ids are namespaced ``{source_model}.{class_name}``, the same convention
DicomSegVR's registry uses, so the same organ segmented by two different models
stays two distinct addressable things. That matters for QA: comparing CADS's
``Heart`` against a clinician's contour is a different measurement from comparing
VoxTell's, and averaging them would be meaningless.

Why the data is a generated file and not a live call
---------------------------------------------------
The label *indices* are load-bearing -- they map a network output channel onto an
anatomical name, and an off-by-one silently mislabels a structure inside a
patient. They come from CADS's published labelmap through
``scripts/gen_catalog.py``, which is the only supported way to change them. A
runtime HTTP fetch from a neighbouring service would make a clinical request
depend on that service being up, and would make the indices unauditable at
review time. Regenerating is a deliberate, reviewable commit.

Normalisation and the matching contract
---------------------------------------
:func:`normalise` is half of a contract with the Eclipse plugin: punctuation and
separators carry no clinical meaning in a structure name, so ``Kidney_R``,
``Kidney R`` and ``kidney-r`` must all resolve to the same structure. The plugin
normalises the ESAPI ``Structure.Id`` with the identical rule before looking it
up here. Change one side and auto-detect silently stops matching -- which is
precisely how the published audit lost 6% of its cases.
"""

from __future__ import annotations

import functools
import json
import pathlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

CATALOG_PATH = pathlib.Path(__file__).with_name("model_catalog.json")

# The free-text model. Named rather than inferred, because "no model given" has
# to keep meaning "VoxTell" for every already-approved plugin in the field.
DEFAULT_MODEL = "voxtell"

# Model kinds. `prompt` takes free text; `cads` and `custom` take structure ids.
KIND_PROMPT = "prompt"
KIND_CADS = "cads"
KIND_CUSTOM = "custom"
PROMPT_KINDS = frozenset({KIND_PROMPT})


class CatalogError(ValueError):
    """The catalog file is unusable. A startup failure, never a request error."""


@dataclass(frozen=True)
class ModelDef:
    key: str
    display_name: str
    kind: str
    region: str
    modality: str
    count: int | None
    task: str | None
    weights_variant: str | None
    weights_licence: str
    code_licence: str

    @property
    def takes_prompts(self) -> bool:
        return self.kind in PROMPT_KINDS


@dataclass(frozen=True)
class StructureDef:
    id: str
    display_name: str
    group: str
    modality: str
    source_model: str
    label_index: int
    class_name: str
    aliases: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class PresetDef:
    key: str
    display_name: str
    structure_ids: tuple[str, ...]
    models: tuple[str, ...]


# What Eclipse itself allows in a structure Id, and the types it understands. Both
# are enforced here rather than in the plugin: a violation discovered on the
# workstation is discovered during a write into a patient.
MAX_STRUCTURE_ID = 16
DICOM_TYPES = frozenset(
    {"CONTROL", "ORGAN", "PTV", "CTV", "GTV", "AVOIDANCE", "EXTERNAL", "SUPPORT"}
)
_HEX_COLOUR = re.compile(r"^#?[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class ProtocolEntryDef:
    """One structure in a clinic protocol, with the naming the clinic uses."""

    structure_id: str
    write_as: str
    dicom_type: str
    colour: str | None
    required: bool


@dataclass(frozen=True)
class ProtocolDef:
    """
    A named structure set as a clinic writes it.

    A preset only says *what* to segment. A protocol also says what each structure
    is called, what DICOM type it gets and what colour it is drawn in -- which is
    the part that stops naming drift, the failure the published auto-segmentation
    audits blame for silently losing cases. It is served rather than compiled into
    the plugin because ESAPI 16.1 exposes no structure-template or clinical-protocol
    enumeration at all, and because anything inside that DLL costs a physicist's
    re-approval on every workstation to change.
    """

    key: str
    display_name: str
    site: str
    modality: str
    models: tuple[str, ...]
    entries: tuple[ProtocolEntryDef, ...]


@dataclass(frozen=True)
class Catalog:
    version: int
    group_order: tuple[str, ...]
    models: tuple[ModelDef, ...]
    structures: tuple[StructureDef, ...]
    presets: tuple[PresetDef, ...]
    protocols: tuple[ProtocolDef, ...]
    _by_model: dict[str, ModelDef]
    _by_structure: dict[str, StructureDef]
    _by_alias: dict[str, str]
    _by_preset: dict[str, PresetDef]
    _by_protocol: dict[str, ProtocolDef]

    # --- lookups ----------------------------------------------------------- #

    def model(self, key: str) -> ModelDef | None:
        return self._by_model.get(key)

    def structure(self, structure_id: str) -> StructureDef | None:
        return self._by_structure.get(structure_id)

    def preset(self, key: str) -> PresetDef | None:
        return self._by_preset.get(key)

    def protocol(self, key: str) -> ProtocolDef | None:
        return self._by_protocol.get(key)

    def protocol_structure_ids(self, key: str) -> list[str]:
        """The entries of a protocol this deployment can actually produce.

        Entries naming a structure no model produces are legitimate -- a site
        protocol lists what the plan needs, including structures a human draws --
        so they are carried in the payload and shown as unavailable rather than
        being dropped or refused at load.
        """
        protocol = self._by_protocol.get(key)
        if protocol is None:
            return []
        return [
            e.structure_id
            for e in protocol.entries
            if e.structure_id in self._by_structure
        ]

    def resolve_alias(self, name: str) -> StructureDef | None:
        """Map a free-form structure name (e.g. an ESAPI ``Structure.Id``).

        Returns ``None`` rather than guessing. An unmatched name is reported to
        the planner as unmatched; silently dropping it is the failure mode the
        auto-segmentation audit literature calls out as its commonest.
        """
        structure_id = self._by_alias.get(normalise(name))
        return self._by_structure.get(structure_id) if structure_id else None

    def structures_for(self, model_key: str) -> tuple[StructureDef, ...]:
        return tuple(s for s in self.structures if s.source_model == model_key)

    def models_for_structures(self, structure_ids: Iterable[str]) -> list[str]:
        """The distinct models needed, in catalog order.

        This is the "minimal model set" idea: asking for one brain structure and
        one liver structure loads two networks, not all ten.
        """
        wanted = {
            self._by_structure[i].source_model
            for i in structure_ids
            if i in self._by_structure
        }
        return [m.key for m in self.models if m.key in wanted]

    # --- validation -------------------------------------------------------- #

    def unknown_structures(self, structure_ids: Iterable[str]) -> list[str]:
        return [i for i in structure_ids if i not in self._by_structure]


def normalise(text: str) -> str:
    """Match key for a structure name: lowercase, alphanumerics only.

    Must stay byte-identical in behaviour to ``norm()`` in
    ``scripts/gen_catalog.py`` and to ``StructureAutoDetect.Normalise`` in the
    ESAPI plugin. All three are the same contract written in three places.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _require(mapping: dict, key: str, where: str):
    if key not in mapping:
        raise CatalogError(f"{where}: missing {key!r}")
    return mapping[key]


def load_catalog(path: pathlib.Path | None = None) -> Catalog:
    """Parse and validate the catalog file. Raises :class:`CatalogError`."""
    path = path or CATALOG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog not found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog at {path} is not valid JSON: {exc}") from exc

    models = tuple(
        ModelDef(
            key=_require(m, "key", "model"),
            display_name=m.get("display_name", m["key"]),
            kind=_require(m, "kind", f"model {m.get('key')}"),
            region=m.get("region", ""),
            modality=m.get("modality", "CT"),
            count=m.get("count"),
            task=m.get("task"),
            weights_variant=m.get("weights_variant"),
            weights_licence=m.get("weights_licence", "unknown"),
            code_licence=m.get("code_licence", "unknown"),
        )
        for m in raw.get("models", [])
    )
    structures = tuple(
        StructureDef(
            id=_require(s, "id", "structure"),
            display_name=s.get("display_name", s["id"]),
            group=s.get("group", ""),
            modality=s.get("modality", "CT"),
            source_model=_require(s, "source_model", f"structure {s.get('id')}"),
            label_index=_require(s, "label_index", f"structure {s.get('id')}"),
            class_name=s.get("class_name", ""),
            aliases=tuple(s.get("aliases", ())),
        )
        for s in raw.get("structures", [])
    )
    protocols = tuple(
        ProtocolDef(
            key=_require(p, "key", "protocol"),
            display_name=p.get("display_name", p["key"]),
            site=p.get("site", ""),
            modality=p.get("modality", "CT"),
            models=tuple(p.get("models", ())),
            entries=tuple(
                ProtocolEntryDef(
                    structure_id=_require(
                        e, "structure_id", f"protocol {p.get('key')} entry"
                    ),
                    write_as=_require(e, "write_as", f"protocol {p.get('key')} entry"),
                    dicom_type=e.get("dicom_type", "CONTROL"),
                    colour=e.get("colour"),
                    required=bool(e.get("required", True)),
                )
                for e in p.get("entries", ())
            ),
        )
        for p in raw.get("protocols", [])
    )
    presets = tuple(
        PresetDef(
            key=_require(p, "key", "preset"),
            display_name=p.get("display_name", p["key"]),
            structure_ids=tuple(p.get("structure_ids", ())),
            models=tuple(p.get("models", ())),
        )
        for p in raw.get("presets", [])
    )

    by_model = {m.key: m for m in models}
    if len(by_model) != len(models):
        raise CatalogError("duplicate model keys")
    if DEFAULT_MODEL not in by_model:
        raise CatalogError(f"catalog must define the default model {DEFAULT_MODEL!r}")

    by_structure = {s.id: s for s in structures}
    if len(by_structure) != len(structures):
        raise CatalogError("duplicate structure ids")

    for s in structures:
        if s.source_model not in by_model:
            raise CatalogError(f"structure {s.id} names unknown model {s.source_model}")

    # An alias pointing at two structures would make auto-detect pick one at
    # random, so it is a hard load failure rather than a warning.
    by_alias: dict[str, str] = {}
    for s in structures:
        for alias in s.aliases:
            existing = by_alias.get(alias)
            if existing and existing != s.id:
                raise CatalogError(
                    f"alias {alias!r} is claimed by both {existing} and {s.id}"
                )
            by_alias[alias] = s.id

    by_preset = {p.key: p for p in presets}
    for p in presets:
        unknown = [i for i in p.structure_ids if i not in by_structure]
        if unknown:
            raise CatalogError(f"preset {p.key} names unknown structures: {unknown}")

    by_protocol = {p.key: p for p in protocols}
    if len(by_protocol) != len(protocols):
        raise CatalogError("duplicate protocol keys")

    # What is validated here is what can HURT. The write-as id becomes an Eclipse
    # structure Id, so its length and uniqueness decide whether a write succeeds; the
    # DICOM type and the colour are applied when a structure is created. An entry
    # naming a structure no model produces is deliberately NOT an error -- see
    # `protocol_structure_ids` -- because a site protocol legitimately lists
    # structures a human contours.
    for p in protocols:
        for model_key in p.models:
            if model_key not in by_model:
                raise CatalogError(
                    f"protocol {p.key} names unknown model {model_key!r}"
                )

        seen_ids: set[str] = set()
        seen_write_as: set[str] = set()
        for e in p.entries:
            if e.structure_id in seen_ids:
                raise CatalogError(
                    f"protocol {p.key} names {e.structure_id} twice"
                )
            seen_ids.add(e.structure_id)

            write_as = e.write_as.strip()
            if not write_as:
                raise CatalogError(
                    f"protocol {p.key}: {e.structure_id} has an empty write_as"
                )
            if len(write_as) > MAX_STRUCTURE_ID:
                raise CatalogError(
                    f"protocol {p.key}: write_as {write_as!r} is longer than "
                    f"Eclipse's {MAX_STRUCTURE_ID} characters"
                )
            folded = write_as.lower()
            if folded in seen_write_as:
                raise CatalogError(
                    f"protocol {p.key}: two entries write as {write_as!r}"
                )
            seen_write_as.add(folded)

            if e.dicom_type.upper() not in DICOM_TYPES:
                raise CatalogError(
                    f"protocol {p.key}: {e.structure_id} has unknown dicom_type "
                    f"{e.dicom_type!r}"
                )
            if e.colour is not None and not _HEX_COLOUR.match(e.colour):
                raise CatalogError(
                    f"protocol {p.key}: {e.structure_id} has a malformed colour "
                    f"{e.colour!r}"
                )

    return Catalog(
        version=raw.get("version", 0),
        group_order=tuple(raw.get("group_order", ())),
        models=models,
        structures=structures,
        presets=presets,
        protocols=protocols,
        _by_model=by_model,
        _by_structure=by_structure,
        _by_alias=by_alias,
        _by_preset=by_preset,
        _by_protocol=by_protocol,
    )


@functools.lru_cache(maxsize=1)
def catalog() -> Catalog:
    """The process-wide catalog. Cached; the file does not change at runtime."""
    return load_catalog()
