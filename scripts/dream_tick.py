#!/usr/bin/env python3
"""idle tick 끝에서 sdlc 메모리 정리(dreaming) 에이전트를 하루 한 번까지 띄운다(사용자 결정 2026-11-16).

jira-daily-wake가 가드의 idle 판정(`skipping duplicate (idle:`)을 출력한 뒤에만 부른다. 가드의 판정·종료 코드와 무관하고
항상 0으로 끝난다. 결과는 `<state_dir>/dream-last.json`에 남긴다.

순서(앞에서 걸리면 그 사유로 끝):
  1. `<state_dir>/dream` 첫 줄이 off → off(기록 안 함). JIRA_SDLC=off(시험 안전망) → sdlc-off(기록 안 함).
  2. 오늘(KST) 이미 확인했으면 → already-today(기록 안 함). 아래 결과는 기록하되, 오류(check·brief·launch)와
     paseo-query-failed는 `day`를 남기지 않아 다음 idle tick이 다시 시도한다. 하루 시도는 MAX_ATTEMPTS(3)회까지
     (`attempts`), 마지막 시도가 실패하면 `day`를 남기고 reason에 상한 도달을 적는다.
  3. `.dream.lock`을 다른 tick이 잡고 있으면 → locked.
  4. Paseo에 실행 중인 role=dreamer가 있으면 → dreamer-running. 끝난 dreamer는 보관한다. 조회 실패 → paseo-query-failed.
  5. `sdlc.sh dream --check`(cwd=작업 루트): 0=not due, 10=due. `unknown command`=옛 kit → kit-without-dream(일지 한 줄).
     그 밖의 코드 → check-error.
  6. due면 `sdlc.sh dream`(브리프, 이 호출이 kit의 .dream-state를 갱신한다)을 프롬프트로
     DREAM_PROVIDER/DREAM_MODEL/DREAM_THINKING/DREAM_MODE(기본 claude·claude-opus-5-5·medium·bypassPermissions) dreamer 하나를 팩토리 workspace에서 분리 실행한다(무인 최소 환경 포함).
     띄우기가 시간 초과면 Paseo를 한 번 더 보고, 실행 중인 dreamer가 있으면 그 id로 launched를 남긴다.
Usage: dream_tick.py --dir STATE_DIR
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import followup_config as cfg
import factory_env
import factory_workspace
import lifecycle
import run_lock

KST = cfg.TZ
DUE = 10  # sdlc-kit lib/store.sh dream --check: due → exit 10, not due → 0
CHECK_TIMEOUT = 30
BRIEF_TIMEOUT = 60
LAUNCH_TIMEOUT = 120  # 분리 실행이라 tick을 붙잡지 않는다. 2026-11-17 60초 시간 초과(수동 재실행 1.6초).
MAX_ATTEMPTS = 3  # 하루(KST) 시도 상한: 지속 실패가 매 tick 띄우기를 반복하지 않게 한다.


def now_kst():
    value = os.environ.get("JIRA_DREAM_AT")  # 시험용 시각
    if value:
        at = datetime.fromisoformat(value)
        return at.replace(tzinfo=KST) if at.tzinfo is None else at.astimezone(KST)
    return datetime.now(KST)


def switched_off(directory):
    try:
        words = (directory / "dream").read_text().split()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # 읽을 수 없는 끄기 파일은 끈 쪽으로 본다.
    return bool(words) and words[0].lower() == "off"


def last(directory):
    try:
        data = json.loads((directory / "dream-last.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def tick_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".dream.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def dreamer_running(archive=True):
    """실행 중인 dreamer의 id, 없으면 None. archive면 끝난(running 아님) dreamer는 보관해 사이드바에 쌓이지 않게 한다."""
    rows = json.loads(run_lock.call("agent", "ls", "--json", "--label", "factory=jira-followup",
                                    "--label", "role=dreamer").stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                                         for row in rows):
        raise ValueError("invalid agent list")
    running = [row["id"] for row in rows if row.get("status") == "running"]
    if running:
        return running[0]
    if not archive:
        return None
    for row in rows:
        try:
            run_lock.call("agent", "archive", row["id"])
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def sdlc(*args, timeout):
    return subprocess.run(["bash", str(lifecycle.kit() / "sdlc.sh"), "dream", *args], cwd=str(lifecycle.root()),
                          capture_output=True, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def launch(directory, brief):
    root = os.environ.get("JIRA_WAKE_CWD") or str(cfg.WORKSPACE)
    workspace = factory_workspace.lookup(directory, Path(root))
    command = ["env", "-u", "PASEO_AGENT_ID", "-u", "PASEO_WORKSPACE_ID", "-u", "JIRA_WAKE_CHILD",
               cfg.get("PASEO_BIN", "paseo"), "run", "--no-wait", "--provider", cfg.DREAM_PROVIDER,
               "--model", cfg.DREAM_MODEL, "--thinking", cfg.DREAM_THINKING, "--mode", cfg.DREAM_MODE, "--cwd", root]
    command += factory_env.paseo_args(directory)
    if workspace:
        command += ["--workspace", workspace]
    command += ["--title", "sdlc dreamer " + now_kst().strftime("%m-%d %H:%M"),
                "--label", "factory=jira-followup", "--label", "role=dreamer", "-q", brief]
    result = subprocess.run(command, text=True, capture_output=True, timeout=LAUNCH_TIMEOUT, check=True)
    identity = result.stdout.strip()
    if not run_lock.valid_token(identity):
        raise ValueError("launcher returned invalid agent id")
    return identity


def tick(directory):
    """(outcome, record). record가 None이면 dream-last.json을 건드리지 않는다."""
    if switched_off(directory):
        return "off", None
    if not lifecycle.enabled():
        return "sdlc-off", None
    today = now_kst().date().isoformat()
    if last(directory).get("day") == today:
        return "already-today", None
    with tick_lock(directory) as held:
        if not held:
            return "locked", None
        previous = last(directory)
        if previous.get("day") == today:  # 잠금을 기다리는 사이 다른 tick이 끝냈다.
            return "already-today", None
        attempts = previous.get("attempts")
        count = attempts.get("n", 0) if isinstance(attempts, dict) and attempts.get("day") == today else 0
        count = (count if isinstance(count, int) else 0) + 1
        record = {"at": now_kst().isoformat(), "day": today, "attempts": {"day": today, "n": count}}

        def retry(outcome, reason):
            """오늘을 소비하지 않는 실패. 상한에 닿으면 오늘을 소비하고 그 사실을 reason에 남긴다."""
            failed = {**record, "outcome": outcome, "reason": reason}
            if count >= MAX_ATTEMPTS:
                failed["reason"] += f"; attempt cap {count}/{MAX_ATTEMPTS} reached, no retry today"
            elif previous.get("day"):
                failed["day"] = previous["day"]
            else:
                del failed["day"]
            return outcome, failed

        try:
            running = dreamer_running()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return retry("skipped", "paseo-query-failed: " + type(error).__name__)
        if running:
            return "skipped", {**record, "outcome": "skipped", "reason": "dreamer-running"}
        try:
            check = sdlc("--check", timeout=CHECK_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            return retry("error", "check: " + type(error).__name__)
        output = (check.stdout + check.stderr).strip()
        due = output.splitlines()[-1] if output else ""
        record["due"] = due
        if "unknown command" in output:
            return "skipped", {**record, "outcome": "skipped", "reason": "kit-without-dream"}
        if check.returncode == 0:
            return "not-due", {**record, "outcome": "not-due"}
        if check.returncode != DUE:
            return retry("error", f"check exit {check.returncode}")
        try:
            brief = sdlc(timeout=BRIEF_TIMEOUT)
            if brief.returncode != 0 or not brief.stdout.strip():
                return retry("error", f"brief exit {brief.returncode}")
            identity = launch(directory, brief.stdout)
        except subprocess.TimeoutExpired as error:
            try:  # 띄우기가 느렸을 뿐 dreamer는 떴을 수 있다. 막 뜬 에이전트를 보관하지 않게 archive=False.
                identity = dreamer_running(archive=False)
            except (OSError, ValueError, subprocess.SubprocessError):
                identity = None
            if not identity:
                return retry("error", "launch: " + type(error).__name__)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return retry("error", "launch: " + type(error).__name__)
        return "launched", {**record, "outcome": "launched", "agentId": identity}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)
    directory = args.dir.expanduser()
    try:
        outcome, record = tick(directory)
        if record is not None:
            run_lock.atomic_json(directory / "dream-last.json", record)
            if outcome in ("launched", "error") or record.get("reason") == "kit-without-dream":
                detail = record.get("agentId") or record.get("reason", "")
                run_lock.journal(directory, f"dream-{outcome}: {detail} ({record.get('due', '')})")
        print(f"dream: {outcome}" + (f" {record.get('agentId') or record.get('reason') or record.get('due', '')}"
                                     if record else ""))
    except Exception as error:  # 가드 흐름을 절대 깨지 않는다.
        print(f"dream: error {type(error).__name__}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
