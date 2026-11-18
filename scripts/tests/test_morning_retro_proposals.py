"""아침 점검이 오래 남은 회고 제안 브랜치를 사람 결정 하나로 올린다(2026-11-10 회고: retro/* 3건이 4·11·18일째 미병합).
git·결정 화면은 가짜로 받아 적는다. 공장은 병합·삭제하지 않는다."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_page  # noqa: E402
import morning_check as m  # noqa: E402

NOW = datetime.fromisoformat('2026-11-10T08:00:00+09:00')
REFS = '\n'.join([
    'retro/20261023-1700\t2d239b2\t2026-10-23T17:09:52+09:00\t회고 제안 — 결과를 먼저 쓴다',
    'retro/20261030\t7647756\t2026-10-30T17:08:15+09:00\t회고 제안 — 범위 인용 허용',
    'retro/20261106-1700\tf26c637\t2026-11-06T17:16:11+09:00\t회고 제안 — 결과 댓글은 진행 중 이동 뒤'])


class RetroProposals(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / 'daily'
        self.repo = Path(temporary.name) / 'skills'
        self.calls = []
        self.refs = REFS
        self.morning = m.Morning(self.base, Path(temporary.name) / 'root', Path(temporary.name) / 'out',
                                 runner=self.runner, skills_repo=self.repo)
        self.opener = Mock(side_effect=self.open_decision)

    def runner(self, argv, timeout):
        self.calls.append(argv)
        self.assertEqual(argv[:3], ['git', '-C', str(self.repo)])
        if argv[3] == 'fetch':
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.assertEqual(argv[3:6], ['for-each-ref', '--no-merged', 'origin/main'])
        return subprocess.CompletedProcess(argv, 0, self.refs + '\n', '')

    def open_decision(self, base, path):
        spec, name = decision_page.load_spec('spec', str(path), base)
        return decision_page.register_code(base, spec, name, None, server=False)

    def test_lists_only_unmerged_branches_older_than_seven_days(self):
        found = self.morning.retro_branches(NOW)
        self.assertEqual([(r['branch'], r['days']) for r in found], [('retro/20261023-1700', 17), ('retro/20261030', 10)])
        # 읽기만 한다: fetch와 목록 조회뿐, 병합·삭제·push 없음.
        self.assertEqual([c[3] for c in self.calls], ['fetch', 'for-each-ref'])

    def test_opens_one_decision_for_the_batch_and_does_not_reopen_same_set(self):
        found = self.morning.retro_branches(NOW)
        self.assertEqual(self.morning.retro_decision(found, self.opener), 'opened')
        spec = json.loads(self.opener.call_args.args[1].read_text())
        self.assertEqual(spec['title'], '[회고 제안] 미병합 2건')
        self.assertEqual(spec['recommended'], 'merge')
        self.assertEqual([o['id'] for o in spec['options']], ['merge', 'drop', 'keep-waiting'])
        for part in ('retro/20261023-1700', '결과를 먼저 쓴다', '17일', 'retro/20261030', '10일'):
            self.assertIn(part, spec['summary'])
        self.assertNotIn('20261106', spec['summary'])
        # 같은 묶음은 결정이 열려 있든 정해졌든 다시 열지 않는다.
        self.assertEqual(self.morning.retro_decision(found, self.opener), 'already-asked')
        decision_page.update_code(self.base, 'D1', 'decided')
        self.assertEqual(self.morning.retro_decision(found, self.opener), 'already-asked')
        # 묶음이 바뀌면(새 브랜치) 새로 묻는다.
        self.assertEqual(self.morning.retro_decision(found[:1], self.opener), 'opened')
        self.assertEqual(self.opener.call_count, 2)

    def test_report_lists_stale_proposals_as_factory_action(self):
        found = self.morning.retro_branches(NOW)
        self.morning.retro = found
        self.assertIn('회고 제안 미병합 2건', self.morning.report(
            {'startedAt': NOW.isoformat(), 'since': NOW.isoformat(), 'issues': [], 'repoFetch': []}, [], {})[0])

    def test_main_opens_only_with_execute_and_fetch_failure_is_explicit(self):
        def run(execute, fetch_rc=0):
            calls = []

            def runner(argv, timeout):
                calls.append(argv)
                if argv[0] == 'git' and argv[2] == str(self.repo):
                    if argv[3] == 'fetch':
                        return subprocess.CompletedProcess(argv, fetch_rc, '', 'boom')
                    return subprocess.CompletedProcess(argv, 0, REFS, '')
                return subprocess.CompletedProcess(argv, 0, '[]', '')
            opener = Mock()
            out = Path(self.base.parent / ('out-main-' + str(execute) + str(fetch_rc)))
            args = ['--base', str(self.base), '--root', str(self.base.parent / 'root'), '--out-dir', str(out)]
            with contextlib.redirect_stdout(io.StringIO()):
                m.main(args + (['--execute'] if execute else []), runner=runner, rest=lambda p: {}, refs=None,
                       note=lambda d: subprocess.CompletedProcess([], 0, 'devnote=ok x\n', ''), now=NOW,
                       skills_repo=self.repo, retro_opener=opener)
            return opener, json.loads((out / 'errors.json').read_text()), (out / 'report.md').read_text()
        opener, errors, report = run(False)
        self.assertFalse(opener.called)
        self.assertIn('회고 제안 미병합 2건', report)
        opener, errors, _ = run(True)
        self.assertEqual(opener.call_count, 1)
        opener, errors, _ = run(True, fetch_rc=1)
        self.assertFalse(opener.called)
        self.assertEqual([e['scope'] for e in errors], ['retro'])

    def test_custom_base_without_repo_does_not_touch_real_factory_repo(self):
        morning = m.Morning(self.base, self.base, self.base / 'out', runner=lambda *a, **k: self.fail('git called'))
        self.assertEqual(morning.retro_branches(NOW), [])


if __name__ == '__main__':
    unittest.main()
