#!/usr/bin/env python3
"""운영 반영이 끝난 티켓의 `.scratch` worktree를 지운다. 기본은 dry-run이다.

Usage: scratch_cleanup.py [--workspace PATH] [--execute] [--result WORKER_RESULT_JSON]
- 대상: `.scratch/<slug>` 이름의 티켓 키(JIRA_PROJECT)가 모두 운영 완료 단계(`POST_RELEASE_STATUSES`)인 slug 안의 worktree(`.git` 파일).
- 지우지 않는 것: 미커밋·미추적 변경, 원격에 없는 커밋, 일반 클론(`.git` 폴더), 키 없는 slug, 진행 중 티켓.
- slug 폴더와 그 안의 문서는 남긴다(백로그 접수의 관련 작업 찾기가 읽는다).
- 지우기는 `git worktree remove`(force 없음)만 쓴다. 실패한 것은 남기고 보고한다.
배경(2026-10-29): `.scratch`가 157GB·worktree 177개로 커져 followup_scan이 570초를 넘겼다.
- `--result`: 작업자 결과 JSON의 `artifacts` 중 `cleanup:<절대경로>`로 적은 worktree만 본다(Jira 상태는 보지 않는다).
  workspace의 `worktrees`·`.scratch` 아래 worktree이고 미커밋·미push가 없을 때만 지운다. 그 worktree가 `merge/*` 브랜치였고
  끝 커밋이 `origin/<BRANCH_DEV|BRANCH_STG|BRANCH_MAIN>`에 있으면 그 브랜치도 지운다(승격용 merge/* 브랜치 삭제 조건).
  배경(2026-11-02): 작업자가 일을 다 끝낸 뒤 정리 명령의 확인 질문에 멈춰 결과를 못 내고 human-risk가 됐다.
  그래서 작업자는 정리하지 않고 대상만 적고, 오케스트레이터가 이 명령으로 지운다.
- `--runs [--dir <state_dir>]`: 상태 폴더 보고만 한다(지우지 않는다, `--execute`와 함께 쓰지 않는다). runs/ 아래 실행 토큰이
  아닌 항목(증거 폴더·덤프, 가드·잠금은 무시한다)과 상태 폴더 맨 위의 `<PROJECT>-*` 덤프 파일을 크기와 함께 낸다.
  배경(2026-11-16): runs/PROJ-1389-49723-before-v2 2.7GB, 10월 `PROJ-*.json` 덤프 66개. 지우기는 사람이 정한다.
출력: JSON {removed, kept:[{path, reason}], failed:[...]}. 마지막 줄 stdout: `scratch-cleanup mode=<dry-run|execute> removable=<n> removed=<n> kept=<n> failed=<n>`.
"""
import argparse, json, os, re, subprocess
from pathlib import Path

import followup_config as cfg
from followup_scan import POST_RELEASE_STATUSES, WORKSPACE, acli, statuses

KEY = cfg.TICKET_RE


def git(path, *args, timeout=60):
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, timeout=timeout)


def checkouts(slug):
    """slug 안의 저장소 체크아웃을 찾는다. 체크아웃 안으로는 내려가지 않는다."""
    for directory, dirs, files in os.walk(slug):
        if '.git' in files or '.git' in dirs:
            dirs[:] = []
            yield Path(directory), '.git' in files
        else:
            dirs[:] = [d for d in dirs if d != 'node_modules']


def keep_reason(path, worktree):
    if not worktree:
        return 'clone'
    status = git(path, 'status', '--porcelain')
    if status.returncode:
        return 'status-failed'
    if status.stdout.strip():
        return 'dirty'
    unpushed = git(path, 'log', '--oneline', 'HEAD', '--not', '--remotes')
    if unpushed.returncode:
        return 'log-failed'
    if unpushed.stdout.strip():
        return 'unpushed'
    return None


def remove(path):
    """(오류, 지운 merge/* 브랜치). 오류가 없으면 None."""
    common = git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if common.returncode:
        return 'common-dir-failed', None
    repo = Path(common.stdout.strip()).parent
    branch = git(path, 'symbolic-ref', '-q', '--short', 'HEAD').stdout.strip()
    tip = git(path, 'rev-parse', 'HEAD').stdout.strip()
    # build·bin이 크면 30초를 넘긴다(2026-10 실측).
    done = git(repo, 'worktree', 'remove', str(path), timeout=600)
    if done.returncode:
        return (done.stderr.strip().splitlines() or ['remove-failed'])[-1][:200], None
    if not (branch.startswith('merge/') and tip
            and git(repo, 'branch', '-r', '--contains', tip, '--list',
                    *('origin/' + name for name in (cfg.BRANCH_DEV, cfg.BRANCH_STG, cfg.BRANCH_MAIN))).stdout.strip()):
        return None, None
    deleted = git(repo, 'branch', '-D', branch)
    return (None if deleted.returncode == 0 else 'branch-delete-failed: ' + branch), branch


def result_targets(result, workspace):
    """작업자 결과의 cleanup:<경로>를 (경로, 남길 이유) 목록으로 바꾼다."""
    value = json.loads(Path(result).read_text())
    roots = [(workspace / name).resolve() for name in ('worktrees', '.scratch')]
    for artifact in value.get('artifacts') or []:
        if not (isinstance(artifact, str) and artifact.startswith('cleanup:')):
            continue
        path = Path(artifact[len('cleanup:'):]).expanduser().resolve()
        if not any(root in path.parents for root in roots):
            yield path, 'outside-workspace'
        elif not (path / '.git').is_file():
            yield path, 'not-worktree'
        else:
            yield path, keep_reason(path, True)


def size(path):
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    return sum(f.lstat().st_size for f in path.rglob('*') if f.is_file() or f.is_symlink())


def runs_report(directory):
    """상태 폴더의 비토큰 runs 항목과 맨 위 <PROJECT>-* 덤프. 읽기 전용."""
    import run_lock
    runs = [{'name': name, 'bytes': size(directory / 'runs' / name)} for name in run_lock.stray_runs(directory)]
    dumps = sorted(p for p in directory.glob(cfg.PROJECT + '-*') if p.is_file())
    return {'nonTokenRuns': runs,
            'rootDumps': {'count': len(dumps), 'bytes': sum(p.stat().st_size for p in dumps),
                          'files': [p.name for p in dumps]}}


def main(argv=None, *, search=acli):
    ap = argparse.ArgumentParser()
    ap.add_argument('--workspace', type=Path, default=WORKSPACE)
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--result', type=Path)
    ap.add_argument('--runs', action='store_true')
    ap.add_argument('--dir', type=Path, default=Path(os.environ.get('JIRA_WAKE_DIR') or cfg.HOME_DIR))
    a = ap.parse_args(argv)
    if a.runs:
        if a.execute or a.result:
            ap.error('--runs is report-only')
        report = runs_report(a.dir.expanduser())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print('scratch-cleanup mode=runs-report non_token=%d non_token_bytes=%d root_dumps=%d root_dump_bytes=%d' % (
            len(report['nonTokenRuns']), sum(r['bytes'] for r in report['nonTokenRuns']),
            report['rootDumps']['count'], report['rootDumps']['bytes']))
        return 0
    out = {'removable': [], 'removed': [], 'kept': [], 'failed': [], 'branchesDeleted': []}

    def handle(path, reason):
        if reason:
            out['kept'].append({'path': str(path), 'reason': reason})
            return
        out['removable'].append(str(path))
        if a.execute:
            error, branch = remove(path)
            if branch and not error:
                out['branchesDeleted'].append(branch)
            (out['failed'].append({'path': str(path), 'error': error}) if error
             else out['removed'].append(str(path)))

    root = a.workspace / '.scratch'
    slugs = []
    if a.result:
        for path, reason in result_targets(a.result, a.workspace):
            handle(path, reason)
    elif root.exists():
        slugs = sorted(p for p in root.iterdir() if p.is_dir())
    status = statuses({k for s in slugs for k in KEY.findall(s.name)}, search)
    for slug in slugs:
        keys = KEY.findall(slug.name)
        found = list(checkouts(slug))
        if not found:
            continue
        if not keys:
            slug_reason = 'no-key'
        elif not all(status.get(k) in POST_RELEASE_STATUSES for k in keys):
            slug_reason = 'open:' + ','.join(f'{k}={status.get(k)}' for k in keys)
        else:
            slug_reason = None
        for path, worktree in found:
            handle(path, slug_reason or keep_reason(path, worktree))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print('scratch-cleanup mode=%s removable=%d removed=%d kept=%d failed=%d' % (
        'execute' if a.execute else 'dry-run', len(out['removable']), len(out['removed']),
        len(out['kept']), len(out['failed'])))
    return 1 if out['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
