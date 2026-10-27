#!/usr/bin/env python3
"""운영 후속 티켓 분리: 운영 완료 티켓 댓글의 반영 이슈를 원 티켓과 연결된 새 티켓으로 옮긴다(RUNBOOK §7-2).

왜: 운영 완료 티켓에 반영 뒤 문제가 댓글로 올라오면 원 티켓을 다시 열지 않고, 원 티켓과 연결된 새 티켓에
이슈를 명확히 적어 다시 할 일로 보낸다. 범위는 내 범위만(내 담당 또는 나를 멘션한 댓글, 새 티켓 담당자는 나).

이 스크립트는 판단하지 않는다. 이슈 보고인지는 작업자가 정하고, 설명 md도 작업자가 쓴다.
순서: 가드(상태·작성자·직발행 예고·댓글 뒤 연결 이슈) → 설명 ADF·기획자용 검사 → 소유 확인 → 생성(즉시 기록)
→ Relates 링크 → '할 일' 전이 → 원 티켓 안내 댓글 → 되읽기 → handled 기록. 재실행하면 기록된 단계는 건너뛴다.

  spinoff_ticket.py --key PROJ-101 --comment-id 10001 --summary "[운영 후속] ... (PROJ-101)" \
      --description-md desc.md --token <run_token> [--dir $JIRA_FOLLOWUP_HOME] [--dry-run]

exit: 0 생성·되읽기 완료(또는 이미 완료) / 1 단계 실패 / 2 대상 아님(상태·댓글) / 3 작성자 직발행 예고
      / 4 댓글 이후 연결 이슈 있음 / 5 실행 소유권 없음
마지막 줄: `spinoff=<결과> orig=<KEY> ...`
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402
import state_update  # noqa: E402

BASE = cfg.HOME_DIR
HERE = Path(__file__).resolve().parent
FORMAT = cfg.COMMENT_FORMAT_DIR  # jira-comment-format 스킬의 scripts/
TZ = cfg.TZ
ME = cfg.ME  # followup_scan.ME와 같다
BROWSE = cfg.BROWSE
POST_RELEASE_STATUSES = cfg.POST_RELEASE_STATUSES  # followup_scan과 같다
TODO_STATUS = cfg.TODO_STATUS
ISSUE_TYPE = cfg.SPINOFF_ISSUE_TYPE
LINK_TYPE = 'Relates'
# 작성자가 직접 새 티켓을 만들겠다고 한 댓글(예: "1번은 별도 티켓으로 발행하겠습니다")
AUTHOR_WILL_CREATE = re.compile(r'(별도|따로|신규|새)\s*(의\s*)?(티켓|이슈|일감)\s*(으로|로)?\s*(발행|생성|등록|분리|만들)'
                                r'|티켓\s*(을\s*)?(분리|발행)\s*하')
# 주간 릴리즈·운영배포 관리 티켓은 배포된 티켓마다 연결되므로 후속 티켓으로 보지 않는다
# (dry-run에서 "Release_YYYY/MM/DD~MM/DD" 관리 티켓이 중복 후보로 잡혔다). 접두어는 RELEASE_TRACKER_RE로 바꾼다.
RELEASE_TRACKER = re.compile(cfg.get('RELEASE_TRACKER_RE', r'^\s*(Release_|관리자운영배포)'))
STEPS = ('created', 'linked', 'todo', 'commented')


class Fail(Exception):
    def __init__(self, code, line):
        super().__init__(line)
        self.code, self.line = code, line


def run(cmd, stdin=None, timeout=120):
    p = subprocess.run(cmd, text=True, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


class Jira:
    """acli·검사 도구 호출. 시험은 runner를 바꿔 끼운다."""

    def __init__(self, runner=run):
        self.runner = runner

    def acli(self, args, parse=True):
        rc, out, err = self.runner(['acli', 'jira', 'workitem'] + args, None)
        if rc:
            raise RuntimeError('acli %s rc=%d %s' % (args[0], rc, (err or out or '').strip()[:200]))
        return json.loads(out) if parse else out

    def view(self, key, fields):
        return self.acli(['view', key, '--fields', fields, '--json'])

    def tool(self, cmd, stdin=None):
        rc, out, err = self.runner(cmd, stdin)
        return rc, (out or '') + (err or '')


def plain(node):
    if isinstance(node, list):
        return ''.join(plain(n) for n in node)
    if not isinstance(node, dict):
        return ''
    if node.get('type') == 'text':
        return node.get('text', '')
    if node.get('type') == 'mention':
        return node.get('attrs', {}).get('text', '')
    return plain(node.get('content', [])) + ('\n' if node.get('type') in ('paragraph', 'listItem') else '')


def parse_time(value):
    return datetime.fromisoformat(re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value.replace('Z', '+00:00')))


def linked(fields):
    for link in fields.get('issuelinks') or []:
        other = link.get('outwardIssue') or link.get('inwardIssue') or {}
        if other.get('key'):
            yield link.get('type', {}).get('name'), other


def check_target(jira, key, comment_id):
    f = jira.view(key, 'summary,status,comment,issuelinks')['fields']
    status = (f.get('status') or {}).get('name')
    if status not in POST_RELEASE_STATUSES:
        raise Fail(2, f'spinoff=not-post-release orig={key} status={status}')
    comment = next((c for c in (f.get('comment') or {}).get('comments', []) if c.get('id') == comment_id), None)
    if comment is None or (comment.get('author') or {}).get('accountId') == ME:
        raise Fail(2, f'spinoff=bad-comment orig={key} comment={comment_id}')
    return f, comment


def guard_duplicates(jira, key, fields, comment):
    if AUTHOR_WILL_CREATE.search(plain(comment.get('body'))):
        raise Fail(3, f'spinoff=author-will-create orig={key} comment={comment["id"]}')
    after = parse_time(comment['created'])
    later = []
    for _, other in linked(fields):
        if RELEASE_TRACKER.match((other.get('fields') or {}).get('summary') or ''):
            continue
        created = jira.view(other['key'], 'created')['fields'].get('created')
        if created and parse_time(created) > after:
            later.append(other['key'])
    if later:
        raise Fail(4, f'spinoff=linked-after-comment orig={key} keys={",".join(sorted(later))}')


def description_md(key, fields, comment, body):
    author = (comment.get('author') or {}).get('displayName', '')
    at = parse_time(comment['created']).astimezone(TZ).strftime('%Y-%m-%d %H:%M')
    head = ['## 어디서 나온 이슈인가',
            f'- 원 티켓: [{key} {fields.get("summary", "")}]({BROWSE}{key})',
            f'- 보고: [{author} · {at}]({BROWSE}{key}?focusedCommentId={comment["id"]})',
            '- 원 티켓은 운영 반영이 끝난 상태라, 반영 뒤 나온 문제를 이 티켓에서 이어서 처리합니다.', '']
    return '\n'.join(head) + '\n' + body.strip() + '\n'


def notice(comment, new):
    """원 티켓 안내 댓글: 보고자를 멘션하고 새 티켓으로 안내한다."""
    author = comment.get('author') or {}
    return (f'@[{author.get("displayName", "")}]({author.get("accountId", "")}) 말씀하신 운영 반영 뒤 문제는 '
            f'[새 티켓]({BROWSE}{new})에서 이어서 확인합니다. 진행 상황은 그 티켓에 남기겠습니다.')


def to_adf(jira, md_text, workdir, name, max_chars=None):
    src, out = Path(workdir) / f'{name}.md', Path(workdir) / f'{name}.adf.json'
    src.write_text(md_text, encoding='utf-8')
    cmd = ['python3', str(FORMAT / 'md_to_adf.py'), str(src), '-o', str(out)]
    if max_chars is not None:
        cmd += ['--max-chars', str(max_chars)]
    rc, text = jira.tool(cmd)
    if rc or not out.exists():
        raise Fail(1, f'spinoff=adf-failed name={name} {text.strip()[:300]}')
    return out


def owns(jira, state_dir, token):
    rc, _ = jira.tool([str(HERE / 'jira-daily-guard'), '--dir', str(state_dir), '--owns', token])
    if rc:
        raise Fail(5, f'spinoff=not-owner token={token}')


def record(state_dir, comment_id, values):
    state_update.patch(state_dir, 'state.json', ['followup', 'spinoffs', comment_id], values)


def main(argv=None, *, jira=None, now=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--key', required=True)
    ap.add_argument('--comment-id', required=True)
    ap.add_argument('--summary', required=True)
    ap.add_argument('--description-md', required=True)
    ap.add_argument('--token', required=True)
    ap.add_argument('--dir', default=str(BASE))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    jira = jira or Jira()
    now = now or (lambda: datetime.now(TZ).isoformat())
    state_dir = Path(a.dir)
    try:
        line = spinoff(a, jira, state_dir, now)
        print(line)
        return 0
    except Fail as e:
        print(e.line)
        return e.code
    except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        # 기록된 단계는 남아 있으니 같은 명령을 다시 실행하면 이어서 한다
        print(f'spinoff=step-failed orig={a.key} error={str(e)[:200]!r}')
        return 1


def spinoff(a, jira, state_dir, now):
    key, cid = a.key, a.comment_id
    if '[보류]' in a.summary:
        raise Fail(1, 'spinoff=bad-summary reason=hold-mark')  # intake가 받지 않는다
    saved = ((state_update.read_object(state_dir / 'state.json').get('followup') or {})
             .get('spinoffs') or {}).get(cid) or {}
    fields, comment = check_target(jira, key, cid)
    if not saved.get('newKey'):
        guard_duplicates(jira, key, fields, comment)
    with tempfile.TemporaryDirectory() as work:
        desc = to_adf(jira, description_md(key, fields, comment, Path(a.description_md).read_text(encoding='utf-8')),
                      work, 'description', max_chars=0)
        if a.dry_run:
            return (f'spinoff=dry-run orig={key} comment={cid} summary="{a.summary}" '
                    f'resume={saved.get("newKey") or "-"} description={desc.name}')
        owns(jira, state_dir, a.token)
        steps = dict(saved.get('steps') or {})
        new = saved.get('newKey')
        if not new:
            created = jira.acli(['create', '--project', key.split('-')[0], '--type', ISSUE_TYPE,
                                 '--summary', a.summary, '--description-file', str(desc),
                                 '--assignee', '@me', '--json'])
            new = created.get('key') if isinstance(created, dict) else None
            if not new:
                raise Fail(1, f'spinoff=create-no-key orig={key}')
            steps['created'] = now()
            # 생성 직후 기록: 다음 단계가 실패해도 재실행이 같은 티켓을 이어 쓴다
            record(state_dir, cid, {'key': key, 'newKey': new, 'at': steps['created'], 'steps': steps})
        if 'linked' not in steps:
            jira.acli(['link', 'create', '--out', new, '--in', key, '--type', LINK_TYPE], parse=False)
            steps['linked'] = now()
            record(state_dir, cid, {'steps': steps})
        if 'todo' not in steps:
            status = jira.view(new, 'status')['fields']['status']['name']
            if status != TODO_STATUS:
                jira.acli(['transition', '--key', new, '--status', TODO_STATUS, '--yes'], parse=False)
            steps['todo'] = now()
            record(state_dir, cid, {'steps': steps})
        if 'commented' not in steps:
            body = to_adf(jira, notice(comment, new), work, 'comment')
            jira.acli(['comment', 'create', '--key', key, '--body-file', str(body)], parse=False)
            steps['commented'] = now()
            record(state_dir, cid, {'steps': steps})
    resumed = 'exists' if len(saved.get('steps') or {}) == len(STEPS) else 'resumed'
    return readback(jira, state_dir, key, cid, new, steps, now, resumed if saved.get('newKey') else 'created')


def readback(jira, state_dir, key, cid, new, steps, now, result):
    nf = jira.view(new, 'status,assignee,issuelinks')['fields']
    status = (nf.get('status') or {}).get('name')
    problems = []
    if status != TODO_STATUS:
        problems.append(f'status={status}')
    if (nf.get('assignee') or {}).get('accountId') != ME:
        problems.append('assignee')
    if not any(other['key'] == key and kind == LINK_TYPE for kind, other in linked(nf)):
        problems.append('link')
    of = jira.view(key, 'comment')['fields']
    mine = [c for c in (of.get('comment') or {}).get('comments', [])
            if (c.get('author') or {}).get('accountId') == ME and new in json.dumps(c.get('body'), ensure_ascii=False)]
    if not mine:
        problems.append('comment')
    else:
        rc, _ = jira.tool(['python3', str(FORMAT / 'check_adf.py'), '--comment', mine[-1]['id'], '--min-mention', '1'],
                          stdin=json.dumps({'fields': of}, ensure_ascii=False))
        if rc:
            problems.append('comment-check')
    if problems:
        # 되읽기가 어긋난 단계는 기록에서 빼서 재실행이 다시 시도하게 한다
        redo = {'status': 'todo', 'link': 'linked', 'comment': 'commented'}
        for p in problems:
            steps.pop(redo.get(p.split('=')[0], ''), None)
        record(state_dir, cid, {'steps': steps})
        raise Fail(1, f'spinoff=readback-failed orig={key} new={new} problems={",".join(problems)}')
    comment_id = mine[-1]['id']
    state_update.patch(state_dir, 'state.json', ['followup', 'handled', cid],
                       {'key': key, 'action': 'spinoff', 'newKey': new, 'at': now(), 'slug': None,
                        'note': f'운영 후속 티켓 {new} 분리(할 일), 원 티켓 안내 댓글 {comment_id}'})
    return f'spinoff={result} orig={key} new={new} status={TODO_STATUS} link=ok comment={comment_id}'


if __name__ == '__main__':
    sys.exit(main())
