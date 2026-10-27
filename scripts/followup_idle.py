#!/usr/bin/env python3
"""Skip a tick only when a fresh scan and local state establish no runnable work.

Exit 0: idle, 1: work/error reported by scan, 2: internal/invalid state.
--scan-cmd and --preflight-cmd accept shell-quoted argv (no shell execution).
FOLLOWUP_IDLE_SCAN_CMD / FOLLOWUP_IDLE_PREFLIGHT_CMD provide the same overrides.
Scan receives --out PATH; preflight receives --need LIST. Captured command
output is inspected but never printed, including on errors.
--teams-cmd / FOLLOWUP_IDLE_TEAMS_CMD (optional; unset = no chat watcher) receives
--dir DIR --out PATH and must write {"status": "ok", "events": [...]} there (same event
shape as followup_scan.py, source=teams). Only new unhandled chat trigger messages make a tick
non-idle; an unreadable Teams room is reported as teams-unavailable:<CODE>
(guard-last.json, idle-skip line, one notification a day) but never wakes a model.
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from zoneinfo import ZoneInfo

import followup_config as cfg
from followup_triage import read_object, triage
from preflight_env import next_step_item
from run_lock import blocked_by, reconcile_reasons
from state_update import BOOKKEEPING, atomic_json, item_digest, transaction


ROOT = Path(__file__).resolve().parent


def command(value, script):
    argv = shlex.split(value) if value is not None else [sys.executable, str(ROOT / script)]
    if not argv:
        raise ValueError("empty command")
    return argv


def run(argv):
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()



def timestamp(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def previous_workers(directory, guard_at):
    """직전 가드 이후 시작한 마지막 실행의 실제 작업자 수만 인정한다."""
    if not guard_at:
        return 0
    candidates = []
    for path in (directory / "runs").glob("*"):
        if not path.is_dir():
            continue
        summary = read_object(path / "summary.json", optional=True)
        started = timestamp(summary.get("startedAt"))
        # summary가 없는 최신 실행은 성공 실적을 입증하지 못한다.
        observed = started or path.stat().st_mtime
        if observed >= int(timestamp(guard_at)):
            candidates.append((observed, summary))
    if not candidates:
        return 0
    workers = max(candidates, key=lambda row: row[0])[1].get("workers", 0)
    if type(workers) is not int or workers < 0:
        raise ValueError("invalid workers count")
    return workers


def notify(message):
    override = os.environ.get("FOLLOWUP_IDLE_NOTIFY_CMD")
    try:
        argv = command(override, "") + [message] if override is not None else [
            "osascript", "-e", "display notification " + json.dumps(message, ensure_ascii=False)
            + ' with title ' + json.dumps(cfg.LINE_TITLE, ensure_ascii=False)]
        return run(argv).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def teams_scan(directory, teams_cmd):
    """Return (events, status, error). Any failure is a reported state, never an exception."""
    path = directory / "teams-scan.json"
    try:
        path.unlink(missing_ok=True)
        run(teams_cmd + ["--dir", str(directory), "--out", str(path)])
        data = read_object(path)
        events = data.get("events")
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            raise ValueError("invalid teams scan")
        status = data.get("status")
        error = data.get("error") if isinstance(data.get("error"), str) else None
        if status != "ok":
            return events, "unavailable", error if error and error.isupper() else "TEAMS_READ_FAILED"
        return events, "ok", None
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], "unavailable", "TEAMS_SCAN_FAILED"


def _decide(directory, scan_cmd, preflight_cmd, *, classifier=None, teams_cmd=None):
    result = {"idle": False, "reasons": [], "events": 0, "queue": {}, "waitingHuman": {}}
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    result["at"] = now.isoformat()
    reasons = result["reasons"]
    blocked = result["blockedKeys"] = {}

    def key_blocked(key):
        if key not in blocked:
            blocked[key] = blocked_by(directory, key)
        return bool(blocked[key])
    try:
        reasons.extend(reconcile_reasons(directory))
        pending = directory / "pending-focus.jsonl"
        if pending.exists() and pending.read_text().strip():
            reasons.append("pending-focus")
        scan_path = directory / "followup-scan.json"
        # A failed scan must never reuse an older zero-event result.
        scan_path.unlink(missing_ok=True)
        scan = run(scan_cmd + ["--out", str(scan_path)])
        if scan.returncode:
            reasons.append("scan-failed")
        if "unauthorized" in (scan.stdout + scan.stderr).lower():
            reasons.append("scan-unauthorized")
        if scan_path.exists() or scan.returncode == 0:
            data = read_object(scan_path)
            events, errors = data.get("events"), data.get("errors")
            if not isinstance(events, list) or not isinstance(errors, list):
                raise ValueError("invalid scan")
            events = triage(directory, events, classifier=classifier)
            events = [event for event in events if not key_blocked(event.get("key", ""))]
            result["events"] = len(events)
            if errors:
                reasons.append("scan-errors")
            if events:
                reasons.append("new-events")

        teams_error = None
        if teams_cmd is not None:
            teams_events, teams_status, teams_error = teams_scan(directory, teams_cmd)
            teams_events = triage(directory, teams_events, classifier=classifier)
            teams_events = [event for event in teams_events if not key_blocked(event.get("key", ""))]
            result["teams"] = {"status": teams_status, "events": len(teams_events)}
            if teams_error:
                result["teams"]["error"] = teams_error
                reasons.append("teams-unavailable:" + teams_error)
            if teams_events:
                result["events"] += len(teams_events)
                reasons.append("teams-new-events")

        queue = read_object(directory / "followup-queue.json", optional=True)
        approvals = read_object(directory / "approvals.json", optional=True)
        state = read_object(directory / "guard-state.json", optional=True)
        previous = state.get("runnable", {})
        if not isinstance(previous, dict):
            raise ValueError("invalid guard state")
        workers = previous_workers(directory, state.get("at"))
        runnable, changes, probes, stuck = {}, {}, {}, []
        for key, item in sorted(queue.items()):
            if not isinstance(item, dict):
                raise ValueError("invalid queue item")
            original = dict(item)
            if timestamp(item.get("backoffUntil")) > now.timestamp():
                result["queue"][key] = item.get("waiting")
                # backoff 이전 해시를 보존해야 만료 뒤에도 실제 상태 변화만 비교한다.
                if key in previous:
                    runnable[key] = previous[key]
                continue
            if key_blocked(key):
                result["queue"][key] = item.get("waiting")
                continue
            if item.get("waiting") in ("env-human", "env-down"):
                normalized = next_step_item(item)
                if normalized != item:
                    queue[key] = normalized
                    changes[key] = (original, normalized)
                item = normalized
            waiting = item.get("waiting")
            if waiting is not None and not isinstance(waiting, str):
                raise ValueError("invalid waiting")
            result["queue"][key] = waiting
            if waiting is None:
                item_hash = item_digest(item)
                prior = previous.get(key, {})
                if not isinstance(prior, dict):
                    raise ValueError("invalid runnable state")
                same = prior.get("hash") == item_hash
                defer_count = item.get("deferCount", 0)
                if type(defer_count) is not int or defer_count < 0:
                    raise ValueError("invalid defer count")
                if not same and prior and defer_count:
                    item["deferCount"] = defer_count = 0
                    changes[key] = (original, dict(item))
                exempt = (workers >= 1 and bool(state.get("at"))
                          and timestamp(item.get("deferredAt")) > timestamp(state.get("at"))
                          and defer_count < 4)
                count = prior.get("count", 0) if same else 0
                if exempt:
                    count = 0
                if defer_count >= 4:
                    count = max(1, count)
                if type(count) is not int or count < 0:
                    raise ValueError("invalid runnable count")
                runnable[key] = {"hash": item_hash, "count": count + 1}
                if count:
                    reasons.append("stuck-runnable:" + key)
                    if count == 1 and not prior.get("notified"):
                        runnable[key]["notified"] = True
                        stuck.append(key)
                        with (directory / "lessons.jsonl").open("a", encoding="utf-8") as output:
                            output.write(json.dumps({"at": result["at"], "key": key, "kind": "infra_fail",
                                "what": "unchanged queue-runnable after orchestrator wake",
                                "evidence": str(directory / "guard-last.json"),
                                "suggestion": "실행하거나 nextStep·waiting을 실제 막힘에 맞게 저장한다."}, ensure_ascii=False) + "\n")
                else:
                    reasons.append("queue-runnable:" + key)
                if count > 1 and prior.get("notified"):
                    runnable[key]["notified"] = True
            if waiting in ("env-human", "env-down") and item.get("needs"):
                if not isinstance(item["needs"], str):
                    raise ValueError("invalid needs")
                needs = item["needs"]
                if needs not in probes:
                    probes[needs] = run(preflight_cmd + ["--need", needs, "--fail-fast"])
                probe = probes[needs]
                if probe.returncode == 0:
                    reasons.append("environment-ready:" + key)
                elif waiting == "env-human":
                    try:
                        payload = json.loads(probe.stdout)
                        failures = [r for r in payload.get("results", []) if r.get("status") == "FAIL"]
                        action = (failures[0].get("human_action") or failures[0].get("reason")) if failures else None
                        if isinstance(action, str) and action:
                            result["waitingHuman"].setdefault(action, []).append(key)
                    except (ValueError, TypeError, AttributeError):
                        pass

        if changes:
            def update_queue(latest):
                for key, (before, after) in changes.items():
                    current = latest.get(key)
                    if not isinstance(current, dict) or item_digest(current) != item_digest(before):
                        continue
                    # 스캔 중 다른 작성자가 바꾼 필드는 덮어쓰지 않는다.
                    for field, value in after.items():
                        if before.get(field) != value and current.get(field) == before.get(field):
                            current[field] = value
            transaction(directory, "followup-queue.json", update_queue)
        state["runnable"] = runnable
        state["at"] = result["at"]
        messages = []
        groups = result["waitingHuman"]
        if groups:
            notification_hash = digest(groups)
            if state.get("notificationDay") != now.date().isoformat() or state.get("notificationHash") != notification_hash:
                messages.extend(action + " 시 진행 가능: " + ", ".join(keys) for action, keys in groups.items())
                state.update(notificationDay=now.date().isoformat(), notificationHash=notification_hash)
        if stuck:
            messages.append("동일 상태로 진행 정지: " + ", ".join(stuck))
        # CLI가 설치되지 않은 PC(시험 HOME 포함)는 알리지 않는다. 그 밖의 읽기 실패는 하루 한 번 알린다.
        if teams_error and teams_error != "TEAMS_CLI_NOT_FOUND":
            if state.get("teamsNotified") != {"day": now.date().isoformat(), "error": teams_error}:
                messages.append(f"채팅 방 감시 불가({teams_error}): FOLLOWUP_IDLE_TEAMS_CMD 연결 확인 필요")
                state["teamsNotified"] = {"day": now.date().isoformat(), "error": teams_error}
        if messages:
            result["notify"] = "sent" if notify("\n".join(messages)) else "failed"
        transaction(directory, "guard-state.json", lambda latest: latest.update(state))

        for key, approval in sorted(approvals.items()):
            if not isinstance(approval, dict):
                raise ValueError("invalid approval")
            if key_blocked(key):
                continue
            if timestamp(queue.get(key, {}).get("backoffUntil")) > now.timestamp():
                continue
            if "stg" in approval and not approval.get("usedAt"):
                reasons.append("stg-approval-unused:" + key)

        result["blockedKeys"] = {key: tokens for key, tokens in blocked.items() if tokens}
        # Reported-only reasons: a stuck item and an unreadable Teams room do not wake the orchestrator.
        if any(not reason.startswith(("stuck-runnable:", "teams-unavailable:")) for reason in reasons):
            return result, 1
        result.update(idle=True)
        if not reasons:
            result["reasons"] = ["no-events", "no-runnable-work"]
        return result, 0
    except (OSError, ValueError, subprocess.SubprocessError):
        # Exception strings and subprocess output can contain credentials.
        reasons.append("internal-error")
        return result, 2


def decide(directory, scan_cmd, preflight_cmd, *, classifier=None, teams_cmd=None):
    result, code = _decide(directory, scan_cmd, preflight_cmd, classifier=classifier, teams_cmd=teams_cmd)
    try:
        atomic_json(directory / "guard-last.json", result)
    except (OSError, ValueError):
        result["idle"] = False
        result["reasons"].append("internal-error")
        code = 2
    return result, code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid arguments")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    parser.add_argument("--scan-cmd", default=os.environ.get("FOLLOWUP_IDLE_SCAN_CMD"))
    parser.add_argument("--preflight-cmd", default=os.environ.get("FOLLOWUP_IDLE_PREFLIGHT_CMD"))
    parser.add_argument("--teams-cmd", default=os.environ.get("FOLLOWUP_IDLE_TEAMS_CMD"))
    try:
        args = parser.parse_args(argv)
        teams = command(args.teams_cmd, "") if args.teams_cmd else None
        result, code = decide(args.dir.expanduser(), command(args.scan_cmd, "followup_scan.py"),
                              command(args.preflight_cmd, "preflight_env.py"), teams_cmd=teams)
    except ValueError:
        result = {"idle": False, "reasons": ["internal-error"], "events": 0, "queue": {}}
        code = 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
