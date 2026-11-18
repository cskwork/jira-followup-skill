#!/usr/bin/env python3
"""Skip a tick only when a fresh scan and local state establish no runnable work.

Exit 0: idle, 1: work/error reported by scan, 2: internal/invalid state.
--scan-cmd and --preflight-cmd accept shell-quoted argv (no shell execution).
FOLLOWUP_IDLE_SCAN_CMD / FOLLOWUP_IDLE_PREFLIGHT_CMD provide the same overrides.
Scan receives --out PATH; preflight receives --need LIST. Captured command
output is inspected but never printed, including on errors.
--teams-cmd / FOLLOWUP_IDLE_TEAMS_CMD (default teams_room.py scan) receives
--dir DIR --out PATH and must write {"status": "ok", "events": [...]} there (same event
shape as followup_scan.py, source=teams). Only new unhandled chat trigger messages make a tick
non-idle; an unreadable Teams room is reported as teams-unavailable:<CODE>
(guard-last.json, idle-skip line, one notification a day) but never wakes a model.
With no TEAMS_ROOM_CHAT_ID, teams_room.py writes status ok with no events (watcher off).
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from drain import pending_keys
import factory_hold
import followup_config as cfg
from followup_triage import read_object, triage
from preflight_env import next_step_item
from run_lock import blocked_by, journal, lease_conflicts, lease_list, lanes, reconcile_reasons
from state_update import BOOKKEEPING, atomic_json, guard_append_inbox, item_digest, transaction
import judge
import lifecycle


ROOT = Path(__file__).resolve().parent
# 공장 일이 끝난 Jira 상태. 스테이징 검수 완료 뒤 운영 승격은 사람 몫이다
# (followup_scan.POST_RELEASE_STATUSES + STATUS_QA_DONE, 기본 STATUS_ORDER 다섯째).
FINISHED_STATUSES = tuple(cfg.POST_RELEASE_STATUSES) + (cfg.STATUS_QA_DONE,)
# QA 요청 상태의 human-risk는 실제로 QA 회신 대기다.
QA_STATUSES = (cfg.get("STATUS_ON_DEV", "개발 QA 요청"), cfg.get("STATUS_ON_STG", "스테이징 QA 요청"))
SUMMARY_TAG = "queue-human-summary"


def command(value, script):
    argv = shlex.split(value) if value is not None else [sys.executable, str(ROOT / script)]
    if not argv:
        raise ValueError("empty command")
    return argv


def run(argv):
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def timed(timings, name, operation, clock):
    before = clock()
    try:
        return operation()
    finally:
        timings[name] = max(0, clock() - before)


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
    """직전 가드 이후 갱신된 전역·티켓 실행의 실제 작업자 수를 합한다."""
    if not guard_at:
        return 0
    total = 0
    for path in (directory / "runs").glob("*"):
        if not path.is_dir():
            continue
        summary = read_object(path / "summary.json", optional=True)
        observed = timestamp(summary.get("updatedAt", summary.get("startedAt")))
        if observed < timestamp(guard_at):
            continue
        workers = summary.get("workers", 0)
        if type(workers) is not int or workers < 0:
            raise ValueError("invalid workers count")
        total += workers
    return total


def invalid_inbox_row(directory, path, number, line):
    """원본 줄은 보존하고 같은 위치·내용의 손상은 한 번만 기록한다."""
    name = str(path.relative_to(directory))
    marker = directory / (".inbox-invalid-" + digest([name, number, line]) + "-alerted")
    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        return
    journal(directory, f"liveness-alert: invalid {name} line {number}")


def orphan_inboxes(directory, leased):
    """전달은 ACK가 아니므로 handled 아닌 고아 줄만 다시 깨운다."""
    state = read_object(directory / "state.json", optional=True)
    followup = state.get("followup", {})
    if not isinstance(followup, dict) or not isinstance(followup.get("handled", {}), dict):
        raise ValueError("invalid handled state")
    handled = followup.get("handled", {})
    keys = []
    for path in sorted((directory / "tickets").glob("*.inbox.jsonl")):
        key = path.name.removesuffix(".inbox.jsonl")
        if key in leased:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("commentId"), str) or not event["commentId"]:
                    raise ValueError("invalid inbox event")
            except ValueError:
                invalid_inbox_row(directory, path, number, line)
                continue
            if event["commentId"] not in handled:
                keys.append(key)
                break
    return keys


def record_error(errors, error):
    """Keep the exception type and at most 300 message characters in guard diagnostics."""
    if errors is not None:
        errors.append(f"{type(error).__name__}: {str(error)[:300]}")


def notify(message, *, errors=None):
    override = os.environ.get("FOLLOWUP_IDLE_NOTIFY_CMD")
    try:
        if override is not None:
            return run(command(override, "") + [message]).returncode == 0
        # 채팅 방으로는 보내지 않는다(사용자 지시 2026-11-03). 결정 화면이 html이 아니면 할 일 '확인 필요'에도 올린다.
        import decision_inbox
        decision_inbox.alert_detached(f"[{cfg.LINE_TITLE} 조치 필요]\n" + message)
        return run(["osascript", "-e", "display notification " + json.dumps(message, ensure_ascii=False)
                    + ' with title ' + json.dumps(cfg.LINE_TITLE, ensure_ascii=False)]).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record_error(errors, error)
        return False


SCAN_ALERT_FAILURES = 3


def scan_health(directory):
    state = read_object(directory / "scan-state.json", optional=True)
    count = state.get("failureCount", 0)
    if type(count) is not int or count < 0:
        raise ValueError("invalid scan failure count")
    for name in ("lastSuccessAt", "lastErrorAt", "lastError"):
        state.setdefault(name, None)
    return state


def record_scan(directory, state, at, failure, seconds=None, *, errors=None):
    """Count every observed scan outcome: a tick that died mid-scan must not repeat unnoticed."""
    count = (state.get("failureCount", 0) + 1) if failure else 0
    state.update(attemptStartedAt=None, durationSeconds=seconds, failureCount=count,
                 lastError=failure, alerted=state.get("alerted", False) if failure else False)
    if failure:
        state["lastErrorAt"] = at
    else:
        state["lastSuccessAt"] = at
    alert = None
    if count >= SCAN_ALERT_FAILURES and not state["alerted"]:
        state["alerted"] = True
        alert = "sent" if notify("Jira 스캔 " + str(count) + "회 연속 실패: " + failure, errors=errors) else "failed"
    atomic_json(directory / "scan-state.json", state)
    return alert


def short_keys(keys, limit=8):
    return ", ".join(keys[:limit]) + (f" 외 {len(keys) - limit}건" if len(keys) > limit else "")


def human_summary(queue_waits, leased):
    """진행 가능 0건인데 사람 확인이 있으면 요약 알림 문구. 2026-11-02 17시 큐 40건 전부 대기·진행 0건을
    동시 진행 제한으로 오해했다(가드는 env-human 묶음·정지만 알렸다)."""
    groups = {}
    for key, waiting in sorted(queue_waits.items()):
        groups.setdefault(waiting, []).append(key)
    human = groups.pop("human-risk", [])
    if groups.get(None) or leased or not human:
        return None
    reply = groups.pop("reporter", [])
    other = sum(len(keys) for keys in groups.values())
    lines = [f"진행 가능 0건 · 사람 확인 {len(human)}건 · 회신 대기 {len(reply)}건"
             + (f" · 기타 대기 {other}건" if other else ""), "사람 확인: " + short_keys(human)]
    if reply:
        lines.append("회신 대기: " + short_keys(reply))
    return "\n".join(lines)


def teams_scan(directory, teams_cmd, *, errors=None):
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
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record_error(errors, error)
        return [], "unavailable", "TEAMS_SCAN_FAILED"


def _decide(directory, scan_cmd, preflight_cmd, *, classifier=None, teams_cmd=None, clock=time.monotonic):
    result = {"monitoringStatus": "ok", "timings": {}, "idle": False, "reasons": [], "events": 0, "queue": {}, "waitingHuman": {}}
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    result["at"] = now.isoformat()
    reasons = result["reasons"]
    internal_errors = result["internalErrors"] = []
    blocked = result["blockedKeys"] = {}
    leased = {}
    observed_keys = set()
    lease_blocked = {}

    def refresh_leases():
        leases = lease_list(directory)
        leased.clear()
        leased.update((lease["key"], lease) for lease in leases
                      if lease.get("state") in ("launching", "live"))
        blocked.clear()
        lease_blocked.clear()
        return leases

    def key_blocked(key):
        observed_keys.add(key)
        if key not in blocked:
            blockers = blocked_by(directory, key)
            if isinstance(key, str) and re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
                conflicts = lease_conflicts(directory, key)
                # 다른 키의 임대와 관련키·브랜치·carry로 겹친다. 자기 임대는 티켓 오케스트레이터가 일하는 중이다.
                lease_blocked[key] = [other for other in conflicts if other != key]
                blockers = blockers + conflicts
            if key in leased:
                blockers.append(leased[key]["token"])
            blocked[key] = list(dict.fromkeys(blockers))
        return bool(blocked[key])

    def local_events(events):
        refresh_leases()
        remaining = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("invalid event")
            key = event.get("key", "")
            if key in leased:
                if guard_append_inbox(directory, key, event):
                    continue
                # 스캔과 전달 사이에 해제됐다면 이번 tick의 일반 이벤트로 처리한다.
                leased.pop(key, None)
                blocked.pop(key, None)
            if not key_blocked(key):
                remaining.append(event)
        return remaining

    jira_statuses = {}
    holds = result["held"] = {}
    hold_state = None

    def held(key, status=None):
        """일시정지(factory_hold)로 이 키의 새 단위를 시작하지 않으면 사유를 남기고 True. 큐·handled는 그대로 둔다."""
        if key:
            reason = factory_hold.blocked(directory, key, status=status or jira_statuses.get(key), loaded=hold_state)
        elif hold_state["state"] == "invalid":
            reason = "invalid"
        else:
            # 키 없는 이벤트는 허용 상태의 티켓 후속이 아니다.
            only = hold_state["state"] == "active" and hold_state["hold"]["only"]
            reason = "only(status=unknown)" if only else None
        if reason:
            label = key or "-"
            if label not in holds:
                holds[label] = reason
                reasons.append(f"hold:{label}({reason})")
        return bool(reason)

    try:
        # run_lock.guard가 이미 sweep했다. 여기서 다시 조회/회수하지 않는다.
        hold_state = factory_hold.load(directory)
        if hold_state["state"] in ("active", "invalid"):
            result["hold"] = {"state": hold_state["state"], "error": hold_state["error"],
                              **({k: hold_state["hold"][k] for k in ("until", "only", "freeze", "reason")}
                                 if hold_state["hold"] else {})}
        refresh_leases()
        reasons.extend(reconcile_reasons(directory))
        pending = pending_keys(directory)
        pending_blocked = [key for key in pending if key_blocked(key)]
        if pending_blocked:
            reasons.append("pending-focus-blocked:" + ",".join(pending_blocked))
        pending_held = [key for key in pending if key not in pending_blocked and held(key)]
        if any(key not in pending_blocked and key not in pending_held for key in pending):
            reasons.append("pending-focus")
        health = scan_health(directory)
        if health.get("attemptStartedAt") is not None:
            # 지난 tick이 스캔 도중 끝나 결과 회계에 닿지 못했다. 세지 않으면 조용히 반복된다.
            result["monitoringStatus"] = "degraded"
            reasons.append("scan-interrupted")
            alert = record_scan(directory, health, result["at"], "scan-interrupted", errors=internal_errors)
            if alert:
                result["scanAlert"] = alert
        health["attemptStartedAt"] = result["at"]
        atomic_json(directory / "scan-state.json", health)
        scan_path = directory / "followup-scan.json"
        # A failed scan must never reuse an older zero-event result.
        scan_path.unlink(missing_ok=True)
        scan = timed(result["timings"], "scanSeconds",
                     lambda: run(scan_cmd + ["--out", str(scan_path)]), clock)
        failure = None
        if scan.returncode:
            failure = "scan-failed"
            result["monitoringStatus"] = "degraded"
            reasons.append("scan-failed")
        if "unauthorized" in (scan.stdout + scan.stderr).lower():
            failure = "scan-unauthorized"
            reasons.append("scan-unauthorized")
        if scan_path.exists() or scan.returncode == 0:
            data = read_object(scan_path)
            events, errors = data.get("events"), data.get("errors")
            if not isinstance(events, list) or not isinstance(errors, list):
                raise ValueError("invalid scan")
            jira_statuses = data.get("queueStatuses", {})
            if not isinstance(jira_statuses, dict):
                raise ValueError("invalid scan")
            if data.get("queueStatusError"):
                result["queueStatusError"] = str(data["queueStatusError"])[:300]
            events = [event for event in local_events(events)
                      if not held(event.get("key", ""), event.get("status"))]
            events = timed(result["timings"], "triageSeconds",
                           lambda: triage(directory, events, classifier=classifier), clock)
            events = [event for event in events if not key_blocked(event.get("key", ""))]
            result["events"] = len(events)
            if errors:
                result["monitoringStatus"] = "degraded"
                reasons.append("scan-errors")
            if events:
                reasons.append("new-events")
        if "scan-errors" in reasons and not failure:
            failure = "scan-errors"
        alert = record_scan(directory, health, result["at"], failure,
                            result["timings"].get("scanSeconds"), errors=internal_errors)
        result.setdefault("monitoring", {})["jira"] = {
            "failureCount": health["failureCount"], "lastError": health["lastError"],
            "lastSuccessAt": health["lastSuccessAt"], "lastErrorAt": health["lastErrorAt"]}
        if alert:
            result["scanAlert"] = alert

        teams_error = None
        if teams_cmd is not None:
            teams_events, teams_status, teams_error = timed(result["timings"], "teamsSeconds",
                lambda: teams_scan(directory, teams_cmd, errors=internal_errors), clock)
            teams_events = [event for event in local_events(teams_events)
                            if not held(event.get("key", ""), event.get("status"))]
            teams_events = timed(result["timings"], "teamsTriageSeconds",
                lambda: triage(directory, teams_events, classifier=classifier), clock)
            try:
                data = read_object(directory / "teams-scan.json", optional=True)
            except (OSError, ValueError):
                data = {}
            result.setdefault("monitoring", {})["teams"] = data.get(
                "monitoring", {"lastSuccessAt": None, "error": teams_error})
            teams_events = [event for event in teams_events if not key_blocked(event.get("key", ""))]
            result["teams"] = {"status": teams_status, "events": len(teams_events)}
            if teams_error:
                result["monitoringStatus"] = "unavailable"
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
        removals, to_reporter, reconciled = {}, {}, {"removed": {}, "reporter": {}, "stg": {}}
        to_stg = {}
        judge_mode = judge.mode(directory)
        judge_waits = read_object(directory / "judge-state.json", optional=True).get("stgAsked", {})
        sdlc = lifecycle.Sdlc()  # 이 tick 동안 slug별 sdlc 상태 캐시

        def sdlc_status(key, item):
            """slug가 있으면 sdlc 상태(dict), 없거나 꺼졌거나 실패면 None. 실패는 보고만 한다(sdlc-error)."""
            slug = item.get("slug")
            if not slug:
                return None
            try:
                return sdlc.status(slug)
            except lifecycle.SdlcError:
                if "sdlc-error:" + key not in reasons:
                    reasons.append("sdlc-error:" + key)
                return None
        leases = refresh_leases()
        lane_count = lanes(directory)
        capacity_full = lane_count > 0 and len(leases) >= lane_count
        reasons.extend("ticket-inbox:" + key for key in orphan_inboxes(directory, leased) if not held(key))
        for key, item in sorted(queue.items()):
            if not isinstance(item, dict):
                raise ValueError("invalid queue item")
            original = dict(item)
            delivery = lifecycle.delivery(item)
            # 큐↔Jira 대조. 임대 중인 키는 티켓 오케스트레이터 몫이라 건드리지 않는다.
            # 끝난 일: Jira 완료 상태, delivery=shipped, 또는 sdlc DONE(close 됨).
            status = jira_statuses.get(key)
            if not key_blocked(key):
                if status in FINISHED_STATUSES or delivery == "shipped":
                    removals[key] = (original, status or "delivery=shipped")
                    continue
                if (sdlc_status(key, item) or {}).get("state") == "DONE":
                    removals[key] = (original, "sdlc=DONE")
                    continue
            # QA 요청 상태의 human-risk는 보통 QA 회신 대기다. 단 사용자가 그 티켓의 후속 수정을 다시 연 항목
            # (followupSince)의 human-risk는 진짜 사용자 질문이라 바꾸지 않는다(2026-11-17 실측: 후속 수정 질문이 사라짐).
            if (status in QA_STATUSES and item.get("waiting") == "human-risk" and not item.get("followupSince")
                    and not key_blocked(key)):
                note = item.get("note") or ""
                item.update(waiting="reporter", note=(str(note) + " | " if note else "")
                            + f"가드 {now.date().isoformat()}: Jira '{status}'라 QA 회신 대기(reporter)로 바꿈")
                changes[key] = (original, dict(item))
                to_reporter[key] = status
            # 사용자 결정 2026-11-09: 사람이 '개발 검수 완료'로 옮기면 스테이징 반영을 이어 간다(승인은 judge.py stg가 판정).
            # judge가 이미 이 상태를 보고도 기다리기로 했으면(전환이 새 결과 댓글보다 앞섬) 매 tick 다시 깨우지 않는다.
            # 사람 승인 항목이 열린 stg-approval 대기는 건드리지 않는다.
            if (judge_mode != "off" and status == judge.DEV_DONE and delivery in judge.STG_DELIVERY
                    and item.get("waiting") == "reporter" and not key_blocked(key)
                    and (judge_waits.get(key) or {}).get("status") != judge.DEV_DONE):
                note = item.get("note") or ""
                item.update(waiting=None, note=(str(note) + " | " if note else "")
                            + f"가드 {now.date().isoformat()}: Jira '{status}'라 스테이징 반영 재개(judge 승인 판정)")
                changes[key] = (original, dict(item))
                to_stg[key] = status
            if held(key, status):
                # 일시정지 중에는 runnable 해시를 이어 두지 않는다. 해제 뒤 stuck이 아니라 새 후보로 깨운다.
                result["queue"][key] = item.get("waiting")
                continue
            if timestamp(item.get("backoffUntil")) > now.timestamp():
                result["queue"][key] = item.get("waiting")
                # backoff 이전 해시를 보존해야 만료 뒤에도 실제 상태 변화만 비교한다.
                if key in previous:
                    runnable[key] = previous[key]
                continue
            if key_blocked(key):
                result["queue"][key] = item.get("waiting")
                if lease_blocked.get(key):
                    # 관련 키 임대가 끝날 때까지 못 돈다. 깨우지 않고 보고만 한다(stuck으로 세지 않는다).
                    reasons.append("lease-blocked:" + key)
                continue
            # 개발 단계(delivery=none)는 sdlc가 정본이다. 사람 차례(NEEDS-HUMAN)면 실행 후보가 아니다.
            # 배포 단계인데 sdlc가 deliver·done이 아니면 큐가 앞서 간 것이라 보고만 한다(막지 않는다).
            info = sdlc_status(key, item)
            if info and delivery == "none" and info["state"] == "NEEDS-HUMAN" and (
                    item.get("waiting") in (None, "env-human", "env-down")):
                result["queue"][key] = item.get("waiting")
                reasons.append("sdlc-human:" + key)
                continue
            if info and delivery not in (None, "none") and info["phase"] not in ("deliver", "done"):
                reasons.append("sdlc-lag:" + key)
            if item.get("waiting") in ("env-human", "env-down"):
                normalized = next_step_item(item, phase=info["phase"] if info else None)
                if normalized != item:
                    queue[key] = normalized
                    changes[key] = (original, normalized)
                item = normalized
            waiting = item.get("waiting")
            if waiting is not None and not isinstance(waiting, str):
                raise ValueError("invalid waiting")
            result["queue"][key] = waiting
            if waiting is None:
                if capacity_full:
                    if key in previous:
                        runnable[key] = previous[key]
                    if "capacity-full" not in reasons:
                        reasons.append("capacity-full")
                    continue
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
                # 직전 가드 뒤에 미룬 항목은 이번 비교에서 stuck이 아니다. 작업자가 돈 실행의 미룸,
                # 또는 작업자 0명이어도 정원·자원 부족(capacity·resource) 미룸이다. 4회 이상 미룸은 그대로 알린다.
                deferred_since = (bool(state.get("at"))
                                  and timestamp(item.get("deferredAt")) > timestamp(state.get("at")))
                exempt = (deferred_since and defer_count < 4
                          and (workers >= 1 or item.get("deferReason") in ("capacity", "resource")))
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
                        import lessons
                        lessons.append(directory, {"at": result["at"], "key": key, "kind": "infra_fail",
                            "what": "unchanged queue-runnable after orchestrator wake",
                            "evidence": str(directory / "guard-last.json"),
                            "suggestion": "실행하거나 nextStep·waiting을 실제 막힘에 맞게 저장한다."})
                else:
                    reasons.append("queue-runnable:" + key)
                if count > 1 and prior.get("notified"):
                    runnable[key]["notified"] = True
            if waiting in ("env-human", "env-down") and item.get("needs"):
                if not isinstance(item["needs"], str):
                    raise ValueError("invalid needs")
                needs = item["needs"]
                if needs not in probes:
                    probes[needs] = timed(result["timings"].setdefault("preflightSeconds", {}), needs,
                        lambda: run(preflight_cmd + ["--need", needs, "--fail-fast"]), clock)
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

        if changes or removals:
            def update_queue(latest):
                for key, (before, why) in removals.items():
                    current = latest.get(key)
                    # 스캔 중 다른 작성자가 바꿨으면 지우지 않는다.
                    if isinstance(current, dict) and item_digest(current) == item_digest(before):
                        del latest[key]
                        reconciled["removed"][key] = why
                for key, (before, after) in changes.items():
                    current = latest.get(key)
                    if not isinstance(current, dict) or item_digest(current) != item_digest(before):
                        continue
                    # 스캔 중 다른 작성자가 바꾼 필드는 덮어쓰지 않는다.
                    for field, value in after.items():
                        if before.get(field) != value and current.get(field) == before.get(field):
                            current[field] = value
                    if key in to_reporter and current.get("waiting") == "reporter":
                        reconciled["reporter"][key] = to_reporter[key]
                    if key in to_stg and current.get("waiting") is None:
                        reconciled["stg"][key] = to_stg[key]
            transaction(directory, "followup-queue.json", update_queue)
        if reconciled["removed"] or reconciled["reporter"] or reconciled["stg"]:
            if not reconciled["stg"]:
                del reconciled["stg"]
            result["queueReconcile"] = reconciled
            journal(directory, "queue-reconcile: " + "; ".join(
                [f"removed {k}({v})" for k, v in reconciled["removed"].items()]
                + [f"reporter {k}({v})" for k, v in reconciled["reporter"].items()]
                + [f"stg-resume {k}({v})" for k, v in reconciled.get("stg", {}).items()]))
        if judge_mode != "off" and "human-risk" in result["queue"].values():
            # 질문·권장 답이 있는 사람 대기를 judge가 판정한다. 가드를 붙잡지 않도록 분리 실행한다(judge.py가 잠금).
            try:
                subprocess.Popen([sys.executable, str(ROOT / "judge.py"), "risk", "--dir", str(directory)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                result["judge"] = "risk-launched"
            except OSError as error:
                record_error(internal_errors, error)
        state["runnable"] = runnable
        state["at"] = result["at"]
        messages = []
        groups = result["waitingHuman"]
        if groups:
            messages.extend(action + " 시 진행 가능: " + ", ".join(keys) for action, keys in groups.items())
        if stuck:
            messages.extend("동일 상태로 진행 정지: " + key for key in stuck)
        # CLI가 설치되지 않은 PC(시험 HOME 포함)는 알리지 않는다. 그 밖의 읽기 실패는 하루 한 번 알린다.
        if teams_error and teams_error != "TEAMS_CLI_NOT_FOUND":
            messages.append(f"채팅 방 감시 불가({teams_error}): 채팅 CLI 연결(teams_room.py·FOLLOWUP_IDLE_TEAMS_CMD) 확인 필요")
        hashes = state.get("notificationHashes", []) if state.get("notificationDay") == now.date().isoformat() else []
        if not isinstance(hashes, list) or not all(isinstance(value, str) for value in hashes):
            raise ValueError("invalid notification hashes")
        pending = [(digest(message), message) for message in messages]
        summary = human_summary(result["queue"], leased)
        if summary:
            # 숫자가 바뀌어도 하루 1회만 알린다.
            pending.append((digest(SUMMARY_TAG), summary))
        pending = [(mark, message) for mark, message in pending if mark not in hashes]
        if pending:
            outcomes = [notify(message, errors=internal_errors) for _, message in pending]
            result["notify"] = "sent" if all(outcomes) else "failed"
            hashes.extend(mark for mark, _ in pending)
        state.update(notificationDay=now.date().isoformat(), notificationHashes=hashes)
        transaction(directory, "guard-state.json", lambda latest: latest.update(state))

        for key, approval in sorted(approvals.items()):
            if not isinstance(approval, dict):
                raise ValueError("invalid approval")
            if key_blocked(key):
                continue
            if timestamp(queue.get(key, {}).get("backoffUntil")) > now.timestamp():
                continue
            if "stg" in approval and not approval.get("usedAt") and not held(key):
                # 환경 대기(VPN 등) 중인 키는 위 preflight가 통과할 때(environment-ready)만 깨운다. 승인만으로 깨우면
                # 환경이 막힌 동안 tick마다 할 일 없는 오케스트레이터가 뜬다(2026-11-09 실측: 08:30~10:17 매 tick).
                item = queue.get(key, {})
                if item.get("waiting") in ("env-human", "env-down") and item.get("needs"):
                    continue
                reasons.append("stg-approval-unused:" + key)

        # 캐시 무효화가 pending 등 앞서 관찰한 키의 보고까지 지우면 안 된다.
        refresh_leases()
        for key in sorted(observed_keys):
            key_blocked(key)
        result["blockedKeys"] = {key: tokens for key, tokens in blocked.items() if tokens}
        # 막힌 pending focus·stuck·Teams 읽기 실패·일시정지(hold)는 보고만 하고 깨우지 않는다.
        # 관련 키 임대 대기(lease-blocked)·sdlc 사람 차례·지연·오류도 보고만 한다.
        if any(not reason.startswith(("stuck-runnable:", "teams-unavailable:",
                                      "pending-focus-blocked:", "capacity-full", "hold:",
                                      "lease-blocked:", "sdlc-human:", "sdlc-lag:", "sdlc-error:"))
               for reason in reasons):
            return result, 1
        result.update(idle=True)
        if not reasons:
            result["reasons"] = ["no-events", "no-runnable-work"]
        return result, 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record_error(internal_errors, error)
        result["monitoringStatus"] = "degraded"
        reasons.append("internal-error")
        try:
            health = scan_health(directory)
            if health.get("attemptStartedAt") is not None:
                record_scan(directory, health, result["at"], "scan-malformed", errors=internal_errors)
        except (OSError, ValueError) as error:
            record_error(internal_errors, error)
        return result, 2


def decide(directory, scan_cmd, preflight_cmd, *, classifier=None, teams_cmd=None, clock=time.monotonic):
    started = clock()
    result, code = _decide(directory, scan_cmd, preflight_cmd, classifier=classifier, teams_cmd=teams_cmd, clock=clock)
    result["timings"]["totalSeconds"] = max(0, clock() - started)
    try:
        atomic_json(directory / "guard-last.json", result)
    except (OSError, ValueError) as error:
        record_error(result["internalErrors"], error)
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
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--teams-cmd", default=os.environ.get("FOLLOWUP_IDLE_TEAMS_CMD"))
    try:
        args = parser.parse_args(argv)
        # 빈 값은 기본값(teams_room.py scan)이다. 방(TEAMS_ROOM_CHAT_ID)이 없으면 teams_room이 이벤트 없이 ok를 쓴다.
        teams = (command(args.teams_cmd, "") if args.teams_cmd
                 else [sys.executable, str(ROOT / "teams_room.py"), "scan"])
        if args.force_refresh:
            teams += ["--force-refresh"]
        result, code = decide(args.dir.expanduser(), command(args.scan_cmd, "followup_scan.py"),
                              command(args.preflight_cmd, "preflight_env.py"), teams_cmd=teams)
    except ValueError as error:
        result = {"idle": False, "reasons": ["internal-error"], "events": 0, "queue": {}, "internalErrors": []}
        record_error(result["internalErrors"], error)
        code = 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
