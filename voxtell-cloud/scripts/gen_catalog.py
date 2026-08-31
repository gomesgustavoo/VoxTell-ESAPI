#!/usr/bin/env python3
"""Generate ``voxtell_cloud/model_catalog.json`` from CADS's own labelmap.

Why a generator and not a hand-written JSON: the label *indices* are what map a
network's output channel onto an anatomical name, and getting one wrong silently
mislabels a structure in a patient. CADS publishes them in
``resources/info/labelmap.md``; that file is vendored at ``scripts/data/`` so this
is reproducible without network access, and re-running it after a CADS release is
the only supported way to change the indices.

Run:  python scripts/gen_catalog.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LABELMAP = ROOT / "scripts" / "data" / "cads-labelmap.md"
OUT = ROOT / "voxtell_cloud" / "model_catalog.json"

CATALOG_VERSION = 2

# CADS weights come in three variants chosen by the `-license` flag. `reference`
# is upstream's default and `research` is CC BY-NC-SA 4.0 -- neither is usable in
# a commercial product. `open` is CC BY-SA 4.0 and permits commercial use.
CADS_WEIGHTS_VARIANT = "open"
CADS_WEIGHTS_LICENCE = "CC-BY-SA-4.0"
CADS_CODE_LICENCE = "Apache-2.0"

# --------------------------------------------------------------------------- #
# Task metadata
# --------------------------------------------------------------------------- #
# display name and default group per CADS submodel. Mixed-content tasks are
# split further by STRUCTURE_GROUPS below.
TASKS = {
    "551": ("Abdominal organs & lung lobes", "Abdominal organs", "Abdomen / thorax"),
    "552": ("Vertebrae", "Vertebrae", "Spine"),
    "553": ("Thoracic & abdominal detail", "Thoracic organs", "Thorax / abdomen"),
    "554": ("Appendicular skeleton & muscles", "Appendicular skeleton", "Whole body"),
    "555": ("Ribs", "Ribs", "Thorax"),
    "556": ("Radiotherapy structures", "Radiotherapy structures", "Whole body"),
    "557": ("Brain & head tissue", "Brain & head tissue", "Head"),
    "558": ("Head & neck", "Head & neck", "Head & neck"),
    "559": ("Tissue types & cavities", "Tissue & cavities", "Whole body"),
}

GROUP_ORDER = [
    "Radiotherapy structures",
    "Thoracic organs",
    "Lung lobes",
    "Abdominal organs",
    "Pelvic organs",
    "Head & neck",
    "Brain & head tissue",
    "Vessels",
    "Vertebrae",
    "Ribs",
    "Appendicular skeleton",
    "Muscles",
    "Tissue & cavities",
]

# Per-structure group overrides, for the tasks that mix regions. Keyed by the
# exact display name from the labelmap.
STRUCTURE_GROUPS = {
    # 551 -- the lung lobes are not abdominal organs
    "Upper lobe of lung L": "Lung lobes",
    "Lower lobe of lung L": "Lung lobes",
    "Upper lobe of lung R": "Lung lobes",
    "Middle lobe of lung R": "Lung lobes",
    "Lower lobe of lung R": "Lung lobes",
    "Aorta": "Vessels",
    "Inferior vena cava": "Vessels",
    "Portal vein and splenic vein": "Vessels",
    # 553
    "Brain": "Brain & head tissue",
    "Face": "Head & neck",
    "Common iliac artery L": "Vessels",
    "Common iliac artery R": "Vessels",
    "Common iliac vein L": "Vessels",
    "Common iliac vein R": "Vessels",
    "Pulmonary artery": "Vessels",
    "Small intestine": "Abdominal organs",
    "Duodenum": "Abdominal organs",
    "Colon": "Abdominal organs",
    "Urinary bladder": "Pelvic organs",
    # 554 -- muscles out of the skeleton group
    "Gluteus maximus muscle L": "Muscles",
    "Gluteus maximus muscle R": "Muscles",
    "Gluteus medius muscle L": "Muscles",
    "Gluteus medius muscle R": "Muscles",
    "Gluteus minius muscle L": "Muscles",
    "Gluteus minius muscle R": "Muscles",
    "Deep muscle of back L": "Muscles",
    "Deep muscle of back R": "Muscles",
    "Iliopsoas muscle L": "Muscles",
    "Iliopsoas muscle R": "Muscles",
    # 556
    "Psoas major muscle R": "Muscles",
    "Psoas major muscle L": "Muscles",
    "Rectus abdominis muscle R": "Muscles",
    "Rectus abdominis muscle L": "Muscles",
    "Sigmoid colon": "Pelvic organs",
    "Rectum": "Pelvic organs",
    "Prostate": "Pelvic organs",
    "Seminal vesicle": "Pelvic organs",
    "Bowel space": "Pelvic organs",
    # 559
    "Muscle": "Muscles",
    "Bones": "Appendicular skeleton",
}

# --------------------------------------------------------------------------- #
# Aliases
# --------------------------------------------------------------------------- #
# Extra normalised aliases, on top of the mechanical variants generated below.
# Deliberately conservative: a WRONG alias silently compares two different
# organs, which is worse than failing to match. Anything ambiguous is omitted and
# left for the org-level synonym editor.
#
# Two traps encoded here by omission:
#   * "Spinal canal" (556) is NOT aliased to "Spinal cord" (559). Different
#     structures; conflating them would compare a cord against a canal.
#   * Lung *lobes* (551) are NOT aliased to whole-lung names. A lobe is not the
#     lung, and Eclipse's "Lung_L" is the whole lung.
CURATED_ALIASES = {
    "Urinary bladder": ["bladder"],
    "Mammary gland L": ["breast_l", "l_breast", "breast_left", "left_breast"],
    "Mammary gland R": ["breast_r", "r_breast", "breast_right", "right_breast"],
    "Both lips": ["lips", "lip"],
    "Esophagus": ["oesophagus"],
    "Cervical esophagus": ["cervical_oesophagus", "esophagus_cerv", "oesophagus_cerv"],
    "Bowel space": ["bowel", "bowel_bag", "bowelbag", "bowel_small"],
    "Sigmoid colon": ["sigmoid"],
    "Seminal vesicle": ["seminal_ves", "seminalves", "vesicles_seminal"],
    "Optic chiasm": ["chiasm", "opticchiasm"],
    "Parotid gland L": ["parotid_l", "l_parotid", "glnd_parotid_l"],
    "Parotid gland R": ["parotid_r", "r_parotid", "glnd_parotid_r"],
    "Submandibular gland L": ["submand_l", "glnd_submand_l", "smg_l"],
    "Submandibular gland R": ["submand_r", "glnd_submand_r", "smg_r"],
    "Lacrimal gland L": ["glnd_lacrimal_l", "lacrimal_l"],
    "Lacrimal gland R": ["glnd_lacrimal_r", "lacrimal_r"],
    "Optic nerve L": ["opticnrv_l", "nerve_optic_l"],
    "Optic nerve R": ["opticnrv_r", "nerve_optic_r"],
    "Pituitary gland": ["pituitary"],
    "Thyroid": ["thyroid_gland", "glnd_thyroid"],
    "Oral cavity": ["cavity_oral", "oralcavity"],
    "Spinal cord": ["spinalcord", "cord_spinal", "myelum"],
    "Spinal canal": ["canal_spinal", "spinalcanal"],
    "Larynx": ["larynx_whole"],
    "Brainstem": ["brain_stem", "stem_brain"],
    "Arytenoid cartilage": ["arytenoid", "cartilage_aryt"],
    "Cricopharyngeus": ["cricopharyngeal_inlet", "crico"],
    "Prosthetic breast implant": ["breast_implant", "implant_breast"],
    "Pericardium": ["pericardium_sac"],
    "Sternum": ["sternum_bone"],
    "Sacrum": ["sacrum_bone"],
}

LATERAL = {"L": ("left", "l"), "R": ("right", "r")}


def norm(text: str) -> str:
    """Normalised match key: lowercase, alphanumerics only.

    Punctuation and separators carry no clinical meaning in a structure name, so
    ``Kidney_R``, ``Kidney R`` and ``kidney-r`` all collapse to ``kidneyr``. The
    plugin's auto-detect normalises the Eclipse structure Id exactly the same way.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def class_name(display: str) -> str:
    """snake_case identifier used as the structure-id suffix."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", display).strip("_").lower()
    return re.sub(r"_+", "_", slug)


def aliases_for(display: str) -> list[str]:
    """Normalised alias keys for one structure."""
    out: set[str] = {norm(display), norm(class_name(display))}

    match = re.match(r"^(.*?)\s+([LR])$", display)
    if match:
        stem, side = match.group(1), match.group(2)
        word, letter = LATERAL[side]
        for suffix in (letter, word):
            out.add(norm(f"{stem} {suffix}"))
            out.add(norm(f"{suffix} {stem}"))

    # "Rib-3 L" also appears as "Rib 3 L" / "Rib3_L"; norm() already collapses
    # those. Vertebrae and ribs get their short clinical form too: L5, T12, C1.
    vertebra = re.match(r"^Vertebra ([CTL]\d+)$", display)
    if vertebra:
        out.add(norm(vertebra.group(1)))

    # Curated entries are already side-specific where laterality matters.
    for extra in CURATED_ALIASES.get(display, []):
        out.add(norm(extra))

    return sorted(a for a in out if a)


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
# A preset is a named selection across tasks -- the "structure preset" the
# planner picks instead of hand-ticking 167 boxes. Membership is by display name
# so this survives an index change upstream.
PRESETS = {
    "rt_thorax": ("RT thorax", [
        ("556", "Heart"), ("556", "Sternum"), ("556", "Spinal canal"),
        ("556", "Mammary gland L"), ("556", "Mammary gland R"),
        ("551", "Upper lobe of lung L"), ("551", "Lower lobe of lung L"),
        ("551", "Upper lobe of lung R"), ("551", "Middle lobe of lung R"),
        ("551", "Lower lobe of lung R"),
        ("553", "Esophagus"), ("553", "Trachea"), ("553", "Myocardium"),
        ("553", "Pulmonary artery"),
        ("559", "Spinal cord"),
    ]),
    "rt_pelvis": ("RT pelvis", [
        ("556", "Rectum"), ("556", "Prostate"), ("556", "Seminal vesicle"),
        ("556", "Sigmoid colon"), ("556", "Bowel space"),
        ("553", "Urinary bladder"),
        ("554", "Femur L"), ("554", "Femur R"), ("554", "Hip L"), ("554", "Hip R"),
        ("554", "Sacrum"),
    ]),
    "rt_head_neck": ("RT head & neck", [
        ("558", name) for name in (
            "Mandible", "Brainstem", "Oral cavity", "Cochlear L", "Cochlear R",
            "Cricopharyngeus", "Cervical esophagus", "Lacrimal gland L",
            "Lacrimal gland R", "Submandibular gland L", "Submandibular gland R",
            "Thyroid", "Glottis", "Supraglottis", "Both lips", "Optic chiasm",
            "Optic nerve L", "Optic nerve R", "Parotid gland L", "Parotid gland R",
            "Pituitary gland", "Carotid artery L", "Carotid artery R",
            "Anterior eyeball segment L", "Anterior eyeball segment R",
            "Posterior eyeball segment L", "Posterior eyeball segment R",
        )
    ] + [("556", "Larynx"), ("556", "Spinal canal"), ("559", "Spinal cord")]),
    "rt_abdomen": ("RT abdomen", [
        ("551", "Liver"), ("551", "Spleen"), ("551", "Stomach"), ("551", "Pancreas"),
        ("551", "Kidney L"), ("551", "Kidney R"), ("551", "Gallbladder"),
        ("551", "Aorta"), ("551", "Inferior vena cava"),
        ("553", "Duodenum"), ("553", "Small intestine"), ("553", "Colon"),
        ("559", "Spinal cord"),
    ]),
}


# ---------------------------------------------------------------------------- #
#  Clinic protocols
# ---------------------------------------------------------------------------- #
#
# A preset says only WHAT to segment. A protocol also says what each structure is
# called, what DICOM type it gets and what colour it is drawn in — the part that
# stops naming drift, which the published auto-segmentation audits identify as the
# commonest reason a QA script silently loses cases.
#
# The ids follow AAPM TG-263 where it has a name for the structure, truncated to the
# 16 characters Eclipse allows. They are a DEFAULT, meant to be edited per clinic:
# this table is the one place a physicist changes naming for every workstation at
# once, without a plugin release or a re-approval.
#
# (task_id, CADS display name, write_as, dicom_type, colour)
PROTOCOLS = {
    "prostate": ("Prostate", "Pelvis", [
        ("553", "Urinary bladder", "Bladder", "ORGAN", "#F2C14E"),
        ("556", "Rectum", "Rectum", "ORGAN", "#A9714B"),
        ("556", "Prostate", "Prostate", "ORGAN", "#C77CFF"),
        ("556", "Seminal vesicle", "SeminalVes", "ORGAN", "#B486D9"),
        ("556", "Sigmoid colon", "Colon_Sigmoid", "ORGAN", "#C89060"),
        ("556", "Bowel space", "BowelBag", "ORGAN", "#D2B48C"),
        ("554", "Femur L", "Femur_L", "ORGAN", "#7FB069"),
        ("554", "Femur R", "Femur_R", "ORGAN", "#4F9D69"),
        ("554", "Sacrum", "Sacrum", "ORGAN", "#9AA1AB"),
        ("556", "Spinal canal", "SpinalCanal", "ORGAN", "#5AA9E6"),
    ]),
    "breast": ("Breast", "Thorax", [
        ("556", "Heart", "Heart", "ORGAN", "#E0645C"),
        ("551", "Upper lobe of lung L", "Lung_L_Upper", "ORGAN", "#8FD5E8"),
        ("551", "Lower lobe of lung L", "Lung_L_Lower", "ORGAN", "#6FBFD8"),
        ("551", "Upper lobe of lung R", "Lung_R_Upper", "ORGAN", "#A8E0EF"),
        ("551", "Middle lobe of lung R", "Lung_R_Mid", "ORGAN", "#89CFE4"),
        ("551", "Lower lobe of lung R", "Lung_R_Lower", "ORGAN", "#6AB6D0"),
        ("556", "Mammary gland L", "Breast_L", "ORGAN", "#F09EBB"),
        ("556", "Mammary gland R", "Breast_R", "ORGAN", "#E88CAC"),
        ("556", "Sternum", "Sternum", "ORGAN", "#C9CDD3"),
        ("553", "Esophagus", "Esophagus", "ORGAN", "#B07CC6"),
        ("559", "Spinal cord", "SpinalCord", "ORGAN", "#F2E86D"),
    ]),
    "head_neck": ("Head & neck", "Head & neck", [
        ("558", "Mandible", "Mandible", "ORGAN", "#D8CBB3"),
        ("558", "Brainstem", "Brainstem", "ORGAN", "#F2A65A"),
        ("558", "Parotid gland L", "Parotid_L", "ORGAN", "#7FB069"),
        ("558", "Parotid gland R", "Parotid_R", "ORGAN", "#4F9D69"),
        ("558", "Submandibular gland L", "Glnd_Submand_L", "ORGAN", "#9BC995"),
        ("558", "Submandibular gland R", "Glnd_Submand_R", "ORGAN", "#7EB878"),
        ("558", "Optic nerve L", "OpticNrv_L", "ORGAN", "#F5D06F"),
        ("558", "Optic nerve R", "OpticNrv_R", "ORGAN", "#EFC24E"),
        ("558", "Optic chiasm", "OpticChiasm", "ORGAN", "#E8A33D"),
        ("558", "Cochlear L", "Cochlea_L", "ORGAN", "#C9A0DC"),
        ("558", "Cochlear R", "Cochlea_R", "ORGAN", "#B48BC9"),
        ("558", "Oral cavity", "OralCavity", "ORGAN", "#E4B7B2"),
        ("558", "Thyroid", "Glnd_Thyroid", "ORGAN", "#8FBF9F"),
        ("556", "Larynx", "Larynx", "ORGAN", "#87A9C4"),
        ("559", "Spinal cord", "SpinalCord", "ORGAN", "#F2E86D"),
        ("556", "Spinal canal", "SpinalCanal", "ORGAN", "#5AA9E6"),
    ]),
    "abdomen": ("Abdomen / liver SBRT", "Abdomen", [
        ("551", "Liver", "Liver", "ORGAN", "#B5651D"),
        ("551", "Kidney L", "Kidney_L", "ORGAN", "#8B6BA8"),
        ("551", "Kidney R", "Kidney_R", "ORGAN", "#7458A0"),
        ("551", "Spleen", "Spleen", "ORGAN", "#A0522D"),
        ("551", "Stomach", "Stomach", "ORGAN", "#E2A16F"),
        ("553", "Duodenum", "Duodenum", "ORGAN", "#D98E4A"),
        ("553", "Small intestine", "Bowel_Small", "ORGAN", "#D2B48C"),
        ("553", "Colon", "Bowel_Large", "ORGAN", "#C89060"),
        ("559", "Spinal cord", "SpinalCord", "ORGAN", "#F2E86D"),
    ]),
}

# Eclipse's own limit on a structure Id. Enforced here AND in
# voxtell_cloud/catalog.py: a violation discovered on the workstation is discovered
# during a write into a patient.
MAX_STRUCTURE_ID = 16


def parse_labelmap(path: pathlib.Path) -> dict[str, dict[int, str]]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"<summary><strong>Model (\d+)</strong></summary>", text)
    tasks: dict[str, dict[int, str]] = {}
    for i in range(1, len(blocks), 2):
        task_id, body = blocks[i], blocks[i + 1]
        rows = re.findall(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", body, re.MULTILINE)
        labels = {int(n): name.strip() for n, name in rows if int(n) != 0}
        if labels:
            tasks[task_id] = labels
    return tasks


def main() -> int:
    if not LABELMAP.exists():
        print(f"missing {LABELMAP}", file=sys.stderr)
        return 1

    tasks = parse_labelmap(LABELMAP)
    missing = sorted(set(TASKS) - set(tasks))
    if missing:
        print(f"labelmap has no entries for task(s): {missing}", file=sys.stderr)
        return 1

    models = [
        {
            "key": "voxtell",
            "display_name": "VoxTell (free-text prompts)",
            "kind": "prompt",
            "region": "Whole body",
            "modality": "CT",
            "count": None,
            "task": None,
            "weights_variant": None,
            "weights_licence": "CC-BY-NC-SA-4.0",
            "code_licence": "Apache-2.0",
        }
    ]
    structures = []
    by_display: dict[tuple[str, str], str] = {}

    for task_id in sorted(TASKS):
        labels = tasks[task_id]
        display, default_group, region = TASKS[task_id]
        source_model = f"cads_{task_id}"
        models.append({
            "key": source_model,
            "display_name": f"CADS {task_id} · {display}",
            "kind": "cads",
            "region": region,
            "modality": "CT",
            "count": len(labels),
            "task": task_id,
            "weights_variant": CADS_WEIGHTS_VARIANT,
            "weights_licence": CADS_WEIGHTS_LICENCE,
            "code_licence": CADS_CODE_LICENCE,
        })
        for index in sorted(labels):
            name = labels[index]
            cls = class_name(name)
            sid = f"{source_model}.{cls}"
            structures.append({
                "id": sid,
                "display_name": name,
                "group": STRUCTURE_GROUPS.get(name, default_group),
                "modality": "CT",
                "source_model": source_model,
                "label_index": index,
                "class_name": cls,
                "aliases": aliases_for(name),
            })
            by_display[(task_id, name)] = sid

    presets = []
    for key, (display, members) in PRESETS.items():
        ids, unknown = [], []
        for task_id, name in members:
            sid = by_display.get((task_id, name))
            if sid is None:
                unknown.append(f"{task_id}:{name}")
            elif sid not in ids:
                ids.append(sid)
        if unknown:
            print(f"preset {key} references unknown structures: {unknown}", file=sys.stderr)
            return 1
        presets.append({
            "key": key,
            "display_name": display,
            "structure_ids": ids,
            "models": sorted({i.split(".", 1)[0] for i in ids}),
        })

    protocols = []
    for key, (display, site, members) in PROTOCOLS.items():
        entries: list[dict] = []
        unknown: list[str] = []
        seen_write_as: dict[str, str] = {}
        for task_id, name, write_as, dicom_type, colour in members:
            sid = by_display.get((task_id, name))
            if sid is None:
                unknown.append(f"{task_id}:{name}")
                continue
            if len(write_as) > MAX_STRUCTURE_ID:
                print(
                    f"protocol {key}: write_as {write_as!r} is longer than Eclipse's "
                    f"{MAX_STRUCTURE_ID} characters",
                    file=sys.stderr,
                )
                return 1
            clash = seen_write_as.get(write_as.lower())
            if clash:
                print(
                    f"protocol {key}: {sid} and {clash} both write as {write_as!r}",
                    file=sys.stderr,
                )
                return 1
            seen_write_as[write_as.lower()] = sid
            entries.append({
                "structure_id": sid,
                "write_as": write_as,
                "dicom_type": dicom_type,
                "colour": colour,
                "required": True,
            })
        if unknown:
            print(
                f"protocol {key} references unknown structures: {unknown}",
                file=sys.stderr,
            )
            return 1
        protocols.append({
            "key": key,
            "display_name": display,
            "site": site,
            "modality": "CT",
            "models": sorted({e["structure_id"].split(".", 1)[0] for e in entries}),
            "entries": entries,
        })

    groups_seen = {s["group"] for s in structures}
    unordered = sorted(groups_seen - set(GROUP_ORDER))
    if unordered:
        print(f"groups missing from GROUP_ORDER: {unordered}", file=sys.stderr)
        return 1

    catalog = {
        "version": CATALOG_VERSION,
        "generated_from": "CADS resources/info/labelmap.md (vendored at scripts/data/)",
        "group_order": [g for g in GROUP_ORDER if g in groups_seen],
        "models": models,
        "structures": structures,
        "presets": presets,
        # Presets stay in the payload for as long as any 2.1.x plugin is approved on a
        # workstation: that build reads presets and knows nothing about protocols.
        "protocols": protocols,
    }

    OUT.write_text(json.dumps(catalog, indent=1, sort_keys=False) + "\n", encoding="utf-8")

    duplicate_keys = len(structures) - len({s["id"] for s in structures})
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  models     : {len(models)} ({len(models) - 1} CADS tasks + VoxTell)")
    print(f"  structures : {len(structures)} (duplicate ids: {duplicate_keys})")
    print(f"  presets    : {len(presets)}")
    print(f"  protocols  : {len(protocols)} "
          f"({sum(len(p['entries']) for p in protocols)} entries)")
    print(f"  groups     : {len(catalog['group_order'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
