#!/usr/bin/env python3
"""Follow-up 스캔: SINCE 이후 새 댓글 중 '나를 멘션' 또는 '내 담당 티켓'인 것을 뽑는다 (읽기 전용).

Usage: followup_scan.py [--since ISO8601] [--out PATH]
- SINCE 기본값: 72시간 전 (고정 창).
- 이미 처리한 댓글(state.json followup.handled 의 comment id)은 제외한다.
- JQL 은 ADF 멘션을 검색하지 못하므로(실측: `comment ~`·`watcher` 로는 멘션 티켓이 안 잡힌다)
  'updated >= SINCE' 로 넓게 받은 뒤 댓글 ADF 의 mention attrs.id 로 거른다.
- 백스톱: 진행 중인 내 티켓(INFLIGHT_JQL)은 창보다 오래된 댓글도 '나를 멘션 + 이후 내 답 없음'이면 뽑는다
  (72시간 창과 아침 점검 JQL 밖에 남은 오래된 배포 요청 멘션이 끝내 안 잡히는 일을 막는다).
- 백로그 접수: 내 담당 프로젝트(JIRA_PROJECT) 미해결 티켓 중 시작 전 범주(백로그·해야 할 일, statusCategory new)는
  댓글이 없어도 티켓당 한 번 `intake` 이벤트로 뽑는다(commentId `intake:<KEY>`).
  '[보류]' 제목, 이미 큐에 있는 티켓, handled 에 기록된 것은 제외한다(백로그 상태의 내 업무도 라인이 진행한다).
- to-do 답: sdlc-kit to-do(`~/.sdlc/todo.md`)에서 프로젝트 티켓에 연결된 항목에 내가 단 답 중 에이전트가 아직 읽지 않은 것을
  `source=todo` 이벤트로 뽑는다(commentId `todo:<항목 id>:<해시>`). 답이 모두 처리된 항목은 큐의 waiting 을 to-do 에 옮긴다:
  waiting 이 있으면 `todo wait`(문구 `공장 대기 — <waiting>: <note>`), 풀렸으면 `todo resume`, 남은 안 읽은 답은 `todo take`
  (읽히지 않은 "continue" 답이나 큐는 사람 대기인데 sdlc 는 READY 인 항목이 화면에 몇 시간씩 '에이전트 진행 중'으로 남지 않게 한다).
출력: JSON {since, me, events:[...], errors:[...]}. 마지막 줄 stdout: `scan events=<n> errors=<n> out=<path>`.
설정: followup_config.py(JIRA_ACCOUNT_ID·JIRA_PROJECT·STATUS_*·JIRA_FOLLOWUP_WORKSPACE).
"""
import argparse, hashlib, json, os, re, subprocess, time, unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import followup_config as cfg

BASE = cfg.HOME_DIR
TZ = cfg.TZ
ME = cfg.ME  # 운영자 본인의 Jira accountId (config JIRA_ACCOUNT_ID)
PROJECT = cfg.PROJECT
# 검수 완료 단계는 resolution 이 채워지지만 운영 반영 전이다(실측). 오래된 미정리 티켓이 섞이지 않게
# 그 단계는 최근 30일 갱신분만 본다.
INFLIGHT_JQL = ('assignee = currentUser() AND (resolution = Unresolved OR '
                '(status in (%s) AND updated >= -30d))' % ','.join('"%s"' % s for s in cfg.DONE_NOT_RELEASED))
# '해야 할 일'은 표시 이름이라 JQL status 값으로 받지 않는다(실측). 범주(new)로 고른다.
# 라인이 개발할 저장소가 없는 다른 프로젝트는 받지 않는다.
INTAKE_JQL = f'project = {PROJECT} AND assignee = currentUser() AND resolution = Unresolved AND statusCategory = "To Do"'
HOLD_MARK = '[보류]'
# 운영 완료 단계(Done 범주). 이 상태 티켓 댓글의 반영 이슈는 원 티켓이 아니라 새 운영 후속 티켓으로 옮긴다
# (RUNBOOK §7-2, spinoff_ticket.py).
POST_RELEASE_STATUSES = cfg.POST_RELEASE_STATUSES
WORKSPACE = cfg.WORKSPACE
SDLC_KIT = cfg.SDLC_KIT
TICKET = cfg.TICKET_RE
RELATED_JQL = f'project = {PROJECT} AND (created >= -7d OR updated >= -7d) ORDER BY updated DESC'
# 서비스명·환경명·일반 요청어만 같다는 이유로 모든 티켓을 중복 후보로 만들지 않는다.
TOKEN_STOP = {PROJECT.casefold(), 'ai', 'web', 'api', 'dev', 'stg', 'prd',
              '개발', '스테이징', '운영', '오류', '수정', '요청', '확인', '화면'}
TOKEN_STOP |= {t.strip().casefold() for t in cfg.get('RELATED_STOP_WORDS', '').split(',') if t.strip()}


def significant_tokens(text):
    text = re.sub(re.escape(PROJECT) + r'-\d+', ' ', unicodedata.normalize('NFKC', text), flags=re.I).casefold()
    return {token for token in re.findall(r'[a-z][a-z0-9]*|[가-힣]{2,}', text)
            if len(token) >= 2 and token not in TOKEN_STOP}


def find_related(key, summary, *, search=None, workspace=None, now=None):
    """후보만 찾는다. 담당자·중복 여부·작업 권한은 판정하지 않는다."""
    rows = (search or acli)(['search', '--jql', RELATED_JQL, '--fields',
                            'key,summary,created,updated,assignee', '--json', '--paginate'])
    if not isinstance(rows, list):
        raise ValueError('invalid related search')
    tokens = significant_tokens(summary)
    cutoff = (now or datetime.now(TZ)) - timedelta(days=7)
    related = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        other = row.get('key', '')
        if not isinstance(other, str) or not other.startswith(PROJECT + '-') or other == key:
            continue
        f = row.get('fields') or row
        recent = False
        for value in (f.get('created'), f.get('updated')):
            if value:
                recent |= datetime.fromisoformat(value.replace('Z', '+00:00')) >= cutoff
        shared = sorted(tokens & significant_tokens(f.get('summary') or ''))
        if recent and shared:
            related.append({'source': 'jira', 'key': other, 'summary': f.get('summary'),
                            'matchedTokens': shared})
    if workspace is not None:
        for parent in (Path(workspace) / '.scratch', Path(workspace) / '.sdlc/work'):
            if not parent.exists():
                continue
            for slug in sorted(parent.iterdir()):
                if not slug.is_dir():
                    continue
                # 의도·계획 등 사람이 쓴 자료만 본다. 번들·바이너리·의존성은 읽지 않는다.
                texts = [slug.name]
                def walk_error(error):
                    raise error
                for directory, dirs, files in os.walk(slug, onerror=walk_error):
                    dirs[:] = sorted(d for d in dirs if d not in ('node_modules', '.git', 'build', 'dist'))
                    for name in sorted(files):
                        path = Path(directory) / name
                        if (path.suffix.lower() in ('.md', '.txt', '.json', '.yaml', '.yml')
                                and path.stat().st_size <= 262144):
                            texts.append(path.read_text(encoding='utf-8', errors='replace'))
                text = '\n'.join(texts)
                matched_key = bool(re.search(r'(?<![A-Za-z0-9])' + re.escape(key) + r'(?!\d)', text, re.I))
                shared = sorted(tokens & significant_tokens(text))
                if matched_key or shared:
                    related.append({'source': 'slug', 'path': str(slug), 'matchedKey': matched_key,
                                    'matchedTokens': shared})
    return related


def acli(args, timeout=120):
    p = subprocess.run(['acli', 'jira', 'workitem'] + args, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    if p.returncode:
        raise RuntimeError('acli rc=%d%s' % (p.returncode, ' unauthorized' if 'unauthorized' in p.stdout.lower() else ''))
    return json.loads(p.stdout)


def plain(node):
    """ADF → 사람이 읽는 텍스트 (멘션은 @이름, 링크 카드는 URL)."""
    if isinstance(node, list):
        return ''.join(plain(n) for n in node)
    if not isinstance(node, dict):
        return ''
    t = node.get('type')
    if t == 'text':
        return node.get('text', '')
    if t == 'mention':
        return node.get('attrs', {}).get('text', '@?')
    if t in ('inlineCard', 'blockCard'):
        return node.get('attrs', {}).get('url', '')
    if t == 'hardBreak':
        return '\n'
    inner = plain(node.get('content', []))
    return inner + ('\n' if t in ('paragraph', 'heading', 'listItem') else '')


def mentions(node, acc):
    if isinstance(node, list):
        return any(mentions(n, acc) for n in node)
    if isinstance(node, dict):
        if node.get('type') == 'mention' and node.get('attrs', {}).get('id') == acc:
            return True
        return mentions(node.get('content', []), acc)
    return False


def select_events(key, f, since_dt, handled, inflight=False):
    """한 티켓의 댓글 중 follow-up 이벤트를 고른다. inflight 면 창 밖의 미답변 멘션도 포함(backstop=True)."""
    assignee = (f.get('assignee') or {}).get('accountId')
    comments = (f.get('comment') or {}).get('comments', [])
    status = (f.get('status') or {}).get('name')
    post_release = status in POST_RELEASE_STATUSES
    events = []
    for i, c in enumerate(comments):
        cid = c.get('id')
        author = (c.get('author') or {}).get('accountId')
        if author == ME or cid in handled:
            continue
        created = datetime.fromisoformat(c['created'].replace('Z', '+00:00'))
        mine = mentions(c.get('body'), ME)
        # 이 댓글 뒤에 내가 이미 답했으면 사람이 직접 처리한 것으로 본다
        answered = any((x.get('author') or {}).get('accountId') == ME for x in comments[i + 1:])
        if created > since_dt:
            if not (mine or assignee == ME):
                continue
            backstop = False
        elif inflight and mine and not answered:
            backstop = True
        else:
            continue
        event = {
            'key': key, 'summary': f.get('summary'),
            'status': status, 'postRelease': post_release,
            'assignee': (f.get('assignee') or {}).get('displayName'),
            'assignedToMe': assignee == ME, 'mentionsMe': mine,
            'commentId': cid, 'author': (c.get('author') or {}).get('displayName'),
            'created': c['created'], 'text': plain(c.get('body')).strip(),
            'answeredByMeAfter': answered, 'backstop': backstop,
        }
        if post_release:
            event['links'] = post_release_links(f)
        events.append(event)
    return events


def post_release_links(f):
    """운영 후속 중복 확인용 연결 이슈 요약."""
    rows = []
    for link in f.get('issuelinks') or []:
        other = link.get('outwardIssue') or link.get('inwardIssue') or {}
        if other.get('key'):
            of = other.get('fields') or {}
            rows.append({'key': other['key'], 'summary': of.get('summary'),
                         'status': (of.get('status') or {}).get('name'),
                         'type': (link.get('type') or {}).get('name')})
    return rows


def select_intake(key, f, handled, queued, *, related_search=None):
    """백로그·해야 할 일 상태의 내 티켓을 댓글 없이도 한 번 의도 판정에 올린다."""
    cid = f'intake:{key}'
    summary = f.get('summary') or ''
    status = f.get('status') or {}
    assignee = f.get('assignee') or {}
    if ((status.get('statusCategory') or {}).get('key') != 'new' or not key.startswith(PROJECT + '-')
            or assignee.get('accountId') != ME or HOLD_MARK in summary or key in queued or cid in handled):
        return []
    comments = (f.get('comment') or {}).get('comments', [])
    reporter = f.get('reporter') or {}
    return [{
        'key': key, 'summary': summary, 'status': status.get('name'),
        'assignee': assignee.get('displayName'),
        'assignedToMe': True, 'mentionsMe': False, 'intake': True,
        'commentId': cid, 'author': reporter.get('displayName'), 'reporterId': reporter.get('accountId'),
        'created': f.get('created'), 'text': plain(f.get('description')).strip(),
        # 마지막 댓글이 내 것이면 상대 답을 기다리는 중이다: 같은 질문을 다시 올리지 않는다(RUNBOOK §7-2).
        'lastCommentByMe': bool(comments) and (comments[-1].get('author') or {}).get('accountId') == ME,
        'answeredByMeAfter': False, 'backstop': False,
        'related': related_search(key, summary) if related_search else [],
    }]


def todo_cli(args, timeout=60):
    p = subprocess.run(['node', str(SDLC_KIT / 'todo/todo.mjs'), *args], capture_output=True, text=True, timeout=timeout)
    # take 는 sdlc 대기로 돌아가면 10 을 낸다: 성공으로 본다.
    if p.returncode not in (0, 10):
        raise RuntimeError((p.stderr or p.stdout).strip()[:300] or f'exit {p.returncode}')
    return json.loads(p.stdout) if p.stdout.strip() else None


def todo_key(row):
    """to-do 항목의 티켓 키: 링크 라벨이 우선, 없으면 제목 첫 키."""
    label = ((row.get('link') or {}).get('label') or '')
    match = TICKET.fullmatch(label) or TICKET.search(row.get('title') or '')
    return match.group(0) if match else None


def todo_reply_id(row, reply):
    digest = hashlib.sha1(f"{reply.get('at')}\0{reply.get('what')}".encode()).hexdigest()[:10]
    return f"todo:{row['id']}:{digest}"


WAIT_PREFIX = '공장 대기 — '


def select_todo(rows, handled):
    """to-do 의 처리 안 된 내 답을 이벤트로 바꾼다. 답이 모두 처리된 연결 항목은 (항목, 키)로 돌려준다."""
    events, settled = [], []
    for row in rows:
        if not isinstance(row, dict) or row.get('status') == 'done':
            continue
        key = todo_key(row)
        replies = [r for r in row.get('replies') or [] if r.get('who') == 'human']
        if not key:
            continue
        fresh = []
        for reply in replies:
            cid = todo_reply_id(row, reply)
            if cid in handled:
                continue
            fresh.append({
                'key': key, 'summary': row.get('title'), 'source': 'todo', 'todoId': row['id'],
                'slug': row.get('slug'), 'todoState': row.get('state'), 'todoAction': row.get('action'),
                'commentId': cid, 'author': '운영자 (to-do 답)', 'created': reply.get('at'),
                'text': re.sub(r'^reply — ', '', reply.get('what') or ''),
                'assignedToMe': True, 'mentionsMe': True, 'answeredByMeAfter': False, 'backstop': False,
            })
        events.extend(fresh)
        if not fresh:
            settled.append((row, key))
    return events, settled


def todo_actions(settled, queue):
    """큐의 사람 대기를 to-do 표시로 옮기는 todo 명령들. 공장이 쓴 대기(WAIT_PREFIX)만 지운다."""
    actions = []
    for row, key in settled:
        item = queue.get(key) if isinstance(queue.get(key), dict) else {}
        waiting = item.get('waiting')
        current = row.get('waiting') or ''
        if waiting:
            note = ' '.join(str(item.get('note') or '').split())[:200]
            text = f'{WAIT_PREFIX}{waiting}' + (f': {note}' if note else '')
            if current != text or row.get('unread'):
                actions.append(['wait', row['id'], text])
            continue
        if current.startswith(WAIT_PREFIX):
            actions.append(['resume', row['id'], '공장 큐 대기 해제'])
        if row.get('unread'):
            actions.append(['take', row['id'], '--json'])
    return actions


def main(argv=None, *, runner=acli, todo=None, related_search=None, workspace=WORKSPACE, base=BASE, sleep=time.sleep):
    ap = argparse.ArgumentParser()
    ap.add_argument('--since')
    ap.add_argument('--out', default=str(base / 'followup-scan.json'))
    a = ap.parse_args(argv)
    state_path = base / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    fu = state.get('followup', {})
    # 고정 72시간 창 + handled 중복 제거: 끝내지 못한 댓글이 창 이동으로 사라지지 않게 한다.
    since = a.since or (datetime.now(TZ) - timedelta(hours=72)).isoformat()
    since_dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
    handled = set(fu.get('handled', {}).keys())
    queue_path = base / 'followup-queue.json'
    queued = set(json.loads(queue_path.read_text())) if queue_path.exists() else set()
    out = {'since': since, 'scannedAt': datetime.now(TZ).isoformat(), 'me': ME, 'events': [], 'errors': []}

    jql_since = since_dt.astimezone(TZ).strftime('%Y-%m-%d %H:%M')
    keys, inflight, intake = [], set(), set()
    for scope, jql in (('search', f'updated >= "{jql_since}" ORDER BY updated DESC'),
                       ('inflight', INFLIGHT_JQL + ' ORDER BY updated DESC'),
                       ('intake', INTAKE_JQL + ' ORDER BY created ASC')):
        try:
            issues = runner(['search', '--jql', jql, '--fields', 'key,summary', '--json', '--limit', '200'])
        except Exception as e:  # noqa: BLE001 - 보고용
            out['errors'].append({'scope': scope, 'error': str(e)})
            continue
        for it in issues:
            if not isinstance(it, dict) or not it.get('key'):
                continue  # acli search 결과에 null 항목이 섞여 온다 (실측)
            if scope == 'inflight':
                inflight.add(it['key'])
            if scope == 'intake':
                intake.add(it['key'])
            if it['key'] not in keys:
                keys.append(it['key'])

    recent_rows = None
    def recent_search(args):
        nonlocal recent_rows
        if recent_rows is None:
            recent_rows = runner(args)
        return recent_rows

    def related(key, summary):
        return find_related(key, summary, search=recent_search, workspace=workspace)

    for key in keys:
        for attempt in range(2):
            try:
                v = runner(['view', key, '--fields', 'summary,status,assignee,reporter,comment,description,created,issuelinks', '--json'])
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    sleep(3)
                else:
                    out['errors'].append({'scope': key, 'error': str(e)})
        else:
            continue
        f = v.get('fields', {})
        out['events'].extend(select_events(key, f, since_dt, handled, key in inflight))
        if key in intake:
            events = select_intake(key, f, handled, queued)
            for event in events:
                try:
                    event['related'] = (related_search or related)(key, event['summary'])
                except Exception:
                    # 검색 실패를 후보 없음으로 기록해 착수하지 않게 한다.
                    event['relatedIncomplete'] = True
                    out['errors'].append({'scope': 'related:' + key, 'error': 'related-search-failed'})
            out['events'].extend(events)

    # 시험은 todo 를 주입한다. 실제 to-do 는 스크립트 실행(__main__)에서만 읽는다.
    try:
        events, settled = select_todo(todo(['list', '--json']), handled) if todo else ([], [])
        out['events'].extend(events)
        queue = json.loads(queue_path.read_text()) if queue_path.exists() else {}
        out['todoSynced'] = []
        for action in todo_actions(settled, queue):
            todo(action)
            out['todoSynced'].append(action[:2])
    except Exception as e:  # noqa: BLE001 - to-do 를 못 읽어도 Jira 스캔은 그대로 낸다
        out['errors'].append({'scope': 'todo', 'error': str(e)[:300]})

    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(f"scan events={len(out['events'])} errors={len(out['errors'])} out={a.out}")


if __name__ == '__main__':
    main(todo=todo_cli)
