"""Retrospective regressions; every command and notification is injected."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import followup_idle as idle


class IdleFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.calls = []
        self.notifications = []
        self.code = 10
        self.action = 'VPN dev 연결(OTP)'
        self.patch = patch.object(idle, 'run', side_effect=self.run_command)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = patch.dict('os.environ', FOLLOWUP_IDLE_NOTIFY_CMD='notify-stub')
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, argv):
        self.calls.append(argv)
        if argv[0] == 'scan-stub':
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps({'events': [], 'errors': []}))
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if argv[0] == 'notify-stub':
            self.notifications.append(argv)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return SimpleNamespace(returncode=self.code, stderr='', stdout=json.dumps({'results': [
            {'name': 'vpn', 'status': 'PASS'},
            {'name': 'dev-db', 'status': 'FAIL', 'human_action': self.action, 'reason': 'wrong-vpn'}]}))

    def queue(self, value):
        (self.directory / 'followup-queue.json').write_text(json.dumps(value))

    def decide(self):
        return idle.decide(self.directory, ['scan-stub'], ['preflight-stub'])

    def local_queue(self):
        self.queue({'PROJ-1389': {'waiting': 'env-human', 'stage': 'tobe',
                                'needs': 'dev-db,jira,jenkins-dev'}})

class RetroIdleTests(IdleFixture):
    def test_1i_normalization_is_persisted_and_guard_last_saved(self):
        self.local_queue()
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('queue-runnable:PROJ-1389', result['reasons'])
        row = json.loads((self.directory / 'followup-queue.json').read_text())['PROJ-1389']
        self.assertEqual((row['nextStep'], row['needs'], row['waiting']), ('local', '', None))
        last = json.loads((self.directory / 'guard-last.json').read_text())
        self.assertEqual(last['reasons'], result['reasons'])
        self.assertIn('at', last)

    def test_1ii_unchanged_second_tick_is_idle_notifies_and_logs_once(self):
        self.local_queue()
        self.decide()
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertTrue(result['idle'])
        self.assertIn('stuck-runnable:PROJ-1389', result['reasons'])
        self.assertNotIn('queue-runnable:PROJ-1389', result['reasons'])
        self.decide()
        self.assertEqual(len(self.notifications), 1)
        lessons = (self.directory / 'lessons.jsonl').read_text().splitlines()
        self.assertEqual(len(lessons), 1)
        self.assertEqual(json.loads(lessons[0])['kind'], 'infra_fail')

    def test_1iii_changed_item_is_runnable_again(self):
        self.local_queue()
        self.decide()
        self.decide()
        path = self.directory / 'followup-queue.json'
        queue = json.loads(path.read_text())
        queue['PROJ-1389']['nextStep'] = 'dev-deploy'
        self.queue(queue)
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('queue-runnable:PROJ-1389', result['reasons'])

    def test_2_same_needs_three_items_probe_once_with_fail_fast(self):
        self.queue({f'PROJ-{i}': {'waiting': 'env-human', 'needs': 'dev-db,jira'} for i in range(3)})
        self.decide()
        probes = [c for c in self.calls if c[0] == 'preflight-stub']
        self.assertEqual(len(probes), 1)
        self.assertIn('--fail-fast', probes[0])

    def test_3_group_notification_dedupes_by_day_and_content(self):
        self.queue({f'PROJ-{i}': {'waiting': 'env-human', 'needs': 'dev-db'} for i in range(3)})
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertEqual(result['waitingHuman'], {self.action: ['PROJ-0', 'PROJ-1', 'PROJ-2']})
        self.assertEqual(len(self.notifications), 1)
        self.assertIn('PROJ-0, PROJ-1, PROJ-2', self.notifications[0][-1])
        self.decide()
        self.assertEqual(len(self.notifications), 1)
        self.action = 'VPN stg 연결(OTP)'
        self.decide()
        self.assertEqual(len(self.notifications), 2)
        state = json.loads((self.directory / 'guard-state.json').read_text())
        state['notificationDay'] = '2000-01-01'
        (self.directory / 'guard-state.json').write_text(json.dumps(state))
        self.decide()
        self.assertEqual(len(self.notifications), 3)

    def test_notification_failure_does_not_change_idle(self):
        self.queue({'PROJ-1': {'waiting': 'env-human', 'needs': 'dev-db'}})
        original = self.run_command
        def failure(argv):
            if argv[0] == 'notify-stub':
                raise OSError('unavailable')
            return original(argv)
        with patch.object(idle, 'run', side_effect=failure):
            result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertEqual(result['notify'], 'failed')

    def test_invalid_next_step_still_saves_internal_error_evidence(self):
        self.queue({'PROJ-1389': {'waiting': 'env-human', 'nextStep': []}})
        result, code = self.decide()
        self.assertEqual(code, 2)
        self.assertFalse(result['idle'])
        self.assertIn('internal-error', result['reasons'])
        self.assertEqual(json.loads((self.directory / 'guard-last.json').read_text()), result)

    def test_guard_last_records_scan_failure_and_never_suppresses_it_as_stuck(self):
        self.local_queue()
        self.decide()
        original = self.run_command
        def failure(argv):
            result = original(argv)
            if argv[0] == 'scan-stub':
                result.returncode = 1
            return result
        with patch.object(idle, 'run', side_effect=failure):
            result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertFalse(result['idle'])
        self.assertIn('scan-failed', result['reasons'])
        self.assertIn('stuck-runnable:PROJ-1389', result['reasons'])
        self.assertEqual(json.loads((self.directory / 'guard-last.json').read_text()), result)


class ScanAccountingTests(IdleFixture):
    """A tick that dies inside the scan must be counted, and a streak must reach a human."""

    def health(self):
        return json.loads((self.directory / 'scan-state.json').read_text())

    def fail_scan(self):
        original = self.run_command

        def failure(argv):
            result = original(argv)
            if argv[0] == 'scan-stub':
                result.returncode = 1
            return result
        return patch.object(idle, 'run', side_effect=failure)

    def test_healthy_scan_records_success_and_clears_the_attempt_marker(self):
        result, code = self.decide()
        self.assertEqual(code, 0)
        health = self.health()
        self.assertEqual((health['failureCount'], health['lastError']), (0, None))
        self.assertIsNone(health['attemptStartedAt'])
        self.assertIsNotNone(health['lastSuccessAt'])
        self.assertEqual(result['monitoring']['jira']['failureCount'], 0)

    def test_tick_killed_inside_the_scan_is_counted_by_the_next_tick(self):
        (self.directory / 'scan-state.json').write_text(json.dumps(
            {'attemptStartedAt': '2026-10-29T09:00:00+09:00', 'failureCount': 0}))
        result, code = self.decide()
        self.assertIn('scan-interrupted', result['reasons'])
        self.assertEqual(result['monitoringStatus'], 'degraded')
        # The scan still runs: an interrupted tick never skips this tick's Jira read.
        self.assertTrue(any(argv[0] == 'scan-stub' for argv in self.calls))
        self.assertEqual(self.health()['failureCount'], 0)
        self.assertIsNone(self.health()['attemptStartedAt'])

    def test_failure_streak_notifies_once_and_a_healthy_scan_clears_it(self):
        with self.fail_scan():
            for _ in range(2):
                self.decide()
            self.assertEqual(self.notifications, [])
            result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertEqual(result['scanAlert'], 'sent')
        self.assertEqual(len(self.notifications), 1)
        self.assertIn('3회 연속 실패', self.notifications[0][-1])
        health = self.health()
        self.assertEqual((health['failureCount'], health['lastError'], health['alerted']),
                         (3, 'scan-failed', True))
        with self.fail_scan():
            self.decide()
        self.assertEqual(len(self.notifications), 1)
        self.decide()
        health = self.health()
        self.assertEqual((health['failureCount'], health['alerted']), (0, False))

    def test_reported_scan_errors_count_without_hiding_the_reason(self):
        original = self.run_command

        def errored(argv):
            if argv[0] == 'scan-stub':
                Path(argv[argv.index('--out') + 1]).write_text(
                    json.dumps({'events': [], 'errors': ['jira-5xx']}))
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            return original(argv)
        with patch.object(idle, 'run', side_effect=errored):
            result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('scan-errors', result['reasons'])
        self.assertEqual(self.health()['lastError'], 'scan-errors')

    def test_malformed_payload_is_counted_as_malformed_not_as_an_interruption(self):
        original = self.run_command

        def broken(argv):
            if argv[0] == 'scan-stub':
                Path(argv[argv.index('--out') + 1]).write_text(json.dumps({'events': 'no'}))
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            return original(argv)
        with patch.object(idle, 'run', side_effect=broken):
            result, code = self.decide()
        self.assertEqual(code, 2)
        self.assertIn('internal-error', result['reasons'])
        health = self.health()
        self.assertEqual((health['failureCount'], health['lastError']), (1, 'scan-malformed'))
        self.assertIsNone(health['attemptStartedAt'])

    def test_invalid_saved_failure_count_is_an_internal_error(self):
        (self.directory / 'scan-state.json').write_text(json.dumps({'failureCount': -1}))
        result, code = self.decide()
        self.assertEqual(code, 2)
        self.assertIn('internal-error', result['reasons'])
