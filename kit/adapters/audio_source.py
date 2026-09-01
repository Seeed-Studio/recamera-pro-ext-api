"""
AudioSource adapter for reCamera Pro (Rockchip RV1126B).

L0 adapter layer (see docs/guide/voice-app.md §4/§5/§6, docs/guide/adapter-bootstrap.md §2.3).
The application only ever sees the `AudioSource` ABC + `PcmFrame` (16k mono PCM);
the concrete capture backend is swappable. When the official **R8 clean-PCM
broker** (`/var/run/recamera/audio.sock`, VQE-clean 16k mono) arrives, only a new
AudioSource implementation (`OfficialPcmSource`) is selected by the capability
registry -- no application (KWS / VAD / ASR / state-machine) code changes.

This module is the canonical home of the audio contract: `PcmFrame` and the
`AudioSource` ABC are defined here and re-exported by `official.py` so both the
workaround (`AlsaTakeoverSource`) and the migration stub (`OfficialPcmSource`)
implement the exact same interface.


P0 FEASIBILITY VERDICT (firmware 6.1.157, verified on device root@... 2026-08-09)
================================================================================
The single hardware mic (`/dev/snd/pcmC0D0c`, card0 = rockchip,rv1126b-acodec,
ONE capture subdevice) is **held exclusively** by `rkipc` (pid 939):

    $ arecord -D hw:0,0 -d 2 -f S16_LE -c 2 -r 22050 /tmp/t.wav
    arecord: main:831: audio open error: Device or resource busy   <-- EBUSY
    $ fuser /dev/snd/pcmC0D0c
    939                                                             <-- rkipc

ALSA is NOT configured with a `dsnoop` capture-sharing PCM, and rkipc opens the
raw `hw` device, so no second reader can attach while rkipc holds it.

`rkipc` is a SINGLE process that runs BOTH the camera/encoder/video pipeline AND
audio-in (`RK_MPI_AI`). Its RTSP server (`127.0.0.1:5554/live/*`) publishes a
*combined* stream -- `video H265` + `audio PCMA/22050/2` (the mic, G711A) -- that
`go2rtc` pulls and forwards to the app-market WebRTC viewer (verified live via
`http://127.0.0.1:1984/api/streams`). The rkipc binary itself contains the
string `"/oem/usr/etc/init.d/S50go2rtc restart"` and a `ser_rk_audio_restart`
handler: **changing rkipc's audio configuration cascades a go2rtc restart**,
because the audio RTP track it serves changes.

Consequences (the honest cost of "freeing the mic" today):
  1. There is NO runtime path to release ONLY the mic while leaving the exact
     video stream untouched. Every way to free `pcmC0D0c` either
       (a) sets `[audio.0] enable=0` + audio-deinit -> rkipc restarts go2rtc
           (video *blip* for live viewers) and drops the RTSP audio track, OR
       (b) kills/restarts rkipc -> video goes DOWN entirely.
  2. Taking over the mic also forfeits the hardware VQE (AEC/denoise/AGC in
     librkaudio) -- the app must bring its own software denoise (VOICE_APP §5.2).
  3. The official R8 PCM broker (`/var/run/recamera/audio.sock`) does not exist
     on this firmware, so `OfficialPcmSource` is still a stub.

=> **Mic takeover WITHOUT disturbing video is NOT feasible on 6.1.157.**
   `AlsaTakeoverSource` below is technically correct and *will* capture 16k mono
   PCM the moment the mic is free -- but on shipping firmware the mic is never
   free unless rkipc audio is disabled first, which blips video. Therefore the
   takeover lifecycle here **does NOT auto-disable rkipc audio**; it refuses with
   a clear, actionable error on EBUSY. The clean path is R8 (`OfficialPcmSource`);
   the only workaround that yields the mic is a product decision to accept a
   one-time go2rtc/video blip + permanent loss of the RTSP audio track & VQE
   while the voice app is active (opt-in, see `AlsaTakeoverSource(...,
   release_rkipc_audio=<callback>)`).


Capture backend
---------------
`AlsaTakeoverSource` shells out to `arecord` (already on device; zero extra
Python deps for the primary path) reading `plughw:0,0`, letting the ALSA `plug`
plugin downmix stereo->mono and resample 22050->16000 in-kernel, and streams raw
S16 mono 16k PCM off arecord's stdout in fixed chunks -> `PcmFrame`. A pure-numpy
resample/downmix path (`_downmix_to_mono` / `_resample_linear`) is provided for
the raw-`hw` variant and is unit-testable off-device.
"""
from __future__ import annotations

import shutil
import signal
import subprocess
import time
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

from kit.errors import ResourceBusyError


# --- Contract (canonical; re-exported by official.py) ------------------------ #
@dataclass
class PcmFrame:
    """One chunk of PCM audio (docs/guide/adapter-bootstrap.md §2.3 contract).

    `pcm` is little-endian signed 16-bit samples. For the app-facing contract
    this is always 16 kHz mono, so `len(pcm) // 2` samples == duration * 16000.
    """
    pcm: bytes
    rate: int = 16000
    ch: int = 1
    pts: float = 0.0

    @property
    def n_samples(self) -> int:
        return len(self.pcm) // (2 * self.ch)


class AudioSource(ABC):
    """Abstract PCM producer. Applications depend only on this (16 kHz mono).

    Usage:
        with open_audio_source() as src:
            while True:
                frame = src.read()      # -> PcmFrame | None (None == stream end)
                if frame is None:
                    break
                feed_kws_and_asr(frame.pcm)
    """

    @abstractmethod
    def read(self) -> Optional[PcmFrame]:
        """Return the next PCM chunk, or None at end-of-stream."""
        raise NotImplementedError

    def open(self) -> "AudioSource":  # optional override
        return self

    def close(self) -> None:  # optional override
        pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


# --- Audio gain / normalization ---------------------------------------------- #
# The RV1126B mic delivered through rkipc's RTSP audio track is very quiet
# (measured RMS ~0.0034 / ~-49 dBFS on a normal-distance speaker -- ~40x below
# the level a sherpa KWS/ASR expects). `RtspAudioSource` is otherwise unity gain,
# so the KeywordSpotter sits right on the edge of triggering and the wake word is
# unreliable. We therefore apply an ffmpeg audio filter that adaptively lifts the
# level before it reaches KWS/ASR.
#
# Default: `loudnorm` (EBU R128 loudness normalization) targeting -16 LUFS. This
# was the empirical winner on-device (2026-08-10) against the weak mic_probe.wav:
# of every candidate (unity, dynaudnorm at several settings, static volume gain),
# loudnorm was the ONLY filter that still fired the "HELLO CAMERA" KeywordSpotter
# at a *stricter* KWS threshold (0.5) -- i.e. it is the only one that buys real
# detection MARGIN, not just a louder waveform. Measured: orig RMS 0.0034/-49dBFS
# -> loudnorm RMS 0.165/-15.7dBFS, peak 0.857 (no clip), 1 true hit / 0 false in
# a 20 s clip. dynaudnorm raised the level (RMS ~0.09) but gave the KWS no more
# confidence than unity at the stress threshold; static `volume` gain clipped.
#
# Trade-off: loudnorm applies more gain (so it lifts the noise floor the most) and
# its single-pass dynamic mode carries a short lookahead (small added latency).
# Both are acceptable for an always-listening wake pipeline, and both are tunable:
# a lower-latency / lower-noise-floor alternative that still normalizes adaptively
# is `dynaudnorm=f=150:g=11:p=0.95:m=50` -- set it via the `audio_filter` config.
#
# Set to "" / "none" / "off" to disable (unity gain, original pre-fix behaviour).
DEFAULT_AUDIO_FILTER = "loudnorm=I=-16:TP=-1.5"

_FILTER_OFF = frozenset({"", "none", "off", "0", "false", "no", "unity", "bypass"})


def _normalize_filter(audio_filter: Optional[str]) -> Optional[str]:
    """Return a usable ffmpeg -af string, or None when normalization is disabled.

    None / a disable-word ("none"/"off"/"") -> None (unity gain). Any other
    string is passed through verbatim as the ffmpeg filtergraph (so an operator
    can drop in `loudnorm=I=-16:TP=-1.5` or tune dynaudnorm params from config).
    """
    if audio_filter is None:
        return None
    s = str(audio_filter).strip()
    if s.lower() in _FILTER_OFF:
        return None
    return s


# --- Errors ------------------------------------------------------------------ #
class AudioDeviceBusy(ResourceBusyError):
    """Raised when the ALSA capture device is held exclusively (rkipc)."""


# --- pure-numpy DSP helpers (raw-hw path; unit-testable off device) ---------- #
def _downmix_to_mono(samples, ch: int):
    """Average interleaved `ch`-channel int16 samples down to mono int16."""
    import numpy as np
    if ch <= 1:
        return samples
    frames = samples.reshape(-1, ch).astype(np.int32)
    mono = (frames.sum(axis=1) // ch).astype(np.int16)
    return mono


def _resample_linear(samples, src_rate: int, dst_rate: int):
    """Linear-interpolation resample of a mono int16 buffer."""
    import numpy as np
    if src_rate == dst_rate or samples.size == 0:
        return samples
    n_src = samples.size
    n_dst = int(round(n_src * dst_rate / src_rate))
    if n_dst <= 0:
        return samples[:0]
    xp = np.arange(n_src, dtype=np.float64)
    x = np.linspace(0.0, n_src - 1, num=n_dst)
    out = np.interp(x, xp, samples.astype(np.float64))
    return np.rint(out).astype(np.int16)


def pcm_stats(pcm: bytes) -> dict:
    """Cheap energy/liveness stats to prove a WAV is real audio, not silence.

    Returns mean, peak, RMS and a crude dBFS. Silence -> rms ~ 0; a real mic in
    a quiet room -> rms typically > ~30 (‑60 dBFS). Used by the on-device
    verification and by callers wanting a sanity gate before ASR.
    """
    import numpy as np
    s = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if s.size == 0:
        return {"samples": 0, "mean": 0.0, "peak": 0, "rms": 0.0, "dbfs": -120.0}
    rms = float(np.sqrt(np.mean(s * s)))
    peak = int(np.max(np.abs(s)))
    dbfs = 20.0 * np.log10(rms / 32768.0) if rms > 0 else -120.0
    return {"samples": int(s.size), "mean": float(s.mean()),
            "peak": peak, "rms": rms, "dbfs": round(dbfs, 1)}


# --- Workaround backend: ALSA takeover via arecord --------------------------- #
class AlsaTakeoverSource(AudioSource):
    """Capture 16 kHz mono PCM from the RV1126B mic via an `arecord` subprocess.

    Primary path (`use_plug=True`, default): reads `plughw:CARD,DEV` asking
    arecord for `-c1 -r16000`; the ALSA `plug` plugin does the stereo->mono
    downmix + 22050->16000 resample, so Python just reads raw S16 mono bytes.

    Raw path (`use_plug=False`): reads native `hw:CARD,DEV` at 22050/2ch and
    downmixes+resamples in numpy (`_downmix_to_mono` + `_resample_linear`).

    IMPORTANT (see module verdict): on firmware 6.1.157 the mic is held by
    rkipc, so `open()` will hit `Device or resource busy`. We do NOT silently
    disable rkipc audio (that blips the live video). If the product explicitly
    accepts that cost, pass `release_rkipc_audio` -- a caller-provided callback
    that frees the mic (e.g. flips `[audio.0] enable=0` and triggers rkipc audio
    re-init) and returns a `restore` callable invoked on `close()`. Absent that
    callback, EBUSY raises `AudioDeviceBusy` with guidance to use R8 instead.
    """

    def __init__(
        self,
        device: str = "hw:0,0",
        *,
        target_rate: int = 16000,
        target_ch: int = 1,
        capture_rate: int = 22050,
        capture_ch: int = 2,
        chunk_ms: int = 100,
        use_plug: bool = True,
        arecord_bin: str = "arecord",
        release_rkipc_audio: Optional[Callable[[], Callable[[], None]]] = None,
    ):
        self.device = device
        self.target_rate = int(target_rate)
        self.target_ch = int(target_ch)
        self.capture_rate = int(capture_rate)
        self.capture_ch = int(capture_ch)
        self.chunk_ms = int(chunk_ms)
        self.use_plug = bool(use_plug)
        self.arecord_bin = arecord_bin or "arecord"
        self._release_cb = release_rkipc_audio
        self._restore_cb: Optional[Callable[[], None]] = None
        self._proc: Optional[subprocess.Popen] = None

        # bytes read per read() call, expressed in the arecord *capture* format
        if self.use_plug:
            self._read_rate, self._read_ch = self.target_rate, self.target_ch
        else:
            self._read_rate, self._read_ch = self.capture_rate, self.capture_ch
        self._chunk_bytes = max(
            2 * self._read_ch,
            int(self._read_rate * self.chunk_ms / 1000) * 2 * self._read_ch,
        )

    # -- command ------------------------------------------------------------- #
    def _dev_arg(self) -> str:
        if not self.use_plug:
            return self.device
        # hw:0,0 -> plughw:0,0 ; leave already-plug/other names as-is
        return "plug" + self.device if self.device.startswith("hw:") else self.device

    def _build_cmd(self) -> list[str]:
        return [self.arecord_bin, "-D", self._dev_arg(),
                "-f", "S16_LE", "-c", str(self._read_ch),
                "-r", str(self._read_rate), "-t", "raw", "-q", "-"]

    # -- lifecycle ----------------------------------------------------------- #
    def open(self) -> "AlsaTakeoverSource":
        if shutil.which(self.arecord_bin) is None:
            raise FileNotFoundError(f"{self.arecord_bin} not found on device")

        # Optional, opt-in: free the mic from rkipc (blips video -- caller owns
        # that decision). Returns a restore callable used on close().
        if self._release_cb is not None:
            self._restore_cb = self._release_cb()

        self._proc = subprocess.Popen(
            self._build_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=self._chunk_bytes,
        )
        # Probe one chunk so an exclusive-device (EBUSY) error surfaces here,
        # not silently as an empty stream later.
        first = self._read_exact(self._chunk_bytes)
        if first is None:
            err = b""
            if self._proc and self._proc.stderr:
                try:
                    err = self._proc.stderr.read() or b""
                except Exception:
                    pass
            self.close()
            msg = err.decode(errors="replace").strip()
            if "busy" in msg.lower() or "resource busy" in msg.lower():
                raise AudioDeviceBusy(
                    f"{self._dev_arg()} is held exclusively (rkipc holds "
                    f"/dev/snd/pcmC0D0c on firmware 6.1.157). arecord: {msg}. "
                    "Freeing it requires disabling rkipc audio, which restarts "
                    "go2rtc and blips video -- pass release_rkipc_audio=... to "
                    "opt in, or migrate to the official R8 PCM broker "
                    "(OfficialPcmSource).")
            raise AudioDeviceBusy(f"arecord failed to start on {self._dev_arg()}: {msg}")
        self._pending = first
        return self

    _pending: Optional[bytes] = None

    def _read_exact(self, n: int) -> Optional[bytes]:
        if not self._proc or not self._proc.stdout:
            return None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def read(self) -> Optional[PcmFrame]:
        if self._pending is not None:
            raw, self._pending = self._pending, None
        else:
            raw = self._read_exact(self._chunk_bytes)
        if raw is None:
            return None
        pts = time.monotonic()

        if self.use_plug:
            # arecord already delivered target_rate/target_ch S16 mono
            return PcmFrame(pcm=raw, rate=self.target_rate, ch=self.target_ch, pts=pts)

        # raw-hw path: downmix + resample in numpy
        import numpy as np
        samples = np.frombuffer(raw, dtype=np.int16)
        mono = _downmix_to_mono(samples, self._read_ch)
        out = _resample_linear(mono, self.capture_rate, self.target_rate)
        return PcmFrame(pcm=out.tobytes(), rate=self.target_rate,
                        ch=self.target_ch, pts=pts)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # restore rkipc audio if we ever took it over
        if self._restore_cb is not None:
            try:
                self._restore_cb()
            finally:
                self._restore_cb = None


# --- Workaround backend: RTSP audio-track demux via ffmpeg ------------------- #
class RtspAudioSource(AudioSource):
    """Capture 16 kHz mono PCM by demuxing the audio track from rkipc's combined
    RTSP stream -- WITHOUT touching the mic, the video, or /dev/snd.

    rkipc publishes video+audio on `rtsp://admin:admin@127.0.0.1:5554/live/1`
    (the mic, G711A/PCMA @ 22050/2). We pull that stream over TCP, drop video
    (`-vn`) and let ffmpeg decode+downmix+resample the audio track to raw S16LE
    16k mono on stdout, streamed off in fixed chunks -> `PcmFrame`.

    This is the DEFAULT audio source on firmware 6.1.157 (verified feasible
    2026-08-09): it needs no mic takeover (which would blip video, see
    AlsaTakeoverSource) and no extra Python deps (ffmpeg already on device).
    Trade-off: the mic still runs through rkipc's hardware VQE and the audio is
    whatever rkipc encodes (22050 PCMA upstream). `AlsaTakeoverSource` remains
    the mic-takeover fallback for when direct capture is explicitly wanted.
    """

    def __init__(
        self,
        url: str = "rtsp://admin:admin@127.0.0.1:5554/live/1",
        *,
        target_rate: int = 16000,
        target_ch: int = 1,
        chunk_ms: int = 100,
        rtsp_transport: str = "tcp",
        ffmpeg_bin: str = "ffmpeg",
        audio_filter: Optional[str] = DEFAULT_AUDIO_FILTER,
    ):
        self.url = url
        self.target_rate = int(target_rate)
        self.target_ch = int(target_ch)
        self.chunk_ms = int(chunk_ms)
        self.rtsp_transport = rtsp_transport
        self.ffmpeg_bin = ffmpeg_bin or "ffmpeg"
        # Adaptive gain/normalization filter (see DEFAULT_AUDIO_FILTER above).
        # None when disabled -> unity gain (original behaviour).
        self.audio_filter = _normalize_filter(audio_filter)
        self._proc: Optional[subprocess.Popen] = None
        self._pending: Optional[bytes] = None
        self._chunk_bytes = max(
            2 * self.target_ch,
            int(self.target_rate * self.chunk_ms / 1000) * 2 * self.target_ch,
        )

    def _build_cmd(self) -> list[str]:
        cmd = [self.ffmpeg_bin, "-loglevel", "error", "-nostdin",
               "-rtsp_transport", self.rtsp_transport, "-i", self.url,
               "-vn"]
        # Adaptive gain BEFORE downmix/resample so KWS/ASR get a normal level.
        if self.audio_filter:
            cmd += ["-af", self.audio_filter]
        cmd += ["-ac", str(self.target_ch), "-ar", str(self.target_rate),
                "-f", "s16le", "-"]
        return cmd

    def open(self) -> "RtspAudioSource":
        if shutil.which(self.ffmpeg_bin) is None:
            raise FileNotFoundError(f"{self.ffmpeg_bin} not found on device")
        self._proc = subprocess.Popen(
            self._build_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=self._chunk_bytes,
        )
        # Probe one chunk so a connect/negotiation failure surfaces at open()
        # rather than as a silent empty stream. RTSP + decode startup can take a
        # moment, so a short read here just means "not yet"; a dead pipe means
        # ffmpeg exited (bad URL / no audio track).
        first = self._read_exact(self._chunk_bytes)
        if first is None:
            err = b""
            if self._proc and self._proc.stderr:
                try:
                    err = self._proc.stderr.read() or b""
                except Exception:
                    pass
            self.close()
            msg = err.decode(errors="replace").strip()
            raise RuntimeError(
                f"ffmpeg produced no audio from {self.url}: {msg or 'stream ended'}")
        self._pending = first
        return self

    def _read_exact(self, n: int) -> Optional[bytes]:
        if not self._proc or not self._proc.stdout:
            return None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def read(self) -> Optional[PcmFrame]:
        if self._pending is not None:
            raw, self._pending = self._pending, None
        else:
            raw = self._read_exact(self._chunk_bytes)
        if raw is None:
            return None
        return PcmFrame(pcm=raw, rate=self.target_rate, ch=self.target_ch,
                        pts=time.monotonic())

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# --- Official path: ALSA `ai_asr` shared-capture PCM ------------------------- #
class AiAsrAudioSource(AudioSource):
    """Capture 16 kHz mono PCM from the firmware's reserved ALSA `ai_asr` PCM.

    THIS IS THE OFFICIAL, RECOMMENDED audio path on reCamera Pro (RV1126B) --
    see docs/guide/audio-pcm.md (authoritative, derived from the firmware's
    /etc/asound.conf). It supersedes both workarounds (`RtspAudioSource`,
    `AlsaTakeoverSource`) because it is clean and non-invasive:

      * The mic hardware (`hw:0,0`) is shared across processes via an ALSA
        `dsnoop` plugin. The firmware pre-declares four named capture PCMs that
        all read the SAME 6ch/16k dsnoop stream through their own cursor:
            ai_main  -> held by rkipc (camera+encoder+audio main program)
            ai_kws   -> held by the official keyword-detection service
            ai_asr   -> RESERVED for third-party ASR/audio apps  <-- we use this
            ai_debug -> reserved for debug recording
        Because dsnoop shares a ring buffer, `ai_asr` does NOT conflict with and
        does NOT need to stop rkipc: no mic takeover, no `Device or resource
        busy`, video and the RTSP audio track are untouched (unlike
        AlsaTakeoverSource, which fights rkipc for `hw:0,0`).

      * Hardware side is fixed at 16 kHz / S16_LE (dsnoop slave). Any higher
        `-r` would only trigger a software resample of the same 16 kHz source --
        so we always request exactly 16000 (no extra CPU, no extra information).

    Channel layout (asound.conf `ai_2mic_2ref` route -> 4 output channels):
        ch0 = Mic 1        ch1 = Mic 2
        ch2 = Reference    ch3 = Reference (fill)
    The reference channels (ch2/ch3) are the AEC playback-loopback reference,
    carried IN the same stream -- reserved here for a FUTURE software AEC
    (speexdsp / WebRTC AEC: feed ch0 as near-end, ch2 as far-end reference).
    We do NOT use them for the ASR feed.

    IMPORTANT -- NO VQE: `ai_asr` delivers the RAW microphone signal. rkipc's
    hardware VQE (AEC / ANS / AGC in librkaudio) runs inside its own RK_MPI_AI
    path and does NOT reach the dsnoop taps. Any denoise / AEC / AGC is the
    app's responsibility (the loudnorm gain below is our only current step).

    PERMISSIONS -- needs root or the `audio` group: dsnoop's IPC key is 0666,
    but every client still opens `/dev/snd/pcmC0D0c` + `/dev/snd/controlC0`,
    which are `root:audio 0660`. An SSH `admin` user (not in `audio`) fails with
    `Cannot get card index` / `audio open error`. This is satisfied in the real
    deployment: appmgr launches extension processes as root. Off-device / as a
    non-audio user, `open()` surfaces a clear, actionable error.

    Channel-selection policy (default `capture_ch=4`, `mic_channel=0`):
        We capture the NATIVE 4 channels and deterministically select Mic 1
        (hw ch0) with ffmpeg `pan=mono|c0=c0`. This is the robust path: the
        plug layer's own N->1 downmix behaviour is NOT verified in the firmware
        docs (it might average in the near-silent reference channels, halving
        mic energy). Set `capture_ch=1` to instead let the ALSA plug layer do
        the downmix (documented but unverified) and skip ffmpeg's pan.

    GAIN -- loudnorm preserved from RtspAudioSource: the RV1126B mic is very
    quiet (~-49 dBFS raw; see DEFAULT_AUDIO_FILTER above). Wake-word detection
    empirically depends on lifting the level -- `loudnorm=I=-16:TP=-1.5` was the
    only filter that still fired the KeywordSpotter at a stricter threshold. We
    therefore run the identical ffmpeg loudnorm here (arecord | ffmpeg) so the
    downstream KWS / VAD / ASR see the SAME normalized 16k-mono frames as the
    RTSP path -- migrating the source is invisible to the pipeline. Set
    `audio_filter` to ""/"none" for unity gain.

    Pipeline:
        arecord -D ai_asr -f S16_LE -r 16000 -c <capture_ch> -t raw -q -
          | ffmpeg -f s16le -ar 16000 -ac <capture_ch> -i -
                   -af "pan=mono|c0=c<mic>,loudnorm=I=-16:TP=-1.5"
                   -ac 1 -ar 16000 -f s16le -
    When no ffmpeg processing is needed (`capture_ch==1` AND filter disabled),
    arecord already yields 16k mono and is read directly with no ffmpeg hop.

    ON-DEVICE VERIFICATION TODO (blocked off-device / as non-audio user):
      1. Run as root (or add the run user to the `audio` group).
      2. Prove the tap is live & non-silent:
           arecord -D ai_asr -f S16_LE -r 16000 -c 4 -d 5 /tmp/ai_asr.wav
         then check ch0 RMS with pcm_stats() (> ~30 / > -60 dBFS = real audio).
      3. Confirm it does NOT disturb rkipc (video + RTSP audio keep running;
         no EBUSY -- dsnoop shared).
      4. End-to-end: run the voice app on `audio_source=ai_asr`, speak the wake
         word, confirm idle -> listening -> transcribing (loudnorm gives the KWS
         the same margin it had on the RTSP path).
      5. (Future AEC) Capture 4ch while the speaker plays; verify ch2/ch3 carry
         the playback reference (non-zero during playback) before wiring AEC.
    """

    def __init__(
        self,
        device: str = "ai_asr",
        *,
        target_rate: int = 16000,
        target_ch: int = 1,
        capture_rate: int = 16000,   # dsnoop slave is fixed 16k; do NOT raise
        capture_ch: int = 4,         # native 4ch (ch0=Mic1..ch3=Ref); select ch0
        mic_channel: int = 0,        # hw ch0 = Mic 1 -> the ASR feed
        chunk_ms: int = 100,
        arecord_bin: str = "arecord",
        ffmpeg_bin: str = "ffmpeg",
        audio_filter: Optional[str] = DEFAULT_AUDIO_FILTER,
    ):
        self.device = device
        self.target_rate = int(target_rate)
        self.target_ch = int(target_ch)
        self.capture_rate = int(capture_rate)
        self.capture_ch = int(capture_ch)
        self.mic_channel = int(mic_channel)
        self.chunk_ms = int(chunk_ms)
        self.arecord_bin = arecord_bin or "arecord"
        self.ffmpeg_bin = ffmpeg_bin or "ffmpeg"
        # loudnorm (or operator override); None -> unity gain. Same knob/semantics
        # as RtspAudioSource so the two sources are interchangeable.
        self.audio_filter = _normalize_filter(audio_filter)
        self._arecord: Optional[subprocess.Popen] = None
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._proc: Optional[subprocess.Popen] = None   # the process we read from
        self._pending: Optional[bytes] = None
        # We always read the app-facing target format off the tail of the pipe.
        self._chunk_bytes = max(
            2 * self.target_ch,
            int(self.target_rate * self.chunk_ms / 1000) * 2 * self.target_ch,
        )

    # -- command assembly ---------------------------------------------------- #
    def _needs_ffmpeg(self) -> bool:
        """ffmpeg is needed to pick a single hw channel (pan) or to apply gain.

        The one case we can skip it: arecord already delivers mono (capture_ch
        == target_ch == 1) and no gain filter is requested -> read arecord raw.
        """
        multichannel = self.capture_ch > 1 or self.target_ch != 1
        return bool(multichannel or self.audio_filter)

    def _arecord_cmd(self) -> list[str]:
        return [self.arecord_bin, "-D", self.device, "-f", "S16_LE",
                "-r", str(self.capture_rate), "-c", str(self.capture_ch),
                "-t", "raw", "-q", "-"]

    def _ffmpeg_filtergraph(self) -> str:
        parts = []
        # Deterministic Mic 1 (hw ch0) selection when we captured >1 channel.
        # `pan=mono|c0=c<mic>` copies exactly one input channel to the mono out
        # (NOT an average across channels -> reference channels never dilute it).
        if self.capture_ch > 1:
            parts.append(f"pan=mono|c0=c{self.mic_channel}")
        if self.audio_filter:
            parts.append(self.audio_filter)
        return ",".join(parts)

    def _ffmpeg_cmd(self) -> list[str]:
        cmd = [self.ffmpeg_bin, "-loglevel", "error", "-nostdin",
               "-f", "s16le", "-ar", str(self.capture_rate),
               "-ac", str(self.capture_ch), "-i", "-"]
        fg = self._ffmpeg_filtergraph()
        if fg:
            cmd += ["-af", fg]
        cmd += ["-ac", str(self.target_ch), "-ar", str(self.target_rate),
                "-f", "s16le", "-"]
        return cmd

    # -- lifecycle ----------------------------------------------------------- #
    def open(self) -> "AiAsrAudioSource":
        if shutil.which(self.arecord_bin) is None:
            raise FileNotFoundError(f"{self.arecord_bin} not found on device")

        use_ffmpeg = self._needs_ffmpeg()
        if use_ffmpeg and shutil.which(self.ffmpeg_bin) is None:
            raise FileNotFoundError(f"{self.ffmpeg_bin} not found on device")

        self._arecord = subprocess.Popen(
            self._arecord_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=self._chunk_bytes,
        )
        if use_ffmpeg:
            self._ffmpeg = subprocess.Popen(
                self._ffmpeg_cmd(), stdin=self._arecord.stdout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=self._chunk_bytes,
            )
            # Let arecord get SIGPIPE if ffmpeg dies (parent keeps its own ref).
            if self._arecord.stdout:
                self._arecord.stdout.close()
            self._proc = self._ffmpeg
        else:
            self._proc = self._arecord

        # Probe one chunk so an arecord permission/EBUSY error (or a bad
        # filtergraph) surfaces at open(), not as a silent empty stream later.
        first = self._read_exact(self._chunk_bytes)
        if first is None:
            msg = self._drain_errors()
            self.close()
            low = msg.lower()
            if ("card index" in low or "no such" in low or "permission" in low
                    or "audio open error" in low):
                raise AudioDeviceBusy(
                    f"arecord could not open ALSA '{self.device}': {msg}. "
                    "ai_asr is a shared dsnoop PCM (it does NOT go busy), so this "
                    "is almost always a PERMISSION problem: /dev/snd is "
                    "root:audio 0660. Run as root or add the user to the 'audio' "
                    "group (appmgr launches extensions as root, which satisfies "
                    "this). See docs/guide/audio-pcm.md.")
            raise RuntimeError(
                f"ai_asr capture produced no audio from '{self.device}': "
                f"{msg or 'stream ended'}")
        self._pending = first
        return self

    def _drain_errors(self) -> str:
        """Collect stderr from both stages (arecord perms, ffmpeg filtergraph)."""
        out = []
        for name, proc in (("arecord", self._arecord), ("ffmpeg", self._ffmpeg)):
            if proc and proc.stderr:
                try:
                    e = proc.stderr.read() or b""
                except Exception:
                    e = b""
                if e:
                    out.append(f"[{name}] {e.decode(errors='replace').strip()}")
        return " ".join(out)

    def _read_exact(self, n: int) -> Optional[bytes]:
        if not self._proc or not self._proc.stdout:
            return None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def read(self) -> Optional[PcmFrame]:
        if self._pending is not None:
            raw, self._pending = self._pending, None
        else:
            raw = self._read_exact(self._chunk_bytes)
        if raw is None:
            return None
        return PcmFrame(pcm=raw, rate=self.target_rate, ch=self.target_ch,
                        pts=time.monotonic())

    def close(self) -> None:
        # Terminate ffmpeg first (downstream), then arecord (upstream source).
        for attr in ("_ffmpeg", "_arecord"):
            proc = getattr(self, attr, None)
            setattr(self, attr, None)
            if proc and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._proc = None


# --- Test / replay backend: WAV file as an AudioSource ----------------------- #
class WavFileAudioSource(AudioSource):
    """Replay a WAV file as a `PcmFrame` stream (16 kHz mono), for offline
    injection tests of the voice pipeline WITHOUT a live mic/RTSP.

    Reads the WAV, downmixes to mono + resamples to `target_rate` (reusing the
    same numpy helpers as the raw-hw capture path), then hands it out in fixed
    `chunk_ms` `PcmFrame`s exactly like a live source. Used by the on-device
    state-machine verification: feed a "<wake word> + <sentence>" WAV in and
    watch idle -> listening -> transcribing flow.

    `pad_silence_sec` appends trailing silence so the VAD reliably endpoints the
    final utterance even without an explicit flush(). `realtime=True` sleeps
    `chunk_ms` between reads to emulate a live capture cadence.
    """

    def __init__(
        self,
        path: str,
        *,
        target_rate: int = 16000,
        target_ch: int = 1,
        chunk_ms: int = 100,
        pad_silence_sec: float = 1.0,
        realtime: bool = False,
    ):
        self.path = path
        self.target_rate = int(target_rate)
        self.target_ch = int(target_ch)
        self.chunk_ms = int(chunk_ms)
        self.pad_silence_sec = float(pad_silence_sec)
        self.realtime = bool(realtime)
        self._samples = None            # int16 mono ndarray @ target_rate
        self._pos = 0
        self._chunk_samples = max(1, int(self.target_rate * self.chunk_ms / 1000))

    def open(self) -> "WavFileAudioSource":
        import numpy as np
        with wave.open(self.path) as wf:
            sr, n, ch = wf.getframerate(), wf.getnframes(), wf.getnchannels()
            raw = wf.readframes(n)
        arr = np.frombuffer(raw, dtype=np.int16)
        mono = _downmix_to_mono(arr, ch)
        out = _resample_linear(mono, sr, self.target_rate)
        if self.pad_silence_sec > 0:
            pad = np.zeros(int(self.target_rate * self.pad_silence_sec), dtype=np.int16)
            out = np.concatenate([out, pad])
        self._samples = np.ascontiguousarray(out, dtype=np.int16)
        self._pos = 0
        return self

    def read(self) -> Optional[PcmFrame]:
        if self._samples is None or self._pos >= self._samples.size:
            return None
        if self.realtime:
            time.sleep(self.chunk_ms / 1000.0)
        chunk = self._samples[self._pos:self._pos + self._chunk_samples]
        self._pos += self._chunk_samples
        return PcmFrame(pcm=chunk.tobytes(), rate=self.target_rate,
                        ch=self.target_ch, pts=time.monotonic())

    def close(self) -> None:
        self._samples = None


# --- Factory ----------------------------------------------------------------- #
def open_audio_source(**kw) -> AudioSource:
    """Factory. Delegates to the capability registry, which returns
    `OfficialPcmSource` when the R8 broker socket exists and otherwise the
    `AlsaTakeoverSource` workaround. On today's firmware the socket is absent,
    so the ALSA workaround is selected (and will raise `AudioDeviceBusy` on
    shipping firmware -- see module verdict)."""
    from .registry import select_audio_source
    return select_audio_source(**kw)
