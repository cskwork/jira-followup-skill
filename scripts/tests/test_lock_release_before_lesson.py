"""잃어버린 실행·임대는 교훈을 쓰기 전에 푼다(2026-11-10 회고).

lessons.append는 교훈 줄을 쓴 뒤 결정을 열다 실패하면 예외를 올린다(설계). 그 예외가 잠금 해제보다 먼저 나면
running.json·임대 파일이 남아 다음 guard가 계속 '실행 중'으로 보고 공장이 다시 깨지 않는다.
여기서는 lessons.append 자체를 실패시켜, 예외는 그대로 올라오되 잠금·임대는 이미 풀려 있음을 확인한다."""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lessons  # noqa: E402
from test_run_lock import LOCK, LivenessFixture  # noqa: E402


class ReleaseBeforeLesson(LivenessFixture):
    def lease(self, key, token):
        LOCK.atomic_json(self.state / 'tickets' / (key + '.json'), dict(
            key=key, token=token, parent='global', startedAt=time.time() - 700,
            deadline=time.time() + 9900, state='launching', orchestratorId=None))
        LOCK.run_path(self.state, token).mkdir(parents=True, exist_ok=True)

    def test_lost_run_lock_is_cleared_even_when_the_lesson_fails(self):
        self.lock(orchestratorId=None, startedAt=int(time.time()) - 700)
        with patch.object(lessons, 'append', side_effect=RuntimeError('decision surface down')) as append:
            with self.assertRaisesRegex(RuntimeError, 'decision surface down'):
                self.busy()
        append.assert_called_once()
        self.assertFalse((self.state / 'running.json').exists())
        self.assertFalse((self.state / 'running').exists())
        # 다음 guard는 잠금이 없는 정상 상태를 본다.
        self.assertFalse(self.busy())

    def test_every_lost_lease_is_released_and_the_first_lesson_failure_is_raised(self):
        self.lock('global')
        self.lease('PROJ-1', 'ticket-1')
        self.lease('PROJ-2', 'ticket-2')
        errors = [RuntimeError('first failure'), RuntimeError('second failure')]
        with patch.object(lessons, 'append', side_effect=errors) as append:
            with self.assertRaisesRegex(RuntimeError, 'first failure'):
                LOCK.lease_sweep(self.state)
        self.assertEqual(append.call_count, 2)
        self.assertEqual(LOCK.lease_list(self.state), [])

    def test_lost_lease_sweep_still_returns_the_keys_when_lessons_work(self):
        self.lock('global')
        self.lease('PROJ-1', 'ticket-1')
        with patch.object(lessons, 'append') as append:
            self.assertEqual(LOCK.lease_sweep(self.state), ['PROJ-1'])
        append.assert_called_once()
        self.assertEqual(LOCK.lease_list(self.state), [])


if __name__ == '__main__':
    unittest.main()
