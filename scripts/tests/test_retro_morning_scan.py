"""회고 5b/5c 회귀: fetch 등급과 이슈 조회 재시도, 외부 호출 없음."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import followup_scan as scan
import morning_check as morning

KEY = 'PROJ-1349'
NOW = datetime.fromisoformat('2026-10-23T08:00:00+09:00')
DNS = "fatal: unable to access 'https://example.invalid/repo/': Could not resolve host: example.invalid"


class MorningFetchRegressionTests(unittest.TestCase):
    def collect(self, diagnostic, *, targeted=False, refs_fail=False, stdout=False, empty=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root, out = base / 'root', base / 'out'
        repo = root / 'ops-dashboard'
        (repo / '.git').mkdir(parents=True)

        def runner(argv, timeout):
            if argv[0] == 'git':
                if argv[3] == 'remote':
                    return subprocess.CompletedProcess(argv, 0, 'https://example.invalid/repo', '')
                self.assertEqual(argv[3], 'fetch')
                return subprocess.CompletedProcess(argv, 128, diagnostic if stdout else '', '' if stdout else diagnostic)
            args = argv[3:]
            if args[0] == 'search':
                value = [] if empty else [{'key': KEY, 'fields': {'assignee': {'accountId': scan.ME}}}]
            else:
                self.assertEqual(args[:2], ['view', KEY])
                value = {'fields': {'summary': '조회 재시도', 'status': {'name': morning.ORDER[0]},
                                    'updated': NOW.isoformat(), 'comment': {'comments': []}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), '')

        def refs(key, repos):
            if refs_fail:
                raise RuntimeError('ticket_refs 조회 실패')
            return {'key': key, 'status': 'none', 'repos': [{'repo': repo.name, 'dev': False}] if targeted else []}

        instance = morning.Morning(base, root, out, runner=runner,
                                   rest=lambda path: {'values': [], 'total': 0}, refs=refs)
        data = instance.collect(NOW)
        report, *_ = instance.report(data, instance.plan(data), {})
        return instance, data['repoFetch'][0], report, json.loads((out / 'fetch.json').read_text())

    def test_dns_without_target_refs_warns_and_remains_in_report(self):
        instance, fetch, report, evidence = self.collect(DNS)
        self.assertFalse(fetch['success'])
        self.assertEqual(fetch['status'], 'WARN')
        self.assertEqual(fetch['reason'], 'remote-unreachable')
        self.assertEqual(instance.errors, [])
        self.assertIn('| ops-dashboard | WARN | remote-unreachable |', report)
        self.assertEqual(evidence['repos'][0]['status'], 'WARN')

    def test_dns_in_stdout_without_target_refs_also_warns(self):
        _, fetch, _, _ = self.collect(DNS, stdout=True)
        self.assertEqual(fetch['status'], 'WARN')

    def test_dns_without_any_target_tickets_warns(self):
        _, fetch, _, _ = self.collect(DNS, empty=True)
        self.assertEqual(fetch['status'], 'WARN')

    def test_dns_with_target_refs_remains_fail(self):
        instance, fetch, report, _ = self.collect(DNS, targeted=True)
        self.assertEqual(fetch['status'], 'FAIL')
        self.assertEqual(instance.errors[0]['scope'], 'git fetch')
        self.assertIn('| ops-dashboard | FAIL |', report)

    def test_other_fetch_failure_without_target_refs_remains_fail(self):
        instance, fetch, _, _ = self.collect('fatal: Authentication failed')
        self.assertEqual(fetch['status'], 'FAIL')
        self.assertEqual(instance.errors[0]['scope'], 'git fetch')

    def test_unknown_target_refs_does_not_downgrade_dns_failure(self):
        instance, fetch, _, _ = self.collect(DNS, refs_fail=True)
        self.assertEqual(fetch['status'], 'FAIL')
        self.assertTrue(any(error['scope'] == 'git fetch' for error in instance.errors))


class ScanRetryRegressionTests(unittest.TestCase):
    def run_scan(self, failures):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        steps, views = [], 0

        def runner(args):
            nonlocal views
            if args[0] == 'search':
                return [{'key': KEY}] if 'updated >=' in args[args.index('--jql') + 1] else []
            self.assertEqual(args[:2], ['view', KEY])
            views += 1
            steps.append('view')
            if views <= failures:
                raise RuntimeError('acli rc=1')
            return {'fields': {'summary': '조회 재시도', 'status': {'name': '진행 중'},
                               'assignee': {'accountId': scan.ME}, 'comment': {'comments': [
                                   {'id': '1', 'created': NOW.isoformat(), 'author': {'accountId': 'other'},
                                    'body': {'type': 'text', 'text': '확인 요청'}}]}}}

        def sleep(seconds):
            steps.append(('sleep', seconds))

        with contextlib.redirect_stdout(io.StringIO()):
            scan.main(['--since', '2026-10-22T08:00:00+09:00'], runner=runner,
                      base=base, workspace=base, sleep=sleep)
        return json.loads((base / 'followup-scan.json').read_text()), steps

    def test_transient_issue_read_failure_retries_after_three_seconds(self):
        output, steps = self.run_scan(1)
        self.assertEqual(steps, ['view', ('sleep', 3), 'view'])
        self.assertEqual(output['errors'], [])
        self.assertEqual([event['commentId'] for event in output['events']], ['1'])

    def test_exhausted_issue_read_retry_keeps_existing_error(self):
        output, steps = self.run_scan(2)
        self.assertEqual(steps, ['view', ('sleep', 3), 'view'])
        self.assertEqual(output['errors'], [{'scope': KEY, 'error': 'acli rc=1'}])
        self.assertEqual(output['events'], [])

    def test_successful_issue_read_has_no_delay_or_retry(self):
        output, steps = self.run_scan(0)
        self.assertEqual(steps, ['view'])
        self.assertEqual(output['errors'], [])


if __name__ == '__main__':
    unittest.main()
