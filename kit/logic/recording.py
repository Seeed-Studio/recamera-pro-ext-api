"""Application-owned recording decisions; never infer them from display events."""
import math


def request_configured_recording(app, event_kind, pts):
    """Gate an explicit business edge with live opt-in and per-kind cooldown.

    Defaults are off. Only successful local queueing starts the cooldown; this
    is not a recording acknowledgement. At most the declared event kinds are
    retained, and presentation/ML state is unaffected by configuration changes.
    """
    config = getattr(app, "config", {}) or {}
    if config.get("recording_enabled") is not True:
        return False
    defaults = {"blink": False, "yawn": False, "drowsiness": True}
    if event_kind in defaults and config.get(
            "recording_" + event_kind, defaults[event_kind]) is not True:
        return False
    try:
        now = float(pts)
        cooldown = float(config.get("recording_cooldown_sec", 10))
    except (ValueError, TypeError):
        return False
    if not math.isfinite(now) or not math.isfinite(cooldown):
        return False
    cooldown = max(0.0, min(3600.0, cooldown))
    previous = getattr(app, "_recording_last_requests", {})
    last = previous.get(event_kind)
    if last is not None and 0 <= now - last < cooldown:
        return False
    attempts = getattr(app, "_recording_failed_attempts", {})
    failed_at = attempts.get(event_kind)
    if failed_at is not None and 0 <= now - failed_at < 1.0:
        return False
    if not app.request_recording(event_kind, now):
        if len(attempts) >= 32 and event_kind not in attempts:
            attempts = {}
        attempts[event_kind] = now
        app._recording_failed_attempts = attempts
        return False
    attempts.pop(event_kind, None)
    if len(previous) >= 32 and event_kind not in previous:
        previous = {}
    previous[event_kind] = now
    app._recording_last_requests = previous
    return True
