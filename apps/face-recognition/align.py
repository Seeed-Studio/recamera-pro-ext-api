"""ArcFace 5-point alignment (Umeyama similarity transform), numpy + cv2.

Verbatim port of face_rec_api ``src/face_pipeline.py`` :27-36 (template) and
:84-142 (``umeyama_similarity``). The float32 discipline in ``umeyama`` is
load-bearing and is documented at its call site there: upcasting to float64
shifts warped pixels by 1 LSB, which a quantized embedder turns into a ~0.0025
cosine drift against already-enrolled vectors.
"""
from __future__ import annotations

from typing import Sequence, Tuple

import cv2
import numpy as np

# Canonical ArcFace landmark positions for a 112x112 aligned face.
ARCFACE_DST = np.array(
    [
        [38.2946, 51.6963],  # left eye
        [73.5318, 51.5014],  # right eye
        [56.0252, 71.7366],  # nose
        [41.5493, 92.3655],  # left mouth corner
        [70.7299, 92.2041],  # right mouth corner
    ],
    dtype=np.float32,
)

ARCFACE_SIZE = 112


def umeyama(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (Umeyama 1991), pure numpy.

    Returns the ``(dim+1, dim+1)`` homogeneous matrix, matching
    ``skimage.transform.SimilarityTransform.estimate``. Degenerate input
    (rank-0 covariance) yields NaNs, same as skimage.

    NOTE: intermediate math runs in the INPUT dtype — float32 landmarks stay
    float32. Do not "improve" this by upcasting; see the module docstring.
    """
    src = np.asarray(src)
    dst = np.asarray(dst)
    num, dim = src.shape

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_demean = src - src_mean
    dst_demean = dst - dst_mean

    A = dst_demean.T @ src_demean / num

    d = np.ones((dim,), dtype=np.float64)
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1

    T = np.eye(dim + 1, dtype=np.float64)
    U, S, V = np.linalg.svd(A)            # V is V^T
    rank = np.linalg.matrix_rank(A)
    if rank == 0:
        return np.full_like(T, np.nan)
    if rank == dim - 1:
        if np.linalg.det(U) * np.linalg.det(V) > 0:
            T[:dim, :dim] = U @ V
        else:
            s = d[dim - 1]
            d[dim - 1] = -1
            T[:dim, :dim] = U @ np.diag(d) @ V
            d[dim - 1] = s
    else:
        T[:dim, :dim] = U @ np.diag(d) @ V

    scale = 1.0 / src_demean.var(axis=0).sum() * (S @ d)
    T[:dim, dim] = dst_mean - scale * (T[:dim, :dim] @ src_mean)
    T[:dim, :dim] *= scale
    return T


def align_face(frame_rgb: np.ndarray,
               kps5: Sequence[Tuple[float, float]],
               out_size: int = ARCFACE_SIZE) -> np.ndarray:
    """Warp the face at `kps5` into the canonical 112x112 ArcFace pose.

    `frame_rgb` is the ORIGINAL-resolution HWC uint8 RGB frame and `kps5` the
    five landmarks in original pixels. Returns a ``(out_size, out_size, 3)``
    uint8 RGB crop, ready to hand to the embedder (the (x-127.5)/127.5
    normalization is baked into the rknn graph, so no scaling here).
    """
    src = np.asarray(kps5, dtype=np.float32).reshape(5, 2)
    dst = ARCFACE_DST
    if out_size != ARCFACE_SIZE:
        dst = dst * (float(out_size) / ARCFACE_SIZE)
    M = umeyama(src, dst)[0:2, :]
    warped = cv2.warpAffine(np.asarray(frame_rgb), M, (out_size, out_size),
                            borderValue=0.0)
    return np.ascontiguousarray(warped.astype(np.uint8))
