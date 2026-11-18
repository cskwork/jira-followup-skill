"""'[공장 수정]' 추적의 할 일 실패가 실행 잠금 회수·결정 수신기를 막지 않는다(2026-11-10 회고).

run_lock.busy는 잃어버린 실행을 lessons.append로 기록한 뒤 clear()로 잠금을 푼다. 추적이 할 일 CLI를 부르다 실패해
예외가 나오면 잠금이 영원히 남아 공장이 다시 깨지 않는다. 할 일 CLI는 가짜 sdlc-kit(node 스크립트)으로 실패시킨다:
비정상 종료, 시간 초과, 실행 자체 실패(OSError)."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_inbox  # noqa: E402
import decision_replies  # noqa: E402
import lessons  # noqa: E402
from test_run_lock import LOCK, LivenessFixture  # noqa: E402

TZ = ZoneInfo('Asia/Seoul')
# list는 빈 목록으로 답하고(수신기가 돌게), 그 밖(add·wait·note)은 FAKE_TODO에 따라 실패한다.
FAKE = """const [command] = process.argv.slice(2);
if (command === 'list') { console.log('[]'); process.exit(0); }
if (process.env.FAKE_TODO === 'ok') { console.log('fx1'); process.exit(0); }
if (process.env.FAKE_TODO === 'hang') setTimeout(() => {}, 600000);
else { console.error('fake to-do down'); process.exit(3); }
"""


class TrackingFailure(LivenessFixture):
    def setUp(self):
        super().setUp()
        kit = self.root / 'kit'
        (kit / 'todo').mkdir(parents=True)
        (kit / 'todo/todo.mjs').write_text(FAKE)
        patch.object(decision_inbox, 'SDLC_KIT', kit).start()
        patch.dict(os.environ, JIRA_DECISION_SURFACE='app').start()
        patch.object(lessons, 'TODO_TIMEOUT', 2, create=True).start()
        self.sig = lessons.signature({'kind': 'infra_fail', 'what': 'lost-run: old'})
        # 하루 전에 fix-now로 정했고 아직 '[공장 수정]' 항목이 없다(D67·D68처럼). 다음 lesson이 항목을 만들려 한다.
        stamp = (datetime.now(TZ) - timedelta(days=1)).isoformat()
        (self.state / 'lessons.jsonl').write_text(json.dumps(
            {'at': stamp, 'kind': 'infra_fail', 'what': 'lost-run: old', 'signature': self.sig}) + '\n')
        pages = self.state / 'decision-pages'
        pages.mkdir()
        (pages / 'codes.json').write_text(json.dumps({'D1': {
            'name': 'lesson-' + self.sig, 'kind': 'choice', 'key': 'FACTORY', 'state': 'decided',
            'createdAt': stamp, 'terminalAt': stamp, 'messageId': 'todo:x:1'}}))
        (self.state / 'decisions.jsonl').write_text(json.dumps({'option': 'fix-now', 'messageId': 'todo:x:1'}) + '\n')

    def fail_todo(self, how):
        if how == 'raise':
            patch.object(decision_inbox, 'todo_cli', side_effect=OSError('node not found')).start()
        else:
            patch.dict(os.environ, FAKE_TODO=how).start()

    def lessons_rows(self):
        return [json.loads(line) for line in (self.state / 'lessons.jsonl').read_text().splitlines()]

    def test_lost_run_reclaim_clears_lock_when_todo_fails(self):
        for how in ('exit', 'hang', 'raise'):
            with self.subTest(how=how):
                patch.stopall()
                self.setUp()
                self.fail_todo(how)
                self.lock(orchestratorId=None, startedAt=int(time.time()) - 700)
                started = time.monotonic()
                self.assertFalse(self.busy())  # 예외 없이 회수한다.
                self.assertLess(time.monotonic() - started, 30)
                self.assertFalse((self.state / 'running.json').exists())
                self.assertFalse((self.state / 'running').exists())
                self.assertEqual(self.lessons_rows()[-1]['what'], 'lost-run: old')
                # 실패는 숨기지 않는다: 운영자가 보는 파일에 남고, 다음 기록 때 다시 시도한다.
                error = json.loads((self.state / lessons.TRACKING_ERROR).read_text())
                self.assertIn('error', error['lessons.append'])
                self.assertFalse((self.state / lessons.FIXES).exists())

    def test_lesson_result_reports_tracking_error_and_next_success_clears_it(self):
        self.fail_todo('exit')
        result = lessons.append(self.state, {'kind': 'infra_fail', 'what': 'lost-run: again'})
        self.assertIn('fake to-do down', result['trackingError'])
        self.assertIn('lessons.append', json.loads((self.state / lessons.TRACKING_ERROR).read_text()))
        # 할 일이 돌아오면 다음 lesson이 같은 추적을 다시 하고 실패 기록을 지운다.
        patch.dict(os.environ, FAKE_TODO='ok').start()
        result = lessons.append(self.state, {'kind': 'infra_fail', 'what': 'lost-run: again'})
        self.assertNotIn('trackingError', result)
        self.assertEqual(json.loads((self.state / lessons.FIXES).read_text())[self.sig]['todoId'], 'fx1')
        self.assertFalse((self.state / lessons.TRACKING_ERROR).exists())

    def test_receiver_poll_survives_todo_failure_in_tracking(self):
        for how in ('exit', 'hang', 'raise'):
            with self.subTest(how=how):
                patch.stopall()
                self.setUp()
                self.fail_todo(how)
                patch.object(decision_inbox, 'judge_setting').start()  # 설정 항목은 이 시험 대상이 아니다.
                if how == 'raise':  # list까지 실패하면 수신 자체가 안 되므로 추적만 실패시킨다.
                    patch.object(decision_inbox, 'todo_cli', side_effect=lambda args, timeout=60: [] if args[0] == 'list'
                                 else (_ for _ in ()).throw(OSError('node not found'))).start()
                self.assertEqual(decision_inbox.poll(self.state), [])
                self.assertTrue((self.state / lessons.TRACKING_ERROR).exists())
                decision_replies.poll(self.state)  # 수신기 한 회차도 끝난다.


if __name__ == '__main__':
    unittest.main()
