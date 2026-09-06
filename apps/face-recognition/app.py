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
             (optional) liveness       MiniFAS 2.7x + 4.0x texture ensemble,
                                       five-point motion, sampled FaceMesh
                                       blink -> fused per-track verdict; a
                                       spoof gets no name and no vote, and a
                                       PENDING verdict gets no name either
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
import json
import os
import queue
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from kit import config as kit_cfg
from kit.app import App, run_app
from kit.logic import drowsiness
from kit.logic.tracker import Tracker, TrackerConfig
from kit.runtime.postprocess import landmark as landmark_post

import align
import depth_liveness
import gallery as gallery_mod
import liveness as liveness_mod
import liveness_temporal as lt
import scrfd
from cmd_server import CmdServer

MODEL_TAG = "rv1126b:scrfd500m+mbf512@fp16"
SCRFD_ID = "scrfd"
ARCFACE_ID = "arcface"
LIVENESS_ID = "liveness"
LIVENESS_V1SE_ID = "liveness_v1se"
FACEMESH_ID = "facemesh"
DEPTH_ID = "depth"
DEPTH_INPUT = 256           # MiDaS v2.1 small; matches manifest models[depth].input
DET_SIZE = 640
FACEMESH_SIZE = 192
FACEMESH_PAD = 0.25
EMBED_DIM = 512
UNKNOWN = "unknown"
CAPTURE_FILE = "liveness_capture.jsonl"
CAPTURE_LABELS = ("real", "print", "screen")


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
    # -- liveness v2 ---------------------------------------------------- #
    lv: lt.LivenessState = field(default_factory=lt.LivenessState)
    first_seen: Optional[float] = None   # frame pts of this track's first sample
    last_texture: int = -(10 ** 9)       # frame index of the last texture run
    liveness: Optional[dict] = None      # last fused verdict object

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
    liveness_capture_max_sec: int = 60
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
        self._lv_cfg = lt.LivenessConfig()
        self._capture: Optional[dict] = None     # in-flight calibration capture
        self._capture_lock = threading.Lock()
        self._capture_dir: Optional[str] = None  # overridden in tests

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
        self.liveness_capture_max_sec = int(c.get("liveness_capture_max_sec",
                                                  self.liveness_capture_max_sec))
        self._lv_cfg = lt.LivenessConfig.from_config(c)
        if self._lv_cfg.depth_enabled:
            try:
                dm = self._model(DEPTH_ID)
                dm.input_size = DEPTH_INPUT
                depth_liveness.set_model(dm)
            except Exception as e:              # noqa: BLE001
                print(f"[face-recognition] depth model unavailable, depth evidence off: {e}",
                      flush=True)
                self._lv_cfg.depth_enabled = False
        else:
            depth_liveness.set_model(None)
        # `_rt` (which carries start()'s app_dir) is only populated AFTER
        # setup() returns, so resolve the install dir the same way the kit does.
        if self._capture_dir is None:
            self._capture_dir = kit_cfg.app_dir_of(self)
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
        self._stop_capture()
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

    # -- calibration capture --------------------------------------------- #
    @property
    def capture_path(self) -> str:
        return os.path.join(self._capture_dir or os.getcwd(), CAPTURE_FILE)

    def start_liveness_capture(self, label: str, seconds: float) -> dict:
        """Arm a bounded, append-only feature capture. Returns the ack skeleton.

        ★Features, not frames★ the rows carry the model outputs and the geometry
        the fusion actually consumes — never pixels, never an embedding, never a
        name. A calibration set therefore contains nothing that identifies the
        people who helped record it, which is the only way collecting one on a
        deployed camera is defensible.

        The returned dict carries a `future` that the command handler waits on;
        the loop resolves it at the deadline with the written-row count, so the
        caller gets the real number rather than a zero taken at arm time.
        """
        lab = str(label or "").strip().lower()
        if lab not in CAPTURE_LABELS:
            raise ValueError(f"label must be one of {list(CAPTURE_LABELS)}")
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("seconds must be a number")
        hi = max(1.0, float(self.liveness_capture_max_sec))
        if not (0 < secs <= hi):
            raise ValueError(f"seconds must be in (0, {hi:g}]")
        with self._capture_lock:
            if self._capture is not None:
                raise ValueError("a liveness capture is already running")
            path = self.capture_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fh = open(path, "a", encoding="utf-8")
            fut: Future = Future()
            fut.set_running_or_notify_cancel()
            self._capture = {"label": lab, "seconds": secs, "path": path,
                             "fh": fh, "rows": 0, "fut": fut,
                             "deadline": time.monotonic() + secs,
                             "flushed": time.monotonic()}
        print(f"[face-recognition] liveness capture {lab!r} for {secs:g}s -> "
              f"{path}", flush=True)
        return {"label": lab, "seconds": secs, "path": path, "future": fut}

    def _capture_row(self, state: TrackState, track_id, ts: float,
                     face_px: float, p_v2, p_v1se, ear, blink: bool) -> None:
        cap = self._capture
        if cap is None:
            return
        row = {
            "P_tex_v2": (None if p_v2 is None else float(p_v2)),
            "P_tex_v1se": (None if p_v1se is None else float(p_v1se)),
            "motion_residual": (None if state.lv.motion_residual is None
                                else float(state.lv.motion_residual)),
            "correlation": (None if state.lv.correlation is None
                            else float(state.lv.correlation)),
            "EAR": (None if ear is None else float(ear)),
            "blink": bool(blink),
            "face_px_size": float(face_px),
            "track_id": (None if track_id is None else int(track_id)),
            "label": cap["label"],
            "ts": float(ts),
        }
        try:
            cap["fh"].write(json.dumps(row, ensure_ascii=False) + "\n")
            cap["rows"] += 1
        except Exception as e:                  # noqa: BLE001
            print(f"[face-recognition] capture write failed: {e}", flush=True)

    def _tick_capture(self) -> None:
        """Flush at least once a second; close and ack at the deadline."""
        cap = self._capture
        if cap is None:
            return
        now = time.monotonic()
        if now >= cap["deadline"]:
            self._stop_capture()
            return
        if now - cap["flushed"] >= 1.0:
            try:
                cap["fh"].flush()
            except Exception:                   # noqa: BLE001
                pass
            cap["flushed"] = now

    def _stop_capture(self) -> None:
        with self._capture_lock:
            cap, self._capture = self._capture, None
        if cap is None:
            return
        try:
            cap["fh"].flush()
            cap["fh"].close()
        except Exception:                       # noqa: BLE001
            pass
        print(f"[face-recognition] liveness capture done: {cap['rows']} rows -> "
              f"{cap['path']}", flush=True)
        fut: Future = cap["fut"]
        if not fut.done():
            fut.set_result({"label": cap["label"], "seconds": cap["seconds"],
                            "path": cap["path"], "rows": int(cap["rows"])})

    # -- inference helpers ---------------------------------------------- #
    def _model(self, model_id: str):
        """A manifest model handle, or None when the install lacks that file."""
        try:
            return self.models[model_id]
        except Exception:                       # noqa: BLE001
            return None
    def _embed(self, frame_rgb: np.ndarray, kps5) -> np.ndarray:
        chip = align.align_face(frame_rgb, kps5)
        out = self.models[ARCFACE_ID].infer(chip)
        vec = np.asarray(out[0] if isinstance(out, (list, tuple)) else out,
                         dtype=np.float32).reshape(-1)
        return gallery_mod.l2_normalize(vec)

    # -- liveness stage timing (printed every 30 frames; cheap monotonic reads) --
    def _lt_add(self, stage: str, t0: float) -> None:
        acc = self.__dict__.setdefault("_lt_acc", {})
        acc[stage] = acc.get(stage, 0.0) + (time.monotonic() - t0) * 1000.0
        cnt = self.__dict__.setdefault("_lt_cnt", {})
        cnt[stage] = cnt.get(stage, 0) + 1

    def _lt_report(self) -> None:
        acc = self.__dict__.get("_lt_acc") or {}
        if not acc or self._frame_idx % 30:
            return
        cnt = self.__dict__.get("_lt_cnt") or {}
        parts = ["%s=%.1fms/%d" % (k, v, cnt.get(k, 0)) for k, v in sorted(acc.items())]
        print("[face-recognition] liveness timing (30 frames): " + " ".join(parts), flush=True)
        acc.clear(); cnt.clear()

    def _facemesh_ear(self, frame, box) -> Optional[float]:
        """One FaceMesh pass on a 192 ROI -> average EAR, or None.

        `crop_roi_hw` is the padded-square ROI contract the landmark decoder's
        `roi_map` is defined against, so the 468 points come back in ORIGINAL
        frame pixels and the 6-point EAR is scale-free.
        """
        model = self._model(FACEMESH_ID)
        if model is None:
            return None
        t0 = time.monotonic()
        roi, roi_map = self.crop_roi_hw(frame, box, FACEMESH_SIZE, FACEMESH_PAD)
        self._lt_add("mesh_crop", t0)
        t0 = time.monotonic()
        out = model.infer(roi)
        self._lt_add("mesh_infer", t0)
        t0 = time.monotonic()
        lm, presence = landmark_post.decode(out, roi_map, FACEMESH_SIZE)
        if presence < 0.5:
            self._lt_add("mesh_post", t0)
            return None
        m = drowsiness.compute_metrics(
            lm, ear_threshold=float(self._lv_cfg.ear_threshold))
        self._lt_add("mesh_post", t0)
        return float(m.avg_ear) if m.valid else None

    def _sample_liveness(self, frame, det: dict, state: TrackState,
                         track_id) -> dict:
        """Advance one track's liveness evidence for this frame.

        Three different clocks, deliberately:

          * **motion** every detected frame — the five points are already
            decoded, so the whole term costs a 5x2 least-squares fit on the CPU
            and the window would be useless if it were sampled sparsely.
          * **texture** on `embed_interval` frames (every frame while a capture
            is running, because a calibration set must not contain the same
            cached value repeated).
          * **FaceMesh** every `liveness_facemesh_interval` frames — it is the
            expensive term and a blink lasts several frames.
        """
        lv = state.lv
        now = float(frame.pts or 0.0)
        if state.first_seen is None:
            state.first_seen = now
        x1, y1, x2, y2 = det["box"]
        face_px = max(1.0, float(min(x2 - x1, y2 - y1)))

        motion_score = None
        kps = det.get("kps")
        if kps is not None:
            t0 = time.monotonic()
            try:
                _res, _corr, motion_score = lt.update_motion(
                    lv, now, kps, face_px, self._lv_cfg)
            except Exception as e:              # noqa: BLE001
                print(f"[face-recognition] motion update failed: {e}", flush=True)
            self._lt_add("motion", t0)

        capturing = self._capture is not None
        # A track already judged live keeps its verdict and only re-checks every
        # live_recheck_interval frames -- FaceMesh is the single largest cost.
        if lt.skip_heavy_for_live(lv, self._frame_idx, self._lv_cfg, capturing) \
                and state.liveness is not None:
            return state.liveness
        p_v2 = p_v1se = None
        due = capturing or (self._frame_idx - state.last_texture) >= self.embed_interval
        if due:
            state.last_texture = self._frame_idx
            lv.last_heavy_frame = self._frame_idx
            t0 = time.monotonic()
            try:
                p_v2, p_v1se, mean = liveness_mod.infer_texture_ensemble(
                    frame.data, (x1, y1, x2 - x1, y2 - y1),
                    self._model(LIVENESS_ID), self._model(LIVENESS_V1SE_ID),
                    rgb_input=True)
                lv.update_texture(mean, self._lv_cfg.texture_ema_alpha)
            except Exception as e:              # noqa: BLE001
                print(f"[face-recognition] liveness failed: {e}", flush=True)
            self._lt_add("texture", t0)

        ear = None
        interval = max(1, int(self._lv_cfg.facemesh_interval))
        if self._frame_idx % interval == 0 and \
                lt.facemesh_allowed(face_px, self._lv_cfg, capturing):
            lv.last_heavy_frame = self._frame_idx
            try:
                ear = self._facemesh_ear(frame, det["box"])
            except Exception as e:              # noqa: BLE001
                print(f"[face-recognition] facemesh failed: {e}", flush=True)
        blink = lt.update_blink(lv, ear, self._lv_cfg.ear_threshold,
                                self._lv_cfg.blink_min_samples,
                                self._lv_cfg.blink_max_samples)

        # Depth runs on the texture cadence (every embed_interval frames, or
        # every frame while capturing): ~51 ms per call on RV1126B, so not
        # per frame. Between runs the last result is reused.
        depth_score = None
        if self._lv_cfg.depth_enabled:
            if due and lt.depth_allowed(face_px, self._lv_cfg, capturing):
                t0 = time.monotonic()
                try:
                    lv.depth = depth_liveness.depth_flatness(frame.data, det["box"])
                except Exception as e:          # noqa: BLE001
                    print(f"[face-recognition] depth failed: {e}", flush=True)
                self._lt_add("depth", t0)
            d = lv.depth
            depth_score = None if d is None else d.get("score")

        out = lt.fuse_liveness(lv, now, state.first_seen, motion_score,
                               depth_score, self._lv_cfg)
        state.liveness = out
        state.live = (True if lv.decision == lt.LIVE
                      else False if lv.decision == lt.SPOOF else None)
        # ★Legacy field★ `liveness_score` stays a probability-like number, i.e.
        # the texture EMA — NOT the fused score, which mixes in motion and would
        # silently change meaning for an existing consumer.
        state.liveness_score = lv.texture_ema
        self._capture_row(state, track_id, now, face_px, p_v2, p_v1se, ear, blink)
        return out

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
                    if self.liveness_enabled or self._capture is not None:
                        self._sample_liveness(frame, d, st, tid)
                    due = (st.samples == 0
                           or (self._frame_idx - st.last_embed) >= self.embed_interval)
                    if due:
                        self._embed_track(st, rgb, d)

                if st is not None and not gated:
                    name, score, stable = st.verdict(self.min_track_frames)
                    st.name, st.score = name, score
                    # ★Pitfall: identity leakage while pending★ a track whose
                    # liveness has not settled must not publish a name -- the
                    # UI would show it, an MQTT consumer would act on it, and
                    # the verdict that arrives 300 ms later cannot un-open a
                    # door. Withheld, not renamed: `reason` says why.
                    if self.liveness_enabled and st.live is not True:
                        name, score = None, 0.0
                        if st.reason is None:
                            st.reason = st.lv.decision
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
                    "liveness": (st.liveness if st is not None else None),
                })

            extra = {"model_tag": MODEL_TAG, "faces": face_rows,
                     "enrolled": self.user_count()}
            if self.gallery_error:
                extra["gallery_error"] = self.gallery_error
            self.emit([], t, results=results, extra=extra)
            self._lt_report()

            # After emit: the rows for THIS frame are already written, so the
            # deadline closes the file with a complete frame in it.
            self._tick_capture()
        self._stop_capture()

    def _embed_track(self, st: TrackState, frame_rgb, det: dict) -> None:
        """One embedding for `det`, folded into `st`, gated on the verdict.

        The liveness sampling itself already ran this frame (`_sample_liveness`)
        because motion and blink need EVERY frame, not the embedding cadence.
        What is left here is the consequence of the verdict.
        """
        st.last_embed = self._frame_idx
        st.reason = None
        if self.liveness_enabled:
            if st.live is False:
                # ★A spoof contributes NO evidence★ -- annotating it while still
                # voting would let a phone screen accumulate an identity.
                st.reason = "spoof"
                st.votes.clear()
                st.last_cos.clear()
                return
            if st.live is not True:
                # Pending: do not spend the embedder, and do not accumulate
                # evidence that would become actionable the moment the verdict
                # flips. The track re-tries on the next due frame.
                st.reason = st.lv.decision
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
