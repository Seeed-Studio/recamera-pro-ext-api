#!/usr/bin/env python3
"""
face-recognition -- 1:N on-device face identification with optional anti-spoofing.

`run()` owns the loop and the whole cascade reads top to bottom as ordinary
Python (internal/KIT_APP_SHAPE_SPEC.md §1/§3):

  frame -> self.pre()            RGA letterbox to 640 (manifest models[0].input)
        -> self.models.scrfd     SCRFD-500M raw head, 9 tensors
        -> scrfd.decode          boxes + 5 landmarks in ORIGINAL pixels
        -> results[:max_faces]   ★business★ top-K by score
        -> self.tracker.update   ★business★ stable identity per face
        -> for each tracked face:
             gate on min_face_px       too small to embed -> gated, no inference
             align.align_face          5-point similarity warp to 112x112
             self.models.arcface       512-D embedding, L2-normalized here
             gallery.match             cosine linear scan over enrolled people
             (optional) liveness       MiniFAS 2.7_80x80, P(real) < threshold
                                       -> spoof: no name, no vote
             per-track evidence        accumulate cosine weight per candidate
        -> self.emit()           results[] (box/label/score) + extra.faces[]

`model_frame = "hw"` (not "hw-roi"): the alignment warp needs the ORIGINAL
camera pixels — it is a similarity transform driven by the five landmarks, not
an axis-aligned crop, so `crop_roi_hw` cannot produce it. "hw" keeps full-res
RGB in `frame.data` while the detector still gets its letterbox off RGA.

★Accuracy★ four things this app deliberately does NOT do naively:

  * **Vote, not single frame.** A track accumulates a cosine weight per
    candidate name across its embedding frames and reports the argmax. One bad
    frame (motion blur, a yawn) cannot flip an identity that ten frames agree
    on. `evidence_decay < 1.0` makes the vote forget, for a camera where one
    person genuinely walks out of a box another walks into.
  * **Identity, not slot.** Evidence is keyed by `track_id` from
    `kit.logic.tracker`, never by the index in the score-sorted detection list,
    which reorders the moment two people's scores cross.
  * **Gate before embed.** A face whose shorter side is under `min_face_px` is
    mostly upsampling artefact by the time it reaches the 112 embedder, and a
    garbage embedding does not fail — it matches *somebody*. Gated faces still
    appear in `results[]` with `gated: true` and no name.
  * **Spoof suppresses the vote, it does not just annotate it.** A face that
    fails liveness contributes nothing to the track's evidence, so holding a
    phone up cannot slowly accumulate its way to a positive identification.

★NPU budget★ one core, serialized. At most `max_faces` embeddings run in a
frame and each track re-embeds only every `embed_interval` frames, so the
per-frame cost is bounded and the tracks are naturally staggered across frames.
Between two embeddings a track keeps reporting its last verdict.

★Gallery is model-tagged★ `model_tag = "rv1126b:scrfd500m+mbf512@fp16"` is
written into the gallery file and checked on load. Embeddings from a different
ArcFace have cosine ~0 against these, and the failure mode is silent (nobody
matches), so `gallery.load()` refuses rather than degrades.

Enrollment is an HTTP command channel on 127.0.0.1:<cmd_port> (see
cmd_server.py). Both sources run INSIDE this loop, never on the HTTP thread:
the NPU is single-core, and "camera" needs frames only the loop has.

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/face-recognition \\
        --model models/scrfd500m_640_fp16.rknn --sink ws --port 8124
"""
from __future__ import annotations

import base64
import queue
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from kit.app import App, run_app
from kit.logic.tracker import Tracker, TrackerConfig

import align
import gallery as gallery_mod
import liveness as liveness_mod
import scrfd
from cmd_server import CmdServer

MODEL_TAG = "rv1126b:scrfd500m+mbf512@fp16"
SCRFD_ID = "scrfd"
ARCFACE_ID = "arcface"
LIVENESS_ID = "liveness"
DET_SIZE = 640
EMBED_DIM = 512
UNKNOWN = "unknown"


@dataclass
class TrackState:
    """One tracked face's accumulated identity evidence."""
    votes: Dict[str, float] = field(default_factory=dict)
    last_cos: Dict[str, float] = field(default_factory=dict)
    samples: int = 0                  # embeddings folded in (spoof frames excluded)
    last_embed: int = -(10 ** 9)      # frame index of the last embedding attempt
    name: Optional[str] = None
    score: float = 0.0
    live: Optional[bool] = None
    liveness_score: Optional[float] = None
    reason: Optional[str] = None

    def add(self, name: Optional[str], cos: float, decay: float) -> None:
        if decay != 1.0:
            for k in list(self.votes):
                self.votes[k] *= decay
        self.samples += 1
        if name is not None:
            self.votes[name] = self.votes.get(name, 0.0) + max(0.0, float(cos))
            self.last_cos[name] = float(cos)

    def verdict(self, min_frames: int):
        """(name, score, stable) from the ACCUMULATED evidence."""
        stable = self.samples >= max(1, int(min_frames))
        if not self.votes:
            return None, 0.0, stable
        name = max(self.votes.items(), key=lambda kv: kv[1])[0]
        return name, float(self.last_cos.get(name, 0.0)), stable


class FaceRecognitionApp(App):
    id = "face-recognition"
    name = "Face Recognition"
    owns_loop = True
    model_frame = "hw"          # alignment needs ORIGINAL pixels; see module doc
    input_size = DET_SIZE
    class_names = ("face",)

    # config_schema-backed knobs (defaults mirror manifest.json)
    confidence: float = 0.5
    max_faces: int = 5
    min_face_px: int = 64
    match_threshold: float = 0.40
    embed_interval: int = 5
    min_track_frames: int = 3
    evidence_decay: float = 1.0
    track_max_lost: int = 15
    enroll_frames: int = 5
    liveness_enabled: bool = False
    liveness_threshold: float = 0.5
    cmd_port: int = 8125
    cmd_host: str = "127.0.0.1"

    def __init__(self) -> None:
        super().__init__()
        self._frame_idx = 0
        self._states: Dict[int, TrackState] = {}
        self._tracker: Optional[Tracker] = None
        self.gallery: Optional[gallery_mod.Gallery] = None
        self.gallery_error: Optional[str] = None
        self._jobs: "queue.Queue" = queue.Queue()
        self._enroll: Optional[dict] = None      # in-flight camera enrollment
        self._server: Optional[CmdServer] = None

    # -- setup ---------------------------------------------------------- #
    def setup(self, config) -> None:
        super().setup(config)
        c = self.config or {}
        # Read explicitly rather than leaning on the auto-bind: `iou` already
        # exists on the base class with a different default, and every test
        # drives start() with a partial config.
        self.confidence = float(c.get("confidence", self.confidence))
        self.iou = float(c.get("iou", 0.4))
        self.max_faces = int(c.get("max_faces", self.max_faces))
        self.min_face_px = int(c.get("min_face_px", self.min_face_px))
        self.match_threshold = float(c.get("match_threshold", self.match_threshold))
        self.embed_interval = max(1, int(c.get("embed_interval", self.embed_interval)))
        self.min_track_frames = max(1, int(c.get("min_track_frames",
                                                 self.min_track_frames)))
        self.evidence_decay = float(c.get("evidence_decay", self.evidence_decay))
        self.track_max_lost = int(c.get("track_max_lost", self.track_max_lost))
        self.enroll_frames = max(1, int(c.get("enroll_frames", self.enroll_frames)))
        self.liveness_enabled = bool(c.get("liveness_enabled", self.liveness_enabled))
        self.liveness_threshold = float(c.get("liveness_threshold",
                                              self.liveness_threshold))
        self.cmd_port = int(c.get("cmd_port", self.cmd_port))
        self.cmd_host = str(c.get("cmd_host", self.cmd_host))

        self._tracker = Tracker(TrackerConfig(
            max_lost_frames_center=self.track_max_lost,
            max_lost_frames_edge=min(self.track_max_lost, 15),
        ))
        self._states = {}
        self._frame_idx = 0

        self.gallery = gallery_mod.Gallery(
            c.get("gallery_path"), MODEL_TAG, dim=EMBED_DIM,
            threshold=self.match_threshold)
        self._load_gallery()

        # cmd_port <= 0 disables the endpoint (tests, or a locked-down install).
        # -1 rather than 0 because 0 is a valid socket request for "any free port".
        self._server = CmdServer(self.cmd_port if self.cmd_port > 0 else -1,
                                 self, host=self.cmd_host).start()
        if self.cmd_port > 0:
            print(f"[face-recognition] cmd endpoint http://{self.cmd_host}:"
                  f"{self._server.port}/cmd  gallery={self.gallery.path} "
                  f"users={len(self.gallery)}", flush=True)

    def _load_gallery(self) -> int:
        """Load the gallery, surviving a mismatch as an EMPTY gallery + a flag."""
        try:
            self.gallery.load()
            self.gallery_error = None
        except gallery_mod.GalleryMismatch as e:
            # Do not crash the loop: recognition simply reports nobody, and the
            # reason rides out on every frame so it is visible in the stream.
            self.gallery_error = str(e)
            print(f"[face-recognition] gallery NOT loaded: {e}", flush=True)
        except Exception as e:                  # noqa: BLE001
            self.gallery_error = str(e)
            print(f"[face-recognition] gallery load failed: {e}", flush=True)
        return len(self.gallery)

    def finish(self) -> None:
        if self._server is not None:
            try:
                self._server.stop()
            finally:
                self._server = None
        super().finish()

    # -- CmdServer ops interface ---------------------------------------- #
    @property
    def model_tag(self) -> str:
        return MODEL_TAG

    def user_count(self) -> int:
        return len(self.gallery) if self.gallery else 0

    def list_users(self) -> List[dict]:
        return self.gallery.list() if self.gallery else []

    def remove_user(self, name: str) -> bool:
        return bool(self.gallery and self.gallery.remove(name))

    def reload(self) -> int:
        n = self._load_gallery()
        self._states = {}
        return n

    def submit_enroll(self, job: dict) -> Future:
        fut: Future = Future()
        self._jobs.put((job, fut))
        return fut

    # -- inference helpers ---------------------------------------------- #
    def _embed(self, frame_rgb: np.ndarray, kps5) -> np.ndarray:
        chip = align.align_face(frame_rgb, kps5)
        out = self.models[ARCFACE_ID].infer(chip)
        vec = np.asarray(out[0] if isinstance(out, (list, tuple)) else out,
                         dtype=np.float32).reshape(-1)
        return gallery_mod.l2_normalize(vec)

    def _liveness(self, frame_rgb: np.ndarray, box) -> float:
        x1, y1, x2, y2 = box
        bgr = np.ascontiguousarray(np.asarray(frame_rgb)[..., ::-1])
        crop = liveness_mod.crop_minifas(bgr, (x1, y1, x2 - x1, y2 - y1))
        return liveness_mod.real_probability(self.models[LIVENESS_ID].infer(crop))

    def _detect(self, model_input, info) -> List[dict]:
        outs = self.models[SCRFD_ID].infer(model_input)
        dets = scrfd.decode(outs, info, conf_thres=self.confidence,
                            iou_thres=self.iou, model_size=DET_SIZE)
        return dets

    # -- enrollment ------------------------------------------------------ #
    def _pick_enroll_face(self, faces: List[dict], w: int, h: int) -> Optional[dict]:
        """The biggest, most centred face — the one being deliberately presented.

        Area alone picks whoever walked closest to the lens at the edge of the
        frame; the centre term breaks that tie toward the person standing in
        front of the camera to enroll.
        """
        best, best_key = None, -1.0
        cx0, cy0 = w / 2.0, h / 2.0
        diag = max(1.0, (w ** 2 + h ** 2) ** 0.5)
        for f in faces:
            x1, y1, x2, y2 = f["box"]
            area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            if area <= 0:
                continue
            d = (((x1 + x2) / 2 - cx0) ** 2 + ((y1 + y2) / 2 - cy0) ** 2) ** 0.5
            key = area * (1.0 - 0.5 * min(1.0, d / diag))
            if key > best_key:
                best, best_key = f, key
        return best

    def _pump_jobs(self) -> None:
        """Adopt the next queued enrollment if none is in flight."""
        while self._enroll is None:
            try:
                job, fut = self._jobs.get_nowait()
            except queue.Empty:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            if job.get("source") == "image":
                self._enroll_from_image(job, fut)
                continue
            n = int(job.get("frames") or 0) or int(self.enroll_frames)
            self._enroll = {"name": job["name"], "need": max(1, n),
                            "embs": [], "fut": fut}
            print(f"[face-recognition] enrolling {job['name']!r} from camera, "
                  f"{max(1, n)} frames", flush=True)

    def _enroll_from_image(self, job: dict, fut: Future) -> None:
        """One-shot enrollment from a base64 still, on the SAME detect path."""
        try:
            import cv2
            raw = base64.b64decode(job["image_b64"], validate=False)
            bgr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise ValueError("could not decode image_b64 as an image")
            rgb = np.ascontiguousarray(bgr[..., ::-1])
            from kit.runtime.preprocess import letterbox
            padded, info = letterbox(rgb, DET_SIZE)
            dets = self._detect(padded, info)
            if len(dets) != 1:
                raise ValueError(
                    f"enroll from image needs exactly 1 face, found {len(dets)}")
            emb = self._embed(rgb, dets[0]["kps"])
            res = self.gallery.enroll(job["name"], [emb])
            fut.set_result({"source": "image", "frames": 1, **res})
        except Exception as e:                  # noqa: BLE001
            fut.set_exception(e)

    def _step_enroll(self, frame_rgb, faces: List[dict], w: int, h: int) -> None:
        """Collect one embedding per frame for the in-flight camera enrollment.

        Deliberately NOT subject to `embed_interval`: an operator is standing in
        front of the camera waiting, and the HTTP request has a 15 s budget.
        """
        job = self._enroll
        if job is None:
            return
        fut: Future = job["fut"]
        if fut.cancelled():
            self._enroll = None
            return
        face = self._pick_enroll_face(faces, w, h)
        if face is None:
            return
        try:
            job["embs"].append(self._embed(frame_rgb, face["kps"]))
        except Exception as e:                  # noqa: BLE001
            self._enroll = None
            fut.set_exception(e)
            return
        if len(job["embs"]) >= job["need"]:
            self._enroll = None
            try:
                res = self.gallery.enroll(job["name"], job["embs"])
                fut.set_result({"source": "camera",
                                "frames": len(job["embs"]), **res})
            except Exception as e:              # noqa: BLE001
                fut.set_exception(e)

    # -- main loop -------------------------------------------------------- #
    def run(self):
        for frame in self.frames():
            self._frame_idx += 1
            t = frame.pts
            rgb = frame.data

            x = self.pre(frame)
            dets = self._detect(x.data, x.info)
            faces = dets[: max(1, int(self.max_faces))]

            tracks = self._tracker.update(faces, t, frame.w, frame.h)
            for tid in self._tracker.removed_ids:
                self._states.pop(tid, None)
            by_det = {tr.det_index: tr for tr in tracks if tr.det_index >= 0}

            self._pump_jobs()
            self._step_enroll(rgb, faces, frame.w, frame.h)

            results: List[dict] = []
            face_rows: List[dict] = []
            fw = float(frame.w or 1)
            fh = float(frame.h or 1)

            for i, d in enumerate(faces):
                x1, y1, x2, y2 = d["box"]
                tr = by_det.get(i)
                tid = tr.track_id if tr is not None else None
                gated = tr is None or min(x2 - x1, y2 - y1) < float(self.min_face_px)

                st = self._states.get(tid) if tid is not None else None
                if tid is not None and st is None:
                    st = self._states.setdefault(tid, TrackState())

                if not gated and st is not None:
                    due = (st.samples == 0
                           or (self._frame_idx - st.last_embed) >= self.embed_interval)
                    if due:
                        self._embed_track(st, rgb, d)

                if st is not None and not gated:
                    name, score, stable = st.verdict(self.min_track_frames)
                    st.name, st.score = name, score
                else:
                    name, score, stable = None, 0.0, False

                results.append({
                    "box": [float(x1), float(y1), float(x2), float(y2)],
                    "label": name or UNKNOWN,
                    "score": float(score),
                    "cls": 0,
                })
                face_rows.append({
                    "track_id": tid,
                    "bbox": [float(x1) / fw, float(y1) / fh,
                             float(x2) / fw, float(y2) / fh],
                    "det_score": float(d["score"]),
                    "name": name,
                    "score": float(score),
                    "live": (st.live if st is not None else None),
                    "liveness_score": (st.liveness_score if st is not None else None),
                    "stable": bool(stable),
                    "gated": bool(gated),
                    "reason": (st.reason if st is not None else None),
                })

            extra = {"model_tag": MODEL_TAG, "faces": face_rows,
                     "enrolled": self.user_count()}
            if self.gallery_error:
                extra["gallery_error"] = self.gallery_error
            self.emit([], t, results=results, extra=extra)

    def _embed_track(self, st: TrackState, frame_rgb, det: dict) -> None:
        """One embedding + optional liveness for `det`, folded into `st`."""
        st.last_embed = self._frame_idx
        st.reason = None
        if self.liveness_enabled:
            try:
                p = self._liveness(frame_rgb, det["box"])
            except Exception as e:              # noqa: BLE001
                print(f"[face-recognition] liveness failed: {e}", flush=True)
                p = None
            st.liveness_score = p
            st.live = None if p is None else bool(p >= self.liveness_threshold)
            if st.live is False:
                # ★A spoof contributes NO evidence★ -- annotating it while still
                # voting would let a phone screen accumulate an identity.
                st.reason = "spoof"
                st.votes.clear()
                st.last_cos.clear()
                return
        try:
            emb = self._embed(frame_rgb, det["kps"])
        except Exception:                       # noqa: BLE001
            traceback.print_exc()
            return
        name, cos, _cands = self.gallery.match(emb)
        st.add(name, cos, self.evidence_decay)


if __name__ == "__main__":
    run_app(FaceRecognitionApp())
