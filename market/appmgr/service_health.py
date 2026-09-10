"""Bounded, stdlib-only readiness probes for the firmware init scripts.

These test the platform services themselves. IPC readiness belongs to each
application's admission checks, so a missing camera never blocks appmgr.
"""
from __future__ import annotations

import argparse
import http.client
import json
import signal


def probe_appmgr(host: str = "127.0.0.1", port: int = 8130,
                 timeout: float = 1.0) -> bool:
    # HTTPConnection ignores HTTP_PROXY and cannot redirect the local probe.
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/health", headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        body = response.read(4097)
        if len(body) > 4096:
            return False
        payload = json.loads(body)
        return (isinstance(payload, dict)
                and payload.get("service") == "appmgr"
                and payload.get("ready") is True)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _deadline(_signum, _frame):
    raise TimeoutError("service readiness probe timed out")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("inferenced", "appmgr"))
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--socket", default="/run/recamera/inferenced.sock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8130)
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 10:
        parser.error("--timeout must be between 0 and 10 seconds")

    # A socket timeout applies to individual reads. Bound the complete probe
    # too, including peers that keep sending partial headers or bodies.
    previous = signal.signal(signal.SIGALRM, _deadline)
    signal.setitimer(signal.ITIMER_REAL, args.timeout)
    try:
        if args.service == "inferenced":
            from .resources import probe_inference_service
            ready = probe_inference_service(args.socket, args.timeout)["available"]
        else:
            ready = probe_appmgr(args.host, args.port, args.timeout)
        return 0 if ready else 1
    except (OSError, ValueError):
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
