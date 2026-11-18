"""2026-11-02 회고: 허위 대기·진행 0건 무알림·sol 거절 재시도. 임시 상태와 주입 명령만 쓴다."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drain  # noqa: E402
import followup_idle as idle  # noqa: E402
import followup_scan as scan  # noqa: E402
import state_update  # noqa: E402


def item(waiting='human-risk', stage='build', note='원래 사유'):
    return dict(slug=None, stage=stage, service=None, branch=None, commentIds=[], waiting=waiting, note=note)


class GuardReconcile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.statuses, self.blocked, self.notifications = {}, set(), []
        self.addCleanup(patch.stopall)
        patch('lessons.open_request').start()  # Human decision surface stays offline.
        patch.object(idle, 'run', side_effect=self.command).start()
        patch.object(idle, 'notify', side_effect=lambda text, **kwargs: self.notifications.append(text) or True).start()
        patch.object(idle, 'blocked_by', side_effect=lambda d, key, *a: ['lease'] if key in self.blocked else []).start()
        patch.object(idle, 'reconcile_reasons', return_value=[]).start()

    def command(self, argv):
        if '--out' in argv:
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps(
                {'events': [], 'errors': [], 'queueStatuses': self.statuses}))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def write_queue(self, queue):
        state_update.atomic_json(self.directory / 'followup-queue.json', queue)

    def queue(self):
        return json.loads((self.directory / 'followup-queue.json').read_text())

    def decide(self):
        return idle.decide(self.directory, ['scan-stub'], ['preflight-stub'])

    def test_finished_or_qa_tickets_leave_false_human_risk(self):
        # 11-02 17시 실제 상태: 1353=운영 검수 완료, 1468=스테이징 검수 완료, 1404=stage shipped, 1429=개발 QA 요청.
        self.write_queue({'PROJ-1353': item(), 'PROJ-1468': item(), 'PROJ-1404': item(stage='shipped'),
                          'PROJ-1429': item(), 'PROJ-1462': item(), 'PROJ-1500': item(),
                          'PROJ-1334': item(waiting='reporter')})
        self.statuses = {'PROJ-1353': '운영 검수 완료', 'PROJ-1468': '스테이징 검수 완료', 'PROJ-1429': '개발 QA 요청',
                         'PROJ-1462': '진행 중', 'PROJ-1500': '운영계 배포 완료', 'PROJ-1334': '스테이징 QA 요청'}
        self.blocked = {'PROJ-1500'}  # 임대 중인 키는 티켓 오케스트레이터 몫이다.
        result, code = self.decide()
        self.assertEqual(code, 0)
        queue = self.queue()
        self.assertEqual(sorted(queue), ['PROJ-1334', 'PROJ-1429', 'PROJ-1462', 'PROJ-1500'])
        self.assertEqual(queue['PROJ-1429']['waiting'], 'reporter')
        self.assertTrue(queue['PROJ-1429']['note'].startswith('원래 사유 | 가드 '))
        self.assertIn("'개발 QA 요청'", queue['PROJ-1429']['note'])
        self.assertEqual((queue['PROJ-1462']['waiting'], queue['PROJ-1500']['waiting']), ('human-risk', 'human-risk'))
        self.assertEqual(queue['PROJ-1334'], item(waiting='reporter'))
        self.assertEqual(result['queueReconcile'], {
            'removed': {'PROJ-1353': '운영 검수 완료', 'PROJ-1468': '스테이징 검수 완료', 'PROJ-1404': 'delivery=shipped'},
            'reporter': {'PROJ-1429': '개발 QA 요청'}})
        journal = next(self.directory.glob('followup-*.md')).read_text()
        self.assertIn('queue-reconcile: removed PROJ-1353(운영 검수 완료)', journal)
        # 다시 돌려도 바뀔 것이 없다.
        result, _ = self.decide()
        self.assertNotIn('queueReconcile', result)

    def test_followup_human_risk_on_qa_ticket_keeps_question(self):
        # 2026-11-17 PROJ-1505: 사용자가 '개발 QA 요청' 티켓의 후속 수정을 다시 열었는데 질문이 QA 회신 대기로 사라졌다.
        followup = dict(item(), question='반 구분 기준을 바꿀까요?', suggest='네',
                        followupSince='2026-11-17T09:00:00+09:00')
        self.write_queue({'PROJ-1505': followup, 'PROJ-1429': item()})
        self.statuses = {'PROJ-1505': '개발 QA 요청', 'PROJ-1429': '개발 QA 요청'}
        result, _ = self.decide()
        queue = self.queue()
        self.assertEqual(queue['PROJ-1505'], followup)
        self.assertEqual(queue['PROJ-1429']['waiting'], 'reporter')  # 후속 표시가 없으면 그대로 바꾼다.
        self.assertEqual(result['queueReconcile']['reporter'], {'PROJ-1429': '개발 QA 요청'})

    def test_followup_cleared_when_result_posted(self):
        since = '2026-11-17T09:00:00+09:00'
        self.write_queue({'PROJ-1505': dict(item(waiting=None), delivery='dev-verified', followupSince=since)})
        state_update.patch(self.directory, 'followup-queue.json', ['PROJ-1505'], {'stage': 'tobe'})
        self.assertEqual(self.queue()['PROJ-1505']['followupSince'], since)  # 결과 전 단계 쓰기는 유지
        state_update.patch(self.directory, 'followup-queue.json', ['PROJ-1505'], {'delivery': 'result-posted'})
        self.assertNotIn('followupSince', self.queue()['PROJ-1505'])
        # 이미 결과 단계인 항목을 다시 열 때 함께 쓴 표시는 지우지 않는다.
        state_update.patch(self.directory, 'followup-queue.json', ['PROJ-1505'], {'followupSince': since})
        self.assertEqual(self.queue()['PROJ-1505']['followupSince'], since)

    def test_validate_queue_checks_followup_since(self):
        drain.validate_queue({'PROJ-1': dict(item(), followupSince='2026-11-17T09:00:00+09:00')})
        for bad in (True, 'yesterday'):
            with self.assertRaisesRegex(ValueError, 'followupSince'):
                drain.validate_queue({'PROJ-1': dict(item(), followupSince=bad)})

    def test_concurrent_writer_change_is_not_overwritten(self):
        self.write_queue({'PROJ-1353': item(), 'PROJ-1429': item()})
        self.statuses = {'PROJ-1353': '운영 검수 완료', 'PROJ-1429': '개발 QA 요청'}
        real = idle.transaction

        def racing(directory, name, callback):
            if name == 'followup-queue.json':
                queue = self.queue()
                queue['PROJ-1353']['stage'] = 'tobe'      # 다른 작성자가 진행시킴
                queue['PROJ-1429']['waiting'] = None
                self.write_queue(queue)
            return real(directory, name, callback)
        with patch.object(idle, 'transaction', side_effect=racing):
            result, _ = self.decide()
        queue = self.queue()
        self.assertEqual(queue['PROJ-1353']['stage'], 'tobe')
        self.assertIsNone(queue['PROJ-1429']['waiting'])
        self.assertNotIn('queueReconcile', result)

    def test_zero_runnable_with_human_wait_is_summarised_once_a_day(self):
        self.write_queue({'PROJ-1': item(), 'PROJ-2': item(), 'PROJ-3': item(waiting='reporter'),
                          'PROJ-4': item(waiting='stg-approval')})
        self.decide()
        self.assertEqual(self.notifications, [
            '진행 가능 0건 · 사람 확인 2건 · 회신 대기 1건 · 기타 대기 1건\n사람 확인: PROJ-1, PROJ-2\n회신 대기: PROJ-3'])
        queue = self.queue()
        queue['PROJ-5'] = item()
        self.write_queue(queue)
        self.decide()
        self.assertEqual(len(self.notifications), 1)  # 숫자가 바뀌어도 같은 날 다시 알리지 않는다.
        state = json.loads((self.directory / 'guard-state.json').read_text())
        state['notificationDay'] = '2000-01-01'
        state_update.atomic_json(self.directory / 'guard-state.json', state)
        self.decide()
        self.assertTrue(self.notifications[-1].startswith('진행 가능 0건 · 사람 확인 3건 · 회신 대기 1건'))

    def test_runnable_or_leased_work_is_not_summarised(self):
        self.write_queue({'PROJ-1': item(), 'PROJ-2': item(waiting=None)})
        self.decide()
        self.assertEqual(self.notifications, [])
        self.write_queue({'PROJ-1': item(), 'PROJ-2': item(waiting='reporter')})
        self.assertIsNone(idle.human_summary({'PROJ-1': 'human-risk'}, {'PROJ-9': {}}))
        self.assertIsNone(idle.human_summary({'PROJ-2': 'reporter'}, {}))
        keys = {f'PROJ-{n}': 'human-risk' for n in range(10, 20)}
        self.assertTrue(idle.human_summary(keys, {}).endswith('PROJ-17 외 2건'))


class ScanQueueStatuses(unittest.TestCase):
    def run_scan(self, queue, fail=False):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'followup-queue.json').write_text(json.dumps(queue))

            def runner(argv):
                calls.append(argv)
                jql = argv[argv.index('--jql') + 1] if argv[0] == 'search' else ''
                if jql.startswith('key in'):
                    if fail:
                        raise RuntimeError('acli rc=1')
                    return [None, {'key': 'PROJ-1353', 'fields': {'status': {'name': '운영 검수 완료'}}}]
                if argv[0] == 'search':
                    return [{'key': 'PROJ-1429'}] if 'updated >=' in jql and 'assignee' not in jql else []
                return {'fields': {'summary': 's', 'status': {'name': '개발 QA 요청'}, 'comment': {'comments': []}}}
            with contextlib.redirect_stdout(io.StringIO()):
                scan.main([], runner=runner, workspace=None, base=base)
            return json.loads((base / 'followup-scan.json').read_text()), calls

    def test_queue_statuses_reuse_views_and_ask_rest_in_one_search(self):
        out, calls = self.run_scan({'PROJ-1353': {}, 'PROJ-1429': {}})
        self.assertEqual(out['queueStatuses'], {'PROJ-1429': '개발 QA 요청', 'PROJ-1353': '운영 검수 완료'})
        batch = [argv for argv in calls if argv[0] == 'search' and argv[argv.index('--jql') + 1].startswith('key in')]
        self.assertEqual([argv[argv.index('--jql') + 1] for argv in batch], ['key in (PROJ-1353)'])
        self.assertEqual(out['errors'], [])

    def test_status_lookup_failure_does_not_wake_the_line(self):
        out, _ = self.run_scan({'PROJ-1353': {}, 'PROJ-1429': {}}, fail=True)
        self.assertEqual(out['errors'], [])
        self.assertEqual(out['queueStatuses'], {'PROJ-1429': '개발 QA 요청'})
        self.assertIn('acli rc=1', out['queueStatusError'])


class ModelRefusal(unittest.TestCase):
    """2026-10-30 PROJ-1449: sol이 보안 티켓 brief를 사이버보안 위험으로 거절 → retryAllowed=false라 human-risk."""
    REFUSAL = json.dumps({'error': {'code': 'OUTPUT_SCHEMA_FAILED', 'message':
                          'This content was flagged for possible cybersecurity risk. If this seems wrong, try rephrasing.'}})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        state_update.atomic_json(self.directory / 'running.json', {'token': 'test', 'startedAt': 1})
        state_update.atomic_json(self.directory / 'followup-queue.json', {'PROJ-1449': item(waiting=None)})
        state_update.atomic_json(self.directory / 'required.json', {'key': 'PROJ-1449', 'stage': 'asis', 'checks': [
            {'name': 'asis', 'artifact': 'asis.json', 'resultField': 'status', 'environment': {'environment': 'dev'}}]})
        self.result = self.directory / 'result.json'
        self.result.write_text('')  # 실제처럼 0바이트
        self.stderr = self.directory / 'result.stderr'
        state_update.atomic_json(Path(str(self.result) + '.worker.json'), {'key': 'PROJ-1449', 'exitCode': 1})

    def check(self, stderr, *extra):
        self.stderr.write_text(stderr)
        p = subprocess.run([sys.executable, str(Path(drain.__file__)), '--dir', str(self.directory), '--token', 'test',
                            'check-result', '--key', 'PROJ-1449', '--result', str(self.result),
                            '--stderr', str(self.stderr), *extra], capture_output=True, text=True)
        return p.returncode, json.loads(p.stdout)

    def test_sol_refusal_retries_once_with_opus(self):
        code, out = self.check('Using workspace wks\n' + self.REFUSAL, '--requirements', str(self.directory / 'required.json'))
        self.assertEqual(code, 4)
        self.assertEqual((out['failureType'], out['retryWorker'], out['retryAllowed']), ('model-refusal', 'opus', True))
        self.assertFalse(out['stageComplete'])
        self.assertNotIn('queueWaiting', out)
        self.assertNotIn('backoffUntil', json.loads((self.directory / 'followup-queue.json').read_text())['PROJ-1449'])
        code, out = self.check(self.REFUSAL)
        self.assertEqual((code, out['failureType'], out['retryAllowed']), (0, 'model-refusal', True))

    def test_opus_retry_failure_is_human_risk(self):
        code, out = self.check('opus: worker failed', '--requirements', str(self.directory / 'required.json'))
        self.assertEqual((code, out['retryAllowed'], out['queueWaiting']), (4, False, 'human-risk'))
        self.assertNotIn('failureType', out)
        for text in ('OUTPUT_SCHEMA_FAILED: Selected model is at capacity', 'cybersecurity review passed'):
            self.stderr.write_text(text)
            self.assertFalse(drain.model_refused(self.stderr))


if __name__ == '__main__':
    unittest.main()
