#!/usr/bin/env python3
"""Populate the model store so the GPU pod needs no Hugging Face egress.

Run once on the host (not in the pod), pointing at whatever directory the
worker mounts at /models:

    python scripts/fetch_models.py --dest /home/tavulha/voxtell/models

Fetches three things, ~12 GB total:

  voxtell_v1.1/            the segmentation checkpoint + plans.json
  text_embeddings.npz      the published prompt-embedding bank -- with this in
                           place, known prompts never touch the text backbone
  hf/                      Hugging Face cache holding Qwen3-Embedding-4B, for
                           the free-text prompts that do

Idempotent: everything is content-addressed and skipped when already present.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

MODEL_REPO = "mrokuss/VoxTell"
MODEL_NAME = "voxtell_v1.1"
TEXT_MODEL = "Qwen/Qwen3-Embedding-4B"


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


def tree_size(path: Path) -> int:
    # Skip symlinks: the Hugging Face cache stores each file once under blobs/ and
    # symlinks it into snapshots/, so following links would double every byte.
    return sum(
        f.stat().st_size
        for f in path.rglob("*")
        if f.is_file() and not f.is_symlink()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True, help="Model store directory (mounted at /models)")
    parser.add_argument(
        "--skip-text-model",
        action="store_true",
        help="Only fetch the checkpoint and the embedding bank (~2 GB). Free-text "
             "prompts outside the bank will then fail in an offline pod.",
    )
    args = parser.parse_args()

    from huggingface_hub import hf_hub_download, snapshot_download

    dest = Path(args.dest).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    hf_cache = dest / "hf"
    hf_cache.mkdir(exist_ok=True)

    print(f"model store: {dest}")

    # 1. Segmentation model. snapshot_download into the store itself so the
    #    worker can point INFER_MODEL_DIR at a stable path instead of digging
    #    through the hash-named cache layout.
    model_dir = dest / MODEL_NAME
    if (model_dir / "plans.json").exists() and (model_dir / "fold_0").exists():
        print(f"  {MODEL_NAME}: already present ({human(tree_size(model_dir))})")
    else:
        print(f"  {MODEL_NAME}: downloading ...")
        local = snapshot_download(
            repo_id=MODEL_REPO, allow_patterns=[f"{MODEL_NAME}/*"], local_dir=str(dest)
        )
        got = Path(local) / MODEL_NAME
        if got != model_dir and got.exists():
            shutil.move(str(got), str(model_dir))
        print(f"  {MODEL_NAME}: {human(tree_size(model_dir))}")

    if not (model_dir / "fold_0" / "checkpoint_final.pth").exists():
        print(
            f"ERROR: {model_dir}/fold_0/checkpoint_final.pth is missing — the "
            "worker will not start.",
            file=sys.stderr,
        )
        return 1

    # 2. Published embedding bank.
    bank = dest / "text_embeddings.npz"
    if bank.exists():
        print(f"  text_embeddings.npz: already present ({human(bank.stat().st_size)})")
    else:
        print("  text_embeddings.npz: downloading ...")
        # Path duplicated from voxtell.utils.embedding_bank.hf_embedding_path so
        # this script runs on the host with only huggingface_hub installed.
        path = hf_hub_download(
            repo_id=MODEL_REPO, filename=f"embeddings/{MODEL_NAME}/text_embeddings.npz"
        )
        shutil.copyfile(path, bank)
        print(f"  text_embeddings.npz: {human(bank.stat().st_size)}")

    # 3. Text backbone, into the HF cache the pod mounts as HF_HOME.
    #
    # cache_dir is passed explicitly rather than setting HF_HOME: huggingface_hub
    # reads that variable once, at import, so assigning it here would be too late
    # and the download would silently land in ~/.cache/huggingface instead. The
    # pod sets HF_HOME=/models/hf, and transformers looks under <HF_HOME>/hub, so
    # the target is the hub subdirectory.
    hub_cache = hf_cache / "hub"
    if args.skip_text_model:
        print("  text backbone: skipped (--skip-text-model)")
    else:
        print(f"  {TEXT_MODEL}: downloading (~8 GB, this takes a while) ...")
        hub_cache.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=TEXT_MODEL,
            allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
            cache_dir=str(hub_cache),
        )
        print(f"  {TEXT_MODEL}: cache now {human(tree_size(hf_cache))}")

    weights = list(hub_cache.rglob("*.safetensors")) if hub_cache.exists() else []
    if not args.skip_text_model and not weights:
        print(
            f"ERROR: no .safetensors under {hub_cache} — the text backbone did not "
            "land in the store, and free-text prompts will fail in an offline pod.",
            file=sys.stderr,
        )
        return 1

    print(f"\ndone. Total store: {human(tree_size(dest))}")
    print("Mount this directory read-only at /models in the worker Deployment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
