"""
CLI entry: python3 -m appmgr <cmd>

  install <pkg.tar.gz>   validate + unpack into /userdata/local/apps/<id>/
  uninstall <id>         stop if running, clear active, rm /userdata/local/apps/<id>/
                         (shared /userdata/local/models is NOT removed)
  start   <id>           start one managed app without stopping compatible apps
  restart <id>           restart one managed app with a new instance generation
  switch  <id>           legacy single-active switch
  stop    [id]           stop id (or current active)
  config  <id> [json]    get effective config (schema+values); with json, set it
  list                   JSON list of installed apps (+ running/active)
  serve                  run the loopback HTTP API (127.0.0.1:8130)

Thin wrapper over server.py functions so CLI and HTTP share one path.
"""
from __future__ import annotations

import json
import sys

from . import server


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "list":
            print(json.dumps(server.do_list(), indent=2))
        elif cmd == "install":
            if not rest:
                print("usage: install <pkg.tar.gz>", file=sys.stderr); return 2
            print(json.dumps(server.do_install(rest[0]), indent=2))
        elif cmd == "uninstall":
            if not rest:
                print("usage: uninstall <id>", file=sys.stderr); return 2
            print(json.dumps(server.do_uninstall(rest[0]), indent=2))
        elif cmd == "start":
            if not rest:
                print(f"usage: {cmd} <id>", file=sys.stderr); return 2
            print(json.dumps(server.do_start(rest[0]), indent=2))
        elif cmd == "restart":
            if not rest:
                print("usage: restart <id>", file=sys.stderr); return 2
            print(json.dumps(server.do_restart(rest[0]), indent=2))
        elif cmd == "switch":
            if not rest:
                print("usage: switch <id>", file=sys.stderr); return 2
            print(json.dumps(server.do_switch(rest[0]), indent=2))
        elif cmd == "stop":
            print(json.dumps(server.do_stop(rest[0] if rest else None), indent=2))
        elif cmd == "activate":
            if not rest:
                print("usage: activate <id|builtin|none>", file=sys.stderr); return 2
            print(json.dumps(server.do_activate(rest[0]), indent=2))
        elif cmd == "config":
            if not rest:
                print("usage: config <id> [json]", file=sys.stderr); return 2
            if len(rest) >= 2:
                print(json.dumps(server.do_set_config(rest[0], json.loads(rest[1])),
                                 indent=2))
            else:
                print(json.dumps(server.do_get_config(rest[0]), indent=2,
                                 ensure_ascii=False))
        elif cmd == "serve":
            server.serve()
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            print(__doc__, file=sys.stderr)
            return 2
    except server.BusyError as e:
        print(json.dumps({"error": str(e), "code": -2}), file=sys.stderr); return 3
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
