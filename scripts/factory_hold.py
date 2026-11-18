#!/usr/bin/env python3
"""공장 일시정지(hold). 시각 만료가 있는 설정 하나로 새 작업 단위 시작을 코드에서 막는다.

파일 `<state_dir>/hold.json` (쓰기는 이 CLI만):
  {"until": "2026-11-06T12:00:00+09:00", "only": ["스테이징 QA 요청", "스테이징 검수 완료"],
   "freeze": [], "reason": "...", "setBy": "...", "at": "<ISO>"}
- only: 비어 있지 않으면 Jira 상태가 이 목록에 있는 티켓만 새 단위를 시작한다. 상태를 모르면(스캔에 없음) 막는다.
- freeze: `stg`면 스테이징 단계(미사용 stg 승인이 있는 티켓, RUNBOOK §7-3-6 또는 스테이징 브랜치(BRANCH_STG)를 적은 brief,
  stg-approval-request)를 막는다. Jira 상태 이름이면 그 상태의 티켓은 새 단위를 시작하지 않는다.
- 파일 없음: 평소대로. until이 지났으면 무시하고 해제 알림을 한 번 남긴다.
  형식 오류: 파일을 보존하고 모든 단위 시작을 막는다(`hold:invalid`, 알림 한 번). 오늘 일시정지를 걸어 둔 사람의
  뜻이 무엇이든 그보다 더 멀리 가지 않는 쪽이다.
- 막힌 항목의 큐 waiting·handled는 건드리지 않는다. 해제되면 그대로 다시 고른다.

CLI: factory_hold.py set --until ISO [--only 상태,...] [--freeze stg,상태,...] --reason 문구
     factory_hold.py clear | status [--key KEY] | check [--key KEY] [--brief FILE] [--stage stg]
check exit: 0 진행 가능, 3 hold로 막힘, 2 잘못된 사용.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import followup_config as cfg

TZ = cfg.TZ
NAME = "hold.json"
STAGES = ("stg",)
KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def now_kst(now=None):
    return now if now is not None else datetime.now(TZ)


def parse_until(value):
    if not isinstance(value, str):
        raise ValueError("until must be ISO text")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("until needs an offset such as +09:00")
    return moment


def names(value, field):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(field + " must be a list of names")
    return [v.strip() for v in value]


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("hold must be an object")
    parse_until(data.get("until"))
    only = names(data.get("only", []), "only")
    freeze = names(data.get("freeze", []), "freeze")
    if not only and not freeze:
        raise ValueError("hold needs only or freeze")
    if not isinstance(data.get("reason"), str) or not data["reason"].strip():
        raise ValueError("hold needs reason")
    return dict(data, only=only, freeze=freeze)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def notify(directory, text):
    """[공장 알림] 항목(decision_inbox)으로 한 번 알린다. 시험은 JIRA_HOLD_NOTIFY_FILE로 받는다."""
    target = os.environ.get("JIRA_HOLD_NOTIFY_FILE")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(text.replace("\n", " / ") + "\n")
        return
    try:
        import decision_inbox
        decision_inbox.alert_detached(text)
    except (ImportError, OSError, ValueError):
        pass


def once(directory, marker, text):
    path = directory / marker
    try:
        with path.open("x"):
            pass
    except FileExistsError:
        return False
    notify(directory, text)
    return True


def load(directory, now=None):
    """{"state": "none"|"active"|"expired"|"invalid", "hold": dict|None, "error": str|None}."""
    path = Path(directory) / NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"state": "none", "hold": None, "error": None}
    except OSError as error:
        raw, failure = "", "unreadable: " + type(error).__name__
    else:
        failure = None
    if failure is None:
        try:
            data = validate(json.loads(raw))
        except ValueError as error:
            failure = str(error)[:200]
    if failure is not None:
        once(Path(directory), ".hold-invalid-" + digest(raw) + "-alerted",
             f"[{cfg.LINE_TITLE} 조치 필요]\n공장 일시정지 파일 형식 오류({failure}): {path}. 고칠 때까지 모든 작업 단위 시작을 막는다. "
             "factory_hold.py status로 확인 후 set 또는 clear")
        return {"state": "invalid", "hold": None, "error": failure}
    if parse_until(data["until"]) <= now_kst(now):
        once(Path(directory), ".hold-" + digest(raw) + "-expired",
             f"[공장 알림]\n공장 일시정지 만료({data['until']}): 평소대로 처리한다. 사유: {data['reason']}")
        return {"state": "expired", "hold": data, "error": None}
    return {"state": "active", "hold": data, "error": None}


def statuses(directory):
    """마지막 스캔(followup-scan.json)이 본 Jira 상태. 이벤트·큐·조회 티켓 순서로 합친다."""
    try:
        data = json.loads((Path(directory) / "followup-scan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    known = {}
    for source in (data.get("statuses"), data.get("queueStatuses")):
        if isinstance(source, dict):
            known.update({k: v for k, v in source.items() if isinstance(k, str) and isinstance(v, str)})
    for event in data.get("events") or []:
        if isinstance(event, dict) and isinstance(event.get("key"), str) and isinstance(event.get("status"), str):
            known[event["key"]] = event["status"]
    return known


def unused_stg(directory, key):
    try:
        approvals = json.loads((Path(directory) / "approvals.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # 승인 파일을 읽지 못하면 스테이징 단계일 수 있다고 본다.
    approval = approvals.get(key) if isinstance(approvals, dict) else None
    return isinstance(approval, dict) and "stg" in approval and not approval.get("usedAt")


def stg_brief(text):
    # 스테이징 브랜치 이름을 낱말로 적었으면(origin/stg, merge/PROJ-1-stg 등) 스테이징 단계로 본다.
    if re.search(r"(?<![\w.])" + re.escape(cfg.BRANCH_STG) + r"(?![\w.])", text):
        return True
    try:
        from runbook_section import cited_sections
        return "7-3-6" in cited_sections(text)["RUNBOOK"]
    except ValueError:
        return False


def blocked(directory, key=None, *, status=None, brief=None, stage=None, now=None, loaded=None):
    """막힘 사유(`only(...)`·`freeze(...)`·`invalid`) 또는 None. key가 없는 단위(아침 점검)는 상태 규칙을 보지 않는다."""
    loaded = loaded or load(directory, now)
    if loaded["state"] == "invalid":
        return "invalid"
    if loaded["state"] != "active":
        return None
    hold = loaded["hold"]
    freeze = set(hold["freeze"])
    if stage in freeze or ("stg" in freeze and brief is not None and stg_brief(brief)):
        return f"freeze({stage or 'stg'})"
    if not key:
        return None
    if "stg" in freeze and unused_stg(directory, key):
        return "freeze(stg)"
    rules = hold["only"] or freeze - set(STAGES)
    if not rules:
        return None
    status = status or statuses(directory).get(key)
    if status is None:
        return "only(status=unknown)" if hold["only"] else None
    if status in freeze:
        return f"freeze(status={status})"
    if hold["only"] and status not in hold["only"]:
        return f"only(status={status})"
    return None


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def split(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def summary(hold):
    parts = []
    if hold["only"]:
        parts.append("처리할 상태=" + ", ".join(hold["only"]))
    if hold["freeze"]:
        parts.append("멈춤=" + ", ".join(hold["freeze"]))
    return " · ".join(parts) + f" · 해제 {hold['until']} · 사유: {hold['reason']}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("set", "clear", "status", "check"))
    parser.add_argument("--dir", type=Path,
                        default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    parser.add_argument("--until")
    parser.add_argument("--only")
    parser.add_argument("--freeze")
    parser.add_argument("--reason")
    parser.add_argument("--set-by", default=os.environ.get("USER", "unknown"))
    parser.add_argument("--key")
    parser.add_argument("--brief", type=Path)
    parser.add_argument("--stage", choices=STAGES)
    args = parser.parse_args(argv)
    directory = args.dir.expanduser()
    path = directory / NAME
    try:
        if args.key is not None and args.key and not KEY.fullmatch(args.key):
            raise ValueError("invalid key")
        if args.command == "set":
            hold = validate({"until": args.until, "only": split(args.only), "freeze": split(args.freeze),
                             "reason": args.reason, "setBy": args.set_by, "at": now_kst().isoformat()})
            if parse_until(hold["until"]) <= now_kst():
                raise ValueError("until is already past")
            unknown = [name for name in hold["freeze"] if name.islower() and name not in STAGES]
            if unknown:
                raise ValueError("unknown freeze stage: " + ", ".join(unknown) + " (stages: " + ", ".join(STAGES) + ")")
            atomic_write(path, hold)
            notify(directory, "[공장 알림]\n공장 일시정지 시작: " + summary(hold))
            print(json.dumps({"state": "active", "hold": hold}, ensure_ascii=False))
            return 0
        if args.command == "clear":
            loaded = load(directory)
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed and loaded["state"] in ("active", "invalid"):
                notify(directory, "[공장 알림]\n공장 일시정지 해제(수동): 평소대로 처리한다.")
            print(json.dumps({"state": "none", "cleared": existed}, ensure_ascii=False))
            return 0
        brief = args.brief.read_text(encoding="utf-8") if args.brief else None
        loaded = load(directory)
        reason = blocked(directory, args.key or None, brief=brief, stage=args.stage, loaded=loaded)
        if args.command == "status":
            print(json.dumps({**loaded, **({"key": args.key, "blocked": reason} if args.key else {})},
                             ensure_ascii=False))
            return 0
        if reason:
            print(f"hold:{args.key or '-'} {reason}", file=sys.stderr)
            return 3
        return 0
    except (OSError, ValueError, TypeError) as error:
        print("factory_hold: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
