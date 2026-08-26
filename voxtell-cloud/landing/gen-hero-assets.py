#!/usr/bin/env python3
"""Build the hero's CT raster and contour JSON from REAL model output.

    landing/gen-hero-assets.py --dicom <dir> --result <voxtell_result.json>

Replaces the old ``gen-hero-contours.py``, which drew harmonic perturbations of
ellipses inside a fake radial-gradient "scan". That figure was labelled
``provenance: "schematic"`` and was honest, but it was also unsellable: there was
no image, and the contours were invented.

WHAT THIS PRODUCES

    assets/hero-slice.v2.webp      one windowed axial slice, 512x384, 8-bit grey
    assets/hero-contours.v3.json   polylines for that slice, in its pixel space

WHERE THE DATA COMES FROM

The CT is TotalSegmentator's in-tree reference series
(``tests/reference_files/example_ct_dicom``): 20 axial slices, 512x512,
0.977 mm in-plane, 2 mm spacing, and — the part that matters for publishing it —
``PatientName`` and ``PatientID`` are both empty. Dataset CC BY 4.0
(DOI 10.5281/zenodo.10047292), code Apache-2.0.

The contours are VoxTell's own output, obtained by pushing that series through
the live service exactly as the Eclipse plugin does:

    scripts/e2e_client.py --base https://voxtell.dicomsegvr.com/v1 --key vxt_... \\
        --dicom <dir> --prompt liver --prompt spleen ... --save result.json

So the figure on the marketing page is the product's actual output on a publicly
redistributable image, and ``provenance`` says ``model_output`` truthfully. The
prompts are deliberately the phrasings a planner would type rather than the
model's canonical labels — ``T12 vertebra``, ``left erector spinae muscle``,
``right 11th rib`` — because "it already knows how you say it" is the claim the
hero exists to demonstrate.

NEVER regenerate this from patient data. ``dicomsegvr/Brain-Substructures-Dicom.zip``
is on this box and is tempting: real MR, real RTSTRUCT, 34 ROIs. It is HCP Wu-Minn
under a Data Use Terms agreement that forbids redistribution, and its
``PatientName`` is set. It must not reach ``landing/``.

COORDINATES

The API returns ``points_lps`` in millimetres in DICOM patient space. With an
identity ``ImageOrientationPatient`` (checked, not assumed) the inverse is:

    col = (x - ipp_x) / spacing_col        row = (y - ipp_y) / spacing_row

Points are then moved into the cropped, rescaled frame the raster is written in,
so the SVG viewBox and the image share one coordinate system and the overlay
cannot drift from the picture.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import pathlib
import sys

import numpy as np
import pydicom
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE / "assets"

# Output raster size, and therefore the SVG viewBox. 4:3 rather than the square
# the old schematic used: a torso at this level is much wider than it is deep, so
# a square crop spends the top and bottom fifths of the frame on black. 512 wide
# keeps roughly the source's in-plane sampling after the crop is rescaled.
WIDTH, HEIGHT = 512, 384

# The slice that ships. Sixteen of sixteen structures appear on both z=3 and z=9;
# z=9 is the one to show, because at z=3 the bowel gas fragments the colon into a
# dozen small loops that read as noise over the stomach. Pinned rather than
# computed so regenerating reproduces what shipped; pass --slice to explore.
DEFAULT_SLICE = 9

# Soft-tissue abdomen window. The series carries WindowCenter 40 / WindowWidth
# 300 in its headers; 400 is opened up slightly so the liver/spleen boundary and
# the fat planes both stay readable once the image is dimmed behind the contours.
WINDOW_CENTRE = 40.0
WINDOW_WIDTH = 400.0

# Fraction of the contour bounding box added as margin before squaring the crop.
# Without it the torso is cut at the skin line, which reads as a broken image
# rather than a zoomed viewport.
CROP_MARGIN = 0.13

# Points per contour after decimation. The draw-on animation strokes a path whose
# length is precomputed, so fewer points is cheaper with no visible cost at this
# scale; below ~40 the vessels visibly polygonise.
MAX_POINTS = 72

# prompt (as submitted) -> how the ledger and the colour system name it.
#
# `name` follows TG-263 style because that is what the structure list in Eclipse
# shows. `token` selects a --vx-s-* colour. `keywords` are substrings that resolve
# free text to this structure and `side` lets a generic word hit both members of a
# pair — "erector spinae" draws both, "left erector spinae" draws one. The
# vocabulary lives in this data rather than in the demo script, so regenerating
# the figure regenerates what the demo understands.
# Eclipse's DicomType for the structure the plugin creates. The hero's approval
# sheet shows this column because it is one of the fields a planner checks before
# import, so it has to be real rather than decorative. Every structure below is an
# organ at risk, hence one default and no per-entry overrides yet; a target volume
# would carry type="Ptv" and this is the seam for it.
DEFAULT_DICOM_TYPE = "Organ"

STRUCTURES: dict[str, dict] = {
    "liver": dict(
        name="Liver", token="s-liver", side=None,
        keywords=["liver", "hepat"]),
    "spleen": dict(
        name="Spleen", token="s-spleen", side=None,
        keywords=["spleen", "splenic", "lien"]),
    "stomach": dict(
        name="Stomach", token="s-stomach", side=None,
        keywords=["stomach", "gastric"]),
    "pancreas": dict(
        name="Pancreas", token="s-pancreas", side=None,
        keywords=["pancrea"]),
    "aorta": dict(
        name="Aorta", token="s-aorta", side=None,
        keywords=["aorta", "aortic"]),
    "inferior vena cava": dict(
        name="IVC", token="s-ivc", side=None,
        keywords=["vena cava", "ivc", "cava"]),
    "portal vein and splenic vein": dict(
        name="PortalVein", token="s-vein", side=None,
        keywords=["portal", "splenic vein"]),
    "spinal cord": dict(
        name="SpinalCord", token="s-cord", side=None,
        keywords=["spinal cord", "cord", "myelum", "medulla spinalis"]),
    "T12 vertebra": dict(
        name="Vertebra_T12", token="s-vertebra", side=None,
        keywords=["t12", "vertebra", "vertebral", "twelfth thoracic", "spine"]),
    "colon": dict(
        name="Colon", token="s-colon", side=None,
        keywords=["colon", "large bowel", "large intestine"]),
    "right adrenal gland": dict(
        name="Adrenal_R", token="s-adrenal", side="right",
        keywords=["adrenal", "suprarenal"]),
    # ^ and `pancreas` are submitted but SKIPped below — see SKIP.
    "left erector spinae muscle": dict(
        name="ErectorSpinae_L", token="s-muscle", side="left",
        keywords=["erector spinae", "paraspinal", "autochthon", "paravertebral"]),
    "right erector spinae muscle": dict(
        name="ErectorSpinae_R", token="s-muscle", side="right",
        keywords=["erector spinae", "paraspinal", "autochthon", "paravertebral"]),
    "costal cartilages": dict(
        name="CostalCartilages", token="s-cartilage", side=None,
        keywords=["costal cartilage", "cartilage"]),
    "right 11th rib": dict(
        name="Rib_R_11", token="s-bone", side="right",
        keywords=["rib", "costa", "eleventh rib", "11th rib"]),
    "left 11th rib": dict(
        name="Rib_L_11", token="s-bone", side="left",
        keywords=["rib", "costa", "eleventh rib", "11th rib"]),
}

# Submitted to the model, kept in the record above, but NOT drawn on the page.
#
# This 40 mm slab clips the pancreas at its tail and the right adrenal at its
# upper pole, so VoxTell returns 1.7 cc and 2.1 cc — two- or three-pixel specks
# that add nothing to the figure and invite "what is that dot?". They are also the
# only two structures where VoxTell and the TotalSegmentator reference genuinely
# disagree on this slice (Dice 0.00 and 0.13, against 0.97 for liver and 0.94 for
# aorta), because the reference finds almost nothing there either. A marketing
# figure should not showcase the output its author cannot stand behind — and
# dropping them is an editorial choice, recorded here, not a silent omission.
SKIP = {"pancreas", "right adrenal gland"}

# Suggestion chips. Deliberately a mix of a plain name, an abbreviation, a
# laterality shorthand and an ordinal, because the demo has to *show* that the
# vocabulary is flexible rather than assert it. Every one of these draws.
CHIPS: list[dict] = [
    dict(label="liver", targets=["Liver"]),
    dict(label="spleen", targets=["Spleen"]),
    dict(label="stomach", targets=["Stomach"]),
    dict(label="aorta", targets=["Aorta"]),
    dict(label="IVC", targets=["IVC"]),
    dict(label="spinal cord", targets=["SpinalCord"]),
    dict(label="T12 vertebra", targets=["Vertebra_T12"]),
    dict(label="r. 11th rib", targets=["Rib_R_11"]),
    dict(label="erector spinae", targets=["ErectorSpinae_L", "ErectorSpinae_R"]),
    dict(label="colon", targets=["Colon"]),
]

# Free text -> structures, for phrasings that are not substrings of any keyword.
# None of these is a canonical name; that is the point.
ALIASES: dict[str, list[str]] = {
    "hepatic parenchyma": ["Liver"],
    "cava": ["IVC"],
    "gastric": ["Stomach"],
    "lien": ["Spleen"],
    "t12": ["Vertebra_T12"],
    "twelfth thoracic vertebra": ["Vertebra_T12"],
    "large bowel": ["Colon"],
    "paraspinal muscles": ["ErectorSpinae_L", "ErectorSpinae_R"],
    "costa xi dextra": ["Rib_R_11"],
    "the cord": ["SpinalCord"],
}

ATTRIBUTION = {
    "image": "TotalSegmentator reference series (Wasserthal et al.)",
    "image_licence": "CC BY 4.0",
    "image_doi": "10.5281/zenodo.10047292",
    "contours": "VoxTell v1.1, this service",
}


def load_series(folder: pathlib.Path) -> list[pydicom.Dataset]:
    files = sorted(glob.glob(str(folder / "*")))
    ds = [pydicom.dcmread(f) for f in files]
    if not ds:
        raise SystemExit(f"no DICOM files in {folder}")

    iop = np.array(ds[0].ImageOrientationPatient, float)
    # The LPS->pixel inverse below is only this simple for an axis-aligned series.
    # Refuse rather than silently place contours a few millimetres off.
    if not np.allclose(iop, [1, 0, 0, 0, 1, 0], atol=1e-6):
        raise SystemExit(f"series is not axis-aligned: ImageOrientationPatient={list(iop)}")

    normal = np.cross(iop[:3], iop[3:])
    ds.sort(key=lambda d: float(np.dot(np.array(d.ImagePositionPatient, float), normal)))

    # Publishing an identified image would be the one unrecoverable mistake here.
    for d in ds:
        if str(getattr(d, "PatientName", "")).strip() or str(getattr(d, "PatientID", "")).strip():
            raise SystemExit(
                "REFUSING: this series carries PatientName/PatientID. Only a "
                "publicly redistributable, de-identified image may ship."
            )
    return ds


def hu_plane(d: pydicom.Dataset) -> np.ndarray:
    return d.pixel_array.astype(np.float32) * float(d.RescaleSlope) + float(d.RescaleIntercept)


def to_pixels(points_lps: list[list[float]], d: pydicom.Dataset) -> np.ndarray:
    """LPS millimetres -> (col, row) float pixel indices on this slice."""
    ipp = np.array(d.ImagePositionPatient, float)
    sr, sc = (float(v) for v in d.PixelSpacing)  # (row spacing, col spacing)
    p = np.asarray(points_lps, float)
    col = (p[:, 0] - ipp[0]) / sc
    row = (p[:, 1] - ipp[1]) / sr
    return np.stack([col, row], axis=1)


def decimate(pts: np.ndarray, limit: int = MAX_POINTS) -> np.ndarray:
    if len(pts) <= limit:
        return pts
    idx = np.linspace(0, len(pts) - 1, limit).round().astype(int)
    return pts[np.unique(idx)]


def path_d(pts: np.ndarray) -> str:
    head = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
    rest = "".join(f"L{x:.1f} {y:.1f}" for x, y in pts[1:])
    return head + rest + "Z"


def path_length(pts: np.ndarray) -> int:
    d = np.diff(np.vstack([pts, pts[:1]]), axis=0)
    return int(np.hypot(d[:, 0], d[:, 1]).sum()) + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dicom", required=True, type=pathlib.Path)
    ap.add_argument("--result", required=True, type=pathlib.Path,
                    help="JSON saved by scripts/e2e_client.py --save")
    ap.add_argument("--slice", type=int, default=None,
                    help="z_index to render (default: the one with most structures)")
    args = ap.parse_args()

    series = load_series(args.dicom)
    payload = json.loads(args.result.read_text())
    results = payload["results"]

    unknown = [r["prompt"] for r in results if r["prompt"] not in STRUCTURES]
    if unknown:
        raise SystemExit(f"result contains prompts with no STRUCTURES entry: {unknown}")

    # Which slice to draw: the one carrying the most distinct structures, so the
    # figure shows the widest range the model actually produced.
    per_slice: dict[int, set[str]] = {}
    for r in results:
        for c in r["contours"]:
            per_slice.setdefault(c["z_index"], set()).add(r["prompt"])
    z = args.slice if args.slice is not None else DEFAULT_SLICE
    if z not in per_slice:
        raise SystemExit(f"slice {z} has no contours")
    if z >= len(series):
        raise SystemExit(f"slice {z} beyond the {len(series)}-slice series")
    print(f"slice z={z}: {len(per_slice[z])} structure(s) of {len(results)}")

    d = series[z]
    hu = hu_plane(d)

    # ---- crop, from the contours rather than the body, so the frame is filled
    # by the anatomy that is actually labelled. A body-extent crop would include
    # the arms at the image edges and shrink the torso to the middle third.
    all_pts = []
    for r in results:
        if r["prompt"] in SKIP:
            continue
        for c in r["contours"]:
            if c["z_index"] == z:
                all_pts.append(to_pixels(c["points_lps"], d))
    stacked = np.vstack(all_pts)
    x0, y0 = stacked.min(axis=0)
    x1, y1 = stacked.max(axis=0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    want_w = (x1 - x0) * (1 + CROP_MARGIN)
    want_h = (y1 - y0) * (1 + CROP_MARGIN)
    # Grow whichever axis is short until the crop matches the output aspect, so
    # the rescale is uniform and the contours cannot be stretched off the image.
    aspect = WIDTH / HEIGHT
    if want_w / want_h < aspect:
        want_w = want_h * aspect
    else:
        want_h = want_w / aspect
    # Clamp inside the source, shrinking (both axes together) if it will not fit.
    fit = min(1.0, hu.shape[1] / want_w, hu.shape[0] / want_h)
    want_w, want_h = want_w * fit, want_h * fit
    cx = min(max(cx, want_w / 2), hu.shape[1] - want_w / 2)
    cy = min(max(cy, want_h / 2), hu.shape[0] - want_h / 2)
    left, top = cx - want_w / 2, cy - want_h / 2
    scale = WIDTH / want_w
    print(f"crop: left={left:.1f} top={top:.1f} {want_w:.1f}x{want_h:.1f} "
          f"-> {WIDTH}x{HEIGHT} (scale {scale:.3f})")

    # ---- raster
    lo = WINDOW_CENTRE - WINDOW_WIDTH / 2
    grey = np.clip((hu - lo) / WINDOW_WIDTH, 0, 1)
    img = Image.fromarray((grey * 255).astype(np.uint8), mode="L")
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS,
                     box=(left, top, left + want_w, top + want_h))
    out_img = ASSETS / "hero-slice.v2.webp"
    # Lossless: this is a 512px greyscale medical image behind thin vector
    # contours, and lossy ringing on the fat/muscle boundaries is exactly the
    # artefact a physicist would notice.
    img.save(out_img, format="WEBP", lossless=True, quality=100, method=6)
    print(f"wrote {out_img.relative_to(HERE)}  {out_img.stat().st_size:,} bytes")

    # ---- contours, in the cropped frame
    # Volume per voxel from the *measured* slice spacing, not SliceThickness —
    # this series reports thickness 3 mm on a 2 mm grid, and using 3 would inflate
    # every reported volume by half.
    sr, sc = (float(v) for v in d.PixelSpacing)
    spacing_z = (abs(float(series[1].ImagePositionPatient[2])
                     - float(series[0].ImagePositionPatient[2]))
                 if len(series) > 1 else float(d.SliceThickness))
    voxel_cc = sr * sc * spacing_z / 1000.0

    structures = []
    for r in results:
        if r["prompt"] in SKIP:
            continue
        spec = STRUCTURES[r["prompt"]]
        polys = []
        for c in r["contours"]:
            if c["z_index"] != z:
                continue
            pts = (to_pixels(c["points_lps"], d) - [left, top]) * scale
            pts = decimate(pts)
            if len(pts) < 6:
                continue
            polys.append(pts)
        if not polys:
            continue
        # A structure can be several closed loops on one slice — the colon and the
        # costal cartilages always are. Each keeps its own path and its own length
        # so the draw-on animation strokes them together.
        structures.append({
            "name": spec["name"],
            "prompt": r["prompt"],
            "token": spec["token"],
            "paths": [{"d": path_d(p), "len": path_length(p)} for p in polys],
            "vol": round(r["voxel_count"] * voxel_cc, 1),
            "type": spec.get("type", DEFAULT_DICOM_TYPE),
            "keywords": spec["keywords"],
            "side": spec["side"],
        })

    names = {s["name"] for s in structures}
    for chip in CHIPS:
        missing = [t for t in chip["targets"] if t not in names]
        if missing:
            raise SystemExit(f"chip {chip['label']!r} targets absent on this slice: {missing}")
    for phrase, targets in ALIASES.items():
        missing = [t for t in targets if t not in names]
        if missing:
            raise SystemExit(f"alias {phrase!r} targets absent on this slice: {missing}")

    slab_mm = round(spacing_z * len(series))
    out = {
        # Bumped when the shape changes, so hero.v3.js can refuse a file it does
        # not understand rather than rendering an approval sheet with holes in it.
        "schema": 3,
        "provenance": "model_output",
        "caption": (
            f"Real axial CT. {len(structures)} structures contoured by VoxTell from "
            f"the prompts above — volumes are over the {len(series)}-slice "
            f"({slab_mm} mm) public reference series, CC BY 4.0."
        ),
        "width": WIDTH,
        "height": HEIGHT,
        "image": "/assets/hero-slice.v2.webp",
        "window": {"centre": WINDOW_CENTRE, "width": WINDOW_WIDTH},
        # Millimetres per rendered pixel, so the page can draw a true scale bar
        # instead of a decorative one.
        "mm_per_px": round(sc / scale, 5),
        "slice_index": z,
        "slice_count": len(series),
        "slab_mm": slab_mm,
        "model": payload.get("model", "voxtell"),
        "attribution": ATTRIBUTION,
        "structures": structures,
        "chips": CHIPS,
        "aliases": ALIASES,
    }
    dest = ASSETS / "hero-contours.v3.json"
    raw = json.dumps(out, separators=(",", ":"))
    dest.write_text(raw)
    print(f"wrote {dest.relative_to(HERE)}  {len(raw):,} bytes")
    print(f"  provenance={out['provenance']}  structures={len(structures)}  "
          f"paths={sum(len(s['paths']) for s in structures)}  chips={len(CHIPS)}")
    print(f"  mm/px={out['mm_per_px']}  volumes: "
          + ", ".join(f"{s['name']} {s['vol']}cc" for s in structures[:4]) + " ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
