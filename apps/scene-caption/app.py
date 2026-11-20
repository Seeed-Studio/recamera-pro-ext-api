import codecs
from concurrent.futures import ThreadPoolExecutor
import ctypes as ct
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import unicodedata
import time

import cv2
import numpy as np
from kit.app import App
from kit.config import app_dir_of
from kit.geometry import GeometryBuilder
from kit.resources import ExternalNpuLease


DEFAULT_PROMPT = "请用一句简短的中文描述这张图片，只描述清楚可见的内容。"
TokenCallback = ct.CFUNCTYPE(None, ct.c_char_p)


class SceneCaption(App):
    id = "scene-caption"
    name = "Scene Caption"
    owns_loop = True
    model_frame = "hw-direct"
    input_size = 448

    def prepare_runtime(self):
        if os.environ.get("RECAMERA_NPU_MODE") != "legacy-direct":
            raise RuntimeError("Scene Caption requires App Center's exclusive NPU allocation")
        # One broker lease covers the vision stages and the language model.
        self.decoder_lease = ExternalNpuLease()
        if self.decoder_lease.backend != "broker":
            raise RuntimeError("The firmware NPU broker is required")
        self.decoder_lease.acquire(timeout=10)
        root = Path(app_dir_of(self))
        self.decoder = ct.CDLL(str(root / "lib/libcaption.so"))
        self.vision_dir = os.fsencode(root / "models")
        self.decoder.caption_encode.argtypes = [
            ct.c_char_p, ct.POINTER(ct.c_uint8), ct.c_size_t,
            ct.POINTER(ct.c_float), ct.c_size_t,
        ]
        self.decoder.caption_open.argtypes = [ct.c_char_p, ct.c_int, ct.POINTER(ct.c_void_p)]
        self.decoder.caption_generate.argtypes = [
            ct.c_void_p, ct.POINTER(ct.c_float), ct.c_size_t,
            ct.c_char_p, TokenCallback,
        ]
        self.decoder.caption_close.argtypes = [ct.c_void_p]
        self.handle = ct.c_void_p()
        status = self.decoder.caption_open(
            os.fsencode(root / "models/language.rkllm"), self.max_new_tokens, ct.byref(self.handle),
        )
        if status:
            raise RuntimeError(f"RKLLM initialization failed ({status})")
        self.decoder_lease.ready()

    def run(self):
        updates = SimpleQueue()
        pending = None
        last_finished = None
        self._visible_caption = None
        with ThreadPoolExecutor(max_workers=1) as worker:
            for frame in self.frames():
                self._flush(updates)
                self._refresh_preview()
                if pending is not None:
                    if not pending.done():
                        continue
                    try:
                        pending.result()
                    finally:
                        self._flush(updates)
                    pending = None
                    last_finished = time.monotonic()
                started = time.monotonic()
                if last_finished is not None and started - last_finished < self.interval_seconds:
                    continue
                if not self.decoder_lease.alive():
                    raise RuntimeError("Lost the NPU broker lease")
                # resize allocates the worker's image. No borrowed camera frame
                # survives this iteration, even during a long generation.
                prepared = self.pre(frame)
                pixels = prepared.data.reshape(self.input_size, self.input_size, 3)
                info = prepared.info
                left, top = int(info.pad_w), int(info.pad_h)
                width = round(info.orig_w * info.scale)
                height = round(info.orig_h * info.scale)
                tile = cv2.resize(pixels[top:top + height, left:left + width],
                                  (self.input_size, self.input_size))
                # A live config change applies to the next sample as a unit.
                pending = worker.submit(self._analyze, tile, frame.pts, started, updates,
                                        self._prompt_bytes(), self.stream_results)
            if pending is not None:
                try:
                    pending.result()
                finally:
                    self._flush(updates)

    def _flush(self, updates):
        while True:
            try:
                text, pts, final, events = updates.get_nowait()
            except Empty:
                return
            if text is None:
                self._visible_caption = None
                self.emit([], pts, results=[], geometry=[])
            else:
                self._publish_caption(text, pts, final=final, events=events)

    def _refresh_preview(self):
        # The firmware canvas expires geometry after 1.5 s. Refresh only the
        # drawing, keeping the sampled frame's PTS and the single caption event.
        if self._visible_caption is None or time.monotonic() < self._refresh_at:
            return
        pts, drawing = self._visible_caption
        self.emit([], pts, geometry=drawing)
        self._refresh_at = time.monotonic() + 0.5

    def _prompt_bytes(self):
        prompt = self.prompt.replace("\x00", " ").strip()[:128] or DEFAULT_PROMPT
        # A user may paste a chat template. Keep its markers literal so the
        # native runtime sees exactly one image placeholder for this frame.
        for marker in ("<image>", "<img>", "</img>", "<IMG_CONTEXT>", "<|"):
            prompt = prompt.replace(marker, "＜" + marker[1:])
        return prompt.encode("utf-8", errors="replace")

    def _analyze(self, tile, pts, started, updates, prompt, stream):
        embedding = np.empty((1, 256, 896), dtype=np.float32)
        status = self.decoder.caption_encode(
            self.vision_dir, tile.ctypes.data_as(ct.POINTER(ct.c_uint8)),
            tile.size, embedding.ctypes.data_as(ct.POINTER(ct.c_float)), embedding.size,
        )
        if status or not np.isfinite(embedding).all():
            raise RuntimeError(f"RKNN image encoding failed ({status})")
        self._describe(embedding, pts, started, updates, prompt, stream)

    def _describe(self, embedding, pts, started, updates, prompt, stream):
        vision_done = time.monotonic()
        utf8 = codecs.getincrementaldecoder("utf-8")()
        text = ""
        first_text_at = None
        last_sent = None
        callback_error = None

        @TokenCallback
        def on_token(fragment):
            nonlocal text, first_text_at, last_sent, callback_error
            # ctypes cannot propagate exceptions through the C callback. Report
            # any failure to the Kit loop after the synchronous RKLLM call.
            if callback_error is not None:
                return
            try:
                text += utf8.decode(fragment)
                now = time.monotonic()
                if not text.strip():
                    return
                if first_text_at is None:
                    first_text_at = now
                if stream and (last_sent is None or now - last_sent >= 0.12):
                    updates.put((text, pts, False, []))
                    last_sent = now
            except BaseException as exc:
                callback_error = exc

        status = self.decoder.caption_generate(
            self.handle, embedding.ctypes.data_as(ct.POINTER(ct.c_float)),
            embedding.size, prompt, on_token,
        )
        try:
            if callback_error is not None:
                raise callback_error
            if status:
                raise RuntimeError(f"RKLLM generation failed ({status})")
            # A token cap may land between the byte tokens of one character.
            # All complete characters have already been returned by decode().
            if not text.strip() or first_text_at is None:
                raise ValueError("RKLLM returned an empty caption")
        except BaseException:
            updates.put((None, pts, False, []))
            raise
        event = {
            "kind": "caption", "text": " ".join(text.split()),
            "vision_ms": round((vision_done - started) * 1000),
            "first_text_ms": round((first_text_at - started) * 1000),
            "decode_ms": round((time.monotonic() - vision_done) * 1000),
        }
        updates.put((text, pts, True, [event]))

    def _publish_caption(self, text, pts, *, final, events=None):
        caption = " ".join(text.split())
        lines = []
        line = ""
        width = 0
        for char in caption:
            columns = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            if width + columns > 96:
                lines.append(line.rstrip())
                line, width = "", 0
            line += char
            width += columns
        if line:
            lines.append(line.rstrip())
        # Keep a readable rolling window for answers longer than the video.
        # results[].text and the completion event always carry the whole answer.
        lines = lines[-16:]
        drawing = GeometryBuilder()
        for i, line in enumerate(lines):
            drawing.point(0.035, 0.94 - 0.055 * (len(lines) - i - 1),
                          id=f"caption-{i}", label=line)
        self.emit(events or [], pts, results=[{"text": caption, "final": final}],
                  geometry=drawing)
        self._visible_caption = (pts, drawing)
        self._refresh_at = time.monotonic() + 0.5

    def finish(self):
        if getattr(self, "handle", None):
            status = self.decoder.caption_close(self.handle)
            if status:
                raise RuntimeError(f"RKLLM release failed ({status})")
            self.handle = None
        if getattr(self, "decoder_lease", None):
            self.decoder_lease.release()
            self.decoder_lease = None
        super().finish()
