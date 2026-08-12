"""Shared, torch-free primitives used by both the API and the GPU worker.

Nothing in this package may import torch, nnunetv2 or voxtell at module scope —
the API image has none of them installed.
"""

__all__ = ["geometry", "contours", "wire"]
