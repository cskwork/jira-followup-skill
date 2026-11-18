"""Append-only lessons and recurrence decisions, shared by every factory writer."""
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import followup_config as cfg

from state_update import atomic_json, locked, read_object

TZ = cfg.TZ
CLASSES_PATH = Path(__file__).with_name('lesson_classes.json')


def classes():
    """기록자와 프롬프트가 함께 쓰는 닫힌 lesson 분류표."""
    return read_object(CLASSES_PATH, optional=False)


def validate_class(row, *, required=False):
    allowed = classes()
    if ('class' not in row and required) or ('class' in row and (
            not isinstance(row['class'], str) or row['class'] not in allowed)):
        raise ValueError('lesson class must be one of: ' + ', '.join(allowed))


def signature(row):
    """분류는 SHA256(normalized kind + ':class:' + class), other/과거 행은 기존 방식:
    SHA256(kind + normalized what): remove ticket keys, ISO times, hex SHAs,
    comment ids and numbers, then collapse punctuation/whitespace and lowercase.
    Paths and suggestions are evidence, not recurrence identity.
    """
    if row.get('class') and row['class'] != 'other':
        return hashlib.sha256((row['kind'].strip().lower() + ':class:' + row['class']).encode()).hexdigest()
    what = row['what']
    what = re.sub(r'\b\d{4}-\d\d-\d\d(?:[T ][\d:.]+(?:Z|[+-]\d\d:\d\d)?)?', ' ', what)
    what = re.sub(r'\b[A-Z][A-Z0-9]+-\d+\b', ' ', what, flags=re.I)
    what = re.sub(r'\b(?:comment[_-]?ids?|teams|todo)\s*[:=]\s*[\w:-]+', ' ', what, flags=re.I)
    what = re.sub(r'\b[0-9a-f]{7,64}\b', ' ', what, flags=re.I)
    what = re.sub(r'\d+', ' ', what)
    what = ' '.join(re.sub(r'[^\w]+', ' ', what).lower().split())
    return hashlib.sha256((row['kind'].strip().lower() + ':' + what).encode()).hexdigest()


def _time(value):
    if not isinstance(value, str):
        raise ValueError('lesson timestamp must be an ISO string')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed


def open_request(directory, path):
    """Use the configured decision surface; never write codes or answers ourselves."""
    subprocess.run([str(Path(__file__).with_name('decision-request')), 'spec', str(path)],
                   env={**os.environ, 'JIRA_WAKE_DIR': str(Path(directory).resolve())},
                   capture_output=True, text=True, check=True, timeout=120)


def _facts(rows, directory):
    """최근 실패 한 문장, 날짜들, 근거 경로(최대 3개)."""
    sentence = ' '.join(rows[-1]['what'].split())[:300]
    dates = ', '.join(sorted({_time(r['at']).astimezone(TZ).date().isoformat() for r in rows}))
    evidence = []
    for row in reversed(rows):
        paths = row.get('evidence', [])
        for path in ([paths] if isinstance(paths, str) else paths if isinstance(paths, list) else []):
            if isinstance(path, str) and path.strip() and path not in evidence:
                evidence.append(path)
    return sentence, dates, (evidence or [str(Path(directory) / 'lessons.jsonl')])[:3]


def _spec(rows, sig, directory, fixed=None):
    latest = rows[-1]
    sentence, dates, evidence = _facts(rows, directory)
    key = latest.get('key') or next(iter(re.findall(r'\b[A-Z][A-Z0-9]+-\d+\b', sentence)), 'FACTORY')
    if fixed is not None:
        # 고쳤다고 닫은 실패가 다시 났다: 3회를 기다리지 않고 바로 묻는다.
        how = f"커밋 {fixed['commit']}" if fixed.get('commit') else '사람이 항목을 닫음'
        return dict(_spec(rows, sig, directory), title='고쳤다던 공장 실패가 다시 났어요. 어떻게 할까요?',
                    summary=f"수정({how}, {fixed['closedAt'][:10]}) 뒤 재발해 수정이 듣지 않았습니다. {sentence}. "
                            f"날짜: {dates}. 근거: " + ', '.join(evidence),
                    recommendReason='수정으로 닫은 실패가 다시 나서 원인을 다시 고쳐야 합니다.')
    return {'kind': 'choice', 'key': key, 'humanReason': 'policy', 'lessonSignature': sig,
            'title': '반복되는 공장 실패를 어떻게 처리할까요?',
            'question': '이 실패를 지금 고칠까요, 받아들이거나 제외할까요?',
            'summary': f'{sentence}. 14일 안에 {len(rows)}회 반복. 날짜: {dates}. 근거: ' + ', '.join(evidence),
            'evidence': evidence, 'recommended': 'fix-now',
            'recommendReason': '같은 실패가 14일 안에 3회 이상 반복되어 원인 수정이 필요합니다.',
            'options': [
                {'id': 'fix-now', 'label': '지금 수정', 'detail': '원인을 고치는 작업을 우선 진행합니다.'},
                {'id': 'accept', 'label': '현재 동작 수용', 'detail': '이 제약을 수용하고 기록합니다.'},
                {'id': 'ignore', 'label': '이번에는 제외', 'detail': '이번 반복에 대한 조치를 하지 않습니다.'}]}


def append(directory, row, *, now=None, opener=None, todo=None):
    """Append once; at 3 occurrences in [now-14d, now], open one choice per signature.

    A `fix-now` answer becomes a tracked '[공장 수정]' to-do item (see _track). While it
    is open, a recurrence adds at most one line per day to it instead of a decision; once
    closed (by a human or `lesson-fixed`), the next recurrence opens a 'fix did not hold'
    decision without waiting for 3 occurrences or the cooldown.

    A separate lesson lock covers append and decision opening, including concurrent
    guard/run-lock writers. Existing codes.json is the decision authority: open
    codes deduplicate indefinitely; decided codes suppress reopening for 7 days.
    Expired unanswered requests may reopen. Opening errors propagate after the
    lesson is safely appended. Historical rows are read without rewriting them.
    """
    directory = Path(directory).resolve()
    now = datetime.now(TZ) if now is None else now
    if (not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
                                           for k in ('kind', 'what'))):
        raise ValueError('lesson needs nonempty kind and what')
    row = dict(row)
    validate_class(row)
    if 'key' in row and (not isinstance(row['key'], str) or not row['key'].strip()):
        raise ValueError('lesson key must be a nonempty string')
    row.setdefault('at', now.isoformat())
    _time(row['at'])
    sig = row['signature'] = signature(row)
    name = 'lesson-' + sig
    with locked(directory / 'lesson-decisions'):
        path = directory / 'lessons.jsonl'
        historical = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        recent = []
        for previous in [*historical, row]:
            if (not isinstance(previous, dict) or not all(isinstance(previous.get(k), str)
                                                        for k in ('kind', 'what', 'at'))):
                continue
            stamp = _time(previous['at'])
            if now - timedelta(days=14) <= stamp <= now and signature(previous) == sig:
                recent.append(previous)
        result = {'signature': sig, 'count': len(recent), 'decision': 'not-needed'}
        codes = read_object(directory / 'decision-pages/codes.json')
        # '[공장 수정]' 추적(할 일 CLI)의 실패는 이 lesson 기록과 호출자(실행 잠금 회수·가드)를 막지 않는다.
        # 기록하고 다음 lesson·수신기 회차가 다시 시도한다(2026-11-10: 할 일 CLI 실패가 run_lock.busy의 clear()를 막았다).
        try:
            todo = _todo(directory) if todo is None else todo
            fixes = _track(directory, codes, todo)
            fix = fixes.get(sig)
            if fix and fix['state'] == 'open' and fix.get('todoId') and todo is not None:
                rows = todo(['list', '--json']) or []
                if not any(r.get('id') == fix['todoId'] and r.get('status') != 'done' for r in rows):
                    fix.update(state='closed', closedAt=now.isoformat())  # 사람이 할 일 항목을 닫았다.
                    atomic_json(directory / FIXES, fixes)
            today = now.astimezone(TZ).date().isoformat()
            if fix and fix['state'] == 'open' and fix.get('notedDay') != today:
                if fix.get('todoId') and todo is not None:
                    after = [r for r in [*historical, row] if _same(r, sig) and _time(r['at']) > _time(fix['decidedAt'])] or [row]
                    latest = max(_time(r['at']) for r in after).astimezone(TZ).date().isoformat()
                    todo(['note', fix['todoId'], f"fix-now 뒤 재발 {len(after)}회, 최근 {latest}"])
                fix['notedDay'] = today
                atomic_json(directory / FIXES, fixes)
        except TRACK_ERRORS as error:
            result['trackingError'] = _tracking_failed(directory, 'lessons.append', error)
            fixes = _fixes_or_empty(directory)
        else:
            _tracking_ok(directory, 'lessons.append')
        fix = fixes.get(sig)
        if fix and fix['state'] == 'open':
            return dict(result, decision='fix-tracked')
        if fix and _time(row['at']) >= _time(fix['closedAt']) and not any(
                c.get('name') == name and _time(c['createdAt']) > _time(fix['closedAt']) for c in codes.values()):
            spec_path = directory / 'lesson-decisions' / (name + '.json')
            atomic_json(spec_path, _spec(recent, sig, directory, fixed=fix))
            (opener or open_request)(directory, spec_path)
            return dict(result, decision='opened-fix-failed')
        if len(recent) < 3:
            return result
        # 열린 요청이 최근 답한 요청보다 앞선다. codes 순서와 무관하게 전부 본다.
        matching = [c for c in codes.values() if c.get('name') == name]
        if any(c.get('state') == 'open' for c in matching):
            return dict(result, decision='already-open')
        if any(c.get('state') == 'decided' and now - _time(c['terminalAt']) < timedelta(days=7)
               for c in matching):
            return dict(result, decision='answer-cooldown')
        spec_path = directory / 'lesson-decisions' / (name + '.json')
        atomic_json(spec_path, _spec(recent, sig, directory))
        (opener or open_request)(directory, spec_path)
        return dict(result, decision='opened')


FIXES = 'lesson-decisions/fixes.json'
FIX_TITLE = '[공장 수정] '
# 추적 실패 기록: 호출 위치 → {at, error}. 그 위치가 다음에 성공하면 지운다. 아침 점검이 보인다.
TRACKING_ERROR = 'lesson-decisions/tracking-error.json'
TRACK_ERRORS = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)
# 할 일 CLI 한 번의 한도. 가드·실행 잠금 회수 안에서 불리므로 decision_inbox 기본(60초)보다 짧다.
TODO_TIMEOUT = 20


def _tracking_failed(directory, where, error):
    message = f'{type(error).__name__}: {error}'[:300]
    print(f'lessons: fix-now tracking failed ({where}): {message}', file=sys.stderr)
    with locked(Path(directory) / 'lesson-decisions/tracking-errors'):
        errors = _object_or_empty(Path(directory) / TRACKING_ERROR)
        errors[where] = {'at': datetime.now(TZ).isoformat(), 'error': message}
        atomic_json(Path(directory) / TRACKING_ERROR, errors)
    return message


def _tracking_ok(directory, where):
    path = Path(directory) / TRACKING_ERROR
    if not path.exists():
        return
    with locked(Path(directory) / 'lesson-decisions/tracking-errors'):
        errors = _object_or_empty(path)
        if errors.pop(where, None) is not None:
            atomic_json(path, errors) if errors else path.unlink()


def _object_or_empty(path):
    """실패 기록 중 깨진 파일이 또 예외를 내지 않게 한다. 깨진 내용은 새 기록으로 덮는다."""
    try:
        return read_object(path)
    except ValueError:
        return {}


def _fixes_or_empty(directory):
    try:
        return read_object(Path(directory) / FIXES)
    except ValueError as error:
        _tracking_failed(directory, 'fixes.json', error)
        return {}


def _same(row, sig):
    return (isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ('kind', 'what', 'at'))
            and signature(row) == sig)


def _todo(directory):
    """공장 경보·결정과 같은 할 일 연동. 결정 화면이 html이면 할 일 항목을 만들지 않는다(None)."""
    import decision_inbox
    if decision_inbox.surface(directory) == 'html':
        return None
    return lambda args: decision_inbox.todo_cli(args, timeout=TODO_TIMEOUT)


def _chosen(directory, codes):
    """결정된 lesson 결정의 선택지: 서명 → (코드, 결정 시각, 선택지 id). 서명마다 가장 최근 결정만 본다.

    선택지는 codes.json에 없고 decisions.jsonl 감사 줄에 있다. 할 일 답은 codes의 messageId,
    judge는 'judge:<코드>'로 잇는다."""
    path = Path(directory) / 'decisions.jsonl'
    audits = {}
    for text in (path.read_text(encoding='utf-8').splitlines() if path.exists() else []):
        try:  # decision_page.has_message와 같이 깨진 감사 줄은 건너뛴다.
            line = json.loads(text)
        except ValueError:
            continue
        if isinstance(line, dict) and line.get('messageId'):
            audits[line['messageId']] = line.get('option')
    latest = {}
    for code, entry in codes.items():
        name = entry.get('name') if isinstance(entry, dict) else None
        if not (isinstance(name, str) and name.startswith('lesson-') and entry.get('state') == 'decided'):
            continue
        option = audits.get(entry.get('messageId') or f'judge:{code}')
        sig = name.removeprefix('lesson-')
        if sig not in latest or _time(entry['terminalAt']) > _time(latest[sig][1]):
            latest[sig] = (code, entry['terminalAt'], option)
    return latest


def _untracked(directory, codes, fixes):
    """아직 할 일이 없는 fix-now 결정: [(서명, 코드, 결정 시각)]."""
    found = []
    for sig, (code, at, option) in _chosen(directory, codes).items():
        fix = fixes.get(sig)
        if option == 'fix-now' and not (fix and (fix['code'] == code or fix['state'] == 'open'
                                                 or _time(at) <= _time(fix['closedAt']))):
            found.append((sig, code, at))
    return found


def _track(directory, codes, todo):
    """`fix-now`로 정한 서명마다 '[공장 수정]' 할 일 항목을 한 번 만든다. 호출자가 lesson 잠금을 잡는다."""
    directory = Path(directory)
    fixes = read_object(directory / FIXES)
    for sig, code, at in _untracked(directory, codes, fixes):
        path = directory / 'lessons.jsonl'
        rows = [r for r in (json.loads(t) for t in path.read_text(encoding='utf-8').splitlines() if t.strip())
                if _same(r, sig)] if path.exists() else []
        if not rows:
            raise ValueError(f'lesson decision {code} has no lessons for signature {sig}')
        sentence, dates, evidence = _facts(rows, directory)
        item = todo(['add', FIX_TITLE + sentence[:150], '--by', 'factory']) if todo is not None else None
        # 항목을 만들자마자 기록한다: 뒤 단계가 실패해 다시 시도해도 같은 항목을 두 번 만들지 않는다.
        fixes[sig] = {'code': code, 'decidedAt': at, 'todoId': item, 'state': 'open', 'what': sentence,
                      'count': len(rows)}
        atomic_json(directory / FIXES, fixes)
        if todo is not None:
            todo(['wait', item, f'공장 수정 필요 — 결정 {code} fix-now({at[:10]}). 고친 커밋을 '
                                f'`state_update.py lesson-fixed {sig[:12]} --commit <sha>`로 알리거나 직접 닫아 주세요'])
            todo(['note', item, f'서명 {sig} · {len(rows)}회 · 날짜: {dates} · 근거: ' + ', '.join(evidence)])
    return fixes


def track_fixes(directory, *, todo=None):
    """결정 수신기·judge가 결정 뒤 부른다. 열린 '[공장 수정]' 항목: 서명 → 항목 id."""
    directory = Path(directory).resolve()
    try:
        fixes = read_object(directory / FIXES)
        # 수신기는 60초마다 부른다. 할 일이 없으면 lesson 잠금(결정 열기 중 최대 120초)을 기다리지 않는다.
        if _untracked(directory, read_object(directory / 'decision-pages/codes.json'), fixes):
            todo = _todo(directory) if todo is None else todo
            with locked(directory / 'lesson-decisions'):
                fixes = _track(directory, read_object(directory / 'decision-pages/codes.json'), todo)
    except TRACK_ERRORS as error:  # 수신기·judge 회차를 끝내지 않는다. 기록하고 다음 회차가 다시 시도한다.
        _tracking_failed(directory, 'track_fixes', error)
        fixes = _fixes_or_empty(directory)
    else:
        _tracking_ok(directory, 'track_fixes')
    return {sig: fix.get('todoId') for sig, fix in fixes.items() if fix['state'] == 'open'}


def mark_fixed(directory, prefix, commit, *, now=None, todo=None):
    """공장 커밋이 수정을 알린다: 기록하고 '[공장 수정]' 항목을 닫는다. 이후 재발은 '수정이 듣지 않음' 결정을 연다."""
    directory = Path(directory).resolve()
    if not re.fullmatch(r'[0-9a-f]{7,40}', commit or ''):
        raise ValueError('--commit must be a 7-40 character hex commit sha')
    if not re.fullmatch(r'[0-9a-f]{8,64}', prefix or ''):
        raise ValueError('signature must be at least 8 hex characters')
    now = datetime.now(TZ) if now is None else now
    todo = _todo(directory) if todo is None else todo
    with locked(directory / 'lesson-decisions'):
        fixes = read_object(directory / FIXES)
        found = [sig for sig, fix in fixes.items() if sig.startswith(prefix) and fix['state'] == 'open']
        if len(found) != 1:
            raise ValueError(f'expected one open fix-now lesson for {prefix}, found {len(found)}')
        fix = fixes[found[0]]
        if fix.get('todoId') and todo is not None:
            todo(['done', fix['todoId'], f'수정 커밋 {commit}로 닫음'])
        fix.update(state='fixed', commit=commit, closedAt=now.isoformat())
        atomic_json(directory / FIXES, fixes)
        return {'signature': found[0], 'state': 'fixed', 'commit': commit}


def _index_fix(rows, fixes):
    """한 분류의 여러 kind/서명 중 미완료 수정이 우선이다."""
    found = [fixes.get(sig) for sig in {signature(row) for row in rows}]
    found = [fix for fix in found if isinstance(fix, dict)]
    if any(fix.get('state') == 'open' for fix in found):
        return 'open'
    fixed = [fix for fix in found if fix.get('state') == 'fixed' and fix.get('commit')]
    if fixed:
        latest = max(fixed, key=lambda fix: (_time(fix['closedAt']) if fix.get('closedAt') else
                                            datetime.min.replace(tzinfo=TZ), str(fix['commit'])))
        return 'fixed ' + str(latest['commit'])
    return 'none'


def _compact(value, limit):
    value = ' '.join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + '…'


def index(directory, *, kind=None, stage=None, max=20, now=None):
    """최근 14일의 분류별 교훈. 읽기만 하며 잠금·수정 추적·결정을 만들지 않는다.

    명시한 class는 한 줄로 묶고, class 없는 과거 행은 기존 서명으로 2회 이상만
    보인다. kind와 stage는 저장된 행의 값을 각각 정확히 비교한다(함께 주면 AND).
    """
    if not isinstance(max, int) or isinstance(max, bool) or max < 0:
        raise ValueError('--max must be a nonnegative integer')
    limit = min(max, 20)
    if not limit:
        return ''
    directory = Path(directory)
    path = directory / 'lessons.jsonl'
    if not path.exists():
        return ''
    now = datetime.now(TZ) if now is None else _time(now.isoformat())
    allowed = classes()
    groups = {}
    for number, text in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except ValueError as error:
            raise ValueError(f'{path}:{number}: invalid lesson JSON') from error
        if not (isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k].strip()
                                             for k in ('kind', 'what', 'at'))):
            continue  # append의 과거 행 집계와 같게 불완전한 예전 기록은 집계하지 않는다.
        if (kind is not None and row.get('kind') != kind) or (stage is not None and row.get('stage') != stage):
            continue
        stamp = _time(row['at'])
        if not now - timedelta(days=14) <= stamp <= now:
            continue
        # 회고가 분류를 합치거나 지운 뒤에도 예전 행 하나 때문에 색인 전체가 빠지지 않게,
        # 목록에 없는 분류는 검증 오류 대신 서명 묶음으로 보인다.
        label = row.get('class')
        key = ('class', label) if label in allowed else ('signature', signature(row))
        groups.setdefault(key, []).append(row)
    fixes = read_object(directory / FIXES)
    lines = []
    for key, rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        if key[0] == 'signature' and len(rows) < 2:
            continue
        rows.sort(key=lambda row: (_time(row['at']), json.dumps(row, sort_keys=True, ensure_ascii=False)))
        latest = rows[-1]
        label = key[1] if key[0] == 'class' else _compact(latest['what'], 36)
        suggestions = [row['suggestion'] for row in rows if isinstance(row.get('suggestion'), str)
                       and row['suggestion'].strip()]
        suggestion = suggestions[-1] if suggestions else '제안 없음'
        prefix = f"- {label} ×{len(rows)} (last {_time(latest['at']).astimezone(TZ):%m-%d}) — "
        suffix = f' [fix: {_index_fix(rows, fixes)}]'
        # 20줄과 줄바꿈까지 2,500자 안에 들어가도록 각 줄을 124자로 제한한다.
        suggestion = _compact(suggestion, 124 - len(prefix) - len(suffix))
        lines.append(prefix + suggestion + suffix)
        if len(lines) >= limit:
            break
    return '\n'.join(lines)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    summary = commands.add_parser('index', help='최근 14일 lesson 분류별 읽기 전용 요약')
    summary.add_argument('--dir', type=Path, required=True)
    summary.add_argument('--kind')
    summary.add_argument('--stage')
    summary.add_argument('--max', type=int, default=20)
    args = parser.parse_args(argv)
    try:
        result = index(args.dir, kind=args.kind, stage=args.stage, max=args.max)
    except (OSError, ValueError, TypeError) as error:
        print('lessons: ' + str(error), file=sys.stderr)
        return 1
    if result:
        print(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
