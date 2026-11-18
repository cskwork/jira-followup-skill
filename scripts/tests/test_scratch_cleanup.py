import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scratch_cleanup  # noqa: E402


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class ScratchCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / 'ws'
        remote, repo = root / 'remote.git', root / 'repo'
        sh(root, 'git', 'init', '-q', '--bare', str(remote))
        sh(root, 'git', 'init', '-q', '-b', 'main', str(repo))
        for k, v in (('user.name', 't'), ('user.email', 't@t')):
            sh(repo, 'git', 'config', k, v)
        (repo / 'a').write_text('a')
        sh(repo, 'git', 'add', 'a')
        sh(repo, 'git', 'commit', '-q', '-m', 'init')
        sh(repo, 'git', 'remote', 'add', 'origin', str(remote))
        sh(repo, 'git', 'push', '-q', 'origin', 'main')
        sh(repo, 'git', 'fetch', '-q', 'origin')
        self.repo = repo
        self.paths = {}
        for name, slug in (('clean', 'PROJ-1-done'), ('dirty', 'PROJ-1-done'), ('untracked', 'PROJ-1-done'),
                           ('unpushed', 'PROJ-1-done'), ('open', 'PROJ-2-wip'), ('nokey', 'release-x')):
            path = self.ws / '.scratch' / slug / name
            path.parent.mkdir(parents=True, exist_ok=True)
            sh(repo, 'git', 'worktree', 'add', '-q', '--detach', str(path), 'origin/main')
            self.paths[name] = path
        (self.paths['dirty'] / 'a').write_text('changed')
        (self.paths['untracked'] / 'new').write_text('new')
        (self.paths['unpushed'] / 'a').write_text('local')
        sh(self.paths['unpushed'], 'git', 'commit', '-q', '-am', 'local')
        (self.ws / '.scratch' / 'PROJ-1-done' / 'PLAN.md').write_text('PROJ-1 계획')
        clone = self.ws / '.scratch' / 'PROJ-1-done' / 'clone'
        sh(root, 'git', 'clone', '-q', str(remote), str(clone))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cleanup(self, *argv):
        def search(args):
            return [{'key': 'PROJ-1', 'fields': {'status': {'name': '운영 검수 완료'}}},
                    {'key': 'PROJ-2', 'fields': {'status': {'name': '개발 QA 요청'}}}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = scratch_cleanup.main(['--workspace', str(self.ws), *argv], search=search)
        text = buf.getvalue()
        return code, json.loads(text[:text.rindex('\n', 0, len(text) - 1)]), text.splitlines()[-1]

    def test_dry_run_selects_only_clean_pushed_worktrees_of_released_tickets(self):
        code, out, last = self.run_cleanup()
        self.assertEqual(code, 0)
        self.assertEqual(out['removable'], [str(self.paths['clean'])])
        reasons = {Path(k['path']).name: k['reason'] for k in out['kept']}
        self.assertEqual(reasons, {'clone': 'clone', 'dirty': 'dirty', 'untracked': 'dirty',
                                   'unpushed': 'unpushed', 'open': 'open:PROJ-2=개발 QA 요청', 'nokey': 'no-key'})
        self.assertTrue(self.paths['clean'].exists())
        self.assertIn('mode=dry-run removable=1 removed=0', last)

    def test_execute_removes_worktree_and_keeps_slug_documents(self):
        code, out, _ = self.run_cleanup('--execute')
        self.assertEqual((code, out['removed'], out['failed']), (0, [str(self.paths['clean'])], []))
        self.assertFalse(self.paths['clean'].exists())
        self.assertTrue((self.ws / '.scratch' / 'PROJ-1-done' / 'PLAN.md').exists())
        for name in ('dirty', 'untracked', 'unpushed', 'open', 'nokey'):
            self.assertTrue(self.paths[name].exists(), name)
        listed = subprocess.run(['git', '-C', str(self.repo), 'worktree', 'list'], capture_output=True, text=True).stdout
        self.assertNotIn(str(self.paths['clean']), listed)

    def test_result_mode_removes_only_listed_pushed_worktrees_and_promoted_merge_branch(self):
        # 2026-11-02 PROJ-1469·1470: 작업자가 정리 명령의 확인 질문에 멈췄다. 작업자는 대상만 적고 이 명령이 지운다.
        sh(self.repo, 'git', 'push', '-q', 'origin', 'main:dev')
        sh(self.repo, 'git', 'fetch', '-q', 'origin')
        wt = self.ws / 'worktrees'
        merged, unpushed, other = wt / 'PROJ-1469-web-dev', wt / 'PROJ-1469-local', wt / 'PROJ-1469-main'
        sh(self.repo, 'git', 'worktree', 'add', '-q', '-b', 'merge/PROJ-1469-dev', str(merged), 'origin/dev')
        sh(merged, 'git', 'commit', '-q', '--allow-empty', '-m', 'merge')
        sh(merged, 'git', 'push', '-q', 'origin', 'HEAD:dev')
        sh(self.repo, 'git', 'worktree', 'add', '-q', '-b', 'merge/PROJ-1469-local', str(unpushed), 'origin/main')
        sh(unpushed, 'git', 'commit', '-q', '--allow-empty', '-m', 'local only')
        sh(self.repo, 'git', 'worktree', 'add', '-q', '-b', 'merge/PROJ-1469-main', str(other), 'origin/main')
        sh(other, 'git', 'commit', '-q', '--allow-empty', '-m', 'fix only')
        sh(other, 'git', 'push', '-q', 'origin', 'HEAD:fix/PROJ-1469')
        result = self.ws / 'result.json'
        result.write_text(json.dumps({'artifacts': ['asis.json', 'cleanup:' + str(merged), 'cleanup:' + str(unpushed),
                                                    'cleanup:' + str(other), 'cleanup:' + str(self.repo)]}))
        code, out, last = self.run_cleanup('--execute', '--result', str(result))
        self.assertEqual(code, 0)
        self.assertEqual(out['removed'], [str(merged.resolve()), str(other.resolve())])
        self.assertEqual(out['branchesDeleted'], ['merge/PROJ-1469-dev'])
        reasons = {Path(k['path']).name: k['reason'] for k in out['kept']}
        self.assertEqual(reasons, {'PROJ-1469-local': 'unpushed', 'repo': 'outside-workspace'})
        branches = subprocess.run(['git', '-C', str(self.repo), 'branch', '--list', 'merge/*'],
                                  capture_output=True, text=True).stdout
        self.assertNotIn('merge/PROJ-1469-dev', branches)
        self.assertIn('merge/PROJ-1469-main', branches)  # 끝 커밋이 origin/<dev|stg|main>에 없으면 브랜치는 남긴다.
        self.assertTrue(self.paths['clean'].exists())  # --result는 .scratch 전체를 훑지 않는다.
        self.assertIn('mode=execute removable=2 removed=2', last)


class RunsReportTest(unittest.TestCase):
    def test_runs_report_lists_non_token_entries_and_root_dumps_without_deleting(self):
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / 'runs' / ('c' * 32)).mkdir(parents=True)
            (state / 'runs' / 'PROJ-1-before').mkdir()
            (state / 'runs' / 'PROJ-1-before' / 'log').write_bytes(b'x' * 10)
            (state / 'PROJ-1-current.json').write_text('{}')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(scratch_cleanup.main(['--runs', '--dir', str(state)]), 0)
            report = json.loads(out.getvalue().rsplit('\n', 2)[0])
            self.assertEqual(report['nonTokenRuns'], [{'name': 'PROJ-1-before', 'bytes': 10}])
            self.assertEqual(report['rootDumps']['files'], ['PROJ-1-current.json'])
            self.assertIn('mode=runs-report non_token=1', out.getvalue())
            self.assertTrue((state / 'runs' / 'PROJ-1-before' / 'log').exists())
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                scratch_cleanup.main(['--runs', '--execute', '--dir', str(state)])


if __name__ == '__main__':
    unittest.main()
