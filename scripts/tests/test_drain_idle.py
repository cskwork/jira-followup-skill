"""단계 B: 처리 실적·미룸·항목별 backoff는 임시 상태와 주입 명령으로 검증한다."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import followup_idle as idle


class DrainIdleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.events, self.notifications = [], []
        self.addCleanup(patch.stopall)
        patch('lessons.open_request').start()  # Human decision surface stays offline.
        patch.object(idle, 'run', side_effect=self.command).start()
        patch.object(idle, 'notify', side_effect=lambda text, **kwargs: self.notifications.append(text) or True).start()
        patch.object(idle, 'blocked_by', return_value=[]).start()
        patch.object(idle, 'reconcile_reasons', return_value=[]).start()

    def command(self, argv):
        if '--out' in argv:
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps({'events': self.events, 'errors': []}))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def write(self, name, value):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def queue(self):
        return json.loads((self.directory / 'followup-queue.json').read_text())

    def decide(self):
        return idle.decide(self.directory, ['scan-stub'], ['preflight-stub'])

    def prior_run(self, workers=1, defer_count=1, reason='budget'):
        self.write('followup-queue.json', {f'PROJ-{n}': {'waiting': None, 'stage': 'tobe'} for n in range(4)})
        self.decide()
        now = datetime.now(timezone.utc)
        state = json.loads((self.directory / 'guard-state.json').read_text())
        state['at'] = (now - timedelta(minutes=2)).isoformat()
        self.write('guard-state.json', state)
        self.write('runs/previous/summary.json', {'startedAt': (now - timedelta(minutes=1)).timestamp(), 'workers': workers})
        queue = self.queue()
        queue['PROJ-0']['waiting'] = 'reporter'  # 1건 처리 뒤 나머지 세 건을 미룬다.
        for n in range(1, 4):
            queue[f'PROJ-{n}'].update(deferredAt=now.isoformat(), deferReason=reason, deferCount=defer_count)
        self.write('followup-queue.json', queue)

    def test_four_runnable_one_processed_three_deferred_are_not_stuck(self):
        self.prior_run()
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertEqual([r for r in result['reasons'] if r.startswith('queue-runnable:')],
                         ['queue-runnable:PROJ-1', 'queue-runnable:PROJ-2', 'queue-runnable:PROJ-3'])
        self.assertFalse(any(r.startswith('stuck-') for r in result['reasons']))

    def test_workers_zero_cannot_exempt_deferrals(self):
        self.prior_run(workers=0)
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertIn('stuck-runnable:PROJ-1', result['reasons'])
        self.assertEqual(self.notifications, [
            '동일 상태로 진행 정지: PROJ-1', '동일 상태로 진행 정지: PROJ-2',
            '동일 상태로 진행 정지: PROJ-3'])
        self.decide()
        self.assertEqual(len(self.notifications), 3)

    def test_capacity_or_resource_deferral_with_zero_workers_is_not_stuck(self):
        # 2026-10-30~11-11 stuck 19건 중 10건: 정원·자원 부족으로 미뤘는데 작업자 0명이라 면제가 안 됐다.
        for reason in ('capacity', 'resource'):
            with self.subTest(reason=reason):
                (self.directory / 'guard-state.json').unlink(missing_ok=True)
                self.prior_run(workers=0, defer_count=0, reason=reason)
                result, code = self.decide()
                self.assertEqual(code, 1)
                self.assertEqual([r for r in result['reasons'] if r.startswith('queue-runnable:')],
                                 ['queue-runnable:PROJ-1', 'queue-runnable:PROJ-2', 'queue-runnable:PROJ-3'])
                self.assertFalse(any(r.startswith('stuck-') for r in result['reasons']))
                self.assertEqual(self.notifications, [])

    def test_capacity_deferral_with_four_deferrals_stays_stuck(self):
        self.prior_run(workers=0, defer_count=4, reason='capacity')
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertIn('stuck-runnable:PROJ-1', result['reasons'])

    def test_unchanged_runnable_without_deferral_is_stuck_on_second_tick(self):
        # 반례: 미룸 기록 없이 같은 내용이 오케스트레이터를 깨운 뒤에도 그대로면 stuck이다.
        self.write('followup-queue.json', {'PROJ-1': {'waiting': None, 'stage': 'tobe'}})
        result, code = self.decide()
        self.assertEqual((code, result['reasons']), (1, ['queue-runnable:PROJ-1']))
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertIn('stuck-runnable:PROJ-1', result['reasons'])

    def test_related_key_lease_conflict_is_lease_blocked_and_does_not_wake(self):
        self.write('followup-queue.json', {'PROJ-1': {'waiting': None, 'stage': 'tobe', 'relatedKeys': ['PROJ-9']}})
        with patch.object(idle, 'lease_conflicts', side_effect=lambda d, key: ['PROJ-9'] if key == 'PROJ-1' else []):
            for _ in range(2):
                result, code = self.decide()
                self.assertEqual(code, 0)
                self.assertEqual(result['reasons'], ['lease-blocked:PROJ-1'])
                self.assertTrue(result['idle'])
        self.assertEqual(self.notifications, [])
        # 임대가 끝나면 stuck이 아니라 새 실행 후보다.
        result, code = self.decide()
        self.assertEqual((code, result['reasons']), (1, ['queue-runnable:PROJ-1']))

    def test_four_deferrals_are_stuck_and_notify_once(self):
        self.prior_run(defer_count=4)
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertIn('stuck-runnable:PROJ-1', result['reasons'])
        self.assertEqual(self.notifications, [
            '동일 상태로 진행 정지: PROJ-1', '동일 상태로 진행 정지: PROJ-2',
            '동일 상태로 진행 정지: PROJ-3'])
        self.decide()
        self.assertEqual(len(self.notifications), 3)

    def test_old_deferral_is_not_exempt(self):
        self.prior_run()
        queue = self.queue()
        for item in queue.values():
            item['deferredAt'] = '2020-01-01T00:00:00+00:00'
        self.write('followup-queue.json', queue)
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertIn('stuck-runnable:PROJ-1', result['reasons'])

    def test_missing_summary_is_not_exempt(self):
        self.prior_run()
        (self.directory / 'runs/previous/summary.json').unlink()
        self.assertEqual(self.decide()[1], 0)

    def test_bookkeeping_changes_do_not_reset_stuck_hash(self):
        self.prior_run(workers=0)
        queue = self.queue()
        for field in idle.BOOKKEEPING - {'backoffUntil', 'backoffLevel', 'deferCount'}:
            queue['PROJ-1'][field] = '2025-01-01T00:00:00+00:00'
        queue['PROJ-1']['backoffUntil'] = '2025-01-01T00:00:00+00:00'
        queue['PROJ-1']['backoffLevel'] = 2
        self.write('followup-queue.json', queue)
        self.assertIn('stuck-runnable:PROJ-1', self.decide()[0]['reasons'])

    def test_actual_change_resets_defer_count(self):
        self.prior_run(defer_count=4)
        queue = self.queue()
        queue['PROJ-1']['stage'] = 'dev-deploy'
        self.write('followup-queue.json', queue)
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('queue-runnable:PROJ-1', result['reasons'])
        self.assertEqual(self.queue()['PROJ-1']['deferCount'], 0)

    def test_backoff_skips_only_that_item_and_new_events_still_wake(self):
        self.write('followup-queue.json', {
            'PROJ-1': {'waiting': None, 'backoffUntil': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
            'PROJ-2': {'waiting': None}})
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertNotIn('queue-runnable:PROJ-1', result['reasons'])
        self.assertIn('queue-runnable:PROJ-2', result['reasons'])
        self.events = [{'key': 'PROJ-1', 'commentId': 'intake:PROJ-1', 'intake': True}]
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('new-events', result['reasons'])

    def test_pending_focus_wakes_even_with_all_items_in_backoff(self):
        self.write('followup-queue.json', {'PROJ-1': {'waiting': None,
            'backoffUntil': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}})
        self.assertEqual(self.decide()[1], 0)
        (self.directory / 'pending-focus.jsonl').write_text('{"key":"PROJ-1"}\n')
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('pending-focus', result['reasons'])

    def test_expired_backoff_is_runnable(self):
        self.write('followup-queue.json', {'PROJ-1': {'waiting': None, 'backoffUntil': '2020-01-01T00:00:00Z'}})
        self.assertIn('queue-runnable:PROJ-1', self.decide()[0]['reasons'])
