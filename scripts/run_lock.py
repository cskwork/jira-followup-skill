#!/usr/bin/env python3
"""실행 토큰의 잠금·생존·회수 표식을 같은 상태 폴더에서 관리한다."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402


def enabled(directory):
    path = directory / "liveness"
    return not path.exists() or path.read_text().strip() != "off"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, delete=False) as stream:
            name = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def valid_token(token):
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token) is not None


def read_lock(directory):
    path = directory / "running.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not valid_token(data.get("token")):
        raise ValueError("invalid running.json")
    return data


def run_path(directory, token):
    if not valid_token(token):
        raise ValueError("invalid token")
    return directory / "runs" / token


def call(*args):
    return subprocess.run([os.environ.get("PASEO_BIN", "paseo"), *args], capture_output=True, text=True,
                          check=True, timeout=15)


def agents(token, *, key=None, archived=True):
    argv = ["agent", "ls", "--json"]
    if archived:
        argv.append("-a")
    argv += ["--label", "run=" + token]
    if key:
        argv += ["--label", "key=" + key]
    rows = json.loads(call(*argv).stdout)
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
        or row.get("status") not in ("running", "idle", "closed") for row in rows
    ):
        raise ValueError("invalid agent list")
    return rows


def has_running(rows):
    return any(row["status"] == "running" for row in rows)


def journal(directory, message):
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    with (directory / f"followup-{now.date().isoformat()}.md").open("a") as stream:
        stream.write(f"{now.isoformat()} | — | — | 스캔 | — | {message} | none | running.json\n")


def alert(directory, data, now):
    if data.get("livenessAlertAt") or not (
        data.get("queryFailures", 0) >= 3 or now - float(data["startedAt"]) > 21600
    ):
        return
    message = "liveness-alert: " + data["token"]
    data["livenessAlertAt"] = now
    journal(directory, message)
    try:
        subprocess.run(["osascript", "-e", "display notification " + json.dumps(message)
                        + ' with title ' + json.dumps(cfg.LINE_TITLE)], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def clear(directory):
    (directory / "running").unlink(missing_ok=True)
    (directory / "running.json").unlink(missing_ok=True)


def mark(directory, token, name):
    path = run_path(directory, token)
    path.mkdir(parents=True, exist_ok=True)
    (path / name).touch()


def busy(directory, now=None):
    """호출자는 .run.lock을 잡는다. 조회 실패는 잠금 유지로 판정한다."""
    now = time.time() if now is None else now
    try:
        data = read_lock(directory) if enabled(directory) else None
    except (OSError, ValueError):
        # 깨진 running.json이 매 tick exit 2로 라인을 조용히 세우지 않게 알리고 epoch 규칙을 쓴다.
        marker = directory / ".running-invalid-alerted"
        if not marker.exists():
            marker.touch()
            journal(directory, "liveness-alert: invalid running.json")
        data = None
    if data is None:
        epoch = directory / "running"
        return epoch.exists() and now - float(epoch.read_text().strip()) < 10800
    token = data["token"]
    reclaim = False
    try:
        rows = agents(token)
        if has_running(rows):
            data.pop("noLiveSince", None)
        elif not data.get("orchestratorId"):
            reclaim = now - float(data["startedAt"]) >= 600
        else:
            owner = json.loads(call("agent", "inspect", data["orchestratorId"], "--json").stdout)
            if (not isinstance(owner, dict) or type(owner.get("Archived")) is not bool
                    or owner.get("Status") not in ("running", "idle", "closed")):
                raise ValueError("invalid inspect")
            if owner["Archived"] or owner["Status"] == "closed":
                reclaim = True
            elif owner["Status"] == "idle":
                data.setdefault("noLiveSince", now)
                reclaim = now - float(data["noLiveSince"]) >= 1500
            else:
                data.pop("noLiveSince", None)
        data["queryFailures"] = 0
    except (OSError, ValueError, subprocess.SubprocessError):
        data["queryFailures"] = data.get("queryFailures", 0) + 1
    alert(directory, data, now)
    atomic_json(directory / "running.json", data)
    if not reclaim:
        return True
    mark(directory, token, "lost")
    journal(directory, "lost-run: " + token)
    with (directory / "lessons.jsonl").open("a") as stream:
        stream.write(json.dumps({"at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
            "kind": "infra_fail", "what": "lost-run: " + token,
            "evidence": str(run_path(directory, token)),
            "suggestion": "이전 작업자 로그와 검증 JSON을 대조한 뒤 키를 재선정한다."}, ensure_ascii=False) + "\n")
    clear(directory)
    return False


def owns(directory, token):
    data = read_lock(directory)
    return bool(data and token and data["token"] == token)


def release(directory, token, force=False):
    with locked(directory):
        # running.json이 없는 예전 잠금(배포 전 실행)은 토큰 없이 예전처럼 푼다.
        legacy = not (directory / "running.json").exists()
        if enabled(directory) and not force and not legacy:
            if not token:
                return 2
            if not owns(directory, token):
                return 1
        try:
            data = read_lock(directory)
        except (OSError, ValueError):
            if enabled(directory) and not force:
                raise
            data = None
        clear(directory)
        if data:
            try:
                # 해제를 부르는 오케스트레이터 자신은 아직 running이므로 작업자만 본다.
                owner = data.get("orchestratorId")
                ended = not has_running([row for row in agents(data["token"]) if row["id"] != owner])
            except (OSError, ValueError, subprocess.SubprocessError):
                ended = False
            mark(directory, data["token"], "ended" if ended else "lost")
    print("jira-daily-guard: released")
    return 0


def pending_runs(directory):
    return [path for path in sorted((directory / "runs").glob("*"))
            if path.is_dir() and valid_token(path.name) and not (path / "ended").exists()]


def reconcile_reasons(directory):
    if not enabled(directory):
        return []
    reasons = []
    for path in pending_runs(directory):
        if not (path / "lost").exists():
            continue
        try:
            if not has_running(agents(path.name)):
                reasons.append("reconcile:" + path.name)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return reasons


def blocked_by(directory, key, token=None):
    if not enabled(directory):
        return []
    if token is None:
        data = read_lock(directory)
        token = data["token"] if data else None
    blocked = []
    for path in pending_runs(directory):
        if path.name == token:
            continue
        try:
            if agents(path.name, key=key, archived=False):
                blocked.append(path.name)
        except (OSError, ValueError, subprocess.SubprocessError):
            blocked.append(path.name)
    return blocked


def archive_ended(directory):
    archived = failed = 0
    for marker in sorted((directory / "runs").glob("*/ended")):
        token = marker.parent.name
        if not valid_token(token):
            continue
        try:
            rows = agents(token, archived=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            failed += 1
            continue
        for row in rows:
            if row["status"] == "running":
                continue
            try:
                call("agent", "archive", row["id"])
                archived += 1
            except (OSError, subprocess.SubprocessError):
                failed += 1
    print(f"archived={archived} archive_failed={failed}")


def recover(directory, token):
    """로그 수집만 한다. 검증 JSON과 큐 대조 전에는 ended를 쓰지 않는다."""
    path = run_path(directory, token)
    path.mkdir(parents=True, exist_ok=True)
    rows = agents(token)
    for row in rows:
        identity = row["id"]
        if not valid_token(identity):
            raise ValueError("invalid agent id")
        (path / (identity + ".recovered.txt")).write_text(call("agent", "logs", identity).stdout)
    print(json.dumps({"token": token, "running": has_running(rows),
                      "agents": rows, "reconciled": False}))


def guard(args):
    directory = args.dir
    if args.release:
        return release(directory, args.token, args.force)
    if args.owns is not None:
        with locked(directory):
            return 0 if owns(directory, args.owns) else 1
    with locked(directory):
        if busy(directory):
            print("jira-daily-guard: skipping duplicate (running)")
            return 0
    if args.manual:
        print("manual run", flush=True)
    else:
        # Optional same-slot de-duplication helper: `<bin> <name> <seconds> -- <cmd>` prints
        # "skipping duplicate" when the same name already ran inside the window. Without it the
        # run lock above still serializes runs.
        once = Path(os.environ.get("PASEO_ONCE_BIN") or Path.home() / ".local/bin/paseo-once")
        if once.exists():
            result = subprocess.run([str(once), "jira-followup", "1500", "--", "/bin/echo", "run"],
                                    capture_output=True, text=True, check=True)
            print(result.stdout, end="", flush=True)
            if "skipping duplicate" in result.stdout:
                return 0
        else:
            print("run", flush=True)
    today = datetime.now().date().isoformat()
    done = directory / "done-today"
    mode = "tick" if done.exists() and done.read_text().strip() == today else "morning"
    if not args.manual and mode == "tick":
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("followup_idle.py")),
                                 "--dir", str(directory)], capture_output=True, text=True)
        if result.returncode == 0:
            try:
                reasons = ", ".join(json.loads(result.stdout)["reasons"])
                now = datetime.now().astimezone().isoformat()
                with (directory / f"followup-{today}.md").open("a") as stream:
                    stream.write(f"{now} | — | — | 스캔 | — | idle-skip(guard): {reasons} | none | followup-scan.json\n")
                print("jira-daily-guard: skipping duplicate (idle: events=0, no runnable work)")
                return 0
            except (OSError, ValueError, KeyError, TypeError):
                pass
    with locked(directory):
        if busy(directory):
            print("jira-daily-guard: skipping duplicate (running)")
            return 0
        now = int(time.time())
        token = uuid.uuid4().hex
        atomic_json(directory / "running.json", {"token": token, "startedAt": now,
                    "mode": mode, "liveness": "on" if enabled(directory) else "off"})
        (directory / "running").write_text(str(now) + "\n")
        run_path(directory, token).mkdir(parents=True, exist_ok=True)
    print("mode=" + mode)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("guard", "token", "attach", "archive", "worker",
                                           "recover", "reconciled", "blocked"))
    parser.add_argument("--dir", type=Path,
                        default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--owns")
    parser.add_argument("--token")
    parser.add_argument("--current-token")
    parser.add_argument("--id")
    parser.add_argument("--key")
    parser.add_argument("--unit", default="unit")
    args = parser.parse_args(argv)
    directory = args.dir.expanduser()
    args.dir = directory
    try:
        if args.action == "guard":
            return guard(args)
        if args.action == "archive":
            archive_ended(directory)
            return 0
        with locked(directory):
            if args.action == "token":
                data = read_lock(directory)
                print(data["token"] if data else "")
            elif args.action == "attach":
                if not owns(directory, args.token):
                    return 1
                data = read_lock(directory)
                data["orchestratorId"] = args.id
                atomic_json(directory / "running.json", data)
            elif args.action == "worker":
                legacy = not args.token and not (directory / "running.json").exists()
                if enabled(directory) and not legacy and (not owns(directory, args.token) or (
                    args.key and blocked_by(directory, args.key, args.token))):
                    return 3
                if args.token:
                    path = run_path(directory, args.token)
                    path.mkdir(parents=True, exist_ok=True)
                    unit = re.sub(r"[^\w.-]+", "-", args.unit).strip(".-")[:120] or "unit"
                    print(path / f"{args.key or 'no-key'}-{unit}.json")
            elif args.action == "blocked":
                blocked = blocked_by(directory, args.key, args.token)
                print(json.dumps(blocked))
                return 1 if blocked else 0
            elif args.action == "recover":
                if enabled(directory) and not owns(directory, args.current_token):
                    return 1
                recover(directory, args.token)
            elif args.action == "reconciled":
                if not owns(directory, args.current_token) or args.current_token == args.token:
                    return 1
                if has_running(agents(args.token)):
                    mark(directory, args.token, "lost")
                    return 1
                mark(directory, args.token, "ended")
        return 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        if args.action == "guard" and args.owns is not None:
            return 1
        print("run_lock: state or paseo query failed", file=sys.stderr)
        return 3 if args.action == "worker" else 2


if __name__ == "__main__":
    sys.exit(main())
