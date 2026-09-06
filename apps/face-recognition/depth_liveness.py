"""Optional depth evidence for liveness — Pro-only extension point.

A monocular depth head sees a printed photo or a phone screen as a PLANE: the
predicted relative depth inside the face box fits a single plane almost
perfectly, while a real face leaves a nose-shaped residual. That is the
`planarity` measurement this interface is shaped around.

This module is deliberately a stub: the depth model selection is a separate
work item, and `liveness_depth_enabled` defaults to false. `depth_flatness`
returning None is the contract for "no depth evidence available" — fusion
renormalises its weights over the terms that are present, so a None here costs
nothing and changes no verdict.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np

# Model handle installed by the app when a depth model is configured. Kept as a
# module global so the interface stays a plain function for the fusion code.
_MODEL = None


def set_model(model) -> None:
    """Install (or clear, with None) the depth model handle."""
    global _MODEL
    _MODEL = model


def available() -> bool:
    return _MODEL is not None


def depth_flatness(frame_rgb: np.ndarray,
                   bbox_xyxy: Sequence[float]) -> Optional[Dict[str, float]]:
    """Return ``{'planarity', 'score'}`` for the face box, or None.

    `planarity` in [0,1] is how well a single plane explains the face-region
    depth (1.0 = perfectly flat = photo/screen); `score` is the liveness-facing
    value, i.e. roughly ``1 - planarity``, so that higher is more live and it
    can be mixed with texture and motion without a sign flip.

    None means "no depth evidence" and is the only behaviour of this stub.
    """
    if _MODEL is None:
        return None
    return None
