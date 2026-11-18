"""아침 점검의 의존성 상태와 조치 이월(carry-over). 상태 폴더에 기록된 파일만 읽고 외부 호출은 하지 않는다.

의존성 상태(2026-11-10): 11-06·11-09·11-10 보고서가 'BLOCKED·오류 0건'이었지만 Teams 감시는 11-02부터 52회 연속
실패, 스테이징 kube 인증은 11-06부터 실패(11-06 17:28 스테이징 승인 미사용)였다. 실패한 의존성은 오류로 세고, 기록이 없으면
정상이 아니라 '미확인'으로 쓴다.
조치 이월: 같은 조치(티켓 키 + 조치 종류)가 이어진 보고서 3회에 나오면 사람 결정 하나로 올린다(12 → 13 → 19건).
"""
from datetime import date as Date, datetime, timedelta
import hashlib
import json
from pathlib import Path

import followup_config as cfg
import kube_pods
import preflight_env
from state_update import read_object

PASS, WARN, FAIL, UNKNOWN = 'PASS', 'WARN', 'FAIL', 'UNKNOWN'
NAMES = {'teams': 'Teams 방 감시', 'jira': 'Jira 인증', 'git': '서비스 저장소 git fetch', 'guard': '공장 가드 실행',
         'vpn': 'dev·스테이징 사설망(VPN)', 'stg-kube': '스테이징 kube 인증', 'dev-kube': '개발 kube 인증',
         'stg-db': '스테이징 DB', 'dev-db': '개발 DB', 'stg-web': '스테이징 웹', 'dev-web': '개발 웹',
         'stg-ai': '스테이징 AI 엔진', 'dev-ai': '개발 AI 엔진', 'jenkins-dev': '개발 Jenkins'}
ALWAYS = ('teams', 'jira', 'git', 'guard', 'vpn', 'stg-kube')
ENV_WAITING = ('env-human', 'env-down')
CARRY_FILE = 'morning-carry.json'
CARRY_DECISION = 'morning-carry-'
ESCALATE_AT = 3
DEFER_BUSINESS_DAYS = 5
# 자기 결정이 따로 있는 조치는 이월 결정에 넣지 않는다(회고 제안 = retro_decision).
NOT_ESCALATED = {'retro'}
KINDS = {'lookup-failed': '조회 실패', 'comment': '댓글 판정·답', 'idle': '업무일 미갱신', 'assigned': '신규 배정 확인',
         'status-by-other': '타인 상태 변경 확인', 'transition': '상태 이동 멈춤', 'model-action': '우선 조치',
         'lesson-tracking': '공장 수정 추적 실패', 'retro': '회고 제안 미병합', 'pending-focus': 'pending focus 차단',
         'teams-watch': 'Teams 감시 복구'}


def always():
    """늘 보이는 점검. VPN_STATUS_CMD=none이면 VPN, KUBE_CONNECT_CMD가 비면 kube 행을 뺀다(미설정을 '미확인'으로 쓰지 않는다)."""
    return tuple(name for name in ALWAYS
                 if not (name == 'vpn' and preflight_env.VPN_STATUS_CMD == 'none')
                 and not (name == 'stg-kube' and not kube_pods.configured()))


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else None
    except ValueError:
        return 'invalid'  # 깨진 기록: 호출자가 dict가 아니면 미확인으로 본다


def _time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _short(value):
    moment = _time(value)
    return moment.strftime('%Y-%m-%d %H:%M') if moment else str(value)


def previous_business_date(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def add_business_days(day, count):
    while count:
        day += timedelta(days=1)
        count -= day.weekday() < 5
    return day


def row(name, status, detail, source, *, since=None, failures=None, blocked=()):
    label = NAMES.get(name) or (f'환경 대기({name[4:]})' if name.startswith('env:') else name)
    return {'id': name, 'name': label, 'status': status, 'detail': detail, 'source': source,
            'since': since, 'failures': failures, 'blocked': list(blocked)}


def teams(base, now):
    """teams-scan.json이 정본(teams_action과 같다), 없으면 가드 기록. 마지막 성공 후 24시간 넘게 실패면 FAIL."""
    scan, guard = _json(base / 'teams-scan.json'), _json(base / 'guard-last.json')
    guard = guard if isinstance(guard, dict) else {}
    source = 'teams-scan.json'
    if scan is None:
        source = 'guard-last.json'
        teams_state = guard.get('teams') if isinstance(guard.get('teams'), dict) else {}
        code = next((r.split(':', 1)[1] for r in guard.get('reasons', []) if isinstance(r, str)
                     and r.startswith('teams-unavailable:')), None)
        if teams_state.get('status') not in ('ok', 'unavailable') and code is None:
            return row('teams', UNKNOWN, '감시 기록 없음', source)
        scan = {'status': 'unavailable' if code else teams_state['status'], 'error': teams_state.get('error') or code}
    if not isinstance(scan, dict) or scan.get('status') not in ('ok', 'unavailable'):
        return row('teams', UNKNOWN, '감시 기록 형식 오류', source)
    monitoring = scan.get('monitoring')
    if not isinstance(monitoring, dict):
        monitoring = (guard.get('monitoring') or {}).get('teams') if isinstance(guard.get('monitoring'), dict) else None
    monitoring = monitoring if isinstance(monitoring, dict) else {}
    success = _time(monitoring.get('lastSuccessAt')) if monitoring.get('lastSuccessAt') else None
    failures = monitoring.get('failures') if type(monitoring.get('failures')) is int else None
    since = '마지막 성공 ' + (_short(monitoring['lastSuccessAt']) if success else '없음')
    if scan['status'] == 'ok':
        return row('teams', PASS, '읽기 성공', source, since=since)
    status = WARN if success and now - success <= timedelta(hours=24) else FAIL
    code = scan.get('error') or monitoring.get('error') or 'TEAMS_READ_FAILED'
    detail = code + (f'; 복구: `{cfg.CHAT_RECONNECT_HINT}`' if status == FAIL else '; 24시간 안 실패(경고)')
    return row('teams', status, detail, source, since=since, failures=failures)


def jira(base, reauthed):
    """이번 점검의 acli 검색이 성공했으므로 PASS다(실패하면 보고서 전체가 BLOCKED). 가드 기록은 참고로 붙인다."""
    guard = _json(base / 'guard-last.json')
    record = ((guard or {}).get('monitoring') or {}).get('jira') if isinstance(guard, dict) else None
    detail = '이번 점검 검색 성공' + (' (재인증 뒤)' if reauthed else '')
    failures = None
    if isinstance(record, dict) and type(record.get('failureCount')) is int:
        failures = record['failureCount']
        if failures:
            detail += f"; 가드 연속 실패 {failures}회(마지막 오류 {_short(record.get('lastErrorAt'))})"
    return row('jira', PASS, detail, 'acli 검색 · guard-last.json', failures=failures)


def git(fetches):
    if not fetches:
        return row('git', UNKNOWN, '점검한 저장소 없음', 'fetch.json')
    failed = [f['repo'] for f in fetches if f.get('status') == 'FAIL']
    warned = [f['repo'] for f in fetches if f.get('status') == 'WARN']
    if failed:
        return row('git', FAIL, f'{len(failed)}/{len(fetches)}개 실패: ' + ', '.join(failed), 'fetch.json',
                   failures=len(failed))
    if warned:
        return row('git', WARN, f'{len(warned)}개 원격 접속 불가(이번 티켓과 무관): ' + ', '.join(warned), 'fetch.json')
    return row('git', PASS, f'{len(fetches)}개 저장소 fetch 성공', 'fetch.json')


def guard_fresh(guard, now):
    """가드는 평일 08:00~18:30에 돈다. 지난 업무일보다 오래된 기록으로는 지금 상태를 말하지 않는다."""
    at = _time(guard.get('at')) if isinstance(guard, dict) else None
    return at is not None and at.date() >= previous_business_date(now.date())


def guard_row(base, now):
    guard = _json(base / 'guard-last.json')
    if not isinstance(guard, dict) or not guard.get('at'):
        return row('guard', UNKNOWN, '가드 기록 없음', 'guard-last.json')
    errors = guard.get('internalErrors') or []
    if errors or guard.get('monitoringStatus') == 'degraded':
        return row('guard', FAIL, '내부 오류: ' + str(errors[0] if errors else 'degraded')[:120],
                   'guard-last.json', since='관찰 ' + _short(guard['at']))
    if not guard_fresh(guard, now):
        return row('guard', UNKNOWN, f"마지막 실행 {_short(guard['at'])} (지난 업무일보다 오래됨)", 'guard-last.json')
    return row('guard', PASS, f"마지막 실행 {_short(guard['at'])}", 'guard-last.json')


def classify(action, needs):
    """가드 waitingHuman 문구(preflight의 첫 실패 human_action)를 실패한 점검으로 되돌린다.
    문구는 같은 코드(kube_pods·preflight_env)가 만든 값이라 문자열로 대조한다. 모르는 문구는 needs 묶음으로 남긴다."""
    names = [n for n in needs.split(',') if n]
    for name in names:
        env = name.split('-')[0]
        if name.endswith('-kube') and env in ('dev', 'stg'):
            for reason in ('vpn', 'host-key', 'kube-auth', 'ssh-auth', 'other'):
                if action == kube_pods.failure(reason, env)['hint']:
                    return ('vpn', f'{env} VPN 연결 필요') if reason == 'vpn' else (name, f'{reason}: {action[:100]}')
            if action.startswith('SSH/VPN 응답이'):
                return name, 'timeout: ' + action[:100]
        if name.endswith(('-db', '-ai')) and env in ('dev', 'stg') and action == preflight_env.vpn_action(env):
            return 'vpn', f'{env} VPN 연결 필요'
    if action.startswith('Check vpn --status'):
        return 'vpn', 'VPN 상태 이상(동시 연결 등)'
    if 'acli-reauth' in action:
        return 'jira', 'acli 재인증 필요'
    if 'jk ' in action:
        return 'jenkins-dev', action[:80]
    return 'env:' + needs, action[:120]


def environment(base, now):
    """환경 대기 큐 항목(env-human·env-down)과 가드의 preflight 결과(waitingHuman·environment-ready)로 점검별 상태를 낸다.
    반환: {점검 id: row}. 막힌 큐 항목과 쓰지 못한 스테이징 승인은 그 점검 아래 둔다."""
    queue, guard, approvals = (_json(base / name) for name in ('followup-queue.json', 'guard-last.json', 'approvals.json'))
    queue = queue if isinstance(queue, dict) else {}
    approvals = approvals if isinstance(approvals, dict) else {}
    guard = guard if isinstance(guard, dict) else {}
    waiting = {key: item for key, item in queue.items() if isinstance(item, dict)
               and item.get('waiting') in ENV_WAITING and isinstance(item.get('needs'), str) and item['needs']}
    rows, fresh = {}, guard_fresh(guard, now)
    observed = '가드 관찰 ' + _short(guard['at']) if guard.get('at') else None
    stale = '가드 점검 기록 없음' if fresh else ('가드 기록이 낡음' if observed else '가드 기록 없음')
    source = 'guard-last.json · followup-queue.json'

    def blocked(key):
        item, approval = waiting[key], approvals.get(key)
        text = f"{key}({item['waiting']})"
        if isinstance(approval, dict) and approval.get('stg') and not approval.get('usedAt'):
            text += f" 스테이징 승인 {_short(approval.get('at'))} 미사용"
        return text

    placed = set()
    if fresh:
        actions = guard.get('waitingHuman') if isinstance(guard.get('waitingHuman'), dict) else {}
        for action, keys in actions.items():
            for key in keys if isinstance(keys, list) else []:
                if key not in waiting or key in placed:
                    continue
                check, why = classify(action, waiting[key]['needs'])
                current = rows.setdefault(check, row(check, FAIL, why, source, since=observed))
                current['blocked'].append(blocked(key))
                placed.add(key)
        for reason in guard.get('reasons', []):
            if isinstance(reason, str) and reason.startswith('environment-ready:'):
                key = reason.split(':', 1)[1]
                placed.add(key)  # 환경이 준비됐다: 막힌 일이 아니다(가드가 깨운다).
                needs = (queue.get(key) or {}).get('needs') or ''
                checks = [n for n in needs.split(',') if n]
                if any(n.endswith(('-db', '-kube', '-ai')) for n in checks):
                    checks.append('vpn')
                for check in checks:
                    rows.setdefault(check, row(check, PASS, '가드 점검 통과', source, since=observed))
    for key in sorted(set(waiting) - placed):
        # 가드 기록이 낡았거나(env-down 포함) 어느 점검이 막았는지 기록이 없다.
        check = 'env:' + waiting[key]['needs']
        rows.setdefault(check, row(check, UNKNOWN, '어느 점검이 막는지 기록 없음' if fresh else stale,
                                   source, since=observed))['blocked'].append(blocked(key))
    for check in (name for name in always() if name in ('vpn', 'stg-kube')):
        rows.setdefault(check, row(check, UNKNOWN, stale, source, since=observed))
    return rows


def health(base, now, *, fetches, reauthed=False):
    base = Path(base)
    env = environment(base, now)
    found = {'teams': teams(base, now), 'jira': jira(base, reauthed), 'git': git(fetches),
             'guard': guard_row(base, now)}
    if 'jira' in env:  # 가드의 jira preflight 실패는 이번 점검 결과(PASS)보다 오래된 기록이다. 막힌 일만 옮긴다.
        found['jira']['blocked'] += env.pop('jira')['blocked']
    found.update(env)
    ordered = [found.pop(name) for name in always()]
    return ordered + [found[name] for name in sorted(found)]


# ---- 조치 이월 ----

def item_id(key, kind):
    return f'{key}|{kind}'


def load_carry(base):
    state = read_object(Path(base) / CARRY_FILE)
    for field in ('current', 'previous', 'decisions'):
        if not isinstance(state.get(field, {}), dict):
            raise ValueError(f'잘못된 {CARRY_FILE} {field}')
    return state


def prior_snapshot(state, today):
    """같은 날 다시 돌면 그날 앞 보고서 기준으로 다시 센다(재실행이 횟수를 올리지 않는다)."""
    return state.get('previous', {}) if state.get('date') == today else state.get('current', {})


def streaks(prior, ids, today):
    """이어진 보고서 횟수. 직전 보고서에 없던 조치는 1부터 다시 센다."""
    result = {}
    for ident in ids:
        old = prior.get(ident)
        if isinstance(old, dict) and type(old.get('count')) is int and old.get('since'):
            result[ident] = {'count': old['count'] + 1, 'since': old['since']}
        else:
            result[ident] = {'count': 1, 'since': today}
    return result


def answers(base):
    """이월 결정의 선택: 결정 이름 → (코드, 선택지 id). lessons._chosen과 같은 길(할 일 답 messageId, judge:<코드>)."""
    base = Path(base)
    codes = read_object(base / 'decision-pages/codes.json')
    audits = {}
    path = base / 'decisions.jsonl'
    for text in path.read_text(encoding='utf-8').splitlines() if path.exists() else []:
        try:
            line = json.loads(text)
        except ValueError:
            continue
        if isinstance(line, dict) and line.get('messageId'):
            audits[line['messageId']] = line.get('option')
    found = {}
    for code, entry in codes.items():
        name = entry.get('name') if isinstance(entry, dict) else None
        if isinstance(name, str) and name.startswith(CARRY_DECISION) and entry.get('state') == 'decided':
            found[name] = (code, audits.get(entry.get('messageId') or f'judge:{code}'))
    return found


def suppression(decisions, chosen, counted, today):
    """defer는 정한 날까지, drop은 같은 연속(시작일)이 이어지는 동안 그 조치를 조치 필요 수에서 뺀다."""
    hidden = {}
    for name, meta in decisions.items():
        code, option = chosen.get(name, (None, None))
        if option not in ('defer', 'drop') or not isinstance(meta, dict):
            continue
        for ident, since in (meta.get('items') or {}).items():
            if ident not in counted:
                continue
            if option == 'defer' and today <= str(meta.get('deferUntil')):
                hidden[ident] = f"{meta['deferUntil']}까지 미룸({code})"
            elif option == 'drop' and counted[ident]['since'] == since:
                hidden[ident] = f'보고서에서 뺌({code})'
    return hidden


def decision_name(candidates):
    return CARRY_DECISION + hashlib.sha256(
        '\n'.join(sorted(f"{ident}@{row['since']}" for ident, row in candidates.items())).encode()).hexdigest()[:16]


def decision_spec(candidates, labels, today, evidence):
    defer = add_business_days(Date.fromisoformat(today), DEFER_BUSINESS_DAYS).isoformat()
    lines = [f"{labels[ident]} — {row['count']}회째, {row['since']}부터" for ident, row in sorted(candidates.items())]
    spec = {'kind': 'choice', 'key': 'FACTORY', 'humanReason': 'policy',
            'title': f'[아침 점검] 3회 넘긴 조치 {len(candidates)}건',
            'question': '아침 점검에 세 번 넘게 이어서 나온 조치를 어떻게 할까요?',
            'summary': '; '.join(lines) + '. 공장은 이 조치를 스스로 처리하지 않습니다.',
            'background': '같은 조치가 매일 보고서에 다시 나와 조치 필요 수가 12 → 13 → 19건으로 늘었습니다.',
            'intent': '고른 대로 보고서의 조치 필요 수에서 빼거나 그대로 둡니다. 같은 묶음은 다시 묻지 않습니다.',
            'evidence': [str(evidence)], 'recommended': 'now',
            'recommendReason': '세 번 넘게 남은 조치는 미룰수록 보고서가 진짜 새 일을 가립니다. '
                               '오늘 처리하고, 정말 나중 일이면 날짜를 정해 미루세요.',
            'options': [
                {'id': 'now', 'label': '지금 처리', 'detail': '사람이 오늘 처리합니다. 보고서에는 계속 셉니다.'},
                {'id': 'defer', 'label': f'{defer}까지 미룸', 'detail': f'{defer}까지 조치 필요 수에서 뺍니다.'},
                {'id': 'drop', 'label': '보고서에서 뺌', 'detail': '이어서 나오는 동안 조치 필요 수에서 뺍니다. '
                                                          '한 번 사라졌다 다시 나오면 다시 셉니다.'}]}
    return spec, defer
