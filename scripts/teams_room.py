#!/usr/bin/env python3
"""Teams 방 감시: 설정한 방(TEAMS_ROOM_CHAT_ID, 이름 TEAMS_ROOM_NAME)에서 다른 사람이 나를 부른 메시지를 이벤트로 뽑는다.

Usage:
  teams_room.py scan  [--dir D] [--out PATH]                  # 가드(followup_idle)가 매 tick 호출. 읽기 전용 Teams 조회
  teams_room.py draft --event-id teams:<ID> --text-file F --mention LABEL [--key PROJ-N] [--dir D] [--no-open]
  teams_room.py send  --draft PATH [--dir D]                   # 결정 승인(페이지 버튼·할 일 답글) 뒤에만 호출한다
  teams_room.py self-messages [--limit 50]                    # 자기 대화와 현재 계정의 정제된 필드만 조회
  teams_room.py notify --text TEXT [--dir D]                  # 첫 발송 검증 성공 뒤 teams-self.json 신원 고정

- 설정(config.env): TEAMS_ROOM_CHAT_ID(감시할 방), TEAMS_ROOM_NAME(표시 이름), TEAMS_ME_MRI·TEAMS_ME_NAME(내 계정),
  TEAMS_ME_NAMES(나를 부르는 호칭, 쉼표로 나눈 정규식 조각, 대소문자 무시), TEAMS_SELF_CHAT_ID(자기 대화),
  TEAMS_NATIVE_CLI(<채팅 CLI 스킬>의 CLI 경로), TEAMS_NOTIFY_CHAT_ID(알림 대상, 없으면 자기 대화).
  TEAMS_ROOM_CHAT_ID가 비면 scan은 Teams를 부르지 않고 {"status": "ok", "configured": false, "events": []}를 쓴다(exit 0).
- CLI 계약: `python3 <TEAMS_NATIVE_CLI> messages|send|verify|status|connect ...`가 JSON 한 개를 stdout에 낸다
  ({"status": "PASS"|..., "error": "<CODE>", "messages": [{id,time,author,authorMri,text,mentions}], "messageId", "account"}).
- 읽기: <채팅 CLI 스킬> `messages`(읽기 전용, 정제된 필드만). 토큰·원문 HTML은 받지 않는다.
- 상태: `<dir>/teams-room.json` {"<chatId>": {"mark", "lastMineAt", "pending", "lastError", "monitoring"}}. 다른 키는 보존한다.
  첫 실행은 기준선만 잡고 이벤트를 만들지 않는다. 이벤트는 `state.json` followup.handled["teams:<ID>"]가 생길 때까지 남는다.
- 출력 `teams-scan.json`: {status: ok|unavailable, monitoring: {lastSuccessAt, error, failures, retryAt, cooldown}, error?, baseline, events:[...], errors:[...]}. Teams를 못 읽어도
  이전에 잡힌 미처리 이벤트는 그대로 내보내고 exit 0(가드 tick 전체를 깨지 않는다). 내부 오류만 exit 2.
- 답장: 자동 발송하지 않는다. draft가 초안과 결정(teams-reply, decision-request)을 만들고, 사람이 승인하면(결정 페이지 버튼
  또는 할 일 답글 '승인') decisions.jsonl에 승인 줄을 먼저 쓰고 send를 부른다. send는 그 승인 줄(draftId·본문 해시 일치)이
  없으면 Teams CLI를 부르지 않는다. 발송은 한 번뿐이고(`--execute` 후 `verify`), 결과가 불확실해도 재시도하지 않는다.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import followup_config as cfg  # noqa: E402
from state_update import locked, transaction  # noqa: E402

TZ = cfg.TZ
CHAT_ID = cfg.TEAMS_ROOM_CHAT_ID  # 비면 방 감시 꺼짐(scan은 configured=false)
CHAT_NAME = cfg.TEAMS_ROOM_NAME
ME_MRI = cfg.TEAMS_ME_MRI
ME_NAME = cfg.TEAMS_ME_NAME


def trigger_pattern(names):
    """나를 부르는 호칭(정규식 조각들). 영문은 대소문자 무시. 비면 이름 호출은 감지하지 않는다(멘션만)."""
    names = [name for name in names if name]
    return re.compile("|".join(f"(?:{name})" for name in names), re.I) if names else None


# 호칭 예: 실제 방에서 "<이름>선임이 …"처럼 부른 사례까지 넣는다(2026-10-23).
TRIGGER = trigger_pattern(cfg.TEAMS_ME_NAMES)
KEY_RE = re.compile(r"(?<![A-Za-z0-9])" + re.escape(cfg.PROJECT) + r"-\d+(?!\d)")
JIRA_BROWSE = cfg.BROWSE  # morning_check.py·RUNBOOK와 같은 사이트
NO_KEY = "TEAMS-ROOM"
LIMIT = 50
CONTEXT = 5
CLI_TIMEOUT = 90
RECONNECT = ("TEAMS_WORKER_NOT_READY", "TASK_SPACE_NOT_FOUND")


class Unavailable(Exception):
    """Teams를 읽거나 보낼 수 없음. 코드만 담는다(원문 출력은 담지 않는다)."""


def default_cli():
    # PATH가 아니라 HOME 기준이다: 시험(HOME=임시 폴더)에서 실제 브라우저를 부르지 않는다.
    return os.environ.get("TEAMS_NATIVE_CLI") or str(Path.home() / ".local/bin/teams-chat-cli")


def configured():
    """감시할 방이 설정됐는지(TEAMS_ROOM_CHAT_ID)."""
    return bool(CHAT_ID)


def read_json(path, default):
    if not path.exists():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, type(default)):
        raise ValueError("unexpected json shape")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, delete=False) as out:
            name = out.name
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def code_of(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", value) else fallback


def call_cli(argv, cli=None):
    """채팅 CLI(TEAMS_NATIVE_CLI)를 실행하고 JSON 결과를 돌려준다. 실패는 코드만 남긴 Unavailable."""
    path = cli or default_cli()
    if not Path(path).exists():
        raise Unavailable("TEAMS_CLI_NOT_FOUND")
    try:
        proc = subprocess.run([sys.executable, path, *argv],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=CLI_TIMEOUT)
        result = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        raise Unavailable("TEAMS_CLI_TIMEOUT") from None
    except (OSError, ValueError):
        raise Unavailable("TEAMS_CLI_INVALID_OUTPUT") from None
    if not isinstance(result, dict):
        raise Unavailable("TEAMS_CLI_INVALID_OUTPUT")
    return result


def fetch_messages(cli=None, limit=LIMIT):
    def once():
        result = call_cli(["messages", "--chat-id", CHAT_ID, "--limit", str(limit)], cli)
        if result.get("status") != "PASS" or not isinstance(result.get("messages"), list):
            raise Unavailable(code_of(result.get("error"), "TEAMS_READ_FAILED"))
        return result["messages"]
    try:
        return once()
    except Unavailable as error:
        if str(error) not in RECONNECT:
            raise
    # 페이지가 다른 주소에 있으면 기존 작업 공간을 다시 붙인다(session.json의 spaceId).
    # 브라우저 재시작으로 그 공간이 사라졌으면 connect가 같은 프로필의 새 공간을 연다
    # (2026-10-29 실측: spaceId 11 소멸 → 32회 연속 BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED).
    connected = call_cli(["connect"], cli)
    if connected.get("status") != "PASS":
        raise Unavailable(code_of(connected.get("error"), "TEAMS_CONNECT_FAILED"))
    return once()


def plain(text):
    """CLI가 이미 정제하지만, 다른 경로의 값도 HTML·엔티티 없이 비교한다."""
    text = re.sub(r"<br\s*/?>|</p>", "\n", str(text or ""), flags=re.I)
    return html.unescape(re.sub(r"<[^>]*>", "", text)).strip()


def is_mine(message):
    return bool((ME_MRI and message.get("authorMri") == ME_MRI) or (ME_NAME and message.get("author") == ME_NAME))


def trigger_of(message):
    """다른 사람이 나를 부른 메시지면 'mention' 또는 'name', 아니면 None."""
    if is_mine(message):
        return None
    if ME_MRI and ME_MRI in (message.get("mentions") or []):
        return "mention"
    return "name" if TRIGGER is not None and TRIGGER.search(plain(message.get("text"))) else None


def order_key(message):
    stamp = str(message.get("time") or "")
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except ValueError:
        at = datetime.min.replace(tzinfo=timezone.utc)
    ident = str(message.get("id") or "")
    return (at, int(ident) if ident.isdigit() else 0, ident)


def newer(message, mark):
    if not mark:
        return True
    return order_key(message) > order_key({"time": mark.get("time"), "id": mark.get("id")})


def handled_ids(directory):
    state = read_json(directory / "state.json", {})
    handled = (state.get("followup") or {}).get("handled") or {}
    if not isinstance(handled, dict):
        raise ValueError("invalid handled state")
    return set(handled)


def make_event(message, previous, how):
    text = plain(message.get("text"))
    keys = list(dict.fromkeys(KEY_RE.findall(text)))
    return {
        "source": "teams", "chatId": CHAT_ID, "chatName": CHAT_NAME,
        "key": keys[0] if keys else NO_KEY, "keys": keys,
        "commentId": "teams:" + str(message["id"]), "messageId": str(message["id"]),
        "author": message.get("author") or "", "authorMri": message.get("authorMri") or "",
        "created": message.get("time") or "", "text": text[:2000], "trigger": how,
        # luna·오케스트레이터가 받는 수신자 맥락: 나를 직접 부른 메시지다.
        "mentionsMe": True, "assignedToMe": False, "backstop": False,
        "context": [{"author": m.get("author") or "", "time": m.get("time") or "",
                     "mine": is_mine(m), "text": plain(m.get("text"))[:300]} for m in previous],
    }


def scan(directory, *, fetch=None, now=None, force_refresh=False, clock=time.monotonic):
    """teams-scan.json 내용을 돌려주고 teams-room.json을 갱신한다.

    Teams 읽기는 잠금 밖에서 하고, 기준점 갱신만 state_update 잠금 안에서 최신 파일에 적용한다.
    """
    instant = now or datetime.now(TZ)
    at = instant.isoformat()
    if fetch is None and not configured():
        # 방이 설정되지 않았다: Teams를 부르지 않고 상태 파일도 만들지 않는다. 가드에게는 이벤트 없는 정상이다.
        return {"source": "teams", "chatId": None, "scannedAt": at, "status": "ok", "configured": False,
                "baseline": False, "events": [], "errors": [],
                "monitoring": {"configured": False, "lastSuccessAt": None, "error": None, "failures": 0,
                               "retryAt": None, "cooldown": False}}
    started = clock()
    prior = read_json(directory / "teams-room.json", {}).get(CHAT_ID, {})
    health = prior.get("monitoring", {})
    cooldown = (not force_refresh and health.get("retryAt") is not None
                and datetime.fromisoformat(health["retryAt"]) > instant)
    out = {"source": "teams", "chatId": CHAT_ID, "scannedAt": at, "status": "ok",
           "baseline": False, "events": [], "errors": []}
    try:
        if cooldown:
            raise Unavailable(health.get("error") or "TEAMS_READ_FAILED")
        messages = (fetch or fetch_messages)()
    except Unavailable as error:
        out.update(status="unavailable", error=code_of(str(error), "TEAMS_READ_FAILED"))
        messages = None
    done = handled_ids(directory)

    def update(data):
        room = data.setdefault(CHAT_ID, {})
        if not isinstance(room, dict) or not isinstance(room.setdefault("pending", {}), dict):
            raise ValueError("invalid room state")
        pending = room["pending"]
        if messages is None:
            if not cooldown:
                failures = int(room.get("monitoring", {}).get("failures", 0)) + 1
                room["monitoring"] = dict(lastSuccessAt=room.get("monitoring", {}).get("lastSuccessAt"),
                    error=out["error"], failures=failures,
                    retryAt=(instant + timedelta(seconds=1800 if failures == 1 else 3600)).isoformat())
                room["lastError"] = {"at": at, "error": out["error"]}
        else:
            rows = sorted((m for m in messages if isinstance(m, dict) and m.get("id")), key=order_key)
            mark = room.get("mark")
            out["baseline"] = mark is None
            fresh = [i for i, m in enumerate(rows) if newer(m, mark)]
            if mark and len(rows) >= LIMIT and len(fresh) == len(rows):
                # 두 tick 사이 메시지가 읽기 한도보다 많으면 사이 메시지를 못 봤을 수 있다.
                out["errors"].append({"scope": "teams", "error": "possible-gap"})
            for i in fresh:
                m = rows[i]
                if is_mine(m):
                    last = room.get("lastMineAt")
                    if not last or order_key(m) > order_key({"time": last}):
                        room["lastMineAt"] = m.get("time")
                    continue
                how, cid = trigger_of(m), "teams:" + str(m["id"])
                if how and mark is not None and cid not in pending:
                    pending[cid] = make_event(m, rows[max(0, i - CONTEXT):i], how)
            if rows and (mark is None or newer(rows[-1], mark)):
                room["mark"] = {"time": rows[-1].get("time"), "id": str(rows[-1]["id"])}
            room.pop("lastError", None)
            room["monitoring"] = dict(lastSuccessAt=at, error=None, failures=0, retryAt=None)
        for cid in [cid for cid in pending if cid in done]:
            del pending[cid]
        last_mine = room.get("lastMineAt")
        for event in sorted(pending.values(), key=lambda e: order_key({"time": e.get("created"), "id": e.get("messageId")})):
            answered = bool(last_mine) and order_key({"time": last_mine}) > order_key({"time": event.get("created")})
            out["events"].append({**event, "answeredByMeAfter": answered})
        room["scannedAt"] = at
        out["monitoring"] = {**room.get("monitoring", health), "cooldown": cooldown}
        out["timings"] = {"totalSeconds": max(0, clock() - started)}

    transaction(directory, "teams-room.json", update)
    return out


# ---- 답장 초안과 승인된 발송 ----

def text_hash(text, mentions, links):
    return hashlib.sha256(json.dumps([text, mentions, links], ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def find_event(directory, event_id):
    room = read_json(directory / "teams-room.json", {}).get(CHAT_ID, {})
    event = (room.get("pending") or {}).get(event_id)
    if event is None:
        for item in read_json(directory / "teams-scan.json", {"events": []}).get("events", []):
            if isinstance(item, dict) and item.get("commentId") == event_id:
                event = item
    if not isinstance(event, dict):
        raise ValueError("unknown event")
    return event


def draft(directory, event_id, text, mention_label, *, key=None, opener=None):
    """초안·결정 spec을 쓰고 결정 페이지를 연다. 발송하지 않는다."""
    event = find_event(directory, event_id)
    if not text.strip() or len(text.encode("utf-8")) > 20000:
        raise ValueError("text must be non-empty and at most 20000 bytes")
    if not mention_label or mention_label not in text or not event.get("author"):
        raise ValueError("mention label must appear in the text")
    keys = list(dict.fromkeys(KEY_RE.findall(text)))
    links = [[k, JIRA_BROWSE + k] for k in keys]
    mentions = [[mention_label, event["author"]]]
    drafts = directory / "teams-drafts"
    previous = []
    for old in sorted(drafts.glob("*.json")) if drafts.exists() else []:
        row = read_json(old, {})
        if row.get("eventId") != event_id:
            continue
        if row.get("status") in ("sending", "sent", "unknown"):
            # 이미 보냈거나 결과가 불확실하면 새 초안으로 다시 보내지 않는다.
            raise ValueError(f"event already has a {row['status']} reply: " + old.name)
        if row.get("status") == "pending":
            previous.append((old, row))
    draft_id = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    record = {"draftId": draft_id, "status": "pending", "chatId": event.get("chatId") or CHAT_ID,
              "chatName": event.get("chatName") or CHAT_NAME, "eventId": event_id,
              "replyTo": event.get("messageId"), "key": key or event.get("key") or NO_KEY,
              "asker": event["author"], "text": text, "mentions": mentions, "links": links,
              "textSha256": text_hash(text, mentions, links), "createdAt": datetime.now(TZ).isoformat()}
    draft_path = drafts / (draft_id + ".json")
    atomic_json(draft_path, record)
    for old, row in previous:  # 보류·시간 초과로 남은 이전 초안은 승인돼도 보내지 않는다.
        row.update(status="superseded", supersededBy=draft_id)
        atomic_json(old, row)
    spec = {"kind": "teams-reply", "key": record["key"], "title": f"Teams 답장을 보낼까요? ({record['asker']})",
            "summary": f"{record['chatName']} 방에서 {record['asker']}님이 부른 메시지에 대한 답장 초안입니다.",
            "draft": str(draft_path), "draftId": draft_id, "textSha256": record["textSha256"],
            "chatName": record["chatName"], "asker": record["asker"], "question": event.get("text", ""),
            "text": text, "mentions": mentions, "links": links}
    spec_path = directory / "decision-pages" / f"teams-{draft_id}.json"
    atomic_json(spec_path, spec)
    opened = (opener or open_page)(spec_path)
    return {"status": "DRAFTED", "draftId": draft_id, "draft": str(draft_path),
            "spec": str(spec_path), "pageOpened": bool(opened)}


def open_page(spec_path):
    proc = subprocess.run(["/bin/bash", str(HERE / "decision-request"), "spec", str(spec_path)],
                          capture_output=True, text=True, timeout=120)
    return proc.returncode == 0


def approval_for(decisions_path, record):
    if not decisions_path.exists():
        return None
    for line in reversed(decisions_path.read_text(encoding="utf-8").splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("kind") == "teams-reply" and row.get("option") == "approve"
                and row.get("by") in ("button", "todo-app") and row.get("draftId") == record["draftId"]
                and row.get("textSha256") == record["textSha256"]):
            return row
    return None


def send(directory, draft_path, *, cli=None):
    """승인 줄이 있을 때만 한 번 보낸다. 반환: (결과, exit code)."""
    record = read_json(Path(draft_path), {})
    if not record.get("draftId") or record.get("textSha256") != text_hash(record.get("text", ""),
                                                                        record.get("mentions", []), record.get("links", [])):
        return {"status": "BLOCKED", "error": "DRAFT_INVALID_OR_CHANGED"}, 1
    if record.get("status") != "pending":
        return {"status": "BLOCKED", "error": "DRAFT_NOT_PENDING", "draftStatus": record.get("status")}, 1
    approval = approval_for(directory / "decisions.jsonl", record)
    if approval is None:
        return {"status": "BLOCKED", "error": "NO_APPROVAL"}, 1
    # 되돌릴 수 없는 경계 앞에서 먼저 표시한다: 같은 초안을 두 번 보내지 않는다.
    record.update(status="sending", approvedAt=approval.get("at"))
    atomic_json(Path(draft_path), record)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as body:
        body.write(record["text"])
        body_path = body.name
    try:
        argv = ["send", "--chat-id", record["chatId"], "--text-file", body_path]
        for label, member in record["mentions"]:
            argv += ["--mention", label, member]
        for label, url in record["links"]:
            argv += ["--link", label, url]
        try:
            sent = call_cli(argv + ["--execute"], cli)
        except Unavailable as error:
            sent = {"status": "SEND_OUTCOME_UNKNOWN", "error": str(error)}
    finally:
        Path(body_path).unlink(missing_ok=True)
    result = {"status": sent.get("status"), "error": sent.get("error"), "messageId": sent.get("messageId")}
    if sent.get("status") == "PASS" and sent.get("messageId"):
        try:
            check = call_cli(["verify", "--chat-id", record["chatId"], "--message-id", str(sent["messageId"])], cli)
        except Unavailable as error:
            check = {"status": "FAIL", "error": str(error)}
        result["verify"] = check.get("status")
    final = "sent" if result["status"] == "PASS" and result.get("verify") == "PASS" else (
        "failed" if result["status"] == "FAIL" else "unknown")
    record.update(status=final, result=result, finishedAt=datetime.now(TZ).isoformat())
    atomic_json(Path(draft_path), record)
    return {**result, "draft": record["draftId"], "draftStatus": final}, 0 if final == "sent" else 1


SELF_CHAT = cfg.TEAMS_SELF_CHAT_ID  # Teams 예약 ID(기본 48:notes): 로그인한 본인의 자기 대화
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def state_directory(directory=None):
    return Path(directory if directory is not None else (
        os.environ.get("JIRA_WAKE_DIR") or str(cfg.HOME_DIR))).expanduser()


def sanitized_identity(account):
    # <채팅 CLI 스킬>은 현재 사용자 id와 tenant id를 이 필드로 내보낸다.
    if not isinstance(account, dict) or any(
            not isinstance(account.get(key), str) or not UUID_RE.fullmatch(account[key])
            for key in ("id", "tenantId")):
        raise Unavailable("TEAMS_IDENTITY_INVALID")
    return {key: account[key] for key in ("id", "tenantId")}


def self_messages(*, cli=None, limit=LIMIT):
    """같은 읽기 응답의 신원과 자기 대화만 반환한다. 신원 비교·커서는 수신기가 맡는다."""
    if type(limit) is not int or not 1 <= limit <= LIMIT:
        raise ValueError("limit must be between 1 and 50")
    result = call_cli(["messages", "--chat-id", SELF_CHAT, "--limit", str(limit)], cli)
    if result.get("status") != "PASS":
        raise Unavailable(code_of(result.get("error"), "TEAMS_READ_FAILED"))
    account = sanitized_identity(result.get("account"))
    messages = result.get("messages")
    fields = ("id", "time", "text", "authorMri")
    if not isinstance(messages, list) or any(
            not isinstance(row, dict) or not all(isinstance(row.get(key), str) for key in fields)
            or not row["id"] for row in messages):
        raise Unavailable("TEAMS_SELF_MESSAGES_INVALID")
    return {"status": "PASS", "account": account,
            "messages": [{key: row[key] for key in fields} for row in messages[:limit]]}


def pin_self_identity(directory, *, cli=None):
    """첫 검증 성공 때만 status 신원을 저장한다. 기존 신원은 재설정하지 않는다."""
    path = directory / "teams-self.json"

    def existing():
        try:
            pin = read_json(path, {})
        except ValueError:
            raise Unavailable("TEAMS_IDENTITY_INVALID") from None
        return sanitized_identity({"id": pin.get("accountId"), "tenantId": pin.get("tenantId")})

    with locked(directory):
        if path.exists():
            existing()
            return
    result = call_cli(["status"], cli)
    if result.get("status") != "PASS":
        raise Unavailable(code_of(result.get("error"), "TEAMS_STATUS_FAILED"))
    account = sanitized_identity(result.get("account"))
    with locked(directory):
        if path.exists():
            if existing() != account:
                raise Unavailable("TEAMS_IDENTITY_MISMATCH")
            return
        atomic_json(path, {"accountId": account["id"], "tenantId": account["tenantId"]})


def notify(text, *, cli=None, directory=None):
    """승인·결정·조치 요청을 본인 Teams 자기 대화로 보낸다(사용자 지시 2026-10-30). 반환: sent|failed|unknown."""
    chat = os.environ.get("TEAMS_NOTIFY_CHAT_ID") or SELF_CHAT
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as body:
        body.write(text)
        body_path = body.name
    try:
        sent = call_cli(["send", "--chat-id", chat, "--text-file", body_path, "--execute"], cli)
    except Unavailable as error:
        return {"status": "unknown", "error": str(error)}
    finally:
        Path(body_path).unlink(missing_ok=True)
    # SEND_UNVERIFIED도 messageId가 있으면 이미 나간 것이다. 다시 보내지 않고 verify로 판정한다(2026-10-30 실측).
    if sent.get("status") not in ("PASS", "SEND_UNVERIFIED") or not sent.get("messageId"):
        return {"status": "failed" if sent.get("status") == "FAIL" else "unknown", "error": sent.get("error")}
    try:
        check = call_cli(["verify", "--chat-id", chat, "--message-id", str(sent["messageId"])], cli)
    except Unavailable as error:
        check = {"status": "FAIL", "error": str(error)}
    result = {"status": "sent" if check.get("status") == "PASS" else "unknown", "messageId": sent["messageId"]}
    if result["status"] == "sent":
        try:
            pin_self_identity(state_directory(directory), cli=cli)
        except Unavailable as error:
            # 발송 성공과 신원 저장 실패는 별개다. 신원이 없으면 수신기가 결정을 처리하지 않는다.
            result["identityError"] = str(error)
        except (OSError, ValueError):
            result["identityError"] = "TEAMS_IDENTITY_PIN_FAILED"
    return result


def notify_detached(text):
    """호출자(가드·실행 잠금)를 붙잡지 않도록 notify를 분리된 프로세스로 띄운다. 결과는 기다리지 않는다."""
    try:
        subprocess.Popen([sys.executable, str(HERE / "teams_room.py"), "notify", "--text", text],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("notify")
    p.add_argument("--text", required=True)
    p.add_argument("--dir", type=Path, default=state_directory())
    sub.add_parser("self-messages").add_argument("--limit", type=int, default=LIMIT)
    for name in ("scan", "draft", "send"):
        p = sub.add_parser(name)
        p.add_argument("--dir", type=Path, default=state_directory())
        if name == "scan":
            p.add_argument("--out", type=Path)
            p.add_argument("--force-refresh", action="store_true")
        elif name == "draft":
            p.add_argument("--event-id", required=True)
            p.add_argument("--text-file", type=Path, required=True)
            p.add_argument("--mention", required=True, help="본문에 그대로 있는 요청자 호칭(예: '김철수 선임님')")
            p.add_argument("--key")
            p.add_argument("--no-open", action="store_true")
        else:
            p.add_argument("--draft", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "self-messages":
        try:
            result = self_messages(limit=args.limit)
        except Unavailable as error:
            print(json.dumps({"status": "FAIL", "error": str(error)}))
            return 1
        except ValueError:
            print(json.dumps({"status": "FAIL", "error": "TEAMS_LIMIT_INVALID"}))
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "notify":
        result = notify(args.text, directory=args.dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "sent" else 1
    directory = args.dir.expanduser()
    try:
        if args.command == "scan":
            out = scan(directory, force_refresh=args.force_refresh)
            atomic_json(args.out or directory / "teams-scan.json", out)
            print(f"teams status={out['status']} events={len(out['events'])} errors={len(out['errors'])}"
                  + (f" error={out['error']}" if out.get("error") else "")
                  + ("" if out.get("configured", True) else " configured=false"))
            return 0
        if args.command == "draft":
            result = draft(directory, args.event_id, args.text_file.read_text(encoding="utf-8"), args.mention,
                           key=args.key, opener=(lambda _: False) if args.no_open else None)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        result, code = send(directory, args.draft)
        print(json.dumps(result, ensure_ascii=False))
        return code
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # 원문 출력은 비밀을 담을 수 있다. 오류 종류만 남긴다.
        print(json.dumps({"status": "FAIL", "error": type(error).__name__,
                          "detail": str(error)[:200] if isinstance(error, ValueError) else ""}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
