#!/usr/bin/env python3
"""RUNBOOK §§1–6: collect, plan and report; only --execute changes Jira/state/dev-note board.

Default dry-run writes evidence and report.md to --out-dir only. Model judgments
(classifications, actions, drafts) may be supplied with --judgments; missing
judgments stay explicitly pending. No comments are posted and §7 is not run.

Settings (followup_config.py / config.env): STATUS_ORDER and STATUS_ON_DEV/STG/PROD map
deployments to Jira statuses; BRANCH_DEV/STG/MAIN name the deployment branches; REPO_GLOB
selects repos under JIRA_FOLLOWUP_WORKSPACE; JIRA_EMAIL + JIRA_API_TOKEN or JIRA_TOKEN_FILE
authenticate the read-only REST calls; DEV_NOTE_CLI (optional) enables the board sync.
"""
import argparse
import base64
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

import followup_config as cfg
from followup_scan import INFLIGHT_JQL, ME, plain
from state_update import transaction

TZ = cfg.TZ
BASE = cfg.HOME_DIR
ROOT = cfg.WORKSPACE
ORDER = list(cfg.STATUS_ORDER)
# Status a ticket should reach once every ticket commit is on that environment's branch.
TARGETS = [('prd', cfg.get('STATUS_ON_PROD', ORDER[-1])), ('stg', cfg.get('STATUS_ON_STG', ORDER[3])),
           ('dev', cfg.get('STATUS_ON_DEV', ORDER[1]))]
BRANCH = {'dev': cfg.BRANCH_DEV, 'stg': cfg.BRANCH_STG, 'prd': cfg.BRANCH_MAIN}
CLASSES = {'답변 필요', '확인·조치 요청', '정보 공유'}


# Optional wrapper for every external command, e.g. a log-compressing proxy (e.g. CMD_PREFIX="mywrapper proxy").
CMD_PREFIX = cfg.get('CMD_PREFIX').split()
# Optional shell command that re-authenticates acli once after an 'unauthorized' reply.
REAUTH_CMD = cfg.get('JIRA_REAUTH_CMD')
# Jira links stripped from reports; JIRA_LINK_HOST_RE widens it (e.g. to every Jira Cloud host).
SITE = cfg.get('JIRA_LINK_HOST_RE', re.escape(cfg.JIRA_SITE))


def command(argv, timeout=150):
    return subprocess.run([*CMD_PREFIX, *map(str, argv)], capture_output=True, text=True, timeout=timeout)


def jira_get(path):
    """Read-only Jira REST GET. Credentials come from the environment or a git-ignored file."""
    token = cfg.get('JIRA_API_TOKEN') or Path(cfg.get('JIRA_TOKEN_FILE', '~/.config/jira/token')).expanduser().read_text().strip()
    email = cfg.get('JIRA_EMAIL')
    if not email:
        raise RuntimeError('BLOCKED: JIRA_EMAIL 미설정')
    auth = base64.b64encode((email + ':' + token).encode()).decode()
    req = urllib.request.Request(f'https://{cfg.JIRA_SITE}/rest/api/3/' + path,
                                 headers={'Authorization': 'Basic ' + auth, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.load(response)


def iso(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def previous_business_day(now):
    day = now.astimezone(TZ).date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, datetime.min.time(), TZ).replace(hour=9).isoformat()


def business_days(updated, now):
    day, end = iso(updated).astimezone(TZ).date(), now.astimezone(TZ).date()
    count = 0
    while day < end:
        day += timedelta(days=1)
        count += day.weekday() < 5
    return count


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def cell(value):
    text = str(value or '').replace('|', '\\|').replace('\n', ' ')
    text = re.sub(r'\[([^]]+)\]\(https?://[^)\s]*' + SITE + r'[^)\s]*\)', r'\1', text)
    return re.sub(r'<?https?://[^\s)>]*' + SITE + r'[^\s)>]*>?', '', text)


class Morning:
    def __init__(self, base, root, out, *, runner=command, rest=jira_get, refs=None, note=None):
        self.base, self.root, self.out = Path(base), Path(root), Path(out)
        self.runner, self.rest, self.refs, self.note = runner, rest, refs, note
        self.reauthed = False
        self.errors = []
        self.fetch_errors = {}
        self.out.mkdir(parents=True, exist_ok=True)

    def error(self, scope, key, reason):
        self.errors.append({'scope': scope, 'key': key, 'error': reason})

    def acli(self, args):
        argv = ['acli', 'jira', 'workitem', *args]
        result = self.runner(argv, timeout=150)
        if result.returncode and 'unauthorized' in ((result.stdout or '') + (result.stderr or '')).lower():
            if not self.reauthed:
                self.reauthed = True
                if REAUTH_CMD:
                    self.runner(['bash', '-c', REAUTH_CMD], timeout=150)
                result = self.runner(argv, timeout=150)
            if result.returncode:
                raise RuntimeError('BLOCKED: acli 인증')
        if result.returncode:
            raise RuntimeError('acli 실패 rc=' + str(result.returncode))
        return json.loads(result.stdout)

    def git(self, repo, *args, timeout=150):
        return self.runner(['git', '-C', str(repo), *args], timeout=timeout)

    def page(self, key, kind):
        start, result = 0, []
        while True:
            page = self.rest(f'issue/{key}/{kind}?startAt={start}&maxResults=100')
            rows = page.get('comments' if kind == 'comment' else 'values')
            if not isinstance(rows, list):
                raise ValueError('잘못된 페이지')
            result.extend(rows)
            start += len(rows)
            total = page.get('total')
            if page.get('isLast') is True or (isinstance(total, int) and start >= total):
                return result
            if not rows or (total is None and 'isLast' not in page):
                raise ValueError('불완전한 페이지')

    def fetch(self, repo):
        try:
            result = self.git(repo, 'fetch', 'origin', '--tags', '--quiet', timeout=600)
            self.fetch_errors[str(repo)] = (result.stdout or '') + (result.stderr or '')
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError) as error:
            self.fetch_errors[str(repo)] = type(error).__name__
            return False

    def inspect_refs(self, key, repos):
        if self.refs:
            return self.refs(key, repos)
        if not repos:
            return {'key': key, 'status': 'none', 'repos': []}
        args = [sys.executable, str(Path(__file__).with_name('ticket_refs.py')), key]
        for repo in repos:
            args += ['--repo', str(repo)]
        result = self.runner(args, timeout=300)
        if result.returncode not in (0, 1):
            raise RuntimeError('ticket_refs 조회 실패')
        value = json.loads(result.stdout)
        if value.get('error') or value.get('errors'):
            raise RuntimeError('ticket_refs 조회 실패')
        # RUNBOOK §2 uses the complete own-branch tip. Keep marker-only carried
        # repositories too; choosing `own or marker` would silently drop them.
        by_repo = {row['repo']: row for row in value.get('repos', [])}
        for repo in repos:
            for branch in ('fix/' + key, 'feat/' + key):
                tip = self.git(repo, 'rev-parse', '--verify', '-q', 'origin/' + branch)
                if tip.returncode:
                    continue
                sha = tip.stdout.strip()
                if not re.fullmatch(r'[0-9a-fA-F]{40,64}', sha):
                    raise RuntimeError('티켓 브랜치 끝 커밋 식별 실패')
                tags = self.git(repo, 'tag', '--contains', sha, '-l', 'v*')
                if tags.returncode:
                    raise RuntimeError('티켓 브랜치 태그 조회 실패')
                old = by_repo.get(repo.name, {})
                branches = list(old.get('branches', []))
                if not any(b['branch'] == branch for b in branches):
                    branches.append({'branch': branch, 'own': True, 'other_keys': []})
                row = {**old, 'repo': repo.name, 'tip': sha, 'branch': branch,
                       'branches': branches, 'refBasis': 'own-branch-tip'}
                # An own branch can miss another [KEY] commit carried elsewhere.
                # Both its tip and the marker scanner's full set must be present.
                for env in ('dev', 'stg', 'prd'):
                    exists = self.git(repo, 'rev-parse', '--verify', '-q', 'origin/' + BRANCH[env])
                    if exists.returncode == 1:
                        row[env] = False
                        continue
                    if exists.returncode:
                        raise RuntimeError('배포 ref 식별 실패')
                    result = self.git(repo, 'merge-base', '--is-ancestor', sha, 'origin/' + BRANCH[env])
                    if result.returncode not in (0, 1):
                        raise RuntimeError('티켓 브랜치 반영 조회 실패')
                    row[env] = result.returncode == 0 and (not old or old.get(env) is True)
                candidates = set(tags.stdout.splitlines()) if row['prd'] else set()
                if old:
                    candidates.intersection_update(old.get('prd_tags', []))
                production_tags = []
                for tag in sorted(candidates):
                    contains = self.git(repo, 'merge-base', '--is-ancestor', 'refs/tags/' + tag, 'origin/' + BRANCH['prd'])
                    if contains.returncode not in (0, 1):
                        raise RuntimeError('운영 태그 이력 조회 실패')
                    if contains.returncode == 0:
                        production_tags.append(tag)
                row['prd_tags'] = production_tags
                row['prd_state'] = 'not-in-prd' if not row['prd'] else 'tagged' if production_tags else 'untagged'
                by_repo[repo.name] = row
                break
        value['repos'] = list(by_repo.values())
        dump(self.out / (key + '-own-refs.json'), [r for r in value['repos'] if r.get('refBasis') == 'own-branch-tip'])
        return value

    def collect(self, now):
        state = load(self.base / 'state.json', {})
        dump(self.out / 'state-before.json', state)
        since = state.get('lastRunAt') or previous_business_day(now)
        since_dt = iso(since)
        search = self.acli(['search', '--jql', INFLIGHT_JQL + ' ORDER BY updated DESC', '--json', '--limit', '200'])
        if not isinstance(search, list) or len(search) >= 200:
            raise RuntimeError('검색 결과 불완전: 200건 제한 또는 형식 오류')
        entries = [row for row in search if isinstance(row, dict) and row.get('key')]
        accounts = {(row.get('fields', {}).get('assignee') or {}).get('accountId') for row in entries}
        if entries and (None in accounts or len(accounts) != 1):
            raise RuntimeError('검색 담당자 식별 불가')
        me = next(iter(accounts)) if accounts else ME
        dump(self.out / 'search.json', search)
        repos, excluded, fetches = [], [], []
        for repo in sorted(self.root.glob(cfg.REPO_GLOB)):
            if not (repo / '.git').exists():
                continue
            if self.git(repo, 'remote', 'get-url', 'origin').returncode:
                excluded.append(repo.name)
                continue
            repos.append(repo)
            success = self.fetch(repo)
            fetches.append({'repo': repo.name, 'path': str(repo), 'success': success})
        data = {'startedAt': now.isoformat(), 'since': since, 'me': me, 'searchCount': len(entries),
                'repoFetch': fetches, 'excludedNoOrigin': excluded, 'issues': []}
        histories = []
        for entry in entries:
            key = entry['key']
            try:
                raw = self.acli(['view', key, '--fields', 'summary,status,issuetype,parent,subtasks,updated,comment', '--json'])
                dump(self.out / (key + '-view.json'), raw)
                fields = raw['fields']
                comments = fields.get('comment') or {}
                if isinstance(comments, dict):
                    rows = comments.get('comments', [])
                    if comments.get('total', len(rows)) > len(rows):
                        rows = self.page(key, 'comment')
                else:
                    rows = comments
                rows = sorted(rows, key=lambda row: iso(row['created']))
                dump(self.out / (key + '-comments.json'), rows)
                fresh = [{'id': c['id'], 'created': c['created'], 'author': (c.get('author') or {}).get('displayName'),
                          'text': plain(c.get('body')).strip(), 'judgment': 'pending',
                          'answeredByMeAfter': any((n.get('author') or {}).get('accountId') == me and iso(n['created']) > iso(c['created']) for n in rows)}
                         for c in rows if iso(c['created']) > since_dt and (c.get('author') or {}).get('accountId') != me]
                changes = []
                history_error = None
                try:
                    for h in self.page(key, 'changelog'):
                        if iso(h['created']) <= since_dt:
                            continue
                        for item in h.get('items', []):
                            if item.get('field') in ('status', 'assignee'):
                                changes.append({'field': item['field'], 'at': h['created'], 'by': (h.get('author') or {}).get('displayName'),
                                                'byMe': (h.get('author') or {}).get('accountId') == me,
                                                'from': item.get('fromString'), 'to': item.get('toString'),
                                                'toId': item.get('to')})
                except Exception as error:
                    history_error = type(error).__name__
                    self.error('history', key, '이력 조회 실패: ' + history_error)
                histories.append({'key': key, 'changes': changes, **({'error': history_error} if history_error else {})})
                refs = self.inspect_refs(key, repos)
                dump(self.out / (key + '-refs.json'), refs)
                prior = state.get('issues', {}).get(key)
                data['issues'].append({'key': key, 'summary': fields['summary'], 'status': fields['status']['name'],
                    'updated': fields['updated'], 'previousStatus': prior.get('status') if prior else None,
                    'newlyObserved': prior is None, 'newlyAssigned': any(c['field'] == 'assignee' and c['toId'] == me for c in changes),
                    'history': changes, 'historyError': history_error, 'freshComments': fresh,
                    'idleBusinessDays': business_days(fields['updated'], now),
                    'subtasks': [{'key': s['key'], 'status': (s.get('fields', {}).get('status') or {}).get('name')} for s in fields.get('subtasks', [])],
                    'refs': refs})
            except Exception as error:
                reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                self.error('issue', key, reason)
                data['issues'].append({'key': key, 'error': reason})
        targeted = {row['repo'] for issue in data['issues'] for row in issue.get('refs', {}).get('repos', [])}
        refs_complete = all('refs' in issue for issue in data['issues'])
        for fetch in fetches:
            fetch.update(status='PASS' if fetch['success'] else 'FAIL', reason='' if fetch['success'] else 'fetch 실패(낡은 ref)')
            if fetch['success']:
                continue
            if (refs_complete and fetch['repo'] not in targeted
                    and 'Could not resolve host' in self.fetch_errors.get(fetch['path'], '')):
                fetch.update(status='WARN', reason='remote-unreachable')
            else:
                self.error('git fetch', fetch['repo'], fetch['reason'])
        dump(self.out / 'fetch.json', {'repos': fetches, 'excludedNoOrigin': excluded})
        dump(self.out / 'histories.json', histories)
        dump(self.out / 'collection.json', data)
        return data

    def fresh_refs(self, issue, data):
        """A failed fetch is retried; equality checks are the only fallback."""
        for row in issue['refs'].get('repos', []):
            fetch = next((f for f in data['repoFetch'] if f['repo'] == row['repo']), None)
            if not fetch:
                return False
            if fetch['success']:
                continue
            repo = Path(fetch['path'])
            if self.fetch(repo):
                fetch['success'] = True
                # The caller must recompute deployment against the newly fetched refs.
                continue
            names = {b['branch'] for b in row.get('branches', [])}
            if row.get('branch'):
                names.add(row['branch'])
            if not names:
                return False
            names |= {BRANCH['dev'], BRANCH['stg']}
            remote = self.git(repo, 'ls-remote', 'origin', *['refs/heads/' + n for n in sorted(names)], 'refs/tags/v*')
            if remote.returncode:
                return False
            remote_refs = dict(line.split()[::-1] for line in remote.stdout.splitlines() if len(line.split()) == 2)
            for name in names:
                local = self.git(repo, 'rev-parse', '--verify', 'origin/' + name)
                if local.returncode or remote_refs.get('refs/heads/' + name) != local.stdout.strip():
                    return False
            tags = self.git(repo, 'show-ref', '--tags')
            if tags.returncode not in (0, 1):
                return False
            local_tags = {ref: sha for sha, ref in (line.split() for line in tags.stdout.splitlines()) if ref.startswith('refs/tags/v')}
            remote_tags = {ref: sha for ref, sha in remote_refs.items() if ref.startswith('refs/tags/v') and not ref.endswith('^{}')}
            if local_tags != remote_tags:
                return False
        return True

    def plan(self, data):
        result = []
        repos = [Path(row['path']) for row in data['repoFetch']]
        for issue in data['issues']:
            if 'error' in issue:
                continue
            refs = issue['refs'].get('repos', [])
            deployed = {env: bool(refs) and all(bool(row.get('prd_tags')) if env == 'prd' else row.get(env) is True for row in refs) for env in ('dev', 'stg', 'prd')}
            target = next((status for env, status in TARGETS if deployed[env]), None)
            row = {'key': issue['key'], 'before': issue['status'], 'after': issue['status'], 'target': target, 'steps': [], 'reason': '현재보다 뒤의 반영 목표 없음'}
            if not refs:
                row['reason'] = '브랜치 없음: 자동 이동 근거 없음'
            elif issue['status'] not in ORDER:
                row['reason'] = '자동 이동 대상 상태 밖: ' + issue['status']
            elif target and ORDER.index(target) > ORDER.index(issue['status']):
                if not self.fresh_refs(issue, data):
                    row['reason'] = '낡은 ref로 이동 보류'
                    self.error('refs', issue['key'], row['reason'])
                else:
                    # A successful retry can change the target, never reuse the old plan.
                    issue['refs'] = self.inspect_refs(issue['key'], repos)
                    refs = issue['refs'].get('repos', [])
                    deployed = {env: bool(refs) and all(bool(r.get('prd_tags')) if env == 'prd' else r.get(env) is True for r in refs) for env in ('dev', 'stg', 'prd')}
                    target = next((status for env, status in TARGETS if deployed[env]), None)
                    row['target'] = target
                    if target and ORDER.index(target) > ORDER.index(issue['status']):
                        row['reason'] = 'candidate'
            issue['deployment'] = deployed
            result.append(row)
        dump(self.out / 'transition-plan.json', result)
        return result

    def transitions(self, data, plan, execute=False):
        for row in plan:
            if row['reason'] != 'candidate':
                continue
            key, target = row['key'], row['target']
            try:
                raw = self.acli(['view', key, '--fields', 'status,updated', '--json'])
                dump(self.out / (key + '-pretransition.json'), raw)
                current = raw['fields']['status']['name']
                row['before'] = row['after'] = current
                issue = next(i for i in data['issues'] if i['key'] == key)
                issue.update(status=current, updated=raw['fields']['updated'])
                if current not in ORDER or ORDER.index(current) >= ORDER.index(target):
                    row['reason'] = '최신 상태에서 앞으로 이동할 목표 없음'
                    continue
                while ORDER.index(row['after']) < ORDER.index(target):
                    n = len(row['steps']) + 1
                    options = self.rest(f'issue/{key}/transitions')
                    dump(self.out / (key + f'-transitions-{n}.json'), options)
                    valid = [v['to']['name'] for v in options.get('transitions', []) if v.get('to', {}).get('name') in ORDER and ORDER.index(row['after']) < ORDER.index(v['to']['name']) <= ORDER.index(target)]
                    if not valid:
                        row['reason'] = '목표까지 가능한 앞 단계 전이 없음'
                        self.error('workflow', key, row['reason'])
                        break
                    chosen = min(valid, key=ORDER.index)
                    if not execute:
                        row['proposedNext'] = chosen
                        row['reason'] = 'DRY-RUN: 다음 한 단계만 확인, 이후 전이는 실행 후 재조회'
                        break
                    write = self.runner(['acli', 'jira', 'workitem', 'transition', '--key', key, '--status', chosen, '--yes'], timeout=150)
                    step = {'requested': chosen, 'writeRc': write.returncode, 'readbackRc': None, 'readback': None, 'result': 'FAIL'}
                    row['steps'].append(step)
                    try:
                        raw = self.acli(['view', key, '--fields', 'status,updated', '--json'])
                        dump(self.out / (key + f'-readback-{n}.json'), raw)
                        fields = raw['fields']
                        after = fields['status']['name']
                        step['readbackRc'] = 0
                        step['readback'] = after
                        row['after'] = after
                        issue = next(i for i in data['issues'] if i['key'] == key)
                        issue.update(status=after, updated=fields['updated'])
                        step['result'] = 'PASS' if write.returncode == 0 and after == chosen else 'FAIL'
                    except Exception:
                        row['stateUncertain'] = True
                        raise RuntimeError('FAIL: 상태 되읽기 실패')
                    dump(self.out / 'transition-results.json', plan)
                    if step['result'] != 'PASS':
                        raise RuntimeError('FAIL: 상태 이동 실패 또는 되읽기 불일치')
                    row['reason'] = 'PASS: 상태 되읽기 확인'
            except Exception as error:
                row['reason'] = str(error) if isinstance(error, RuntimeError) else 'FAIL: ' + type(error).__name__
                self.error('transition', key, row['reason'])
            dump(self.out / 'transition-results.json', plan)
        dump(self.out / 'transition-results.json', plan)
        return plan

    def report(self, data, plan, judgments):
        pending, drafts, actions = [], [], {}
        for issue in data['issues']:
            key = issue['key']
            notes = []
            if issue.get('error'):
                notes.append('조회 실패')
            for c in issue.get('freshComments', []):
                judgment = judgments.get('comments', {}).get(str(c['id']))
                if judgment:
                    if judgment.get('key') != key or judgment.get('classification') not in CLASSES:
                        raise ValueError('댓글 판정의 키 또는 분류 오류: ' + str(c['id']))
                    c['judgment'] = judgment['classification']
                    if c['judgment'] != '정보 공유' and not c['answeredByMeAfter']:
                        notes.append('댓글 ' + str(c['id']) + ' ' + c['judgment'])
                    if judgment.get('draft'):
                        drafts.append((key, judgment.get('summary', c['text']), judgment['draft']))
                else:
                    pending.append(str(c['id']))
                    notes.append('댓글 ' + str(c['id']) + ' 모델 판정 필요')
            if issue.get('idleBusinessDays', 0) > 7:
                notes.append(str(issue['idleBusinessDays']) + ' 업무일 미갱신')
            if issue.get('newlyAssigned'):
                notes.append('신규 배정 이력 확인')
            if any(h['field'] == 'status' and not h['byMe'] for h in issue.get('history', [])):
                notes.append('타인 상태 변경 이력 확인')
            row = next((r for r in plan if r['key'] == key), {})
            if row.get('reason') in ('낡은 ref로 이동 보류', '목표까지 가능한 앞 단계 전이 없음') or 'FAIL' in row.get('reason', ''):
                notes.append(row['reason'])
            supplied = judgments.get('issues', {}).get(key, {})
            if supplied.get('action'):
                notes.append(supplied['action'])
            actions[key] = ' / '.join(notes) or '없음'
        moves = sum(s['result'] == 'PASS' for r in plan for s in r['steps'])
        needs = sum(value != '없음' for value in actions.values())
        date = iso(data['startedAt']).astimezone(TZ).date().isoformat()
        lines = [f'# Jira 아침 점검 — {date}', '', '| 항목 | 내용 |', '|---|---|',
                 '| 오늘 먼저 할 일 | ' + cell(', '.join(k for k, v in actions.items() if v != '없음') or '추가 조치 없음') + ' |',
                 f'| 조치 필요 수 | {needs}건 |', f'| 자동 상태 이동 수 | {moves}회 |',
                 f'| BLOCKED·오류 | {len(self.errors)}건 / 댓글 모델 판정 대기 {len(pending)}건 |', '',
                 '확인 구간: ' + data['since'] + ' 이후. 새로 관찰한 티켓과 신규 배정 이력을 구분했다.',
                 '배포 O/X는 Git ref 포함 여부이며 실제 서버 배포 검증을 뜻하지 않는다. 업무일은 주말만 제외한다.', '',
                 '## 티켓', '', '| 티켓 | 제목(짧게) | 상태(변화) | 새 댓글 | 배포 반영 dev/stg/prd | 필요한 조치 |', '|---|---|---|---|---|---|']
        for issue in sorted(data['issues'], key=lambda i: (actions[i['key']] == '없음', i['key'])):
            key = issue['key']
            if issue.get('error'):
                lines.append(f'| {key} | 조회 실패 | unknown | unknown | unknown | 조회 실패 |')
                continue
            prior, current = issue['previousStatus'], issue['status']
            status = (prior + ' → ' + current) if prior and prior != current else current
            if issue['newlyObserved']:
                status += ' (지난 목록 밖)'
            comments = ', '.join(str(c['id']) + ': ' + c['judgment'] + (' (이후 본인 답변 있음)' if c['answeredByMeAfter'] else '') for c in issue['freshComments']) or '없음'
            dep = '/'.join('O' if issue.get('deployment', {}).get(env) else 'X' for env in ('dev', 'stg', 'prd')) if issue['refs'].get('repos') else '브랜치 없음'
            if issue['refs'].get('status') == 'carried':
                dep += ' (다른 티켓 브랜치에 포함)'
            lines.append('| ' + ' | '.join(map(cell, [key, issue['summary'], status, comments, dep, actions[key]])) + ' |')
        lines += ['', '## 이력·하위 작업', '']
        for issue in data['issues']:
            if issue.get('error'):
                continue
            changes = '; '.join(f"{h['at']} {h['by'] or 'unknown'} {h['field']} {h['from']} → {h['to']}" for h in issue['history']) or '변경 이력 없음'
            if issue.get('historyError'):
                changes = '이력 unknown: 조회 실패'
            subtasks = ', '.join(s['key'] + ':' + str(s['status'] or 'unknown') for s in issue['subtasks']) or '없음'
            lines.append(cell(issue['key'] + ': ' + changes + '. 하위 작업: ' + subtasks) + '  ')
        lines += ['', '## 댓글 답변 초안', '']
        if drafts:
            lines += ['| 티켓 | 원 댓글 요지 | 답변 초안 |', '|---|---|---|']
            lines += ['| ' + ' | '.join(map(cell, row)) + ' |' for row in drafts]
        else:
            lines.append('없음' if not pending else '댓글 모델 판정 대기: ' + ', '.join(pending) + '. 답변 초안 미작성.')
        lines += ['', '## 상태 이동', '', '| 티켓 | 이전 | 이후 | 되읽기 확인 또는 멈춘 이유 |', '|---|---|---|---|']
        lines += ['| ' + ' | '.join(map(cell, [r['key'], r['before'], r['after'], r['reason']])) + ' |' for r in plan]
        lines += ['', '## 저장소 fetch', '', '| 저장소 | 결과 | 내용 |', '|---|---|---|']
        lines += ['| ' + ' | '.join(map(cell, [f['repo'], f.get('status', 'PASS' if f['success'] else 'FAIL'), f.get('reason', '')])) + ' |'
                  for f in data['repoFetch']]
        lines += ['', '## 오류', '']
        if self.errors:
            lines += ['| 구분 | 대상 | 내용 |', '|---|---|---|']
            lines += ['| ' + ' | '.join(map(cell, [e['scope'], e['key'], e['error']])) + ' |' for e in self.errors]
        else:
            lines.append('없음')
        lines += ['', '수집 시작: ' + data['startedAt'], '근거 디렉터리: ' + str(self.out), '댓글 게시·§7 follow-up은 수행하지 않는다.']
        report = '\n'.join(lines) + '\n'
        (self.out / 'report.md').write_text(report)
        dump(self.out / 'collection.json', data)
        dump(self.out / 'errors.json', self.errors)
        return report, moves, needs, pending

    def save(self, data, report, date):
        """잠금 안에서 최신 상태의 아침 점검 필드만 갱신한다."""
        self.base.mkdir(parents=True, exist_ok=True)
        report_path = self.base / (date + '.md')
        report_path.write_text(report)
        path = self.base / 'state.json'
        preserved = {}

        def update(state):
            dump(self.out / 'state-before-save.json', state)
            preserved.update({k: v for k, v in state.items() if k not in ('lastRunAt', 'issues')})
            state['lastRunAt'] = data['startedAt']
            state['issues'] = {i['key']: {'status': i['status'], 'updated': i['updated']} for i in data['issues']}

        state = transaction(self.base, 'state.json', update)
        readback = load(path, {})
        # 다른 작성자가 뒤이어 followup을 갱신해도 아침 필드의 되읽기는 유효하다.
        own_fields_match = all(readback.get(k) == state.get(k) for k in ('lastRunAt', 'issues'))
        check = {'result': 'PASS' if own_fields_match else 'FAIL', 'reportExists': report_path.exists(),
                 'issueCount': len(readback.get('issues', {})),
                 'noJiraLinks': cfg.JIRA_SITE not in report_path.read_text(),
                 'followupPreserved': state.get('followup') == preserved.get('followup'),
                 'otherKeysPreserved': {k: v for k, v in state.items() if k not in ('lastRunAt', 'issues')} == preserved,
                 'lastRunAtMatches': readback.get('lastRunAt') == data['startedAt']}
        dump(self.out / 'check-morning-save.json', check)
        if not all(value for key, value in check.items() if key not in ('result', 'issueCount')) or check['result'] != 'PASS':
            raise RuntimeError('상태 저장 되읽기 실패')
        return report_path

    def sync_note(self, date):
        """Optional: push the morning report to the developer-note board (dev_note_sync.py)."""
        try:
            if self.note:
                result = self.note(date)
            else:
                if not cfg.get('DEV_NOTE_CLI'):
                    return 'devnote=skipped not-configured'
                result = self.runner([sys.executable, str(Path(__file__).resolve().parent / 'dev_note_sync.py'),
                                      '--dir', str(self.base), date], timeout=300)
        except Exception as error:
            return 'devnote=BLOCKED dev_note_sync 실패: ' + type(error).__name__
        lines = (result.stdout or '').strip().splitlines()
        last = lines[-1] if lines else ''
        if last.startswith('devnote=BLOCKED '):
            return last
        if result.returncode == 0 and last.startswith('devnote=ok '):
            return last
        return 'devnote=BLOCKED dev_note_sync 실패 rc=' + str(result.returncode)


def main(argv=None, *, runner=command, rest=jira_get, refs=None, note=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='Jira 전이·보고서·state·개발자 노트·done-today 기록')
    mode.add_argument('--dry-run', action='store_true', help='기본값: 증적·보고서 초안만 저장')
    parser.add_argument('--base', type=Path, default=BASE)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--judgments', type=Path, help='JSON: comments[id]={key,classification,summary?,draft?}, issues[key]={action?}')
    args = parser.parse_args(argv)
    started = now or datetime.now(TZ)
    date = started.astimezone(TZ).date().isoformat()
    out = args.out_dir or args.base / 'runs' / ('morning-' + started.strftime('%Y%m%d-%H%M%S-%f'))
    morning = Morning(args.base, args.root, out, runner=runner, rest=rest, refs=refs, note=note)
    report_path, moves, needs, line = out / 'report.md', 0, 0, 'devnote=skipped dry-run'
    try:
        judgments = load(args.judgments, {}) if args.judgments else {}
        data = morning.collect(started)
        plan = morning.plan(data)
        # Validate supplied model judgments before any external write.
        morning.report(data, plan, judgments)
        morning.transitions(data, plan, args.execute)
        report, moves, needs, pending = morning.report(data, plan, judgments)
        blockers = [e for e in morning.errors if e['scope'] in ('issue', 'history', 'transition', 'refs')]
        if args.execute:
            if blockers or pending:
                line = 'devnote=skipped BLOCKED: 수집·되읽기 실패 또는 댓글 모델 판정 대기, state/done-today 미기록'
            else:
                report_path = morning.save(data, report, date)
                line = morning.sync_note(date)
                (args.base / 'done-today').write_text(date + '\n')
                if (args.base / 'done-today').read_text().strip() != date:
                    raise RuntimeError('done-today 되읽기 실패')
        summary = {'report': str(report_path), 'transitions': moves, 'needs_action': needs, 'issues': len(data['issues']),
                   'errors': len(morning.errors), 'judgmentsPending': pending, 'execute': args.execute, 'devnote': line}
        dump(out / 'summary.json', summary)
        print(f'report={report_path} transitions={moves} needs_action={needs} {line}')
        return 1 if blockers or pending or line.startswith('devnote=BLOCKED') else 0
    except Exception as error:
        reason = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
        morning.error('morning', 'all', reason)
        dump(out / 'errors.json', morning.errors)
        if not report_path.exists():
            report_path.write_text('BLOCKED: ' + cell(reason) + '\n')
        print(f'report={report_path} transitions={moves} needs_action={needs} devnote=skipped BLOCKED: {reason}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
