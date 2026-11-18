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
  waiting 이 있으면 `todo wait`(문구 `공장 대기 — <waiting>: <note>`, reporter 는 `todo others`), 풀렸으면 `todo resume`, 남은 안 읽은 답은 `todo take`
  (읽히지 않은 "continue" 답이나 큐는 사람 대기인데 sdlc 는 READY 인 항목이 화면에 몇 시간씩 '에이전트 진행 중'으로 남지 않게 한다).
- 큐 상태: 큐에 있는 키의 현재 Jira 상태를 `queueStatuses`로 낸다. 위에서 조회한 티켓은 그 값을 쓰고 나머지만
  한 번에 묻는다. 가드가 이 값으로 끝난 항목을 지우고 QA 회신 대기를 바로잡는다(followup_idle.py, 2026-11-02 큐 40건 중
  약 8건이 이미 운영·스테이징 검수 완료이거나 QA 요청 상태인데 human-risk로 남음). 조회 실패는 `queueStatusError`에만 남긴다.
출력: JSON {since, me, events:[...], errors:[...], queueStatuses:{KEY: 상태}}. 마지막 줄 stdout: `scan events=<n> errors=<n> out=<path>`.
설정: followup_config.py(JIRA_ACCOUNT_ID·JIRA_PROJECT·STATUS_*·JIRA_FOLLOWUP_WORKSPACE).
"""
import argparse, hashlib, json, os, re, subprocess, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
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
# 티켓 조회는 건당 약 1.4초다. 72시간 창이 150건을 넘으면 순차 조회만으로 가드의 300초 제한에 닿는다(2026-10-29 실측 156건).
VIEW_WORKERS = 6
RELATED_JQL = f'project = {PROJECT} AND (created >= -7d OR updated >= -7d) ORDER BY updated DESC'
# 서비스명·환경명·일반 요청어만 같다는 이유로 모든 티켓을 중복 후보로 만들지 않는다.
TOKEN_STOP = {PROJECT.casefold(), 'ai', 'web', 'api', 'dev', 'stg', 'prd',
              '개발', '스테이징', '운영', '오류', '수정', '요청', '확인', '화면'}
TOKEN_STOP |= {t.strip().casefold() for t in cfg.get('RELATED_STOP_WORDS', '').split(',') if t.strip()}


def significant_tokens(text):
    text = re.sub(re.escape(PROJECT) + r'-\d+', ' ', unicodedata.normalize('NFKC', text), flags=re.I).casefold()
    return {token for token in re.findall(r'[a-z][a-z0-9]*|[가-힣]{2,}', text)
            if len(token) >= 2 and token not in TOKEN_STOP}


def slug_texts(workspace):
    """[(slug 경로, slug 이름 + 사람이 쓴 문서 본문)]. 한 스캔에서 한 번만 읽는다.

    의도·계획 등 사람이 쓴 자료만 본다. 번들·바이너리·의존성과 저장소 체크아웃(`.git` 파일·폴더가 있는 폴더,
    worktree 포함)은 읽지 않는다(2026-10-29 실측: worktree `.git`은 파일이라 이름 제외에 안 걸려 .scratch 157GB·
    411만 파일을 접수 티켓마다 다시 훑었고, 건당 56초로 스캔이 300·570초 제한을 넘겨 공장이 멈췄다).
    """
    rows = []
    def walk_error(error):
        raise error
    for parent in (Path(workspace) / '.scratch', Path(workspace) / '.sdlc/work'):
        if not parent.exists():
            continue
        for slug in sorted(parent.iterdir()):
            if not slug.is_dir():
                continue
            texts = [slug.name]
            for directory, dirs, files in os.walk(slug, onerror=walk_error):
                if '.git' in dirs or '.git' in files:
                    dirs[:] = []
                    continue
                dirs[:] = sorted(d for d in dirs if d not in ('node_modules', 'build', 'dist'))
                for name in sorted(files):
                    path = Path(directory) / name
                    if (path.suffix.lower() in ('.md', '.txt', '.json', '.yaml', '.yml')
                            and path.stat().st_size <= 262144):
                        texts.append(path.read_text(encoding='utf-8', errors='replace'))
            rows.append((slug, '\n'.join(texts)))
    return rows


def find_related(key, summary, *, search=None, workspace=None, slugs=None):
    """후보만 찾는다. 담당자·중복 여부·작업 권한은 판정하지 않는다.

    최근 7일 조건은 RELATED_JQL 이 맡는다. acli search 는 --fields 에 created·updated 를 받지 않는다
    (`fields 'created, updated' are not allowed` → 모든 백로그 접수가 related-search-failed 로 멈춤).
    """
    rows = (search or acli)(['search', '--jql', RELATED_JQL, '--fields',
                            'key,summary,assignee', '--json', '--paginate'])
    if not isinstance(rows, list):
        raise ValueError('invalid related search')
    tokens = significant_tokens(summary)
    related = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        other = row.get('key', '')
        if not isinstance(other, str) or not other.startswith(PROJECT + '-') or other == key:
            continue
        f = row.get('fields') or row
        shared = sorted(tokens & significant_tokens(f.get('summary') or ''))
        if shared:
            related.append({'source': 'jira', 'key': other, 'summary': f.get('summary'),
                            'matchedTokens': shared})
    if slugs is None and workspace is not None:
        slugs = slug_texts(workspace)
    # 토큰은 slug 이름으로만 맞춘다. 문서 본문은 키로만 맞춘다(본문 토큰까지 보면 흔한 단어 하나로
    # 거의 모든 slug 가 걸린다: 2026-10-27 실측 179건).
    for slug, text in slugs or []:
        matched_key = bool(re.search(r'(?<![A-Za-z0-9])' + re.escape(key) + r'(?!\d)', text, re.I))
        shared = sorted(tokens & significant_tokens(slug.name))
        if matched_key or shared:
            related.append({'source': 'slug', 'path': str(slug), 'matchedKey': matched_key,
                            'matchedTokens': shared})
    return related


def statuses(keys, search=None):
    """{KEY: 현재 상태 이름}. 검색 한 번으로 묻는다(scratch_cleanup 도 쓴다)."""
    if not keys:
        return {}
    rows = (search or acli)(['search', '--jql', 'key in (%s)' % ','.join(sorted(keys)),
                             '--fields', 'key,status', '--json', '--limit', str(len(keys))])
    return {row['key']: ((row.get('fields') or {}).get('status') or {}).get('name')
            for row in rows if isinstance(row, dict) and row.get('key')}


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
    # take 는 sdlc 대기로 돌아가면 10, 다른 사람 회신 대기면 15 를 낸다: 성공으로 본다.
    if p.returncode not in (0, 10, 15):
        raise RuntimeError((p.stderr or p.stdout).strip()[:300] or f'exit {p.returncode}')
    out = p.stdout.strip()
    return (json.loads(out) if out else None) if '--json' in args else out


def todo_key(row):
    """to-do 항목의 티켓 키: 링크 라벨이 우선, 없으면 제목 첫 키."""
    label = ((row.get('link') or {}).get('label') or '')
    match = TICKET.fullmatch(label) or TICKET.search(row.get('title') or '')
    return match.group(0) if match else None


def todo_reply_id(row, reply):
    digest = hashlib.sha1(f"{reply.get('at')}\0{reply.get('what')}".encode()).hexdigest()[:10]
    return f"todo:{row['id']}:{digest}"


WAIT_PREFIX = '공장 대기 — '
DECISION_ITEM = re.compile(r'^\[(?:결정 D\d+|스테이징 승인)\] ')
DECISION_CODE = re.compile(r'^\[(결정 D\d+)\] ')
# 스테이징 승인은 '[스테이징 승인]' 항목, 환경 조치는 답글 없이 풀린다. 이 대기는 결정 항목을 가리키지 않는다.
NOT_DECIDED_HERE = ('stg-approval', 'env-human')


def open_decisions(rows):
    """열린 결정 항목('[결정 D62] PROJ-1439 · ...')의 키 → '결정 D62'. 그 키의 결정은 결정 항목 답글로만 기록된다."""
    found = {}
    for row in rows or []:
        match = DECISION_CODE.match(row.get('title') or '') if isinstance(row, dict) and row.get('status') != 'done' else None
        if match and (key := todo_key(row)):
            found.setdefault(key, match[1])
    return found


def decision_where(code):
    """티켓 항목에는 선택지가 없다. 같은 키의 결정 항목이 열려 있으면 그쪽을 가리킨다(2026-11-06 실측)."""
    return f"정해 주실 것은 '[{code}]' 항목에서 답해 주세요(이 항목 답글은 결정으로 기록되지 않아요)"


def select_todo(rows, handled):
    """to-do 의 처리 안 된 내 답을 이벤트로 바꾼다. 답이 모두 처리된 연결 항목은 (항목, 키)로 돌려준다."""
    events, settled = [], []
    for row in rows:
        if not isinstance(row, dict) or row.get('status') == 'done':
            continue
        key = todo_key(row)
        replies = [r for r in row.get('replies') or [] if r.get('who') == 'human']
        # 결정 항목('[결정 D12] ...')의 답은 결정 수신기(decision_inbox.poll)가 결정으로 기록한다.
        if not key or DECISION_ITEM.match(row.get('title') or ''):
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


def slug_actions(rows, queue, summary=lambda key: ''):
    """공장이 sdlc-kit으로 진행하는 큐 티켓(slug 있음)을 할 일 항목으로 잇는 todo 명령들(사용자 지시 2026-11-03).
    slug 항목이 있으면 그대로, 같은 키의 slug 없는 항목이 있으면 그 항목에 slug를 붙이고, 없으면 새로 만든다.
    상태(단계·사람 대기)는 to-do의 sdlc 동기화가 맞춘다."""
    rows = [r for r in rows if isinstance(r, dict) and r.get('status') != 'done']
    actions = []
    for key, item in queue.items():
        slug = item.get('slug') if isinstance(item, dict) else None
        if not slug or any(r.get('slug') == slug for r in rows):
            continue
        plain = next((r for r in rows if todo_key(r) == key and not r.get('slug')
                      and not DECISION_ITEM.match(r.get('title') or '')), None)
        if plain:
            actions.append(['start', plain['id'], '--slug', slug])
        else:
            actions.append(['add', f'{key} {summary(key)}'.strip(), '--slug', slug, '--project', str(WORKSPACE),
                            '--link', cfg.BROWSE + key, '--by', 'factory'])
    return actions


def repo_actions(rows, queue):
    """큐 `service`(쉼표로 이은 저장소)를 그 키의 열린 할 일 항목 `repos`로 옮기는 todo 명령들.
    티켓 항목과 결정 항목 모두 어느 저장소를 고치는지 항상 보이게 한다(사용자 지시 2026-11-03)."""
    actions = []
    for row in rows:
        if not isinstance(row, dict) or row.get('status') == 'done':
            continue
        key = todo_key(row)
        item = queue.get(key) if key and isinstance(queue.get(key), dict) else {}
        repos = [r for r in re.split(r'[\s,]+', str(item.get('service') or '')) if r]
        if repos and list(row.get('repos') or []) != list(dict.fromkeys(repos)):
            actions.append(['repos', row['id'], ','.join(dict.fromkeys(repos))])
    return actions


# 같은 대기에 답이 안 먹혔음을 알리는 머리말. 다음 스캔은 이 머리말을 빼고 비교해 같은 대기를 다시 쓰지 않는다.
NO_EFFECT = re.compile(r"받은 답 '[^']*'\(으\)로는 바뀐 것이 없어요 — ")
# 공장 권한 밖이라 거절한 답(handled action=refused, rule=근거 규칙)의 머리말. 같은 답에는 한 번만 쓴다(2026-11-06
# '시크릿 준비 직접해줘' 거절이 큐 note에만 남고 할 일에는 같은 질문이 그대로였다).
REFUSED = re.compile(r"받은 답 '[^']*'은\(는\) 공장이 할 수 없어요 \[[^\]]*\] — ")


def shown_wait(text):
    return REFUSED.sub('', NO_EFFECT.sub('', text))


def human_action(item):
    """human-risk의 사람만 할 수 있는 조치(state_update patch --action). 권장 답 없이 할 일·실행지만 보인다."""
    action = ' '.join(str(item.get('action') or '').split()) if item.get('waiting') == 'human-risk' else ''
    sheet = ' '.join(str(item.get('runSheet') or '').split())
    return f"할 일: {action}" + (f" · 실행지: {sheet}" if sheet else '') + ACTION_TAIL if action else ''


def refusals(rows, handled):
    """마지막 내 답을 공장이 권한 밖이라 거절했으면 항목 id → 근거 규칙."""
    found = {}
    for row in rows or []:
        replies = [r for r in row.get('replies') or [] if r.get('who') == 'human'] if isinstance(row, dict) else []
        entry = handled.get(todo_reply_id(row, replies[-1])) if replies else None
        if isinstance(entry, dict) and entry.get('action') == 'refused':
            found[row['id']] = ' '.join(str(entry.get('rule') or 'RUNBOOK §0 허용 쓰기 밖').replace(']', ')').split())[:120]
    return found


# 기획·QA 회신 대기는 내 차례가 아니다(2026-11-04 이력: 답글 218건 중 49건이 할 일 없는 reporter 대기에 감).
OTHERS_PREFIX = '기획·QA 회신 대기 — '
STG_WHERE = "승인·보류는 '[스테이징 승인]' 항목에 답해 주세요"
ENV_AUTO = ' · 조치하면 공장이 확인 뒤 스스로 이어갑니다(답글 필요 없음)'
ACTION_TAIL = ' · 공장이 대신할 수 없는 일이에요. 하신 뒤 무엇을 했는지 적어 답해 주세요'
# 환경 조치 대기·공장 경보의 답 칸 권장 답. 보내면 다음 tick을 기다리지 않고 공장이 바로 다시 확인한다(2026-11-05 사용자 지시
# "다른 것도 다 한 줄로 답해 주세요로 있는 것들이 많은데").
ENV_DONE = '조치했어요'
# preflight_env.vpn_action의 'type OTP in Terminal: open -a Terminal <경로>'. 할 일 앱 '터미널에서 열기' 버튼(sdlc-kit v2.22.0 `run`)이
# 명령을 입력만 해 둔 채로 연다(2026-11-05 사용자 지시). 경로 뒤에 붙는 안내 문구는 뺀다.
TERMINAL_COMMAND = re.compile(r"in Terminal: (open -a Terminal (?:'[^']*'|\S+))")


def terminal_command(text):
    found = TERMINAL_COMMAND.search(' '.join(str(text or '').split()))
    return found[1] if found else ''


def env_actions(base):
    """가드가 마지막 tick에 찾은 환경 조치(guard-last.json waitingHuman: 조치 → 키들)를 키별로 돌려준다."""
    try:
        found = json.loads((Path(base) / 'guard-last.json').read_text()).get('waitingHuman') or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {key: action for action, keys in found.items() if isinstance(keys, list) for key in keys}


def last_reply(row):
    replies = [r for r in row.get('replies') or [] if r.get('who') == 'human']
    return re.sub(r'^reply — ', '', replies[-1].get('what') or '') if replies else ''


def suggest_action(row, suggest):
    """할 일 앱 답 칸에 미리 채울 권장 답(sdlc-kit v2.21.0 `suggest`). 같으면 아무것도 안 한다. 빈 값은 지운다."""
    return [['suggest', row['id'], suggest]] if (row.get('suggest') or '') != suggest else []


def todo_actions(settled, queue, env=None, decisions=None, refused=None):
    """큐의 사람 대기를 to-do 표시로 옮기는 todo 명령들. 공장이 쓴 대기(WAIT_PREFIX·OTHERS_PREFIX)만 지운다.
    reporter는 'others'(다른 사람 회신 대기)로, env-human은 해야 할 조치로 보이고, 처리했는데 대기가 그대로면
    같은 문구를 다시 쓰지 않고 답이 안 먹혔다고 알린다(2026-11-04 사용자 지시)."""
    env, decisions, refused = env or {}, decisions or {}, refused or {}
    actions = []
    for row, key in settled:
        item = queue.get(key) if isinstance(queue.get(key), dict) else {}
        waiting = item.get('waiting')
        current = row.get('waiting') or ''
        others = row.get('others') or ''
        unread = row.get('unread')
        # note는 ' | '로 새 기록을 뒤에 덧붙인다. 앞 200자는 가장 오래된 기록이라 보드가 갱신되지 않았다(2026-10-30 실측).
        note = ' '.join(str(item.get('note') or '').split(' | ')[-1].split())[:200]
        # human-risk의 질문·권장 답(작업자 결과 question·suggest). 없으면(옛 항목) 예전처럼 기록을 보인다.
        question = ' '.join(str(item.get('question') or '').split()) if waiting == 'human-risk' else ''
        suggest = ' '.join(str(item.get('suggest') or '').split()) if waiting == 'human-risk' else ''
        action = human_action(item)
        if action:
            question, suggest = action, ''
        command = terminal_command(env.get(key)) if waiting == 'env-human' else ''
        if waiting == 'env-human':
            suggest = ENV_DONE
        if waiting == 'reporter':
            text = OTHERS_PREFIX + (note or '회신 오면 공장이 이어갑니다')
            if unread:
                actions.append(['take', row['id'], '--json'])
            if others != text or current.startswith(WAIT_PREFIX):
                actions.append(['others', row['id'], text])
            actions += suggest_action(row, suggest)
            continue
        if waiting:
            if waiting == 'env-human' and env.get(key):
                body = f"할 일: {env[key]}" + (ENV_AUTO if item.get('needs') else '')
            elif waiting == 'stg-approval':
                # 스테이징 승인은 '[스테이징 승인]' 항목 답글로만 받는다. 티켓 항목의 '승인'은 기록되지 않았다(2026-11-04 실측).
                body = STG_WHERE + (f' · {note}' if note else '')
            else:
                body = note
            # 질문이 있으면 사용자가 정할 것만 쉬운 말로 보인다. 대기 종류 낱말(human-risk)과 작업 기록은 뺀다.
            head = WAIT_PREFIX if question else f'{WAIT_PREFIX}{waiting}: '
            body = question or body
            code = decisions.get(key) if waiting not in NOT_DECIDED_HERE else None
            if code:
                # 권장 답을 비운다: 티켓 항목에 보낸 답은 결정으로 기록되지 않는다.
                head, body, suggest = WAIT_PREFIX, decision_where(code) + (f' · {body}' if body else ''), ''
            text = head + body if body else f'{WAIT_PREFIX}{waiting}'
            shown = shown_wait(current)
            # 답글은 to-do 대기를 지운다. 직전에 물은 문구는 sdlc-kit v2.20.0 `asked`에 남는다.
            before = shown_wait(current or row.get('asked') or '')
            reply = ' '.join(last_reply(row).replace("'", '').split())[:20]
            if row['id'] in refused and reply:
                # 무엇을 요청했고, 공장이 왜 못 하며(근거 규칙), 남은 일이 무엇인지 한 줄. 같은 답에는 문구가 같아 한 번만 쓴다.
                text = f"{head}받은 답 '{reply}'은(는) 공장이 할 수 없어요 [{refused[row['id']]}] — {body or '남은 일은 아래를 보세요'}"
                shown = current
            elif unread and before == text and reply:
                text = f"{head}받은 답 '{reply}'(으)로는 바뀐 것이 없어요 — {body or '아래 내용을 보고 다시 답해 주세요'}"
            if shown != text or unread:
                actions.append(['wait', row['id'], text])
            actions += suggest_action(row, suggest)
            if command and (row.get('run') or '') != command:
                actions.append(['run', row['id'], command])
            continue
        if current.startswith(WAIT_PREFIX) or others.startswith(OTHERS_PREFIX):
            actions.append(['resume', row['id'], '공장 큐 대기 해제'])
            actions += suggest_action(row, '')
        if unread:
            actions.append(['take', row['id'], '--json'])
    return actions


def main(argv=None, *, runner=acli, todo=None, related_search=None, workspace=WORKSPACE, base=BASE, sleep=time.sleep, clock=time.monotonic):
    started = clock()
    timings, attempts = {}, {}
    original_runner = runner
    lock = threading.Lock()
    def add_time(scope, before):
        with lock:
            timings[scope] = timings.get(scope, 0) + max(0, clock() - before)
    def measured(scope, args):
        before = clock()
        with lock:
            attempts[scope] = attempts.get(scope, 0) + 1
        try:
            return original_runner(args)
        finally:
            add_time(scope, before)
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
            issues = measured(scope, ['search', '--jql', jql, '--fields', 'key,summary', '--json', '--limit', '200'])
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
            recent_rows = measured("related", args)
        return recent_rows

    slugs = None
    def related(key, summary):
        nonlocal slugs
        if slugs is None:
            before = clock()
            slugs = slug_texts(workspace) if workspace is not None else []
            timings['slugs'] = max(0, clock() - before)
        return find_related(key, summary, search=recent_search, slugs=slugs)

    def view(key):
        for attempt in range(2):
            try:
                return measured('view:' + key, ['view', key, '--fields', 'summary,status,assignee,reporter,comment,description,created,issuelinks', '--json']), None
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    before = clock()
                    sleep(3)
                    add_time('view:' + key, before)
                else:
                    return None, e

    with ThreadPoolExecutor(max_workers=VIEW_WORKERS) as pool:
        views = list(pool.map(view, keys))
    known = {}
    for key, (v, error) in zip(keys, views):
        if error is not None:
            out['errors'].append({'scope': key, 'error': str(error)})
            continue
        f = v.get('fields', {})
        known[key] = (f.get('status') or {}).get('name')
        out['events'].extend(select_events(key, f, since_dt, handled, key in inflight))
        if key in intake:
            events = select_intake(key, f, handled, queued)
            for event in events:
                try:
                    event['related'] = (related_search or related)(key, event['summary'])
                except Exception as e:
                    # 검색 실패를 후보 없음으로 기록해 착수하지 않게 한다.
                    event['relatedIncomplete'] = True
                    out['errors'].append({'scope': 'related:' + key, 'error': 'related-search-failed: ' + str(e)[:300]})
            out['events'].extend(events)

    # 일시정지(factory_hold)가 이벤트·큐 밖 티켓의 상태도 판정하도록 이번에 조회한 상태를 모두 낸다.
    out['statuses'] = dict(known)
    # 큐 정리용 상태다. 실패해도 새 이벤트가 아니므로 errors(가드를 깨움)에 넣지 않는다.
    out['queueStatuses'] = {k: v for k, v in known.items() if k in queued}
    try:
        out['queueStatuses'].update(statuses(queued - set(known), lambda args: measured('queue-status', args)))
    except Exception as e:  # noqa: BLE001 - 보고용
        out['queueStatusError'] = str(e)[:300]

    # 시험은 todo 를 주입한다. 실제 to-do 는 스크립트 실행(__main__)에서만 읽는다.
    try:
        queue = json.loads(queue_path.read_text()) if queue_path.exists() else {}
        rows = todo(['list', '--json']) if todo else []
        def summary(key):
            try:
                return (runner(['view', key, '--fields', 'summary', '--json']).get('fields') or {}).get('summary') or ''
            except Exception:  # noqa: BLE001 - 제목은 키만으로도 항목을 만든다
                return ''
        linked = slug_actions(rows, queue, summary) if todo else []
        for action in linked:
            todo(action)
        out['todoLinked'] = [action[:2] for action in linked]
        rows = todo(['list', '--json']) if linked else rows
        out['todoRepos'] = []
        for action in repo_actions(rows, queue):
            # 저장소 표시는 보조 정보다. 실패해도(구버전 sdlc-kit 등) 대기·답글 동기화는 이어 간다.
            try:
                todo(action)
                out['todoRepos'].append(action[1:])
            except Exception as e:  # noqa: BLE001
                out['errors'].append({'scope': 'todo-repos', 'error': str(e)[:200]})
                break
        events, settled = select_todo(rows, handled) if todo else ([], [])
        out['events'].extend(events)
        out['todoSynced'] = []
        titles = {row['id']: (row.get('title') or '', key) for row, key in settled}
        optional_failed = set()
        decisions = open_decisions(rows)
        for action in todo_actions(settled, queue, env_actions(base), decisions,
                                   refusals([row for row, _ in settled], fu.get('handled', {}))):
            if action[0] in ('suggest', 'run'):
                # 권장 답·실행 명령은 보조 정보다. 옛 sdlc-kit(suggest는 v2.21.0, run은 v2.22.0 전)이면 종류마다 오류
                # 한 번만 남기고 대기·답글 동기화는 이어 간다.
                if action[0] in optional_failed:
                    continue
                try:
                    todo(action)
                    out['todoSynced'].append(action[:2])
                except Exception as e:  # noqa: BLE001
                    optional_failed.add(action[0])
                    out['errors'].append({'scope': f'todo-{action[0]}', 'error': str(e)[:200]})
                continue
            todo(action)
            out['todoSynced'].append(action[:2])
            if action[0] == 'wait':
                # 할 일 화면이면 공장 대기 항목에 최신 요약(배경·이유)을 붙인다(사용자 지시 2026-11-03).
                import decision_inbox
                if decision_inbox.surface(base) != 'html':
                    title, key = titles[action[1]]
                    decision_inbox.attach_ticket_brief(action[1], key, base, title, decision=decisions.get(key))
    except Exception as e:  # noqa: BLE001 - to-do 를 못 읽어도 Jira 스캔은 그대로 낸다
        out['errors'].append({'scope': 'todo', 'error': str(e)[:300]})

    out.update(timings={**timings, 'totalSeconds': max(0, clock() - started)}, attempts=attempts)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(f"scan events={len(out['events'])} errors={len(out['errors'])} out={a.out}")


if __name__ == '__main__':
    main(todo=todo_cli)
