"""시험용 가짜 sdlc(status --json, check ship).

FAKE_SDLC_STATE: {"<slug>": {"phase": ..., "state": ...} | "error" | "sleep"} JSON 파일.
FAKE_SDLC_SHIP: {"<slug>": "open" | "<닫힌 사유>"} JSON 파일.
FAKE_SDLC_DREAM: due | not-due(기본) | missing(옛 kit: unknown command, exit 2) | error(exit 3). dream --check·dream(브리프).
"""
import json
import os
import sys
import time

args = sys.argv[1:]
if args[:1] == ["status"]:
    slug = args[1]
    row = json.load(open(os.environ["FAKE_SDLC_STATE"])).get(slug)
    if row is None:
        print(f"sdlc: feature not found: {slug}", file=sys.stderr)
        sys.exit(2)
    if row == "error":
        print("garbage")
        sys.exit(0)
    if row == "sleep":
        time.sleep(30)
        sys.exit(0)
    print(json.dumps({"store": "/fake/.sdlc", "human_gates": "", "features": [
        {"slug": slug, "action": "-", "score": "", "route": "", "returns_plan": 0, "returns_build": 0,
         "review_fails": 0, **row}]}))
elif args[:2] == ["check", "ship"]:
    value = json.load(open(os.environ["FAKE_SDLC_SHIP"])).get(args[2], "feature not found")
    if value == "open":
        print("GATE OPEN")
    else:
        print(f"GATE CLOSED: {value}")
        sys.exit(1)
elif args[:1] == ["dream"]:
    mode = os.environ.get("FAKE_SDLC_DREAM", "not-due")
    if mode == "missing":
        print("sdlc: unknown command: dream", file=sys.stderr)
        sys.exit(2)
    if mode == "error":
        print("sdlc: no memory folder", file=sys.stderr)
        sys.exit(3)
    if args[1:] == ["--check"]:
        if mode == "due":
            print("due: 3/3 closes since 2026-11-01T00:00:00Z")
            sys.exit(10)
        print("not due: 1/3 closes since 2026-11-01T00:00:00Z")
    else:
        print("# Role: Dreamer (fake brief)\n\n## This run\n\n- Memory folder: /fake/memory")
else:
    sys.exit(2)
