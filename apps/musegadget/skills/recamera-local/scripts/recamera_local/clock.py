"""Clock repair and timezone discovery (`probe_timezone`, `sync_system_time`).

Two facts shape this module:

* Fixing the clock never requires knowing the timezone first. The clock is
  UTC seconds; the timezone only relabels display. Sync the clock first and
  treat the timezone as a separate, independently-verifiable question.
* Every probe here is broken-clock safe: NTP is plain UDP, HTTP ``Date``
  headers and the geo-IP lookup use plain HTTP (no TLS — a wrong clock makes
  certificate validation fail), WiFi country IEs (802.11d) are passive RF
  beacons, and DHCP timezone options (RFC 4833, options 100/101) come from the
  local broadcast domain. None of them retry-storm: each is a single bounded
  attempt.

Everything runs on the camera itself, so the local clock is *suspect*, never
a reference: "now" is measured via SNTP, then plain-HTTP Date headers, and if
both fail the honest answer is to ask the user.
"""

from __future__ import annotations

import email.utils
import http.client as http_client
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ._errors import RecameraError
from ._tzmap import timezones_for_country
from .status import get_system_time, set_system_time

__all__ = ["probe_timezone", "sync_system_time"]

DEFAULT_GEOIP_URL = "http://ip-api.com/json/?fields=status,countryCode,timezone"
# Plain-HTTP endpoints that answer with a `Date` header even when the client
# clock is broken (used as a coarse, TLS-independent time reference).
DEFAULT_HTTP_DATE_URLS = (
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://www.msftconnecttest.com/connecttest.txt",
    "http://detectportal.firefox.com/success.txt",
)
NTP_EPOCH_OFFSET = 2_208_988_800  # seconds between 1900-01-01 and 1970-01-01


# MARK: Low-level probes (each bounded, each broken-clock safe)

def _resolve_host(server: str, port: int, timeout: float) -> tuple:
    """getaddrinfo with a hard bound — a hung resolver must not wedge a probe."""
    box: Dict[str, Any] = {}

    def _job() -> None:
        try:
            box["addr"] = socket.getaddrinfo(
                server, port, socket.AF_UNSPEC, socket.SOCK_DGRAM
            )[0][4]
        except OSError as exc:
            box["err"] = exc

    t = threading.Thread(target=_job, daemon=True)
    t.start()
    t.join(timeout)
    if "addr" in box:
        return box["addr"]
    if "err" in box:
        raise box["err"]
    raise RecameraError(f"DNS lookup for {server!r} timed out after {timeout:.0f}s")


def _sntp_query(server: str, port: int = 123, timeout: float = 4.0) -> Tuple[float, float]:
    """One SNTP round-trip; returns (unix_time, rtt_seconds). UDP only."""
    addr = _resolve_host(server, port, timeout)
    family = socket.AF_INET6 if len(addr) == 4 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        pkt = bytearray(48)
        pkt[0] = 0x23  # LI=0, VN=4, Mode=3 (client)
        t0 = time.time()
        sock.sendto(bytes(pkt), addr)
        data, _ = sock.recvfrom(48)
        rtt = time.time() - t0
    finally:
        sock.close()
    if len(data) < 48:
        raise RecameraError(f"short NTP response from {server} ({len(data)} bytes)")
    secs, frac = struct.unpack("!II", data[40:48])
    if secs == 0:
        raise RecameraError(f"empty NTP timestamp from {server}")
    return secs - NTP_EPOCH_OFFSET + frac / 2**32, rtt


def _bounded(fn: Any, timeout: float, what: str) -> Any:
    """Run ``fn()`` in a daemon thread with a hard ceiling.

    ``http.client`` inherits libc's unbounded ``getaddrinfo`` — a stalling
    resolver would otherwise hang a probe forever. On timeout the orphan
    thread finishes whenever libc returns; the caller moves on.
    """
    box: Dict[str, Any] = {}

    def _job() -> None:
        try:
            box["ok"] = fn()
        except Exception as exc:  # re-raised verbatim below
            box["err"] = exc

    t = threading.Thread(target=_job, daemon=True)
    t.start()
    t.join(timeout)
    if "ok" in box:
        return box["ok"]
    if "err" in box:
        raise box["err"]
    raise RecameraError(f"{what} timed out after {timeout:.0f}s")


def _http_date(url: str, timeout: float = 4.0) -> Tuple[float, float]:
    """Fetch the ``Date`` header of a plain-HTTP URL; (unix_time, rtt_seconds).

    ``https://`` URLs are refused on purpose: with a broken clock TLS
    validation fails, which is exactly the failure mode this probe avoids.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http":
        raise RecameraError(f"clock-safe time probe needs plain HTTP, got {url!r}")
    host = parts.hostname or ""
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    conn = http_client.HTTPConnection(host, port, timeout=timeout)
    try:
        t0 = time.time()
        conn.request("GET", path, headers={"User-Agent": "recamera-clockprobe/1"})
        resp = conn.getresponse()
        resp.read()
        rtt = time.time() - t0
    finally:
        conn.close()
    date_hdr = resp.getheader("Date")
    if not date_hdr:
        raise RecameraError(f"no Date header from {host}")
    parsed = email.utils.parsedate_to_datetime(date_hdr)
    if parsed is None:
        raise RecameraError(f"unparseable Date header from {host}: {date_hdr!r}")
    return parsed.timestamp() + rtt / 2, rtt


def _geoip(url: str, timeout: float = 4.0) -> Dict[str, Any]:
    """Plain-HTTP IP geolocation; works regardless of the local clock."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http":
        raise RecameraError(f"clock-safe geo-IP probe needs plain HTTP, got {url!r}")
    host = parts.hostname or ""
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    conn = http_client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "recamera-tzprobe/1"})
        resp = conn.getresponse()
        data = resp.read()
    finally:
        conn.close()
    if not 200 <= resp.status < 300:
        raise RecameraError(f"geo-IP probe HTTP {resp.status} from {host}")
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RecameraError(f"geo-IP probe returned invalid JSON from {host}") from exc
    if not isinstance(payload, dict):
        raise RecameraError(f"geo-IP probe returned an unexpected payload from {host}")
    # ip-api.com: {"status":"success","countryCode":"CN","timezone":"Asia/Shanghai"}
    # ipwho.is:   {"success":true,"country_code":"CN","timezone":{"id":"..."}}
    if payload.get("status") == "success":
        tz, cc = payload.get("timezone"), payload.get("countryCode")
    elif payload.get("success") is True:
        tz = (payload.get("timezone") or {}).get("id")
        cc = payload.get("country_code") or payload.get("countryCode")
    else:
        raise RecameraError(
            f"geo-IP probe failed: {payload.get('message') or payload.get('status') or payload}"
        )
    if not tz or not isinstance(tz, str):
        raise RecameraError(f"geo-IP probe returned no timezone from {host}")
    return {"timezone": tz, "country_code": cc, "url": f"{parts.scheme}://{host}{parts.path}"}


def _wifi_interfaces() -> List[str]:
    try:
        out = subprocess.run(
            ["iw", "dev"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r"^\s*Interface\s+(\S+)", out, flags=re.MULTILINE)


def _wifi_countries(timeout: float = 20.0) -> Dict[str, int]:
    """Majority-vote the 802.11d Country IEs of nearby APs (passive RF)."""
    votes: Dict[str, int] = {}
    for iface in _wifi_interfaces():
        try:
            out = subprocess.run(
                ["iw", "dev", iface, "scan"],
                capture_output=True, text=True, timeout=timeout, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for code in re.findall(r"^\s*Country:\s*([A-Z0-9]{2})\b", out, flags=re.MULTILINE):
            if code not in ("00", "99"):  # regulatory-domain "unset" sentinels
                votes[code] = votes.get(code, 0) + 1
    if votes:
        return votes
    # Fallback: the kernel regulatory domain, if an AP's country was adopted.
    try:
        out = subprocess.run(
            ["iw", "reg", "get"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return votes
    m = re.search(r"^country\s+([A-Z0-9]{2}):", out, flags=re.MULTILINE)
    if m and m.group(1) not in ("00", "99"):
        votes[m.group(1)] = 1
    return votes


_UDHPC_PROBE_SCRIPT = """#!/bin/sh
# udhcpc event hook: dump the lease environment once, apply nothing.
env > "$1" 2>/dev/null || env > /tmp/recamera-dhcp-tz.env
exit 0
"""


def _dhcp_tz_probe(iface: str, timeout: float = 12.0) -> Dict[str, Any]:
    """One-shot RFC 4833 probe: request DHCP options 100/101 (and 2), keep nothing.

    Runs a bounded udhcpc whose script only records the environment, so the
    interface configuration is never touched. Only meaningful on interfaces
    that are actually DHCP-managed.
    """
    env_dir = tempfile.mkdtemp(prefix="recamera-tzprobe-")
    env_path = os.path.join(env_dir, "lease.env")
    script_path = os.path.join(env_dir, "probe.sh")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(_UDHPC_PROBE_SCRIPT.replace("$1", env_path))
    os.chmod(script_path, 0o755)
    try:
        subprocess.run(
            [
                "udhcpc", "-i", iface, "-n", "-q", "-t", "2", "-T", "2",
                "-o", "-O", "100", "-O", "101", "-O", "2",
                "-s", script_path,
            ],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecameraError(f"DHCP probe on {iface} failed: {exc}") from exc
    try:
        with open(env_path, encoding="utf-8", errors="replace") as fh:
            env_text = fh.read()
    except OSError as exc:
        raise RecameraError(f"DHCP probe on {iface} obtained no lease options") from exc
    env = dict(
        line.split("=", 1)
        for line in env_text.splitlines()
        if "=" in line
    )
    return _parse_dhcp_env(env)


def _decode_dhcp_string(value: str) -> Optional[str]:
    """udhcpc exports unknown options as hex (or raw); both decode to ASCII."""
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) % 2 == 0:
        try:
            raw = bytes.fromhex(value)
            text = raw.decode("ascii")
            if text.isprintable():
                return text
        except (ValueError, UnicodeDecodeError):
            pass
    return value if value.isprintable() else None


def _parse_dhcp_env(env: Dict[str, str]) -> Dict[str, Any]:
    """Extract RFC 4833 PCode/TCode (+ legacy option-2 offset) from lease env."""
    out: Dict[str, Any] = {}
    tcode = _decode_dhcp_string(env.get("opt101") or env.get("tcode") or "")
    pcode = _decode_dhcp_string(env.get("opt100") or env.get("pcode") or "")
    if tcode:
        out["tcode"] = tcode  # IANA name, e.g. "Asia/Shanghai"
    if pcode:
        out["pcode"] = pcode  # POSIX TZ string, e.g. "CST-8"
        offset = _parse_posix_tz_offset(pcode)
        if offset is not None:
            out["utc_offset_s"] = offset
    if "timezone" in env:  # busybox name for option 2: seconds EAST of UTC
        try:
            out["utc_offset_s"] = int(env["timezone"])
        except ValueError:
            pass
    return out


def _parse_posix_tz_offset(pcode: str) -> Optional[int]:
    """Seconds east of UTC from a POSIX TZ string (sign is inverted: 'CST-8'
    means UTC+8 = +28800). Returns None when unparseable."""
    m = re.match(r"^[A-Za-z<][A-Za-z0-9+\->]*?([+-]?\d{1,2})(?::(\d{2}))?(?::(\d{2}))?", pcode)
    if not m:
        return None
    hours = int(m.group(1))
    minutes = int(m.group(2) or 0)
    sign = -1 if hours < 0 or m.group(1).startswith("-") else 1
    # POSIX: value is the offset ADDED to local time to get UTC.
    return -sign * (abs(hours) * 3600 + minutes * 60)


def _utc_offset_seconds(tz_name: str, at: Optional[float] = None) -> Optional[int]:
    """Current UTC offset of an IANA zone; None when tzdata is unavailable."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(at or time.time(), tz=ZoneInfo(tz_name))
        offset = dt.utcoffset()
        return int(offset.total_seconds()) if offset is not None else None
    except Exception:  # missing tzdata / unknown zone — corroboration is optional
        return None


# MARK: Aggregation

def _vote_winner(votes: Dict[str, int]) -> Optional[str]:
    if not votes:
        return None
    return max(sorted(votes), key=lambda c: votes[c])


def _dhcp_iface_guess() -> Optional[str]:
    """First interface likely to be DHCP-managed (eth0 first, then wlan0)."""
    try:
        with open("/userdata/config/rknetd.ini", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = ""
    for iface, section in (("eth0", "ethernet"), ("wlan0", "wifi")):
        if text:
            m = re.search(
                rf"\[network\.{section}\](.*?)(?=\n\[|\Z)", text, flags=re.DOTALL,
            )
            if m and re.search(r"^\s*method\s*=\s*dhcp\s*$", m.group(1), flags=re.MULTILINE):
                return iface
        else:
            try:
                with open(f"/sys/class/net/{iface}/carrier") as fh:
                    carrier = fh.read().strip()
            except OSError:
                continue
            if carrier == "1":
                return iface
    return None


def probe_timezone(
    *,
    dhcp_probe: bool = False,
    geoip_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Gather timezone evidence from clock-independent sources; changes nothing.

    Sources (each one bounded attempt, no retries):

    * ``geoip`` — plain-HTTP IP geolocation (works with a broken clock);
      yields an IANA timezone directly.
    * ``wifi_country`` — 802.11d Country IEs from visible APs,
      majority-voted; one IANA zone for single-zone countries, a candidate
      list for multi-zone ones.
    * ``dhcp`` — RFC 4833 options 100/101 from a DHCP-managed interface
      (opt-in via ``dhcp_probe=true``; skipped on static addressing).
    """
    sources: List[Dict[str, Any]] = []
    candidates: Dict[str, List[str]] = {}  # timezone -> sources backing it

    def _back(tz: str, source: str) -> None:
        candidates.setdefault(tz, [])
        if source not in candidates[tz]:
            candidates[tz].append(source)

    # 1) Plain-HTTP geo-IP: the richest single source (IANA name directly).
    url = geoip_url or DEFAULT_GEOIP_URL
    geo_cc: Optional[str] = None
    try:
        geo = _bounded(lambda u=url: _geoip(u), 6.0, "geo-IP probe")
        geo_cc = geo.get("country_code")
        entry = {
            "source": "geoip",
            "status": "ok",
            "timezone": geo["timezone"],
            "country_code": geo_cc,
            "endpoint": geo["url"],
        }
        offset = _utc_offset_seconds(geo["timezone"])
        if offset is not None:
            entry["utc_offset_s"] = offset
        sources.append(entry)
        _back(geo["timezone"], "geoip")
    except (RecameraError, OSError) as exc:
        sources.append({"source": "geoip", "status": "unavailable", "reason": str(exc)})

    # 2) WiFi country (802.11d).
    try:
        votes = _wifi_countries()
    except Exception as exc:  # never let a probe kill the report
        votes = {}
        sources.append({"source": "wifi_country", "status": "unavailable",
                        "reason": str(exc)})
    if votes:
        winner = _vote_winner(votes)
        zones = timezones_for_country(winner) if winner else None
        entry = {
            "source": "wifi_country",
            "status": "ok",
            "country_code": winner,
            "votes": votes,
        }
        if geo_cc and winner and geo_cc != winner:
            # e.g. a VPN egress abroad vs local RF — never auto-resolve.
            entry["conflicts_with_geoip"] = True
        if zones:
            entry["timezones"] = zones
            entry["ambiguous"] = len(zones) > 1
            if len(zones) == 1:
                _back(zones[0], "wifi_country")
        else:
            entry["timezones"] = None
            entry["ambiguous"] = True
        sources.append(entry)
    elif not any(s.get("source") == "wifi_country" for s in sources):
        sources.append({
            "source": "wifi_country", "status": "unavailable",
            "reason": "no WiFi country IEs seen (radio off, no APs, or iw missing)",
        })

    # 3) DHCP timezone options (opt-in).
    if not dhcp_probe:
        sources.append({
            "source": "dhcp", "status": "skipped",
            "reason": "passive capture unavailable on stock firmware; re-run with dhcp_probe=true for a one-shot lease probe",
        })
    else:
        iface = _dhcp_iface_guess()
        if iface is None:
            sources.append({
                "source": "dhcp", "status": "skipped",
                "reason": "no DHCP-managed interface found (static addressing)",
            })
        else:
            try:
                lease = _dhcp_tz_probe(iface)
                if lease:
                    entry = {"source": "dhcp", "status": "ok", "interface": iface, **lease}
                    tcode = lease.get("tcode")
                    if tcode:
                        _back(tcode, "dhcp")
                    pcode_off = lease.get("utc_offset_s")
                    if pcode_off is not None:
                        entry["offset_check"] = _offset_check(pcode_off, candidates)
                    sources.append(entry)
                else:
                    sources.append({
                        "source": "dhcp", "status": "unavailable", "interface": iface,
                        "reason": "lease carried no timezone options (100/101/2)",
                    })
            except RecameraError as exc:
                sources.append({"source": "dhcp", "status": "unavailable",
                                "interface": iface, "reason": str(exc)})

    # Aggregate.
    notes: List[str] = []
    suggested: Optional[str] = None
    needs_user = False
    geo_entry = next((s for s in sources if s.get("source") == "geoip"), None)
    wifi_entry = next((s for s in sources if s.get("source") == "wifi_country"), None)
    if (
        geo_entry
        and wifi_entry
        and geo_entry.get("status") == wifi_entry.get("status") == "ok"
        and geo_entry.get("country_code")
        and wifi_entry.get("country_code")
        and geo_entry["country_code"] != wifi_entry["country_code"]
    ):
        needs_user = True
        notes.append(
            f"geo-IP says {geo_entry['country_code']} but WiFi beacons say "
            f"{wifi_entry['country_code']} (VPN egress or misconfigured APs?) — confirm with the user"
        )
    ranked = sorted(candidates, key=lambda t: -len(candidates[t]))
    if ranked:
        suggested = ranked[0]
        if len(ranked) > 1:
            needs_user = True
            notes.append(
                "sources disagree: "
                + "; ".join(f"{tz} (via {', '.join(srcs)})" for tz, srcs in candidates.items())
            )
    else:
        wifi = next((s for s in sources if s.get("source") == "wifi_country"
                     and s.get("status") == "ok"), None)
        if wifi and wifi.get("timezones"):
            if len(wifi["timezones"]) == 1:
                suggested = wifi["timezones"][0]
            else:
                needs_user = True
                notes.append(
                    f"country {wifi['country_code']} spans several timezones: "
                    + ", ".join(wifi["timezones"])
                )
    if suggested is None and not needs_user:
        notes.append("no timezone evidence available; ask the user for their timezone")

    return {
        "perspective": "device",
        "suggested_timezone": suggested,
        "needs_user_confirmation": needs_user,
        "candidates": [
            {"timezone": tz, "via": srcs} for tz, srcs in sorted(candidates.items())
        ],
        "sources": sources,
        "notes": notes,
    }


def _offset_check(offset_s: int, candidates: Dict[str, List[str]]) -> Dict[str, Any]:
    """Corroborate a UTC offset against the IANA candidates gathered so far."""
    matches = {}
    for tz in candidates:
        off = _utc_offset_seconds(tz)
        if off is not None:
            matches[tz] = off == offset_s
    return {"utc_offset_s": offset_s, "matches": matches}


# MARK: Reference clock (never the broken local clock)

def _reference_time(
    explicit: Optional[int],
    ntp_server: Optional[str],
    ntp_port: int,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Resolve a trustworthy 'now' without trusting the possibly-broken clock.

    Order: explicit parameter → one SNTP query → plain-HTTP Date headers.
    The camera's own clock is never a reference — that would be circular.
    """
    if explicit is not None:
        return float(explicit), {"source": "parameter", "detail": "caller-provided reference_unix"}

    server = (ntp_server or "pool.ntp.org").strip()
    try:
        ts, rtt = _sntp_query(server, ntp_port)
        return ts, {"source": "sntp", "detail": f"{server}:{ntp_port}", "rtt_s": round(rtt, 3)}
    except (RecameraError, OSError) as exc:
        sntp_err = str(exc)

    dates = []
    errors = []
    for url in DEFAULT_HTTP_DATE_URLS:
        try:
            ts, rtt = _bounded(lambda u=url: _http_date(u), 6.0, f"HTTP date probe {url}")
            dates.append((ts, rtt, url))
        except (RecameraError, OSError) as exc:
            errors.append(f"{url}: {exc}")
        if len(dates) >= 2:
            break
    if dates:
        best = min(dates, key=lambda d: d[1])
        if len(dates) > 1 and abs(dates[0][0] - dates[1][0]) > 10:
            errors.append("HTTP Date sources disagree by >10s")
        ref = {"source": "http_date", "detail": best[2], "rtt_s": round(best[1], 3)}
        if errors:
            ref["warnings"] = errors
        return best[0], ref

    return None, {
        "source": "none",
        "detail": "NTP and plain-HTTP time sources are all unreachable",
        "ntp_error": sntp_err,
        "http_date_errors": errors,
    }


# MARK: Orchestration

def sync_system_time(
    *,
    timezone: Optional[str] = "auto",
    method: str = "auto",
    ntp_server: Optional[str] = None,
    tolerance_s: int = 120,
    wait_s: int = 15,
    reference_unix: Optional[int] = None,
    dhcp_probe: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fix the device clock (and optionally the timezone) in one bounded call.

    * ``timezone``: an IANA name, ``"auto"`` (probe; applied only when the
      evidence is unambiguous), or ``null`` to keep the current timezone.
    * ``method``: ``"auto"`` (NTP when reachable, manual fallback), ``"ntp"``
      (configure NTP and wait for the sync the PUT triggers), or ``"manual"``
      (set the clock to the reference time directly).
    * The reference time never comes from the local clock (which is what we
      may be repairing): it is measured via SNTP, then plain-HTTP ``Date``
      headers.

    When nothing works, no change is made and the result carries
    ``needs_user_help`` plus ``ask_user`` — relay it instead of retrying.
    """
    method = (method or "auto").strip().lower()
    if method not in ("auto", "ntp", "manual"):
        raise RecameraError(f"Invalid method {method!r}: expected auto|ntp|manual.")

    before = get_system_time()
    before_ts = int(before.get("timestamp") or 0)
    cur_ntp = before.get("ntp") or {}
    server = (ntp_server or "").strip() or cur_ntp.get("address") or "pool.ntp.org"
    try:
        port = int(cur_ntp.get("port") or 123)
    except (TypeError, ValueError):
        port = 123

    ref_ts, ref_info = _reference_time(reference_unix, server, port)
    result: Dict[str, Any] = {
        "changed": False,
        "verified": False,
        "method_used": None,
        "reference": ref_info,
        "timezone": {"before": before.get("timezone"), "requested": timezone,
                     "applied": None, "verified": None},
        "warnings": [],
        "follow_up": [],
    }
    if ref_ts is None:
        result.update({
            "needs_user_help": True,
            "ask_user": (
                "I cannot determine the real time from here: NTP is unreachable and no "
                "plain-HTTP time source answered. Please either check the network (DNS, "
                "firewall blocking UDP 123 / HTTP) or tell me the current time so I can "
                "set it manually."
            ),
            "drift_before_s": None,
        })
        return result

    drift_before = before_ts - ref_ts
    result["drift_before_s"] = round(drift_before, 2)
    mono0 = time.monotonic()

    def _ref_now() -> float:
        # The reference marches forward on a monotonic clock, immune to the
        # (possibly just-changed) system clock.
        return ref_ts + (time.monotonic() - mono0)

    need_clock = abs(drift_before) > tolerance_s

    # --- timezone decision (independent from the clock fix) ---
    tz_apply: Optional[str] = None
    if timezone == "auto":
        try:
            probe = probe_timezone(dhcp_probe=dhcp_probe)
        except Exception as exc:  # a probing bug must never block the clock fix
            probe = None
            result["warnings"].append(f"timezone probe failed (clock fix continues): {exc}")
        if probe is None:
            result["follow_up"].append(
                "timezone could not be probed — set it explicitly or ask the user"
            )
        else:
            result["timezone"]["evidence"] = {
                "suggested": probe["suggested_timezone"],
                "needs_user_confirmation": probe["needs_user_confirmation"],
                "candidates": probe["candidates"],
                "perspective": probe["perspective"],
            }
            suggested = probe["suggested_timezone"]
            if suggested and not probe["needs_user_confirmation"]:
                tz_apply = suggested
            elif suggested == before.get("timezone"):
                pass  # evidence agrees with the current timezone; nothing to ask
            elif suggested:
                result["timezone"]["needs_user_confirmation"] = True
                result["follow_up"].append(
                    "timezone evidence is ambiguous — ask the user; candidates: "
                    + ", ".join(c["timezone"] for c in probe["candidates"])
                )
            else:
                result["follow_up"].append(
                    "timezone could not be determined automatically — ask the user"
                )
    elif timezone:
        tz_apply = timezone

    need_tz = bool(tz_apply) and tz_apply != before.get("timezone")
    if (
        need_tz
        and tz_apply
        and before.get("timezone")
        and _utc_offset_seconds(tz_apply) is not None
        and _utc_offset_seconds(tz_apply) == _utc_offset_seconds(before["timezone"])
    ):
        # tzdb-equivalent alias (e.g. Europe/Brussels vs Europe/Amsterdam):
        # switching would be churn, not a correction.
        result["warnings"].append(
            f"timezone {before['timezone']!r} is behaviorally identical to "
            f"{tz_apply!r}; keeping it"
        )
        tz_apply = None
        need_tz = False

    cur_method = (before.get("method") or "").lower()
    want_ntp = method == "ntp" or (method == "auto" and ref_info.get("source") == "sntp")
    cur_server = (cur_ntp.get("address") or "").strip()
    # An explicit ntp request (or a server override) must reconfigure even when
    # the clock is currently fine; auto also upgrades a pinned-manual device
    # back to self-healing NTP when a server is reachable.
    need_method = want_ntp and (
        cur_method != "ntp"
        or (bool(ntp_server) and server != cur_server)
    )
    if not need_clock and not need_tz and not need_method:
        result["verified"] = True
        result["method_used"] = before.get("method")
        result["timezone"]["verified"] = True
        result["drift_after_s"] = round(before_ts - ref_ts, 2)
        result["ntp"] = before.get("ntp")
        return result

    if dry_run:
        if not need_clock and not need_method:
            would = cur_method if cur_method in ("ntp", "manual") else "ntp"
        else:
            would = "ntp" if want_ntp else (method if method != "auto" else "manual")
        result["dry_run"] = {
            "would_set_method": would,
            "would_set_timezone": tz_apply if need_tz else None,
            "would_set_timestamp": int(ref_ts) if need_clock else None,
        }
        return result

    # --- apply ---
    if not need_clock and not need_method:
        # Timezone-only change: keep the current sync mode (no ntp->manual
        # churn); a manual PUT reuses the device's own timestamp so the clock
        # is not touched at all.
        apply_method = cur_method if cur_method in ("ntp", "manual") else "ntp"
        method_used = apply_method
        try:
            set_system_time(
                method=apply_method,
                timezone=tz_apply,
                timestamp=before_ts if apply_method == "manual" else None,
                ntp_server=server if apply_method == "ntp" else None,
            )
            after = get_system_time()
        except RecameraError as exc:
            result["warnings"].append(str(exc))
            after = get_system_time()
    else:
        if want_ntp:
            method_used = "ntp"
        elif method == "auto":
            method_used = "manual"
        else:
            method_used = method
        try:
            if method_used == "ntp":
                set_system_time(
                    method="ntp",
                    timezone=tz_apply if need_tz else None,
                    ntp_server=server,
                )
                # The PUT triggers an immediate sync on the device; poll for it.
                deadline = time.monotonic() + max(1, wait_s)
                after = get_system_time()
                while time.monotonic() < deadline:
                    drift = int(after.get("timestamp") or 0) - _ref_now()
                    if abs(drift) <= tolerance_s:
                        break
                    time.sleep(1.5)
                    after = get_system_time()
            else:
                set_system_time(
                    method="manual",
                    timezone=tz_apply if need_tz else None,
                    timestamp=int(ref_ts),
                )
                after = get_system_time()
        except RecameraError as exc:
            # A timezone rejection raises *after* the clock part was applied;
            # surface it, keep verifying the clock, don't mask the partial success.
            result["warnings"].append(str(exc))
            after = get_system_time()
    result["method_used"] = method_used

    drift_after = int(after.get("timestamp") or 0) - _ref_now()
    result["drift_after_s"] = round(drift_after, 2)
    result["changed"] = True
    result["verified"] = abs(drift_after) <= tolerance_s
    result["timezone"]["applied"] = after.get("timezone")
    result["timezone"]["verified"] = (
        (tz_apply is None) or after.get("timezone") == tz_apply
    )
    result["ntp"] = after.get("ntp")

    if method_used == "manual" and method == "auto" and cur_method == "ntp":
        # The auto-mode stopgap PUT overwrote the sync mode; hand NTP back so
        # the device keeps correcting itself once the server is reachable.
        # (An explicit method=manual is the user's choice — leave it alone.)
        try:
            restored = set_system_time(method="ntp", ntp_server=server)
            result["method_restored"] = "ntp"
            result["ntp"] = restored.get("ntp")
            after["method"] = "ntp"
        except RecameraError as exc:
            result["warnings"].append(f"could not restore NTP mode after manual stopgap: {exc}")

    if method_used == "ntp" and not result["verified"]:
        result["warnings"].append(
            f"NTP did not sync within {wait_s}s (server {server}:{port}). "
            "The config is stored and the device keeps retrying hourly; "
            "re-run with method=manual for an immediate stopgap."
        )
        result["follow_up"].append(
            "check DNS and that outbound UDP 123 is allowed; consider a local NTP server"
        )
    if method_used == "manual":
        result["warnings"].append(
            "clock was set manually from " + ref_info.get("source", "a reference") +
            "; it will drift without NTP. Re-run sync_system_time once NTP works."
        )
    if result["verified"] and not result["timezone"].get("needs_user_confirmation"):
        result["follow_up"] = result["follow_up"] or ["none — clock verified"]
    return result


COMMANDS = {
    "probe_timezone": probe_timezone,
    "sync_system_time": sync_system_time,
}
