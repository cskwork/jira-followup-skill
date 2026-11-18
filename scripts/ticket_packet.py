#!/usr/bin/env python3
"""티켓 오케스트레이터가 처음 읽는 키 하나의 요약(packet). 읽기 전용이며 아무 파일도 쓰지 않는다.

Usage: ticket_packet.py <KEY> [--dir <state_dir>] [--notes N] [--decisions N] [--no-sdlc] [--markdown]

배경(2026-11-16 맥락 줄이기): 티켓 오케스트레이터가 첫 상태 변경 전에 큐·state·approvals·decisions 전체 파일을
여러 번 읽어 중앙값 약 75K 토큰을 썼다. packet은 그 키에 필요한 것만 담고 전체 파일은 `links`의 경로로 남긴다.
- queue: 큐 항목. note는 마지막 N 덩어리(' | '·줄바꿈 단위)와 첫 덩어리(`noteHead`)만 싣고 나머지 수는
  `noteOmitted`로 알린다. 상한을 넘어 옮긴 기록은 `links.notes`(notes/<KEY>.md)에 있다.
- lifecycle: 배포 단계(`lifecycle.delivery`)와 sdlc 개발 단계(`sdlc.sh status --json`, 실패는 error 문자열).
- requirements: 이 키의 가장 최근 `runs/*/<KEY>-*.requirements.json` 요약(stage·검사 이름·event commentIds).
- approvals·lease·inbox(아직 handled 아닌 줄)·handled(이 키 기록 수와 최근 5건)·decisions(최근 N줄).
출력: JSON(기본) 또는 `--markdown`. exit 0 성공, 1 상태 파일 해석 실패, 2 잘못된 사용.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import followup_config as cfg
import lifecycle

KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")
TEXT_LIMIT = 300


def read_json(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected object: " + str(path))
    return value


def short(value, limit=TEXT_LIMIT):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def note_pieces(note):
    pieces = [piece.strip() for line in str(note or "").split("\n") for piece in line.split(" | ")]
    return [piece for piece in pieces if piece]


def latest_requirements(directory, key):
    rows = [path for path in (directory / "runs").glob(f"*/{key}-*.requirements.json") if path.is_file()]
    if not rows:
        return None
    path = max(rows, key=lambda row: row.stat().st_mtime)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"path": str(path), "error": type(error).__name__}
    if not isinstance(value, dict):
        value = {}
    summary = {"path": str(path), "stage": value.get("stage"),
               "checks": [row.get("name") for row in value.get("checks") or [] if isinstance(row, dict)]}
    event = value.get("event")
    if isinstance(event, dict):
        summary["eventCommentIds"] = event.get("commentIds")
    return summary


def recent_decisions(path, key, count):
    rows = []
    if not path.exists() or count <= 0:
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("key") == key:
            rows.append({name: short(row[name]) if isinstance(row[name], str) else row[name]
                         for name in ("at", "kind", "title", "option", "by", "note", "ok") if name in row})
    return rows[-count:]


def inbox_rows(path, handled):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            rows.append({"invalid": short(line, 120)})
            continue
        if isinstance(row, dict) and row.get("commentId") not in handled:
            rows.append({"commentId": row.get("commentId"), "source": row.get("source", "jira"),
                         "author": row.get("author"), "text": short(row.get("text", ""))})
    return rows


def sdlc_status(slug, enabled):
    if not enabled or not slug:
        return None
    try:
        row = lifecycle.Sdlc().status(slug)
    except lifecycle.SdlcError as error:
        return {"error": str(error)}
    return {name: row.get(name) for name in ("phase", "state", "next") if name in row} if row else None


def build(directory, key, *, notes=5, decisions=3, sdlc=True):
    queue = read_json(directory / "followup-queue.json")
    state = read_json(directory / "state.json")
    approvals = read_json(directory / "approvals.json")
    item = queue.get(key) if isinstance(queue.get(key), dict) else None
    packet = {"key": key, "queue": None, "lifecycle": None}
    if item is not None:
        shown = {name: value for name, value in item.items() if name != "note"}
        pieces = note_pieces(item.get("note"))
        shown["noteTail"] = pieces[-notes:] if notes else []
        if len(pieces) > notes:
            # 옛 기록 일부는 새 기록을 앞에 덧붙였다(state_update가 이제 뒤로 옮긴다). 첫 덩어리도 함께 싣는다.
            shown["noteHead"] = short(pieces[0], 600)
            shown["noteOmitted"] = len(pieces) - notes - 1
        packet["queue"] = shown
        try:
            delivery = lifecycle.delivery(item)
        except ValueError as error:
            delivery = "invalid: " + str(error)
        packet["lifecycle"] = {"delivery": delivery, "sdlc": sdlc_status(item.get("slug"), sdlc)}
    handled = state.get("followup", {}).get("handled", {}) if isinstance(state.get("followup"), dict) else {}
    mine = sorted(((cid, row) for cid, row in handled.items() if isinstance(row, dict) and row.get("key") == key),
                  key=lambda pair: str(pair[1].get("at", "")))
    packet["handled"] = {"count": len(mine), "recent": [dict(commentId=cid, **{name: short(row[name])
                         for name in ("action", "at", "slug") if row.get(name)}) for cid, row in mine[-5:]]}
    packet["inbox"] = inbox_rows(directory / "tickets" / f"{key}.inbox.jsonl", handled)
    packet["approval"] = approvals.get(key)
    packet["decisions"] = recent_decisions(directory / "decisions.jsonl", key, decisions)
    lease = directory / "tickets" / f"{key}.json"
    packet["lease"] = read_json(lease) if lease.exists() else None
    packet["requirements"] = latest_requirements(directory, key)
    links = {name: str(directory / name) for name in
             ("followup-queue.json", "state.json", "approvals.json", "decisions.jsonl")}
    for name, path in (("notes", directory / "notes" / f"{key}.md"), ("inbox", directory / "tickets" / f"{key}.inbox.jsonl"),
                       ("lease", lease)):
        if path.exists():
            links[name] = str(path)
    if item and item.get("slug"):
        links["sdlcWork"] = str(lifecycle.root() / ".sdlc" / "work" / str(item["slug"]))
    packet["links"] = links
    return packet


def markdown(packet):
    lines = [f"# {packet['key']} packet"]
    for name, value in packet.items():
        if name != "key":
            lines.append(f"- {name}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key")
    parser.add_argument("--dir", type=Path, default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    parser.add_argument("--notes", type=int, default=5)
    parser.add_argument("--decisions", type=int, default=3)
    parser.add_argument("--no-sdlc", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)
    if not KEY.fullmatch(args.key) or args.notes < 0 or args.decisions < 0:
        print("ticket_packet: invalid key or count", file=sys.stderr)
        return 2
    try:
        packet = build(args.dir.expanduser(), args.key, notes=args.notes, decisions=args.decisions,
                       sdlc=not args.no_sdlc)
    except (OSError, ValueError) as error:
        print("ticket_packet: " + str(error), file=sys.stderr)
        return 1
    print(markdown(packet) if args.markdown else json.dumps(packet, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
