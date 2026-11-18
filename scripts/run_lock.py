#!/usr/bin/env python3
"""실행 토큰의 잠금·생존·회수 표식을 같은 상태 폴더에서 관리한다."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import gzip
import hashlib
import json
import math
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
import factory_hold  # noqa: E402
import followup_config as cfg  # noqa: E402
from state_update import directory_argument  # noqa: E402


def enabled(directory):
    path = directory / "liveness"
    return not path.exists() or path.read_text().strip() != "off"


def lanes(directory):
    path = directory / "lanes"
    value = int(path.read_text().strip()) if path.exists() else 5
    if value < 0:
        raise ValueError("invalid lanes")
    return value


def lease_path(directory, key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
        raise ValueError("invalid key")
    return directory / "tickets" / (key + ".json")


def read_lease(directory, key):
    path = lease_path(directory, key)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(data, dict) or data.get("key") != key or not valid_token(data.get("token"))
            or not valid_token(data.get("parent")) or data.get("state") not in ("launching", "live")
            or any(type(data.get(field)) not in (int, float) or not math.isfinite(data[field])
                   for field in ("startedAt", "deadline"))
            or any(field in data and (type(data[field]) not in (int, float) or not math.isfinite(data[field]))
                   for field in ('noLiveSince', 'livenessAlertAt'))
            or type(data.get('queryFailures', 0)) is not int or data.get('queryFailures', 0) < 0
            or (data.get("orchestratorId") is not None and not valid_token(data["orchestratorId"]))):
        raise ValueError("invalid ticket lease")
    return data


def lease_list(directory):
    result = []
    for path in sorted((directory / "tickets").glob("*.json")):
        try:
            data = read_lease(directory, path.stem)
            if data is not None:
                result.append(data)
        except (OSError, ValueError, TypeError):
            invalid_alert(directory, path)
    return result


def invalid_lease_keys(directory):
    """손상 파일은 목록에서 빼도 그 파일이 예약한 키는 재배분하지 않는다."""
    keys = set()
    for path in sorted((directory / "tickets").glob("*.json")):
        try:
            read_lease(directory, path.stem)
            continue
        except (OSError, ValueError, TypeError):
            invalid_alert(directory, path)
        candidates = [path.stem]
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                candidates.append(data.get('key'))
        except (OSError, ValueError):
            pass
        for key in candidates:
            try:
                lease_path(directory, key)
            except ValueError:
                continue
            keys.add(key)
    return keys


def owns_ticket(directory, token, key):
    """호출자는 .run.lock을 잡는다. launch 직후에도 같은 세대는 소유자다."""
    data = read_lease(directory, key)
    return bool(data and token and data["token"] == token and data.get("state") in ("launching", "live"))


def run_meta(directory, token):
    """호출자가 .run.lock 안에서 토큰의 실행 예산과 쓰기 범위를 확인한다."""
    data = read_lock(directory)
    if token and data and data["token"] == token:
        started = float(data["startedAt"])
        return {"kind": "global", "startedAt": started, "deadline": started + 165 * 60}
    for data in lease_list(directory):
        if token and data["token"] == token and data.get("state") in ("launching", "live"):
            return {"kind": "ticket", "key": data["key"], "startedAt": float(data["startedAt"]),
                    "deadline": float(data["deadline"])}
    raise PermissionError("run token is not owned")


def lease_conflicts(directory, key):
    """회수 전 모든 예약에 양방향 관련키·공유 브랜치·carry 충돌을 적용한다."""
    lease_path(directory, key)
    path = directory / "followup-queue.json"
    queue = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(queue, dict):
        raise ValueError("invalid queue")
    item = queue.get(key) or {}
    if not isinstance(item, dict):
        raise ValueError("invalid queue item")

    def references(row, other):
        return any(other == row.get(field) or (
            isinstance(row.get(field), list) and other in row[field])
            for field in ("relatedKeys", "carriedOn", "alsoKeys"))

    conflicts = []
    reserved = {lease['key'] for lease in lease_list(directory)} | invalid_lease_keys(directory)
    for other in sorted(reserved):
        row = queue.get(other) or {}
        if not isinstance(row, dict):
            raise ValueError("invalid queue item")
        if (other == key or references(item, other) or references(row, key)
                or (item.get("branch") and item["branch"] == row.get("branch"))):
            conflicts.append(other)
    return conflicts


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


def token_argument(value):
    if value in ('', '/'):
        raise argparse.ArgumentTypeError("token must not be empty or '/'")
    return value


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


def agents(token, *, key=None, archived=True, role=None):
    argv = ["agent", "ls", "--json"]
    if archived:
        argv.append("-a")
    argv += ["--label", "run=" + token]
    if key:
        argv += ["--label", "key=" + key]
    if role:
        argv += ['--label', 'role=' + role]
    rows = json.loads(call(*argv).stdout)
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
        or row.get("status") not in ("running", "idle", "closed", "error") for row in rows
    ):
        raise ValueError("invalid agent list")
    return rows


def has_running(rows):
    return any(row["status"] == "running" for row in rows)


def journal(directory, message):
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    with (directory / f"followup-{now.date().isoformat()}.md").open("a") as stream:
        stream.write(f"{now.isoformat()} | — | — | 스캔 | — | {message} | none | running.json\n")


def invalid_alert(directory, path):
    """손상 파일은 보존하고 경로별 경보를 프로세스 간에도 한 번만 남긴다."""
    marker = directory / ('.invalid-' + hashlib.sha256(str(path).encode()).hexdigest() + '-alerted')
    try:
        with marker.open('x'):
            pass
    except FileExistsError:
        return
    message = 'liveness-alert: invalid ' + str(path)
    journal(directory, message)
    try:
        import decision_inbox
        decision_inbox.alert_detached(f'[{cfg.LINE_TITLE} 조치 필요]\n' + message)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError):
        pass


def alert(directory, data, now):
    if data.get("livenessAlertAt") or not (
        data.get("queryFailures", 0) >= 3 or now - float(data["startedAt"]) > 21600
    ):
        return
    message = "liveness-alert: " + data["token"]
    data["livenessAlertAt"] = now
    journal(directory, message)
    try:
        import decision_inbox
        decision_inbox.alert_detached(f"[{cfg.LINE_TITLE} 조치 필요]\n공장 실행 감시 경보({message}): 상태 조회 3회 실패 또는 6시간 초과. "
                                      "jira-daily-guard 상태 확인 필요")
    except ImportError:
        pass
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


def assess(data, now):
    """공통 생존 판정. 조회 실패 때는 보유하며 관측 필드만 갱신한다."""
    reclaim = False
    try:
        rows = agents(data["token"])
        if has_running(rows):
            data.pop("noLiveSince", None)
            if data.get("state") == "launching" and now - float(data["startedAt"]) >= 600:
                owners = agents(data['token'], role='ticket-orchestrator')
                owner = next((row['id'] for row in owners if row['status'] == 'running'), None)
                if owner:
                    data['state'] = 'live'
                    data['orchestratorId'] = owner
        elif not data.get("orchestratorId"):
            reclaim = now - float(data["startedAt"]) >= 600
        else:
            owner = json.loads(call("agent", "inspect", data["orchestratorId"], "--json").stdout)
            if (not isinstance(owner, dict) or type(owner.get("Archived")) is not bool
                    or owner.get("Status") not in ("running", "idle", "closed", "error")):
                raise ValueError("invalid inspect")
            if owner["Archived"] or owner["Status"] in ("closed", "error"):
                reclaim = True
            elif owner["Status"] == "idle":
                data.setdefault("noLiveSince", now)
                reclaim = now - float(data["noLiveSince"]) >= 1500
            else:
                data.pop("noLiveSince", None)
        data["queryFailures"] = 0
    except (OSError, ValueError, subprocess.SubprocessError):
        data["queryFailures"] = data.get("queryFailures", 0) + 1
    return not reclaim


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
    alive = assess(data, now)
    alert(directory, data, now)
    atomic_json(directory / "running.json", data)
    if alive:
        return True
    mark(directory, token, "lost")
    journal(directory, "lost-run: " + token)
    # 잠금을 먼저 푼다. lessons.append는 교훈을 쓴 뒤 결정을 열다 실패하면 예외를 낸다: 그 예외가 clear() 앞에서 나면
    # 잠금이 남아 공장이 다시 깨지 않는다. 예외는 그대로 올려 이번 guard를 실패로 보이고, 다음 tick은 정상으로 돈다.
    clear(directory)
    import lessons
    lessons.append(directory, {"at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
            "kind": "infra_fail", "what": "lost-run: " + token,
            "evidence": str(run_path(directory, token)),
            # orchestratorId 없음=오케스트레이터를 띄우기 전에 wake가 죽음, 있음=띄운 오케스트레이터가 해제 없이 끝남.
            "orchestratorId": data.get("orchestratorId"), "trigger": data.get("trigger"),
            "suggestion": "이전 작업자 로그와 검증 JSON을 대조한 뒤 키를 재선정한다."})
    return False


def owns(directory, token):
    data = read_lock(directory)
    return bool(data and token and data["token"] == token)


def _lease_sweep(directory, now=None):
    """이미 .run.lock을 보유한 guard와 public sweep이 함께 사용한다."""
    now = time.time() if now is None else now
    reclaimed = []
    failure = None
    for data in lease_list(directory):
        if data.get("state") not in ("launching", "live"):
            raise ValueError("invalid lease state")
        alive = assess(data, now)
        alert(directory, data, now)
        atomic_json(lease_path(directory, data["key"]), data)
        if alive:
            continue
        token, key = data["token"], data["key"]
        mark(directory, token, "lost")
        message = "lost-lease: " + key + " " + token
        journal(directory, message)
        # 임대를 먼저 푼다(busy()와 같은 이유). 교훈 기록이 실패해도 남은 임대를 끝까지 회수하고, 첫 실패를 마지막에 올린다.
        lease_path(directory, key).unlink()
        reclaimed.append(key)
        import lessons
        try:
            lessons.append(directory, {"at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
                    "kind": "infra_fail", "what": message, "evidence": str(run_path(directory, token)),
                    "key": key, "suggestion": "이전 작업자 로그와 검증 JSON을 대조한 뒤 키를 재선정한다."})
        except Exception as error:  # 숨기지 않는다: 회수를 마친 뒤 그대로 올린다.
            failure = failure or error
    if failure is not None:
        raise failure
    return reclaimed


def lease_sweep(directory, now=None):
    with locked(directory):
        return _lease_sweep(directory, now)


class LeaseRejected(ValueError):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.payload = {"reason": reason, **details}


def lease_acquire(directory, parent, key):
    with locked(directory):
        if not owns(directory, parent):
            raise LeaseRejected("parent-not-owned")
        capacity = lanes(directory)
        if capacity == 0:
            raise LeaseRejected("lanes-off")
        held = factory_hold.blocked(directory, key)
        if held:
            journal(directory, "hold: " + key + " " + held + " (lease-acquire)")
            raise LeaseRejected("hold", hold=held)
        conflicts = lease_conflicts(directory, key)
        if conflicts:
            raise LeaseRejected("lease-conflict", keys=conflicts)
        blocked = blocked_by(directory, key, parent)
        if blocked:
            raise LeaseRejected("blocked", tokens=blocked)
        if len(lease_list(directory)) >= capacity:
            raise LeaseRejected("capacity")
        now, token = time.time(), uuid.uuid4().hex
        run_path(directory, token).mkdir(parents=True, exist_ok=True)
        atomic_json(lease_path(directory, key), {"key": key, "token": token, "parent": parent,
            "startedAt": now, "deadline": now + 165 * 60,
            "state": "launching", "orchestratorId": None})
        return token


def lease_attach(directory, key, token, identity):
    with locked(directory):
        data = read_lease(directory, key)
        if not data or data["token"] != token or data.get("state") != "launching":
            journal(directory, "lease-attach-rejected: " + str(key) + " " + str(token))
            return 1
        if not valid_token(identity):
            raise ValueError("invalid agent id")
        data.update(state="live", orchestratorId=identity)
        atomic_json(lease_path(directory, key), data)
    return 0


def lease_release(directory, key, token):
    with locked(directory):
        data = read_lease(directory, key)
        if not data or data["token"] != token:
            return 1
        try:
            ended = not has_running([row for row in agents(token) if row["id"] != data.get("orchestratorId")])
        except (OSError, ValueError, subprocess.SubprocessError):
            ended = False
        # lost는 검증 후 reconciled만 ended로 바꾼다. 늦은 release로 증적을 덮지 않는다.
        if not (run_path(directory, token) / "lost").exists():
            mark(directory, token, "ended" if ended else "lost")
        lease_path(directory, key).unlink()
    return 0


def release(directory, token, force=False):
    with locked(directory):
        if token and not owns(directory, token):
            return 1
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


# runs/ 아래 실행 토큰 폴더(uuid4().hex) 판정. 증거·덤프 폴더(morning-*, PROJ-*-before-v2 등)는 실행이 아니다.
# 가드가 그런 폴더를 끝나지 않은 실행으로 보고 키마다 paseo를 조회해 tick이 13~17분 늘었다(2026-11-09, v0.11.1).
RUN_TOKEN = re.compile(r"[0-9a-f]{32}")
RUN_MARKERS = ("ended", "lost", "archived")
# 보존 규칙(2026-11-16): 끝나고 보관된 지 7일 지난 토큰 실행의 50MB 초과 파일은 gzip으로 줄인다. 지우지 않는다.
RETAIN_DAYS = 7
GZIP_BYTES = 50 * 1024 * 1024
GZIP_PER_CALL = 3


def known_tokens(directory):
    """지금 잠금(running.json)과 티켓 임대가 가리키는 토큰. 깨진 파일은 건너뛴다(그 경보는 읽는 쪽이 낸다)."""
    tokens = set()
    try:
        data = read_lock(directory)
        if data:
            tokens.add(data["token"])
    except (OSError, ValueError):
        pass
    for path in (directory / "tickets").glob("*.json"):
        try:
            lease = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(lease, dict):
            tokens.update(value for value in (lease.get("token"), lease.get("parent")) if valid_token(value))
    return tokens


def run_folder(path, known=()):
    """실행 토큰 폴더면 True: 토큰 모양, 지금 잠금·임대의 토큰, 또는 실행 표식(ended·lost·archived·attempt-*). 아침 점검 증거 폴더도 summary.json을 가지므로 표식이 아니다."""
    if not path.is_dir() or not valid_token(path.name):
        return False
    return (RUN_TOKEN.fullmatch(path.name) is not None or path.name in known
            or any((path / name).exists() for name in RUN_MARKERS)
            or next(path.glob("attempt-*"), None) is not None)


def stray_runs(directory):
    """runs/ 아래 토큰 모양이 아닌 항목(증거 폴더·덤프 파일). 보고만 하고 건드리지 않는다.
    `<토큰>.scan.log`처럼 실행 토큰 뒤에 확장자만 붙은 파일은 그 실행의 기록이라 제외한다."""
    root = directory / "runs"
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir()
                  if RUN_TOKEN.fullmatch(path.name) is None
                  and not (path.is_file() and RUN_TOKEN.fullmatch(path.name.split(".", 1)[0])))


def pending_runs(directory):
    known = known_tokens(directory)
    return [path for path in sorted((directory / "runs").glob("*"))
            if run_folder(path, known) and not (path / "ended").exists()]


def _gzip_file(path):
    target = path.with_name(path.name + ".gz")
    if target.exists():
        raise FileExistsError(str(target))
    temporary = path.with_name("." + path.name + ".gz.tmp")
    try:
        with path.open("rb") as source, gzip.open(temporary, "wb") as out:
            while chunk := source.read(1024 * 1024):
                out.write(chunk)
        with temporary.open("rb") as written:
            os.fsync(written.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    # 압축본을 확정한 뒤에만 원본을 지운다(내용은 .gz에 남는다).
    path.unlink()


def compress_old_runs(directory, *, now=None, limit=GZIP_PER_CALL):
    """끝나고(ended) 보관된(archived) 지 7일 지난 토큰 실행 안의 50MB 초과 파일을 gzip한다. 비토큰 폴더는 건너뛴다.
    한 번에 limit개까지 하고, 다 본 실행에는 `retained` 표식을 남겨 다시 훑지 않는다. (압축 수, 실패 수)를 돌려준다."""
    now = time.time() if now is None else now
    done = failed = 0
    for marker in sorted((directory / "runs").glob("*/archived")):
        run = marker.parent
        ended = run / "ended"
        if (RUN_TOKEN.fullmatch(run.name) is None or not ended.exists() or (run / "retained").exists()
                or now - ended.stat().st_mtime < RETAIN_DAYS * 86400):
            continue
        settled = True
        for path in sorted(run.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.suffix == ".gz" or path.stat().st_size <= GZIP_BYTES:
                continue
            if done >= limit:
                return done, failed
            try:
                _gzip_file(path)
                done += 1
            except OSError:
                failed += 1
                settled = False
        if settled:
            (run / "retained").write_text(datetime.now(ZoneInfo("Asia/Seoul")).isoformat() + "\n")
    return done, failed


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
    parent = None
    if key:
        try:
            lease = read_lease(directory, key)
        except (OSError, ValueError, TypeError):
            lease = None
        if lease and lease['token'] == token:
            parent = lease['parent']
    blocked = []
    for path in pending_runs(directory):
        if path.name == token:
            continue
        try:
            rows = agents(path.name, key=key, archived=False)
            # 같은 상위 실행의 끝난 이벤트는 넘기되 실행 중인 같은 티켓은 겹치지 않는다.
            if rows and (path.name != parent or has_running(rows)):
                blocked.append(path.name)
        except (OSError, ValueError, subprocess.SubprocessError):
            blocked.append(path.name)
    return blocked


def archive_ended(directory):
    """끝난 실행의 남은 에이전트를 보관한다. 남은 것이 없음을 확인한 실행에는 `archived` 표시를 남겨 다음 깨우기에서
    다시 조회하지 않는다(2026-11-09: 끝난 실행 252개를 깨우기마다 `paseo agent ls`로 다시 훑었다)."""
    archived = failed = 0
    for marker in sorted((directory / "runs").glob("*/ended")):
        token = marker.parent.name
        if not run_folder(marker.parent) or (marker.parent / "archived").exists():
            continue
        try:
            rows = agents(token, archived=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            failed += 1
            continue
        settled = True
        for row in rows:
            if row["status"] == "running":
                settled = False
                continue
            try:
                call("agent", "archive", row["id"])
                archived += 1
            except (OSError, subprocess.SubprocessError):
                failed += 1
                settled = False
        if settled:
            (marker.parent / "archived").write_text(datetime.now(ZoneInfo("Asia/Seoul")).isoformat() + "\n")
    try:
        gzipped, gzip_failed = compress_old_runs(directory)
    except OSError:
        gzipped, gzip_failed = 0, 1
    stray = stray_runs(directory)
    print(f"archived={archived} archive_failed={failed} gzipped={gzipped} gzip_failed={gzip_failed}"
          f" non_token={len(stray)}")


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
        _lease_sweep(directory)
        # 다른 작업이 없어도 장기 보유 자원은 매 tick 경보 대상으로 관측한다.
        from resource_lock import list_locks
        list_locks(directory)
        if busy(directory):
            print("jira-daily-guard: skipping duplicate (running)")
            return 0
    refill = getattr(args, "refill", False)
    if args.manual or refill:
        print("refill run" if refill else "manual run", flush=True)
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
    mode = "tick" if refill or (done.exists() and done.read_text().strip() == today) else "morning"
    if not args.manual and mode == "tick":
        with (directory / '.idle.lock').open('a') as idle_lock:
            try:
                fcntl.flock(idle_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('jira-daily-guard: skipping duplicate (idle-check running)')
                return 0
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("followup_idle.py")),
                                     "--dir", str(directory)], capture_output=True, text=True)
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                reasons = ", ".join(payload["reasons"])
                unavailable = payload.get("monitoringStatus") in ("unavailable", "degraded")
                label = "monitoring-unavailable(guard)" if unavailable else "idle-skip(guard)"
                now = datetime.now().astimezone().isoformat()
                with (directory / f"followup-{today}.md").open("a") as stream:
                    stream.write(f"{now} | — | — | 스캔 | — | {label}: {reasons} | none | followup-scan.json\n")
                held = [reason for reason in payload["reasons"] if reason.startswith("hold:")]
                hold = f", hold={len(held)}: " + ", ".join(held) if held else ""
                print("jira-daily-guard: skipping duplicate (monitoring-unavailable: " + reasons + ")"
                      if unavailable else "jira-daily-guard: skipping duplicate (idle: events=0, no runnable work"
                      + hold + ")")
                return 0
            except (OSError, ValueError, KeyError, TypeError):
                pass
    with locked(directory):
        _lease_sweep(directory)
        if busy(directory):
            print("jira-daily-guard: skipping duplicate (running)")
            return 0
        now = int(time.time())
        token = uuid.uuid4().hex
        trigger = "refill" if refill else ("manual" if args.manual else "tick")
        atomic_json(directory / "running.json", {"token": token, "startedAt": now, "mode": mode,
                    "trigger": trigger, "liveness": "on" if enabled(directory) else "off"})
        (directory / "running").write_text(str(now) + "\n")
        run_path(directory, token).mkdir(parents=True, exist_ok=True)
    print("mode=" + mode)
    return 0


def manual_worker_argv(argv):
    """수동 작업자(jira-worker 토큰 없음)는 `worker --token ""`을 넘긴다. worker에서만 빈 토큰을 '토큰 없음'으로 본다."""
    if not argv or argv[0] != "worker":
        return argv
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--token" and i + 1 < len(argv) and argv[i + 1] == "":
            i += 2
            continue
        if argv[i] == "--token=":
            i += 1
            continue
        out.append(argv[i])
        i += 1
    return out


def main(argv=None):
    argv = manual_worker_argv(list(sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("guard", "token", "attach", "archive", "worker",
                                           "recover", "reconciled", "blocked", "lease-acquire",
                                           "lease-attach", "lease-release", "lease-list",
                                           "lease-sweep", "owns-ticket"))
    parser.add_argument("--dir", type=directory_argument,
                        default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--refill", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--owns", type=token_argument)
    parser.add_argument("--token", type=token_argument)
    parser.add_argument("--current-token", type=token_argument)
    parser.add_argument("--parent", type=token_argument)
    parser.add_argument("--id")
    parser.add_argument("--key")
    parser.add_argument("--unit", default="unit")
    parser.add_argument("--brief", type=Path)
    args = parser.parse_args(argv)
    directory = args.dir.expanduser()
    args.dir = directory
    try:
        if args.action == "guard":
            return guard(args)
        if args.action == "archive":
            archive_ended(directory)
            return 0
        if args.action == "lease-acquire":
            print(lease_acquire(directory, args.parent, args.key))
            return 0
        if args.action == "lease-attach":
            return lease_attach(directory, args.key, args.token, args.id)
        if args.action == "lease-release":
            return lease_release(directory, args.key, args.token)
        if args.action == "lease-sweep":
            print(json.dumps(lease_sweep(directory)))
            return 0
        with locked(directory):
            if args.action == "lease-list":
                print(json.dumps(lease_list(directory)))
            elif args.action == "owns-ticket":
                return 0 if owns_ticket(directory, args.token, args.key) else 1
            elif args.action == "token":
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
                owned = owns(directory, args.token) or (
                    args.key and owns_ticket(directory, args.token, args.key))
                if ((args.token and not owned) or (enabled(directory) and not legacy and not owned)
                        or (enabled(directory) and args.key and not legacy
                            and blocked_by(directory, args.key, args.token))):
                    return 3
                # 일시정지(hold.json)는 소유·수동 여부와 무관하게 새 단위 시작을 막는다(프롬프트가 아니라 코드 게이트).
                brief = args.brief.read_text(encoding="utf-8") if args.brief else None
                held = factory_hold.blocked(directory, args.key or None, brief=brief)
                if held:
                    journal(directory, f"hold: {args.key or '-'} {held} (worker {args.unit})")
                    print(f"jira-worker: hold:{args.key or '-'} {held}", file=sys.stderr)
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
                if not owns(directory, args.current_token):
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
    except LeaseRejected as exc:
        print(json.dumps(exc.payload))
        return 1
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        if args.action == "guard" and args.owns is not None:
            return 1
        print("run_lock: state or paseo query failed", file=sys.stderr)
        return 3 if args.action == "worker" else 2


if __name__ == "__main__":
    sys.exit(main())
