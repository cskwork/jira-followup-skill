#!/usr/bin/env python3
"""tick 실행을 Paseo 세션 밖으로 떼어 내고 tick 장부(ticks.log)를 남긴다.

jira-daily-wake가 인자 없이(tick) 불릴 때만 쓴다.
- detach <wake>: 새 세션(start_new_session)으로 `run`을 띄우고 바로 `wake=detached ...`를 출력한다.
  luna의 exec가 30초 만에 빈 출력으로 끝나도 가드·오케스트레이터 실행이 죽지 않게 한다(실측: 그렇게 끝난 tick의 약 30%가 기록 없이 사라졌다).
- run <wake>: 장부를 읽어 이전 슬롯의 lost-tick을 일지에 남기고, start를 쓰고, JIRA_WAKE_CHILD=1로 wake를 실행한 뒤 end를 쓴다.
"""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR)
SLOT = dt.timedelta(minutes=30)
STALE_SECONDS = 1800


def now_kst():
    if os.environ.get("JIRA_WAKE_AT"):
        now = dt.datetime.fromisoformat(os.environ["JIRA_WAKE_AT"])
        return now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    return dt.datetime.now(KST)


def slot_of(at):
    return at.replace(minute=(at.minute // 30) * 30, second=0, microsecond=0)


def read_events(stream):
    stream.seek(0)
    events = []
    for line in stream:
        try:
            item = json.loads(line)
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                if dt.datetime.fromisoformat(item["at"]).tzinfo is not None:
                    events.append(item)
        except (ValueError, KeyError, TypeError):
            continue  # A killed writer's partial record is not start/end evidence.
    return events


def lost_slots(events, now):
    """Earlier weekday slots (08:00~18:30) since the ledger began with no start, or a start without end for 30+ min."""
    if now.weekday() >= 5 or not events:
        return []  # No ledger yet: slots before it existed are unknown, not lost.
    first = min(slot_of(dt.datetime.fromisoformat(e["at"]).astimezone(KST)) for e in events)
    ended = {e.get("id") for e in events if e.get("event") == "end"}
    earlier = max(now.replace(hour=8, minute=0, second=0, microsecond=0), first)
    last = now.replace(hour=18, minute=30, second=0, microsecond=0)
    result = []
    while earlier < slot_of(now) and earlier <= last:
        label = earlier.isoformat()
        attempts = [e for e in events if e.get("slot") == label and e.get("event") == "start"]
        stale = any(e.get("id") not in ended and
                    (now - dt.datetime.fromisoformat(e["at"])).total_seconds() > STALE_SECONDS
                    for e in attempts)
        reason = "no-start" if not attempts else "start-without-end" if stale else ""
        if reason:
            result.append((label, reason))
        earlier += SLOT
    return result


def record(stream, event, outcome, now, identity):
    stream.seek(0)
    existing = stream.read()
    stream.seek(0, 2)
    if existing and not existing.endswith("\n"):
        stream.write("\n")
    at = now if event == "start" else dt.datetime.now(KST)
    stream.write(json.dumps(dict(at=at.isoformat(), slot=slot_of(now).isoformat(), pid=os.getpid(),
                                 id=identity, event=event, outcome=outcome)) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def run(wake):
    now = now_kst()
    ROOT.mkdir(parents=True, exist_ok=True)
    ledger = ROOT / "ticks.log"
    identity = f"{now.isoformat()}:{os.getpid()}"
    with ledger.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        journal = ROOT / f"followup-{now.date().isoformat()}.md"
        previous = journal.read_text() if journal.exists() else ""
        for label, reason in lost_slots(read_events(stream), now):
            if f"lost-tick: {label} " not in previous:
                with journal.open("a") as output:
                    output.write(f"{now.isoformat()} | — | — | 스캔 | — | lost-tick: {label} {reason} | none | ticks.log\n")
        record(stream, "start", "running", now, identity)

    code, outcome = 1, "error"
    try:
        code = subprocess.call(["/bin/bash", wake], env=dict(os.environ, JIRA_WAKE_CHILD="1"))
        outcome = f"exit={code}"
    finally:
        with ledger.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            record(stream, "end", outcome, now, identity)
    return code


def detach(wake):
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"wake-{dt.datetime.now(KST):%Y%m%d}.log"
    with log.open("a") as output:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", wake],
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True)
    print(f"wake=detached pid={child.pid} log={log}")
    return 0


def main(argv):
    if len(argv) != 3 or argv[1] not in ("run", "detach"):
        print("usage: wake_supervisor.py run|detach <jira-daily-wake path>", file=sys.stderr)
        return 2
    wake = str(Path(argv[2]).resolve())
    return run(wake) if argv[1] == "run" else detach(wake)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
