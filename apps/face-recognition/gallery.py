"""On-device face gallery: one averaged 512-D template per person.

Cosine linear scan (face_rec_api ``src/vector_store.py`` :149-205) — with a
handful of enrolled people a brute-force dot product is well under the frame
budget, so there is no index to keep in sync.

★Why the model tag is a hard gate★ the ArcFace weights differ per backend, so
an embedding enrolled on one accelerator and one compared on another have
cosine ~0. Loading a gallery whose `_meta.model_tag` does not match the running
model would not fail loudly — it would silently recognise nobody. `load()`
therefore raises `GalleryMismatch` and refuses to populate.

File format v2::

    {"_meta": {"model_tag": str, "dim": int, "threshold": float, "updated": iso},
     "users": {"<name>": {"emb": [512 floats], "n": int, "ts": iso}}}
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

FORMAT_VERSION = 2
DEFAULT_DIR = "/userdata/local/face-gallery"


class GalleryMismatch(RuntimeError):
    """On-disk gallery was written by a different model or dimensionality."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_tag(model_tag: str) -> str:
    """`rv1126b:scrfd500m+mbf512@fp16` -> `rv1126b_scrfd500m_mbf512_fp16`."""
    out = str(model_tag)
    for ch in (":", "+", "@"):
        out = out.replace(ch, "_")
    return out


def default_path(model_tag: str) -> str:
    """Gallery file for `model_tag`; `FACE_GALLERY_DIR` overrides the directory."""
    base = os.environ.get("FACE_GALLERY_DIR") or DEFAULT_DIR
    return os.path.join(base, f"{sanitize_tag(model_tag)}.json")


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class Gallery:
    """Name -> averaged L2-normalized template, persisted as JSON.

    Thread-safe: the HTTP command server mutates it from its own thread while
    the frame loop reads it.
    """

    def __init__(self, path: Optional[str], model_tag: str,
                 dim: int = 512, threshold: float = 0.40) -> None:
        self.model_tag = str(model_tag)
        self.dim = int(dim)
        self.threshold = float(threshold)
        self.path = path or default_path(self.model_tag)
        self._lock = threading.RLock()
        self._users: Dict[str, dict] = {}
        self._matrix: Optional[np.ndarray] = None
        self._names: List[str] = []

    # -- persistence ---------------------------------------------------- #
    def load(self) -> "Gallery":
        """Read the gallery file. A missing file is an empty gallery, not an error."""
        with self._lock:
            if not os.path.exists(self.path):
                self._users = {}
                self._reindex()
                return self
            with open(self.path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            meta = blob.get("_meta") or {}
            tag = meta.get("model_tag")
            dim = int(meta.get("dim") or 0)
            if tag != self.model_tag or dim != self.dim:
                raise GalleryMismatch(
                    f"{self.path}: gallery is model_tag={tag!r} dim={dim}, "
                    f"running model is {self.model_tag!r} dim={self.dim}; "
                    f"embeddings are not comparable across models — enroll again "
                    f"or point FACE_GALLERY_DIR somewhere else")
            users: Dict[str, dict] = {}
            for name, rec in (blob.get("users") or {}).items():
                emb = np.asarray(rec.get("emb") or [], dtype=np.float32)
                if emb.size != self.dim:
                    raise GalleryMismatch(
                        f"{self.path}: user {name!r} has {emb.size}-D embedding, "
                        f"expected {self.dim}")
                users[str(name)] = {"emb": l2_normalize(emb),
                                    "n": int(rec.get("n") or 1),
                                    "ts": rec.get("ts") or _now()}
            self._users = users
            self._reindex()
            return self

    def save(self) -> str:
        """Atomic write: temp file in the same directory, then rename."""
        with self._lock:
            blob = {
                "_meta": {"model_tag": self.model_tag, "dim": self.dim,
                          "threshold": self.threshold, "updated": _now(),
                          "format": FORMAT_VERSION},
                "users": {name: {"emb": [round(float(v), 6) for v in rec["emb"]],
                                 "n": int(rec["n"]), "ts": rec["ts"]}
                          for name, rec in sorted(self._users.items())},
            }
            d = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".gallery-", suffix=".json", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(blob, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return self.path

    # -- mutation ------------------------------------------------------- #
    def enroll(self, name: str, embs: Sequence[np.ndarray]) -> dict:
        """Average `embs`, re-normalize, store under `name` (replacing any prior)."""
        name = str(name).strip()
        if not name:
            raise ValueError("enroll: empty name")
        mat = np.asarray([l2_normalize(e) for e in embs], dtype=np.float32)
        if mat.ndim != 2 or mat.shape[0] == 0:
            raise ValueError("enroll: no embeddings supplied")
        if mat.shape[1] != self.dim:
            raise ValueError(
                f"enroll: {mat.shape[1]}-D embeddings, gallery is {self.dim}-D")
        tpl = l2_normalize(mat.mean(axis=0))
        with self._lock:
            self._users[name] = {"emb": tpl, "n": int(mat.shape[0]), "ts": _now()}
            self._reindex()
            self.save()
        return {"name": name, "n": int(mat.shape[0])}

    def remove(self, name: str) -> bool:
        with self._lock:
            if str(name) not in self._users:
                return False
            del self._users[str(name)]
            self._reindex()
            self.save()
            return True

    def list(self) -> List[dict]:
        with self._lock:
            return [{"name": n, "n": int(r["n"]), "ts": r["ts"]}
                    for n, r in sorted(self._users.items())]

    def __len__(self) -> int:
        with self._lock:
            return len(self._users)

    # -- query ---------------------------------------------------------- #
    def _reindex(self) -> None:
        self._names = sorted(self._users)
        self._matrix = (np.stack([self._users[n]["emb"] for n in self._names])
                        if self._names else None)

    def match(self, emb: np.ndarray) -> Tuple[Optional[str], float, List[dict]]:
        """Cosine linear scan.

        Returns ``(name_or_None, best_score, candidates)``. `name` is None when
        the gallery is empty or the best score is below `threshold`; `score` is
        still the best cosine observed so a caller can log the near-miss.
        `candidates` is score-descending ``[{"name", "score"}]``.
        """
        q = l2_normalize(emb)
        with self._lock:
            mat = self._matrix
            names = list(self._names)
        if mat is None or q.size != self.dim:
            return None, 0.0, []
        sims = mat @ q
        order = np.argsort(sims)[::-1]
        candidates = [{"name": names[i], "score": float(sims[i])} for i in order]
        best = candidates[0]
        if best["score"] >= self.threshold:
            return best["name"], best["score"], candidates
        return None, best["score"], candidates
