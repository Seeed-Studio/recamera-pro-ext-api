# Audio Playback (Speaker)

Read this reference only when the user explicitly wants audible output (an alarm
cue, a chime, a spoken warning) on the target device. It complements the audio
capability model in `capability-routing.md`; read that first.

The public `recamera_ext` Python binding exposes **no** unified speaker/player
API, and `docs/guide/audio-pcm.md` is capture-only. Audible output is therefore
a **firmware ALSA device-capability path**, not an SDK guarantee. It works only
when the target firmware provides ALSA playback, and the actual sound must be
confirmed by an authorized hardware test.

## Applicability

Use this path only after confirming, on the real target:

- a playback tool (`aplay`) or library (`libasound.so.2`) exists;
- a real playback card exists (not only a virtual/loopback card);
- the App process can open the playback node (`/dev/snd/pcmC*P`, group
  `audio`, mode `0660`); extension Apps launched by AppMgr run as root.

If any of these is missing, report "no established playback path on this
firmware" instead of writing code that cannot sound.

## Discover the playback device (never hardcode)

The real speaker is usually **not** the ALSA default. On the current reCamera Pro
firmware, `aplay -L` reports `default:CARD=Loopback` (a virtual, silent
loopback), while the physical codec is card 1 (`rockchiprv1126b`), reachable as
`hw:1,0` / `plughw:1,0`. `/etc/asound.conf` defines only capture PCMs (`ai_asr`,
`ai_main`, ...), with no playback default. A naive `aplay alarm.wav` therefore
plays into Loopback and is silent.

Card indices are firmware/build-specific. Discover them at runtime and fall back
gracefully; do not ship a single hardcoded `hw:1,0` as the only path:

- `aplay -l` lists playback hardware devices; select the real codec, not
  `Loopback`.
- Confirm the matching `/dev/snd/pcmC<card>D<device>p` node and its group/mode.
- Prefer `plughw:<card>,<device>` over bare `hw:` so ALSA auto-converts the WAV
  rate/format/channels to what the codec accepts. Bare `hw:` fails with
  `unable to install hw params` when the file does not match the codec.

## Two integration options

| Option | How | Pros | Cons |
| --- | --- | --- | --- |
| `aplay` subprocess | `subprocess.run(["aplay", "-q", "-D", dev, wav], ...)` | no ABI handling; aplay parses the WAV and does format conversion; least code | spawns a process per cue; requires `aplay` on target |
| `ctypes` + `libasound.so.2` | `snd_pcm_open` / `snd_pcm_set_params` / `snd_pcm_writei` | in-process, lower latency, reusable handle | you own the ALSA lifecycle, XRUN recovery, and threading; more risk |

Prefer the `aplay` subprocess for short alarm cues unless you have a measured
latency reason for the ctypes path.

### Validator-safe subprocess pattern

The Skill safety validator (`validate_app.py`) rejects `os.system`/`os.popen`,
any `subprocess` call with `shell=True`, and any `subprocess` call whose command
argument is not an **inline literal** (a string constant, or a list/tuple whose
first element is a string constant). Build the argv inline at the call site with
a fixed executable; do not assemble the command in a variable and pass it (that
triggers `configurable_command_entry`):

```python
import subprocess, threading, time


class AlarmPlayer:
    """Best-effort ALSA alarm. Never blocks or kills the detection loop."""

    # Fixed in code, NOT read from config_schema: the device string and WAV path
    # must not be user-configurable, and the executable must stay a literal.
    DEVICE = "plughw:1,0"
    WAV = "alarm.wav"          # bundled kind:"data" artifact at the package root

    def __init__(self, cooldown=1.0):
        self._cooldown = cooldown
        self._last = 0.0
        self._busy = False
        self._lock = threading.Lock()

    def trigger(self):
        now = time.monotonic()
        with self._lock:
            if self._busy or now - self._last < self._cooldown:
                return                       # skip; never stack concurrent plays
            self._busy = True
            self._last = now
        threading.Thread(target=self._play, daemon=True).start()

    def _play(self):
        try:
            # Inline literal argv with a fixed first element passes the validator.
            subprocess.run(["aplay", "-q", "-D", self.DEVICE, self.WAV],
                           timeout=10,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception as e:               # playback is best-effort
            print(f"[alarm] playback failed ({e}); continuing", flush=True)
        finally:
            with self._lock:
                self._busy = False
```

## Playback must not break detection

- Play on a background thread (or a short subprocess); never block the
  NPU/inference loop on audio.
- Guard with a cooldown and a busy flag so a burst of detections cannot spawn
  unbounded concurrent plays; skip rather than queue when a cue is still active.
- Wrap every playback call in `try/except`; a failure logs a warning and the
  detection App keeps running. Audio is best-effort, never fatal.
- Do not push full audio buffers or frames across threads unnecessarily.

## XRUN recovery (ctypes path)

When `snd_pcm_writei` returns `-EPIPE` (XRUN/underrun), call `snd_pcm_prepare`
and resume writing; do not abort the App. The `aplay` subprocess handles this
internally, which is one reason to prefer it for simple cues.

## Bundling the WAV

Ship the alarm sound as a bundled `kind: "data"` artifact at the package root
(see `manifest-contract.md`), with accurate `size` and `sha256`. The firmware's
own `/oem/usr/share/speaker_test.wav` is 8000 Hz, stereo, 16-bit PCM; a small,
short mono or stereo 16-bit WAV is a good alarm format. Keep the file at the
package root so Kit and AppMgr resolve one identical path and so Windows
packaging does not introduce sub-directory separators (see `app-packaging.md`).

## Two-phase verification

Label these separately in the report; never merge them.

- **Static / read-only** (safe to run automatically): `aplay` present,
  `libasound.so.2` present, `aplay -l` lists a real playback card,
  `/dev/snd/pcmC*D*p` group/mode, the WAV file exists and its format, the
  playback config in `/etc/asound.conf`, whether another process already holds
  the playback device, and the App's uid/gid/audio membership.
- **Active hardware** (requires explicit user authorization, makes sound):
  actually play a WAV and confirm audible output, correct volume, and no stutter.
  Never run this automatically.

## Report wording

- Correct: "The public SDK has no speaker API; the App uses the firmware ALSA
  playback path (`aplay` -> `plughw:<card>`); the target has `aplay`,
  `libasound`, and a real playback card; audible output is pending an authorized
  hardware test."
- Wrong: claiming "speaker works" from hardware presence alone, or claiming "the
  device cannot play audio" from the missing SDK API alone.
