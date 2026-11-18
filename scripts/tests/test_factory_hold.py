"""공장 일시정지(hold.json) 계약: 임시 상태 폴더와 스텁만 쓰고 실제 알림·Jira·Paseo를 부르지 않는다."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import drain  # noqa: E402
import factory_hold as hold  # noqa: E402
import followup_idle as idle  # noqa: E402
import test_guard_lanes  # noqa: E402
import test_run_lock  # noqa: E402

LOCK = test_run_lock.LOCK

TZ = ZoneInfo('Asia/Seoul')
STAGING = ['스테이징 QA 요청', '스테이징 검수 완료']


def until(hours=2):
    return (datetime.now(TZ) + timedelta(hours=hours)).isoformat()


def write_hold(directory, **fields):
    data = dict(until=until(), only=list(STAGING), freeze=[], reason='운영 반영', setBy='test', at=until(0))
    data.update(fields)
    (Path(directory) / 'hold.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return data


def write_scan(directory, statuses=None, events=(), queue=None):
    (Path(directory) / 'followup-scan.json').write_text(json.dumps(
        {'events': list(events), 'errors': [], 'statuses': statuses or {}, 'queueStatuses': queue or {}},
        ensure_ascii=False), encoding='utf-8')


class HoldRules(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.notices = self.directory / 'notices.log'
        patch.dict(os.environ, {'JIRA_HOLD_NOTIFY_FILE': str(self.notices)}).start()
        self.addCleanup(patch.stopall)

    def notes(self):
        return self.notices.read_text(encoding='utf-8').splitlines() if self.notices.exists() else []

    def test_no_file_runs_as_usual(self):
        self.assertEqual(hold.load(self.directory)['state'], 'none')
        self.assertIsNone(hold.blocked(self.directory, 'PROJ-1', status='개발 QA 요청'))
        self.assertEqual(self.notes(), [])

    def test_only_allows_listed_status_and_blocks_others_and_unknown(self):
        write_hold(self.directory)
        write_scan(self.directory, statuses={'PROJ-1': '스테이징 QA 요청', 'PROJ-2': '개발 QA 요청'},
                   events=[{'key': 'PROJ-3', 'status': '스테이징 검수 완료'}], queue={'PROJ-4': '할 일'})
        self.assertIsNone(hold.blocked(self.directory, 'PROJ-1'))
        self.assertIsNone(hold.blocked(self.directory, 'PROJ-3'))
        self.assertEqual(hold.blocked(self.directory, 'PROJ-2'), 'only(status=개발 QA 요청)')
        self.assertEqual(hold.blocked(self.directory, 'PROJ-4'), 'only(status=할 일)')
        self.assertEqual(hold.blocked(self.directory, 'PROJ-9'), 'only(status=unknown)')
        # 키 없는 단위(아침 점검)는 상태 규칙 대상이 아니다.
        self.assertIsNone(hold.blocked(self.directory, None))

    def test_freeze_status_and_stg_stage(self):
        write_hold(self.directory, only=[], freeze=['stg', '스테이징 QA 요청'])
        write_scan(self.directory, statuses={'PROJ-1': '스테이징 QA 요청', 'PROJ-2': '개발 QA 요청'})
        self.assertEqual(hold.blocked(self.directory, 'PROJ-1'), 'freeze(status=스테이징 QA 요청)')
        self.assertIsNone(hold.blocked(self.directory, 'PROJ-2'))
        self.assertIsNone(hold.blocked(self.directory, 'PROJ-9'))  # only 없음: 상태 모름은 막지 않는다
        (self.directory / 'approvals.json').write_text(json.dumps(
            {'PROJ-2': {'stg': '승인', 'at': 'x'}, 'PROJ-5': {'stg': '승인', 'usedAt': 'y'}}))
        self.assertEqual(hold.blocked(self.directory, 'PROJ-2'), 'freeze(stg)')
        self.assertIsNone(hold.blocked(self.directory, 'PROJ-5'))
        self.assertEqual(hold.blocked(self.directory, None, brief='RUNBOOK §7-3-6 스테이징 배포'), 'freeze(stg)')
        self.assertEqual(hold.blocked(self.directory, None, brief='origin/stg 병합 준비'), 'freeze(stg)')
        self.assertIsNone(hold.blocked(self.directory, None, brief='RUNBOOK §7-3 dev 배포 §7-3-4'))
        self.assertEqual(hold.blocked(self.directory, 'PROJ-5', stage='stg'), 'freeze(stg)')

    def test_expired_hold_is_ignored_and_announced_once(self):
        write_hold(self.directory, until=until(-1))
        write_scan(self.directory, statuses={'PROJ-2': '개발 QA 요청'})
        for _ in range(2):
            self.assertEqual(hold.load(self.directory)['state'], 'expired')
            self.assertIsNone(hold.blocked(self.directory, 'PROJ-2'))
        self.assertEqual(len(self.notes()), 1)
        self.assertIn('만료', self.notes()[0])

    def test_invalid_file_blocks_everything_keeps_file_and_alerts_once(self):
        for raw in ('{not json', json.dumps({'until': '2026-11-06T12:00:00', 'only': STAGING, 'reason': 'x'}),
                    json.dumps({'until': until(), 'reason': 'x'}), json.dumps({'until': until(), 'only': 'x', 'reason': 'x'})):
            with self.subTest(raw=raw):
                (self.directory / 'hold.json').write_text(raw)
                for _ in range(2):
                    self.assertEqual(hold.load(self.directory)['state'], 'invalid')
                    self.assertEqual(hold.blocked(self.directory, 'PROJ-1', status='스테이징 QA 요청'), 'invalid')
                    self.assertEqual(hold.blocked(self.directory, None), 'invalid')
                self.assertEqual((self.directory / 'hold.json').read_text(), raw)
        self.assertEqual(len(self.notes()), 4)
        self.assertTrue(all('형식 오류' in line for line in self.notes()))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'factory_hold.py'), *args, '--dir', str(self.directory)],
                              capture_output=True, text=True, env=dict(os.environ))

    def test_cli_set_status_check_clear(self):
        self.assertEqual(self.cli('set', '--until', until(-1), '--only', ','.join(STAGING), '--reason', 'x').returncode, 2)
        self.assertEqual(self.cli('set', '--until', until(), '--freeze', 'dev', '--reason', 'x').returncode, 2)
        self.assertEqual(self.cli('set', '--until', until(), '--only', ','.join(STAGING)).returncode, 2)
        self.assertFalse((self.directory / 'hold.json').exists())
        done = self.cli('set', '--until', '2099-11-06T12:00:00+09:00', '--only', '스테이징 QA 요청, 스테이징 검수 완료',
                        '--reason', '운영 반영', '--set-by', 'tester')
        self.assertEqual(done.returncode, 0, done.stderr)
        saved = json.loads((self.directory / 'hold.json').read_text())
        self.assertEqual((saved['only'], saved['freeze'], saved['setBy']), (STAGING, [], 'tester'))
        write_scan(self.directory, statuses={'PROJ-1': '스테이징 QA 요청', 'PROJ-2': '진행 중'})
        self.assertEqual(self.cli('check', '--key', 'PROJ-1').returncode, 0)
        blocked = self.cli('check', '--key', 'PROJ-2')
        self.assertEqual(blocked.returncode, 3)
        self.assertIn('only(status=진행 중)', blocked.stderr)
        status = json.loads(self.cli('status', '--key', 'PROJ-2').stdout)
        self.assertEqual((status['state'], status['blocked']), ('active', 'only(status=진행 중)'))
        self.assertEqual(json.loads(self.cli('clear').stdout)['cleared'], True)
        self.assertEqual(self.cli('check', '--key', 'PROJ-2').returncode, 0)
        notes = self.notes()
        self.assertEqual(len(notes), 2)
        self.assertIn('시작', notes[0])
        self.assertIn('해제', notes[1])

    def test_stg_approval_request_is_refused_while_held(self):
        write_hold(self.directory, only=[], freeze=['stg'])
        result = subprocess.run(['/bin/bash', str(SCRIPTS / 'stg-approval-request'), 'PROJ-1'], capture_output=True,
                                text=True, env=dict(os.environ, JIRA_WAKE_DIR=str(self.directory)))
        self.assertEqual(result.returncode, 5)
        self.assertIn('factory hold', result.stderr)

    def test_rank_skips_held_keys_and_keeps_queue(self):
        write_hold(self.directory)
        write_scan(self.directory, queue={'PROJ-1': '스테이징 QA 요청', 'PROJ-2': '개발 QA 요청'})
        queue = {key: dict(slug=key.lower(), stage='build', service='api', branch=None, commentIds=[],
                           waiting=None, note='') for key in ('PROJ-1', 'PROJ-2')}
        self.assertEqual(drain.rank(queue, directory=self.directory, focus='PROJ-2', eligible=['PROJ-1', 'PROJ-2']),
                         ['PROJ-1'])
        (self.directory / 'hold.json').unlink()
        self.assertEqual(drain.rank(queue, directory=self.directory, eligible=['PROJ-1', 'PROJ-2']), ['PROJ-1', 'PROJ-2'])


class HoldGates(test_run_lock.LivenessFixture):
    def setUp(self):
        super().setUp()
        self.notices = self.root / 'notices.log'
        os.environ['JIRA_HOLD_NOTIFY_FILE'] = str(self.notices)
        write_scan(self.state, statuses={'PROJ-1': '스테이징 QA 요청', 'PROJ-2': '개발 QA 요청'})

    def run_lock(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'run_lock.py'), *args, '--dir', str(self.state)],
                              capture_output=True, text=True, env=dict(os.environ))

    def journal(self):
        return ''.join(path.read_text() for path in self.state.glob('followup-*.md'))

    def test_lease_acquire_refuses_held_key_only(self):
        self.lock('global')
        write_hold(self.state)
        refused = self.run_lock('lease-acquire', '--parent', 'global', '--key', 'PROJ-2')
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stdout), {'reason': 'hold', 'hold': 'only(status=개발 QA 요청)'})
        self.assertEqual(LOCK.lease_list(self.state), [])
        self.assertIn('hold: PROJ-2 only(status=개발 QA 요청)', self.journal())
        allowed = self.run_lock('lease-acquire', '--parent', 'global', '--key', 'PROJ-1')
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_worker_gate_refuses_held_key_and_stg_brief(self):
        self.lock('global')
        brief = self.root / 'brief.md'
        brief.write_text('RUNBOOK §7-3-4 dev 배포\n')
        self.assertEqual(self.run_lock('worker', '--token', 'global', '--key', 'PROJ-2', '--brief', str(brief)).returncode, 0)
        write_hold(self.state)
        refused = self.run_lock('worker', '--token', 'global', '--key', 'PROJ-2', '--unit', 'build', '--brief', str(brief))
        self.assertEqual(refused.returncode, 3)
        self.assertIn('hold:PROJ-2 only(status=개발 QA 요청)', refused.stderr)
        self.assertEqual(self.run_lock('worker', '--token', 'global', '--key', 'PROJ-1', '--brief', str(brief)).returncode, 0)
        # 키 없는 단위(아침 점검)는 only 대상이 아니다. freeze=stg는 스테이징 brief를 막는다.
        self.assertEqual(self.run_lock('worker', '--token', 'global', '--brief', str(brief)).returncode, 0)
        write_hold(self.state, only=[], freeze=['stg'])
        brief.write_text('RUNBOOK §7-3-6 스테이징 배포\n')
        self.assertEqual(self.run_lock('worker', '--token', 'global', '--key', 'PROJ-1', '--brief', str(brief)).returncode, 3)
        self.assertIn('hold: PROJ-1 freeze(stg) (worker unit)', self.journal())

    def test_worker_gate_without_hold_file_matches_old_behaviour(self):
        self.lock('global')
        self.assertEqual(self.run_lock('worker', '--token', 'global', '--key', 'PROJ-2').returncode, 0)
        self.assertEqual(self.run_lock('worker', '--token', 'other', '--key', 'PROJ-2').returncode, 3)


class HoldGuard(test_guard_lanes.GuardLaneTests):
    """가드 idle 판정: 막힌 키는 hold 사유로만 남고 모델을 깨우지 않는다. GuardLaneTests 고정물을 다시 쓴다."""

    def setUp(self):
        super().setUp()
        self.notices = self.directory / 'notices.log'
        patch.dict(os.environ, {'JIRA_HOLD_NOTIFY_FILE': str(self.notices)}).start()
        self.addCleanup(patch.stopall)
        self.statuses = {}

    def command(self, argv):
        result = super().command(argv)
        if argv[0] == 'scan-stub':
            path = Path(argv[argv.index('--out') + 1])
            data = json.loads(path.read_text())
            data.update(statuses=dict(self.statuses), queueStatuses=dict(self.statuses))
            path.write_text(json.dumps(data, ensure_ascii=False))
        return result

    def item(self, **extra):
        return dict(dict(slug='s', stage='build', service='api', branch=None, commentIds=[], waiting=None,
                         note=''), **extra)

    def test_held_events_and_queue_do_not_wake_and_queue_is_untouched(self):
        write_hold(self.directory)
        self.statuses = {'PROJ-1': '진행 중', 'PROJ-2': '할 일'}
        self.events = [{'key': 'PROJ-1', 'commentId': 'c1', 'status': '진행 중'},
                       {'key': 'PROJ-2', 'commentId': 'intake:PROJ-2', 'intake': True, 'status': '할 일'}]
        queue = {'PROJ-1': self.item(), 'PROJ-3': self.item(waiting='env-human', needs='dev-db')}
        self.statuses['PROJ-3'] = '개발 QA 요청'
        self.write('followup-queue.json', queue)
        self.write('approvals.json', {'PROJ-3': {'stg': '승인', 'at': 'x'}})
        with patch.object(idle, 'triage', wraps=idle.triage) as triage:
            result, code = self.decide()
        self.assertEqual(code, 0, result['reasons'])
        self.assertTrue(result['idle'])
        self.assertEqual(result['events'], 0)
        self.assertEqual(triage.call_args.args[1], [])
        self.assertEqual(result['held'], {'PROJ-1': 'only(status=진행 중)', 'PROJ-2': 'only(status=할 일)',
                                          'PROJ-3': 'only(status=개발 QA 요청)'})
        self.assertIn('hold:PROJ-1(only(status=진행 중))', result['reasons'])
        self.assertEqual(self.read('followup-queue.json'), queue)
        self.assertEqual(self.read('guard-state.json')['runnable'], {})
        self.assertEqual(result['hold']['only'], STAGING)

    def test_allowed_status_event_still_wakes(self):
        write_hold(self.directory)
        self.statuses = {'PROJ-1': '스테이징 QA 요청'}
        self.events = [{'key': 'PROJ-1', 'commentId': 'c1', 'status': '스테이징 QA 요청'}]

        def classifier(events):
            return {'predictions': [{'commentId': event['commentId'], 'class': '질문 답변', 'needs_response': True,
                                     'confidence': 1, 'reason': 'fixture'} for event in events]}
        result, code = self.decide(classifier=classifier)
        self.assertEqual((code, result['events']), (1, 1))
        self.assertEqual(result['held'], {})

    def test_runnable_resumes_after_expiry_without_stuck(self):
        self.statuses = {'PROJ-1': '진행 중'}
        self.write('followup-queue.json', {'PROJ-1': self.item()})
        write_hold(self.directory)
        result, code = self.decide()
        self.assertEqual(code, 0)
        write_hold(self.directory, until=until(-1))
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertIn('queue-runnable:PROJ-1', result['reasons'])
        self.assertNotIn('stuck-runnable:PROJ-1', result['reasons'])
        self.assertIn('만료', self.notices.read_text())

    def test_invalid_hold_holds_every_key(self):
        (self.directory / 'hold.json').write_text('{broken')
        self.statuses = {'PROJ-1': '스테이징 QA 요청'}
        self.events = [{'key': 'PROJ-1', 'commentId': 'c1', 'status': '스테이징 QA 요청'}]
        self.write('followup-queue.json', {'PROJ-1': self.item()})
        result, code = self.decide()
        self.assertEqual((code, result['events']), (0, 0))
        self.assertEqual(result['held'], {'PROJ-1': 'invalid'})
        self.assertEqual(result['hold']['state'], 'invalid')

    # GuardLaneTests의 시험은 상속하지 않는다(같은 시험을 두 번 돌리지 않게).
    for name in [name for name in dir(test_guard_lanes.GuardLaneTests) if name.startswith('test_')]:
        locals()[name] = None
    del name


if __name__ == '__main__':
    unittest.main()
