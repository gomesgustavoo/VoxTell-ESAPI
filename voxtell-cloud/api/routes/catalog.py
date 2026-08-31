"""The model catalog — what this deployment can segment.

Unauthenticated, like ``/auth/config``, and for the same reason: the plugin needs
it to render its model picker before the planner has necessarily signed in, and
it contains no patient data and no tenant-specific information. It is the same
answer for every caller.

Serving the catalog from the server rather than compiling it into the plugin is
what makes a new model a server-side deployment. Eclipse approves a plugin DLL by
version *and* content hash on every workstation, so anything baked into the DLL
costs a clinic-wide re-approval to change; a catalog fetched at runtime costs
nothing.
"""

from __future__ import annotations

from fastapi import APIRouter

from voxtell_cloud.catalog import catalog as load

from ..schemas import (
    CatalogModel,
    CatalogPreset,
    CatalogProtocol,
    CatalogProtocolEntry,
    CatalogResponse,
    CatalogStructure,
)

router = APIRouter(tags=["catalog"])


@router.get("/models", response_model=CatalogResponse)
def list_models() -> CatalogResponse:
    """Models, structures, groups, presets and clinic protocols, in display order."""
    cat = load()
    return CatalogResponse(
        version=cat.version,
        group_order=list(cat.group_order),
        models=[
            CatalogModel(
                key=m.key,
                display_name=m.display_name,
                kind=m.kind,
                region=m.region,
                modality=m.modality,
                count=m.count,
                task=m.task,
                weights_variant=m.weights_variant,
                weights_licence=m.weights_licence,
                code_licence=m.code_licence,
            )
            for m in cat.models
        ],
        structures=[
            CatalogStructure(
                id=s.id,
                display_name=s.display_name,
                group=s.group,
                modality=s.modality,
                source_model=s.source_model,
                aliases=list(s.aliases),
            )
            for s in cat.structures
        ],
        presets=[
            CatalogPreset(
                key=p.key,
                display_name=p.display_name,
                structure_ids=list(p.structure_ids),
                models=list(p.models),
            )
            for p in cat.presets
        ],
        protocols=[
            CatalogProtocol(
                key=p.key,
                display_name=p.display_name,
                site=p.site,
                modality=p.modality,
                models=list(p.models),
                entries=[
                    CatalogProtocolEntry(
                        structure_id=e.structure_id,
                        write_as=e.write_as,
                        dicom_type=e.dicom_type,
                        colour=e.colour,
                        required=e.required,
                    )
                    for e in p.entries
                ],
            )
            for p in cat.protocols
        ],
    )
