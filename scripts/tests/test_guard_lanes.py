"""티켓 임대 가드: 임시 상태·명령 주입만 사용하고 외부 호출은 하지 않는다."""

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


class GuardLaneTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.events, self.teams_events, self.notifications = [], [], []
        self.teams_error = None
        self.actions = {'dev-db': 'dev VPN 연결', 'stg-db': 'stg VPN 연결'}
        for name, replacement in (
                ('run', self.command), ('notify', lambda message, **kwargs: self.notifications.append(message) or True),
                ('blocked_by', lambda *args: []), ('reconcile_reasons', lambda *args: [])):
            mock = patch.object(idle, name, side_effect=replacement)
            mock.start()
            self.addCleanup(mock.stop)
        decision = patch('lessons.open_request')
        decision.start()
        self.addCleanup(decision.stop)

    def write(self, name, value):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def read(self, name):
        return json.loads((self.directory / name).read_text())

    def lease(self, key, state='live'):
        return self.write(f'tickets/{key}.json', {
            'key': key, 'token': f'ticket-{key}', 'state': state,
            'parent': 'global-token', 'startedAt': 100, 'deadline': 10000,
            'orchestratorId': None if state == 'launching' else f'agent-{key}'})

    def inbox(self, key, *events):
        path = self.directory / 'tickets' / f'{key}.inbox.jsonl'
        path.parent.mkdir(exist_ok=True)
        path.write_text(''.join(json.dumps(event) + '\n' for event in events))
        return path

    def command(self, argv):
        if argv[0] in ('scan-stub', 'teams-stub'):
            data = {'events': self.events, 'errors': []} if argv[0] == 'scan-stub' else {
                'events': self.teams_events, 'errors': [],
                'status': 'unavailable' if self.teams_error else 'ok', 'error': self.teams_error}
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps(data))
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if argv[0] == 'preflight-stub':
            needs = argv[argv.index('--need') + 1]
            return SimpleNamespace(returncode=10, stderr='', stdout=json.dumps({'results': [
                {'status': 'FAIL', 'human_action': self.actions[needs]}]}))
        raise AssertionError('unexpected external command')

    def decide(self, **kwargs):
        return idle.decide(self.directory, ['scan-stub'], ['preflight-stub'], **kwargs)

    def test_leased_event_is_delivered_before_triage_without_ack_or_wake(self):
        for state in ('launching', 'live'):
            with self.subTest(state=state):
                self.lease('PROJ-1', state)
                self.events = [{'key': 'PROJ-1', 'commentId': 'comment-1', 'answeredByMeAfter': True}]
                with patch.object(idle, 'triage', wraps=idle.triage) as triage:
                    result, code = self.decide()
                self.assertEqual(code, 0)
                self.assertEqual(result['events'], 0)
                self.assertEqual(triage.call_args.args[1], [])
                lines = (self.directory / 'tickets/PROJ-1.inbox.jsonl').read_text().splitlines()
                self.assertEqual([json.loads(line) for line in lines], self.events)
                self.assertFalse((self.directory / 'state.json').exists())

    def test_inbox_delivery_is_idempotent_and_keeps_unleased_events_runnable(self):
        self.lease('PROJ-1')
        self.events = [{'key': 'PROJ-1', 'commentId': 'one'},
                       {'key': 'PROJ-2', 'commentId': 'intake:PROJ-2', 'intake': True}]
        for _ in range(2):
            result, code = self.decide()
            self.assertEqual((code, result['events']), (1, 1))
        self.assertEqual(len((self.directory / 'tickets/PROJ-1.inbox.jsonl').read_text().splitlines()), 1)

    def test_leased_teams_event_bypasses_triage_and_never_wakes_parent(self):
        self.lease('PROJ-1')
        self.teams_events = [{'key': 'PROJ-1', 'commentId': 'teams:one', 'source': 'teams',
                              'answeredByMeAfter': True}]
        result, code = self.decide(teams_cmd=['teams-stub'])
        self.assertEqual((code, result['events'], result['teams']['events']), (0, 0, 0))
        self.assertEqual(json.loads((self.directory / 'tickets/PROJ-1.inbox.jsonl').read_text()),
                         self.teams_events[0])
        self.assertFalse((self.directory / 'state.json').exists())

    def test_teams_room_placeholder_skips_lease_conflicts_and_keeps_existing_run_blockers(self):
        self.teams_events = [{'key': 'TEAMS-ROOM', 'commentId': 'teams:one', 'source': 'teams'}]

        def classifier(events):
            return {'predictions': [{'commentId': event['commentId'], 'class': '질문 답변',
                     'needs_response': True, 'confidence': 1, 'reason': 'fixture'} for event in events]}

        for blockers, expected_code in (([], 1), (['lost-run'], 0)):
            with self.subTest(blockers=blockers), patch.object(
                    idle, 'blocked_by', return_value=blockers) as blocked_by, patch.object(
                    idle, 'lease_conflicts', wraps=idle.lease_conflicts) as conflicts:
                result, code = self.decide(teams_cmd=['teams-stub'], classifier=classifier)
            self.assertEqual(code, expected_code)
            self.assertEqual(result['teams']['events'], 0 if blockers else 1)
            self.assertEqual(result['blockedKeys'], {'TEAMS-ROOM': blockers} if blockers else {})
            blocked_by.assert_any_call(self.directory, 'TEAMS-ROOM')
            conflicts.assert_not_called()

    def test_lease_created_during_scan_is_used_before_triage(self):
        self.events = [{'key': 'PROJ-1', 'commentId': 'one', 'answeredByMeAfter': True}]
        original = self.command

        def command(argv):
            result = original(argv)
            if argv[0] == 'scan-stub':
                self.lease('PROJ-1', 'launching')
            return result

        with patch.object(idle, 'run', side_effect=command):
            result, code = self.decide()
        self.assertEqual((code, result['events']), (0, 0))
        self.assertTrue((self.directory / 'tickets/PROJ-1.inbox.jsonl').exists())
        self.assertFalse((self.directory / 'state.json').exists())

    def test_release_before_atomic_delivery_leaves_event_for_normal_processing(self):
        lease = self.lease('PROJ-1')
        self.events = [{'key': 'PROJ-1', 'commentId': 'intake:PROJ-1', 'intake': True}]

        def released(*args):
            lease.unlink()
            return False

        with patch.object(idle, 'guard_append_inbox', side_effect=released):
            result, code = self.decide()
        self.assertEqual((code, result['events']), (1, 1))
        self.assertIn('new-events', result['reasons'])
        self.assertFalse((self.directory / 'tickets/PROJ-1.inbox.jsonl').exists())

    def test_released_inbox_wakes_even_when_waiting_and_backoff_are_present(self):
        self.write('followup-queue.json', {'PROJ-1': {'waiting': 'human-risk',
                   'backoffUntil': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}})
        self.inbox('PROJ-1', {'key': 'PROJ-1', 'commentId': 'one'})
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('ticket-inbox:PROJ-1', result['reasons'])

    def test_handled_and_empty_inboxes_do_not_wake(self):
        self.inbox('PROJ-1', {'key': 'PROJ-1', 'commentId': 'one'})
        self.inbox('PROJ-2')
        self.write('state.json', {'followup': {'handled': {'one': {'key': 'PROJ-1'}}}})
        self.assertEqual(self.decide()[1], 0)

    def test_capacity_preserves_stuck_state_and_queue_despite_content_changes(self):
        for n in range(5):
            self.lease(f'PROJ-{n}')
        queue = {'PROJ-9': {'waiting': None, 'stage': 'changed', 'deferCount': 4}}
        prior = {'PROJ-9': {'hash': 'old-hash', 'count': 7, 'notified': True}}
        self.write('followup-queue.json', queue)
        self.write('guard-state.json', {'runnable': prior})
        for _ in range(2):
            result, code = self.decide()
            self.assertEqual(code, 0)
            self.assertIn('capacity-full', result['reasons'])
            self.assertFalse(any(reason.startswith(('stuck-runnable:', 'queue-runnable:'))
                                 for reason in result['reasons']))
            self.assertEqual(self.read('guard-state.json')['runnable'], prior)
            self.assertEqual(self.read('followup-queue.json'), queue)
        self.assertEqual(self.notifications, [])

    def test_launching_reservations_count_toward_capacity(self):
        (self.directory / 'lanes').write_text('1')
        self.lease('PROJ-1', 'launching')
        self.write('followup-queue.json', {'PROJ-2': {'waiting': None}})
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertIn('capacity-full', result['reasons'])
        self.assertEqual(self.read('guard-state.json')['runnable'], {})

    def test_capacity_does_not_hide_new_events_or_orphan_inboxes(self):
        (self.directory / 'lanes').write_text('1')
        self.lease('PROJ-1')
        self.write('followup-queue.json', {'PROJ-2': {'waiting': None}})
        self.events = [{'key': 'PROJ-3', 'commentId': 'intake:PROJ-3', 'intake': True}]
        self.inbox('PROJ-4', {'key': 'PROJ-4', 'commentId': 'four'})
        result, code = self.decide()
        self.assertEqual(code, 1)
        for reason in ('capacity-full', 'new-events', 'ticket-inbox:PROJ-4'):
            self.assertIn(reason, result['reasons'])

    def test_lanes_zero_keeps_inbox_delivery_and_blocks_leases_but_runs_other_keys(self):
        (self.directory / 'lanes').write_text('0')
        self.lease('PROJ-1')
        self.write('followup-queue.json', {'PROJ-1': {'waiting': None}, 'PROJ-2': {'waiting': None}})
        self.events = [{'key': 'PROJ-1', 'commentId': 'one'}]
        result, code = self.decide()
        self.assertEqual((code, result['events']), (1, 0))
        self.assertIn('queue-runnable:PROJ-2', result['reasons'])
        self.assertNotIn('queue-runnable:PROJ-1', result['reasons'])
        self.assertNotIn('capacity-full', result['reasons'])
        self.assertTrue((self.directory / 'tickets/PROJ-1.inbox.jsonl').exists())

    def test_related_launching_key_is_blocked_in_sequential_mode(self):
        (self.directory / 'lanes').write_text('0')
        self.lease('PROJ-1', 'launching')
        self.write('followup-queue.json', {
            'PROJ-1': {'waiting': None, 'relatedKeys': ['PROJ-2']},
            'PROJ-2': {'waiting': None}, 'PROJ-3': {'waiting': None}})
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertNotIn('queue-runnable:PROJ-2', result['reasons'])
        self.assertIn('queue-runnable:PROJ-3', result['reasons'])
        self.assertIn('PROJ-1', result['blockedKeys']['PROJ-2'])

    def test_final_blocked_report_preserves_observed_pending_keys_and_rechecks_current_state(self):
        (self.directory / 'pending-focus.jsonl').write_text(
            '{"key":"PROJ-1"}\n{"key":"PROJ-2"}\n')
        blockers = {'PROJ-1': ['still-lost'], 'PROJ-2': ['released']}
        original = self.command

        def command(argv):
            result = original(argv)
            if argv[0] == 'scan-stub':
                blockers.pop('PROJ-2')
            return result

        with patch.object(idle, 'run', side_effect=command), patch.object(
                idle, 'blocked_by', side_effect=lambda directory, key: blockers.get(key, [])):
            result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertEqual(result['blockedKeys'], {'PROJ-1': ['still-lost']})
        self.assertEqual(self.read('guard-last.json')['blockedKeys'], result['blockedKeys'])

    def test_all_updated_summaries_count_including_old_started_ticket(self):
        now = datetime.now(timezone.utc)
        guard = now - timedelta(minutes=2)
        self.write('runs/global/summary.json', {'startedAt': now.timestamp(),
                  'updatedAt': now.isoformat(), 'workers': 2})
        self.write('runs/ticket/summary.json', {'startedAt': (now - timedelta(hours=1)).isoformat(),
                  'updatedAt': now.isoformat(), 'workers': 3})
        self.write('runs/legacy/summary.json', {'startedAt': now.timestamp(), 'workers': 1})
        self.write('runs/old/summary.json', {'startedAt': (now - timedelta(hours=1)).isoformat(),
                  'updatedAt': (now - timedelta(minutes=3)).isoformat(), 'workers': 100})
        (self.directory / 'runs/no-summary').mkdir()
        self.assertEqual(idle.previous_workers(self.directory, guard.isoformat()), 6)
        self.assertEqual(idle.previous_workers(self.directory, None), 0)

    def test_updated_workers_exempt_deferred_runnable_without_stuck(self):
        now = datetime.now(timezone.utc)
        item = {'waiting': None, 'stage': 'tobe', 'deferCount': 1, 'deferredAt': now.isoformat()}
        self.write('followup-queue.json', {'PROJ-1': item})
        self.write('guard-state.json', {'at': (now - timedelta(minutes=2)).isoformat(),
                  'runnable': {'PROJ-1': {'hash': idle.item_digest(item), 'count': 1}}})
        self.write('runs/ticket/summary.json', {'startedAt': (now - timedelta(hours=1)).timestamp(),
                  'updatedAt': now.isoformat(), 'workers': 1})
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('queue-runnable:PROJ-1', result['reasons'])
        self.assertFalse(self.notifications)

    def test_environment_and_teams_notifications_are_separate_and_hash_each_message(self):
        self.write('followup-queue.json', {
            'PROJ-1': {'waiting': 'env-human', 'needs': 'dev-db'},
            'PROJ-2': {'waiting': 'env-human', 'needs': 'stg-db'}})
        self.teams_error = 'CLI_BUSY'
        self.decide(teams_cmd=['teams-stub'])
        self.assertEqual(len(self.notifications), 3)
        self.assertEqual(self.notifications[:2], ['dev VPN 연결 시 진행 가능: PROJ-1',
                                                 'stg VPN 연결 시 진행 가능: PROJ-2'])
        self.assertTrue(all('\n' not in message for message in self.notifications))
        self.decide(teams_cmd=['teams-stub'])
        self.assertEqual(len(self.notifications), 3)
        self.actions['stg-db'] = 'stg VPN 재연결'
        self.decide(teams_cmd=['teams-stub'])
        self.assertEqual(self.notifications[3:], ['stg VPN 재연결 시 진행 가능: PROJ-2'])
        self.actions['stg-db'] = 'stg VPN 연결'
        self.decide(teams_cmd=['teams-stub'])
        self.assertEqual(len(self.notifications), 4)

    def test_one_notification_failure_does_not_prevent_other_messages(self):
        self.write('followup-queue.json', {
            'PROJ-1': {'waiting': 'env-human', 'needs': 'dev-db'},
            'PROJ-2': {'waiting': 'env-human', 'needs': 'stg-db'}})
        with patch.object(idle, 'notify', side_effect=[False, True]) as notify:
            result, code = self.decide()
        self.assertEqual((code, result['notify']), (0, 'failed'))
        self.assertEqual(notify.call_count, 2)


if __name__ == '__main__':
    unittest.main()
