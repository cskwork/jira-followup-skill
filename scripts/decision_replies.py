#!/usr/bin/env python3
"""결정 수신기: 60초마다 할 일 앱 답글을 결정으로 기록하고(decision_inbox.poll), 죽은 결정 페이지를 만료시킨다.

Teams 자기 대화 답장(`D12 1`)은 받지 않는다(사용자 지시 2026-11-03: 결정은 Teams로 보내지 않는다).
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import decision_inbox
import followup_config as cfg
from decision_page import process_start_time
from state_update import atomic_json, directory_argument, locked

POLL_SECONDS = 60
IDLE_SECONDS = 2 * 60 * 60


def timestamp(value):
    """오프셋 없는 시각은 계정/알림 시각 대조에 사용하지 않는다."""
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def _now(value=None):
    value = value() if callable(value) else value
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("now must have a timezone")
    return result.astimezone(timezone.utc)


def _object(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object: " + str(path))
    return value


@contextmanager
def watcher_lock(directory):
    pages = Path(directory) / "decision-pages"
    pages.mkdir(parents=True, exist_ok=True)
    with (pages / ".replies.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def expire_dead_pages(directory, now):
    pages = Path(directory) / "decision-pages"
    with locked(pages):
        registry = _object(pages / "codes.json")
        changed = False
        for row in registry.values():
            if not isinstance(row, dict) or row.get("state") != "open":
                continue
            pid, started = row.get("pid"), row.get("pidStart")
            # 이전 레지스트리에는 프로세스 신원이 없어 사망으로 단정할 수 없다.
            if type(pid) is not int or pid <= 0 or not isinstance(started, str) or not started.strip():
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead = True
            except OSError:
                continue
            else:
                try:
                    current_start = process_start_time(pid)
                except (OSError, subprocess.SubprocessError):
                    continue
                dead = current_start is not None and current_start != started
            if dead:
                row.update(state="expired", terminalAt=now.isoformat(), by="watcher", reason="page-dead")
                changed = True
        if changed:
            atomic_json(pages / "codes.json", registry)
        return registry


def poll(directory, now=None):
    """할 일 화면이면 앱 답글을 처리한다. html이면 받을 답글이 없다."""
    del now
    try:
        if decision_inbox.surface(directory) != "html":
            decision_inbox.poll(directory)
    except Exception as error:  # 한 회차 실패(할 일 CLI 등)가 수신기를 끝내지 않는다. 다음 회차가 이어서 처리한다.
        print(f"todo poll failed: {type(error).__name__}: {str(error)[:300]}", flush=True)


CODE = [Path(__file__).resolve(), Path(decision_inbox.__file__).resolve(),
        Path(decision_inbox.__file__).resolve().with_name("decision_page.py")]


def code_stamp():
    return [path.stat().st_mtime_ns if path.exists() else 0 for path in CODE]


def watch(directory, *, sleep=time.sleep, clock=None, poller=None, restart=None):
    """코드가 바뀌면 새 코드로 다시 시작한다(2026-11-03: 11:00에 뜬 수신기가 11:13 수정 전 코드로 '승인'을 계속 거절)."""
    directory = Path(directory)
    poller = poller or poll
    loaded = code_stamp()
    with watcher_lock(directory) as acquired:
        if not acquired:
            return 0
        started = _now(clock)
        while True:
            current = _now(clock)
            registry = expire_dead_pages(directory, current)
            entries = [row for row in registry.values() if isinstance(row, dict)]
            terminals = [timestamp(row.get("terminalAt")) for row in entries
                         if row.get("state") in ("decided", "expired")]
            terminals = [stamp for stamp in terminals if stamp is not None]
            last_terminal = max(terminals) if terminals else started
            if not any(row.get("state") == "open" for row in entries) and (current - last_terminal).total_seconds() >= IDLE_SECONDS:
                return 0
            if code_stamp() != loaded:
                print("decision code changed; restarting watcher", flush=True)
                return (restart or _restart)()
            poller(directory, now=current)
            sleep(POLL_SECONDS)


def _restart():
    # flock은 O_CLOEXEC fd라 exec와 함께 풀리고, 새 프로세스가 다시 잡는다.
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["watch"])
    parser.add_argument("--dir", type=directory_argument, default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    args = parser.parse_args(argv)
    return watch(args.dir)


if __name__ == "__main__":
    raise SystemExit(main())
