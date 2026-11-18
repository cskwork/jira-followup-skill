#!/usr/bin/env python3
"""공유 상태를 잠금 안에서 읽고 키 단위로 갱신한다."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402
import lifecycle  # noqa: E402


# human-risk 대기의 할 일 앱 표시: 사용자가 정할 질문 한 문장과 그대로 보낼 권장 답 한 줄.
HUMAN_ASK = ("question", "suggest")
# 사람만 할 수 있는 선행 조치(비밀값 생성 등)를 기다리는 human-risk: 질문 대신 할 일과 실행지 경로를 보인다.
# 권장 답이 없어야 한 단어 답('네'·'continue')을 부르지 않는다(2026-11-03~06 한 티켓에서 9회).
HUMAN_ACTION = ("action", "runSheet")
BOOKKEEPING = {"deferredAt", "deferReason", "deferCount", "updatedAt", "createdAt",
               "backoffUntil", "backoffLevel"}
# 큐 note 상한(2026-11-16 맥락 줄이기): note는 ' | '로 새 기록을 뒤에 덧붙인다. 상한을 넘은 앞부분은
# `<state_dir>/notes/<KEY>.md`에 덧붙여 옮긴다(한 티켓 note 약 44,000자를 에이전트가 매번 읽었다).
NOTE_SEPARATOR = " | "
NOTE_MAX_LINES = 20
NOTE_MAX_CHARS = 4000
TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")
# 후속 수정(followupSince)이 풀리는 배포 단계 순번.
DELIVERY_RESULT_POSTED = lifecycle.DELIVERY.index("result-posted")


def directory_argument(value):
    # Path('')가 현재 폴더로 바뀌기 전에 명시적으로 잘못 준 경로를 거부한다.
    if not value or Path(value).expanduser().resolve() == Path('/'):
        raise argparse.ArgumentTypeError("directory must not be empty or filesystem root")
    return Path(value)


def item_digest(item):
    content = {key: value for key, value in item.items() if key not in BOOKKEEPING}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", suffix=".tmp", delete=False) as out:
            temporary = Path(out.name)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_object(path, optional=True):
    path = Path(path)
    if optional and not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected object: " + str(path))
    return value


def _target(directory, filename):
    directory = Path(directory).resolve()
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory) or path == directory:
        raise ValueError("state filename must stay within directory")
    return path


@contextmanager
def authorized(directory, token=None, key=None):
    """소유 확인부터 쓰기까지 회수를 막는다. 순서는 항상 run → state다."""
    if token is None:
        yield None
        return
    import run_lock
    with run_lock.locked(Path(directory)):
        meta = run_lock.run_meta(Path(directory), token)
        if meta['kind'] == 'ticket' and meta['key'] != key:
            raise PermissionError('ticket key does not match lease')
        yield meta


def _inbox_path(directory, key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Z][A-Z0-9]+-\d+', key):
        raise ValueError('invalid ticket key')
    return _target(directory, 'tickets/' + key + '.inbox.jsonl')


def _inbox_lines(directory, key):
    """inbox 원문 줄과 해석한 이벤트(깨졌으면 None)를 함께 돌려준다."""
    path = _inbox_path(directory, key)
    lines = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not (isinstance(row, dict) and isinstance(row.get('commentId'), str)
                and row['commentId'] and row.get('key', key) == key):
            row = None
        lines.append((line, row))
    if any(row is None for _, row in lines):
        # 깨진 줄 하나가 그 키의 전달·ACK를 모두 막지 않게, 원문은 두고 경보만 한 번 남긴다.
        import run_lock
        run_lock.invalid_alert(Path(directory), path)
    return lines


def read_inbox(directory, key):
    return [row for _, row in _inbox_lines(directory, key) if row is not None]


def _event_belongs(directory, key, identity):
    if identity == 'intake:' + key:
        return True
    if any(row['commentId'] == identity for row in read_inbox(directory, key)):
        return True
    for filename in ('followup-scan.json', 'teams-scan.json'):
        events = read_object(Path(directory) / filename).get('events', [])
        if any(isinstance(row, dict) and row.get('key') == key and row.get('commentId') == identity
               for row in events):
            return True
    return False


def _patch_scope(directory, filename, path, key, values):
    name = _target(directory, filename).relative_to(Path(directory).resolve()).as_posix()
    if name in ('followup-queue.json', 'approvals.json') and path and path[0] == key:
        return
    if (name == 'state.json' and len(path) == 3 and path[:2] == ['followup', 'handled']
            and _event_belongs(directory, key, path[2]) and values.get('key', key) == key):
        return
    raise PermissionError('write outside ticket scope')


def transaction(directory, filename, callback):
    """최신 객체에 callback을 적용한다. callback 실패 시 원본을 유지한다."""
    target = _target(directory, filename)
    with locked(directory):
        data = read_object(target)
        before = json.loads(json.dumps(data))
        callback(data)
        if target.name == "followup-queue.json":
            _settle_queue(data, before, directory)
        atomic_json(target, data)
        return data


def note_marker(key):
    return f"(앞 기록: notes/{key}.md){NOTE_SEPARATOR}"


def _cap_note(directory, key, row, previous):
    """바뀐 note를 상한 안으로 줄이고 넘친 앞부분을 notes/<KEY>.md에 덧붙인다. 잠금 안에서만 부른다.

    앞에 덧붙인 새 기록(새 + ' | ' + 옛)은 뒤로 옮겨 순서를 '옛 → 새'로 맞춘다. 그래야 뒤를 남기는 상한이
    새 기록을 지우지 않는다. 옮긴 기록은 지우지 않는다(덧붙이기만, 큐 쓰기보다 먼저)."""
    note = row.get("note")
    old = previous.get("note") if isinstance(previous, dict) else None
    if not isinstance(note, str) or note == old:
        return
    if isinstance(old, str) and old.strip() and note.endswith(old) and not note.startswith(old):
        added = note[:-len(old)].rstrip(" |\n")
        if added:
            note = old + NOTE_SEPARATOR + added
    if not TICKET_KEY.fullmatch(str(key)):
        row["note"] = note
        return
    marker = note_marker(key)
    body = note
    while body.startswith(marker):
        body = body[len(marker):]
    if len(note) <= NOTE_MAX_CHARS and note.count("\n") < NOTE_MAX_LINES:
        row["note"] = note
        return
    lines = body.split("\n")
    start = len("\n".join(lines[:-NOTE_MAX_LINES])) + 1 if len(lines) > NOTE_MAX_LINES else 0
    budget = NOTE_MAX_CHARS - len(marker)
    if len(body) - start > budget:
        cut = len(body) - budget
        # 기록 한 덩어리 중간을 자르지 않도록 다음 경계(' | '·줄바꿈) 뒤에서 자른다.
        boundaries = [index + len(mark) for mark in (NOTE_SEPARATOR, "\n")
                      for index in [body.find(mark, cut)] if index != -1]
        start = min(boundaries) if boundaries else cut
    overflow, kept = body[:start].rstrip(" |\n"), body[start:].lstrip(" |\n")
    if overflow:
        path = _target(directory, "notes/" + key + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as out:
            out.write(f"## {stamp} 큐 note에서 옮김\n\n{overflow}\n\n")
            out.flush()
            os.fsync(out.fileno())
    row["note"] = marker + kept


def _delivery_rank(item):
    """delivery 단계 순번. 판정 불가(옛 stage 단위값·잘못된 값)는 -1."""
    try:
        value = lifecycle.delivery(item)
    except ValueError:
        return -1
    return lifecycle.DELIVERY.index(value) if value in lifecycle.DELIVERY else -1


def _settle_queue(data, before, directory):
    for key, row in data.items():
        previous = before.get(key)
        if isinstance(row, dict) and row != previous:
            _cap_note(directory, key, row, previous)
            if not row.get("createdAt"):
                row["createdAt"] = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
            # 대기 중 바뀐 항목도 재개할 때 이전 미룸 횟수를 물려받지 않는다.
            if (isinstance(previous, dict) and "deferCount" in row
                    and item_digest(row) != item_digest(previous)):
                row["deferCount"] = 0
            # 후속 수정 표시(followupSince)는 이전 쓰기부터 있던 값이 결과 댓글 단계(result-posted) 이상으로 새로
            # 전진하면 지운다. 결과를 다시 올렸으니 QA 요청 상태의 human-risk는 다시 QA 회신 대기로 본다.
            if (isinstance(previous, dict) and row.get("followupSince")
                    and row.get("followupSince") == previous.get("followupSince")
                    and _delivery_rank(row) >= DELIVERY_RESULT_POSTED > _delivery_rank(previous)):
                row.pop("followupSince")
            # 사람 확인 질문·권장 답은 human-risk 대기에만 속한다. 대기가 풀리거나 바뀌면 함께 지운다.
            if row.get("waiting") != "human-risk":
                for field in HUMAN_ASK + HUMAN_ACTION:
                    row.pop(field, None)


def refuse(directory, key, reply, rule, action, run_sheet=None, *, token=None):
    """공장 권한 밖이라 실행하지 않은 답을 한 번에 기록한다(2026-11-06 '시크릿 준비 직접해줘').

    한 잠금 안에서 (1) handled[reply]에 action refused·rule을 쓰고(스캔이 거절 한 줄을 보인다), (2) 큐 대기를 사람이 할 일
    형식(action·runSheet, 질문·권장 답 삭제)으로 바꾸며(judge risk는 action 대기를 판정하지 않는다), (3) 거절 전 질문을
    judge가 본 질문(riskSeen)으로 남긴다. 같은 질문이 다시 쓰여도 judge가 사람에게 또 올리지 않는다(11-09 재상신).
    같은 인자로 다시 부르면 아무것도 쓰지 않는다. 바뀐 파일 이름 목록을 돌려준다."""
    texts = {'reply': reply, 'rule': rule, 'action': action}
    if run_sheet is not None:
        texts['run-sheet'] = run_sheet
    for name, value in texts.items():
        if not isinstance(value, str) or not value.strip() or '\n' in value or '\r' in value:
            raise ValueError(f'refuse --{name} must be one nonempty line')
    _inbox_path(directory, key)  # 키 형식 검사
    import judge
    with authorized(directory, token, key) as meta:
        if meta and meta['kind'] == 'ticket' and not _event_belongs(directory, key, reply):
            raise PermissionError('reply outside ticket scope')
        with locked(directory):
            paths = {name: _target(directory, name) for name in ('state.json', 'followup-queue.json', 'judge-state.json')}
            data = {name: read_object(path) for name, path in paths.items()}
            before = json.loads(json.dumps(data))
            item = data['followup-queue.json'].get(key)
            if not isinstance(item, dict):
                raise ValueError(f'{key} is not in followup-queue.json')
            handled = data['state.json'].setdefault('followup', {}).setdefault('handled', {})
            entry = handled.get(reply) if isinstance(handled.get(reply), dict) else {}
            if entry.get('key', key) != key:
                raise ValueError(f'reply {reply} is recorded for another key')
            again = entry.get('action') == 'refused' and entry.get('rule') == rule and entry.get('at')
            handled[reply] = dict(entry, key=key, action='refused', rule=rule,
                                  at=entry['at'] if again else datetime.now(ZoneInfo('Asia/Seoul')).isoformat())
            if item.get('slug'):
                handled[reply].setdefault('slug', item['slug'])
            if item.get('question') or item.get('suggest'):
                seen = data['judge-state.json'].setdefault('riskSeen', {})
                seen[key] = dict(seen.get(key) or {}, hash=judge.risk_hash(item))
            for field in HUMAN_ASK:
                item.pop(field, None)
            item.update(waiting='human-risk', action=action)
            if run_sheet is not None:
                item['runSheet'] = run_sheet
            _settle_queue(data['followup-queue.json'], before['followup-queue.json'], directory)
            changed = [name for name in paths if data[name] != before[name]]
            for name in changed:
                atomic_json(paths[name], data[name])
            return changed


def patch(directory, filename, path, values, delete=(), *, token=None, key=None):
    """path 아래 키만 합친다. 다른 항목과 형제 키는 보존한다."""
    if (not isinstance(path, list) or not all(isinstance(key, str) for key in path)
            or not isinstance(values, dict) or not isinstance(delete, (list, tuple))
            or not all(isinstance(key, str) for key in delete)):
        raise ValueError("path/delete must contain strings and values must be an object")

    queue = _target(directory, filename).name == 'followup-queue.json'

    def apply(data):
        node = data
        for key in path:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise ValueError("patch path must contain objects")
        # 셸 치환 실패로 빈 note가 기존 기록을 지운 일(2026-11-03 실측). 지우려면 --delete로 명시한다.
        for name, value in values.items():
            if (name == 'note' and isinstance(value, str) and not value.strip()
                    and isinstance(node.get(name), str) and node[name].strip()):
                raise ValueError('refusing to blank an existing note; use --delete \'["note"]\' to clear it')
        if queue and 'action' in values and set(HUMAN_ASK) & set(values):
            raise ValueError('action (사람이 할 일) excludes question and suggest (질문·권장 답)')
        node.update(values)
        for key in delete:
            node.pop(key, None)
        # 질문 형식과 할 일 형식은 한쪽만 남긴다. 새로 준 쪽이 이긴다.
        if queue:
            for field in (HUMAN_ASK if 'action' in values else HUMAN_ACTION if set(HUMAN_ASK) & set(values) else ()):
                node.pop(field, None)
        # human-risk를 새로 거는 쓰기는 사용자가 정할 질문과 권장 답 한 줄(또는 사람이 할 일)을 함께 준다(2026-11-05 실측:
        # 작업 기록만 보여 '승인'을 세 번 보내도 바뀐 것이 없었다). 기존 항목의 다른 필드 갱신은 막지 않는다.
        if (queue and values.get('waiting') == 'human-risk'
                and not all(isinstance(node.get(f), str) and node[f].strip() for f in HUMAN_ASK)
                and not (isinstance(node.get('action'), str) and node['action'].strip())):
            raise ValueError('waiting=human-risk needs non-empty question and suggest '
                             '(--question \'<사용자가 정할 것 한 문장>\' --suggest \'<권장 답 한 줄>\') '
                             'or, for a step only a human may do, --action \'<사람이 할 일>\' [--run-sheet <경로>]')

    with authorized(directory, token, key) as meta:
        if meta and meta['kind'] == 'ticket':
            _patch_scope(directory, filename, path, key, values)
        return transaction(directory, filename, apply)


def _pending_scope(line, key):
    try:
        row = json.loads(line)
    except (ValueError, TypeError):
        raise PermissionError('pending focus must identify ticket') from None
    if not isinstance(row, dict) or row.get('key', row.get('focus')) != key:
        raise PermissionError('pending focus outside ticket scope')


def append_pending(directory, line, *, token=None, key=None):
    """한 줄을 잠금 안에서 추가한다. consume의 교체와 경합하지 않는다."""
    if not isinstance(line, str) or not line.strip() or "\n" in line or "\r" in line:
        raise ValueError("pending focus must be one nonempty line")
    with authorized(directory, token, key) as meta:
        if meta and meta['kind'] == 'ticket':
            _pending_scope(line, key)
        with locked(directory):
            target = Path(directory) / "pending-focus.jsonl"
            with target.open("a", encoding="utf-8") as out:
                out.write(line + "\n")
                out.flush()
                os.fsync(out.fileno())


def consume_pending(directory, lines, *, token=None, key=None):
    """처리한 줄의 개수만 지워 읽은 뒤 새로 추가된 동일 줄도 보존한다."""
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ValueError("consumed lines must be an array of strings")
    requested = Counter(line.rstrip("\r\n") for line in lines)
    with authorized(directory, token, key) as meta:
        if meta and meta['kind'] == 'ticket':
            for line in lines:
                _pending_scope(line, key)
        with locked(directory):
            target = Path(directory) / "pending-focus.jsonl"
            current = target.read_text(encoding="utf-8").splitlines(keepends=True) if target.exists() else []
            kept = []
            for line in current:
                identity = line.rstrip("\r\n")
                if requested[identity]:
                    requested[identity] -= 1
                else:
                    kept.append(line)
            atomic_text(target, "".join(kept))
            return len(current) - len(kept)


def _append_inbox(directory, key, event):
    if (not isinstance(event, dict) or not isinstance(event.get('commentId'), str)
            or not event['commentId'] or event.get('key', key) != key):
        raise ValueError('invalid inbox event')
    lines = _inbox_lines(directory, key)
    if any(row is not None and row['commentId'] == event['commentId'] for _, row in lines):
        return False
    # 깨진 줄도 원문 그대로 남긴다.
    kept = [line for line, _ in lines] + [json.dumps(dict(event, key=key), ensure_ascii=False)]
    atomic_text(_inbox_path(directory, key), ''.join(line + '\n' for line in kept))
    return True


def append_inbox(directory, key, event, *, token=None):
    with authorized(directory, token, key):
        with locked(directory):
            return _append_inbox(directory, key, event)


def consume_inbox(directory, key, ids, *, token=None):
    if not isinstance(ids, list) or not all(isinstance(identity, str) for identity in ids):
        raise ValueError('inbox ids must be strings')
    with authorized(directory, token, key):
        with locked(directory):
            lines = _inbox_lines(directory, key)
            kept = [line for line, row in lines if row is None or row['commentId'] not in ids]
            atomic_text(_inbox_path(directory, key), ''.join(line + '\n' for line in kept))
            return len(lines) - len(kept)


def guard_append_inbox(directory, key, event):
    """가드 전용 전달. 임대 확인과 append를 직렬화하며 handled ACK는 쓰지 않는다."""
    import run_lock
    _inbox_path(directory, key)
    with run_lock.locked(Path(directory)):
        with locked(directory):
            lease = read_object(Path(directory) / 'tickets' / (key + '.json'))
            if lease.get('state') not in ('launching', 'live') or not run_lock.valid_token(lease.get('token')):
                return False
            _append_inbox(directory, key, event)
            return True


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--dir', type=directory_argument, default=argparse.SUPPRESS)
    common.add_argument('--token', default=argparse.SUPPRESS)
    common.add_argument('--key', default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    change = commands.add_parser("patch", parents=[common])
    change.add_argument("filename")
    change.add_argument("--path", type=json.loads, default=[])
    change.add_argument("--json", type=json.loads, required=True, dest="values")
    change.add_argument("--delete", type=json.loads, default=[])
    change.add_argument("--question", help="human-risk: 사용자가 정할 것 한 문장(--json의 question과 같음)")
    reply = change.add_mutually_exclusive_group()
    reply.add_argument("--suggest", help="human-risk: 그대로 보낼 권장 답 한 줄(--json의 suggest와 같음)")
    reply.add_argument("--action", help="human-risk: 사람만 할 수 있는 선행 조치 한 줄(질문·권장 답 대신 '할 일:'로 보임)")
    change.add_argument("--run-sheet", dest="runSheet", help="--action과 함께: 사람이 따라 할 실행지 경로")
    reply.add_argument("--from-result", type=Path, help="human-risk: 작업자 결과의 question·suggest 또는 humanAction(line·runSheet)을 그대로 쓴다")
    refused = commands.add_parser('refuse', parents=[common],
                                  help='공장 권한 밖이라 실행하지 않은 답을 기록하고 대기를 사람이 할 일 형식으로 바꾼다(RUNBOOK §0-2)')
    refused.add_argument('ticket', metavar='KEY')
    refused.add_argument('--reply', required=True, help="거절한 답의 이벤트 id(handled 키, 예: todo:<항목>:<해시>)")
    refused.add_argument('--rule', required=True, help='근거 규칙 한 줄(예: RUNBOOK §0 비밀값 생성)')
    refused.add_argument('--action', required=True, help='사람이 해야 할 남은 일 한 줄')
    refused.add_argument('--run-sheet', dest='runSheet', help='사람이 따라 할 실행지 경로')
    append =commands.add_parser("append-pending", parents=[common])
    content = append.add_mutually_exclusive_group(required=True)
    content.add_argument("--line")
    content.add_argument("--json", type=json.loads, dest="row")
    consume = commands.add_parser("consume-pending", parents=[common])
    consume.add_argument("--lines", type=json.loads, required=True)
    inbox = commands.add_parser('append-inbox', parents=[common])
    inbox.add_argument('--json', type=json.loads, required=True, dest='event')
    consume_box = commands.add_parser('consume-inbox', parents=[common])
    consume_box.add_argument('--ids', type=json.loads, required=True)
    lesson = commands.add_parser('lesson', parents=[common])
    lesson.add_argument('--json', type=json.loads, required=True, dest='row')
    fixed = commands.add_parser('lesson-fixed', parents=[common],
                                help="fix-now로 정한 반복 실패를 고친 공장 커밋을 기록하고 '[공장 수정]' 할 일을 닫는다")
    fixed.add_argument('signature', help='lesson 서명(앞 8자 이상)')
    fixed.add_argument('--commit', required=True)
    args = parser.parse_args(argv)
    directory = getattr(args, 'dir', Path(os.environ.get('JIRA_WAKE_DIR') or cfg.HOME_DIR))
    auth = dict(token=getattr(args, 'token', None), key=getattr(args, 'key', None))
    try:
        if args.command == "patch":
            if not isinstance(args.values, dict):
                raise ValueError("--json must be an object")
            if args.runSheet is not None and args.action is None:
                raise ValueError("--run-sheet needs --action")
            if args.from_result is not None:
                if args.question is not None:
                    raise ValueError("--from-result excludes --question")
                import worker_runtime
                if not worker_runtime.valid_result(args.from_result, key=args.path[0] if args.path else None):
                    raise ValueError("--from-result is not a valid worker result for this key")
                args.values.update(worker_runtime.human_fields(read_object(args.from_result, optional=False)))
            args.values.update({name: getattr(args, name) for name in HUMAN_ASK + HUMAN_ACTION
                                if getattr(args, name) is not None})
            result = patch(directory, args.filename, args.path, args.values, args.delete, **auth)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == 'refuse':
            if auth['key'] not in (None, args.ticket):
                raise ValueError('--key must match KEY')
            print(json.dumps(refuse(directory, args.ticket, args.reply, args.rule, args.action, args.runSheet,
                                    token=auth['token']), ensure_ascii=False))
        elif args.command == "append-pending":
            if args.row is not None and not isinstance(args.row, dict):
                raise ValueError("pending focus JSON must be an object")
            append_pending(directory, args.line if args.line is not None else json.dumps(args.row, ensure_ascii=False), **auth)
        elif args.command == 'consume-pending':
            print(consume_pending(directory, args.lines, **auth))
        elif args.command == 'append-inbox':
            print(append_inbox(directory, auth['key'], args.event, token=auth['token']))
        elif args.command == 'lesson':
            import lessons
            if not isinstance(args.row, dict):
                raise ValueError('lesson --json must be an object')
            lessons.validate_class(args.row, required=True)
            with authorized(directory, **auth) as meta:
                if meta and meta['kind'] == 'ticket' and args.row.get('key') != meta['key']:
                    raise PermissionError('lesson outside ticket scope')
                print(json.dumps(lessons.append(directory, args.row), ensure_ascii=False))
        elif args.command == 'lesson-fixed':
            import lessons
            with authorized(directory, **auth) as meta:
                if meta and meta['kind'] == 'ticket':
                    raise PermissionError('lesson-fixed is a factory-wide write, not a ticket write')
                print(json.dumps(lessons.mark_fixed(directory, args.signature, args.commit), ensure_ascii=False))
        else:
            print(consume_inbox(directory, auth['key'], args.ids, token=auth['token']))
    except PermissionError as error:
        print('state-update: ' + str(error), file=sys.stderr)
        return 3
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print("state-update: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
