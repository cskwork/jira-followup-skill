"""작업자가 도는 동안 할 일 항목에 '작업 중' 표시(sdlc-kit `todo working`)를 유지하고 끝나면 끈다. todo CLI는 가짜다."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker_runtime  # noqa: E402

ROWS = [
    {'id': 'tkt', 'title': 'PROJ-1438 학생 화면 오류', 'status': 'active', 'link': {'label': 'PROJ-1438'}},
    {'id': 'old', 'title': 'PROJ-1438 지난 항목', 'status': 'done'},
    {'id': 'dec', 'title': '[결정 D62] PROJ-1438 · 무엇을 할까요', 'status': 'active'},
    {'id': 'stg', 'title': '[스테이징 승인] PROJ-1438 · 스테이징 배포', 'status': 'active'},
    {'id': 'other', 'title': 'PROJ-1439 다른 티켓', 'status': 'active'},
]


class FakeTodo:
    def __init__(self, rows=ROWS, fail=()):
        self.rows, self.fail, self.calls = rows, set(fail), []
        self.lock = threading.Lock()

    def __call__(self, args, timeout):
        with self.lock:
            self.calls.append((list(args), timeout))
        if args[0] in self.fail:
            raise RuntimeError(f'unknown command: {args[0]}')
        return json.dumps(self.rows) if args[0] == 'list' else ''

    def working(self):
        with self.lock:
            return [args for args, _ in self.calls if args[0] == 'working']


def until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TodoHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stderr = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stderr(self.stderr))
        stack.enter_context(mock.patch.object(worker_runtime.TodoHeartbeat, 'INTERVAL', 0.05))
        self.addCleanup(stack.close)
        self.addCleanup(self.tmp.cleanup)

    def run_worker(self, todo, code='', *, key='PROJ-1438', timeout=None):
        made = []

        def heartbeat(k, unit):
            made.append((k, unit))
            return worker_runtime.TodoHeartbeat(k, unit, cli=todo)
        attempt = worker_runtime.prepare(self.root)
        with contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO())):
            result = worker_runtime.execute(attempt, [sys.executable, '-c', code], key=key, timeout=timeout,
                                            unit='PROJ-1438 build', heartbeat=heartbeat)
        return result, made

    def test_marks_open_ticket_items_refreshes_and_clears_on_success(self):
        todo = FakeTodo()
        code, _ = self.run_worker(todo, 'import time; time.sleep(0.4)')
        self.assertEqual(code, 0)
        working = todo.working()
        self.assertEqual(working[0], ['working', 'tkt', 'build'])
        beats = [args for args in working if args == ['working', 'tkt', 'build']]
        self.assertGreaterEqual(len(beats), 2, working)
        self.assertEqual(working[-1], ['working', 'tkt', '--off'])
        self.assertFalse({args[1] for args in working} - {'tkt'}, 'done·결정·다른 키 항목은 표시하지 않는다')
        self.assertEqual([t for args, t in todo.calls if args[0] == 'list'], [30])
        self.assertTrue(all(t <= 10 for args, t in todo.calls if args[0] == 'working'))
        self.assertEqual(self.stderr.getvalue(), '')

    def test_clears_after_failure_and_timeout(self):
        for script, timeout, expected in (('import time; time.sleep(0.3); raise SystemExit(7)', None, 7),
                                          ('import time; time.sleep(10)', 0.5, 124)):
            with self.subTest(expected=expected):
                todo = FakeTodo()
                code, _ = self.run_worker(todo, script, timeout=timeout)
                self.assertEqual(code, expected)
                self.assertEqual(todo.working()[-1], ['working', 'tkt', '--off'])

    def test_clears_when_launch_fails(self):
        todo = FakeTodo()
        attempt = worker_runtime.prepare(self.root)
        pulse = worker_runtime.TodoHeartbeat('PROJ-1438', 'PROJ-1438 build', cli=todo)

        def popen(*_args, **_kwargs):
            until(todo.working)
            raise OSError('no such file')
        with mock.patch.object(worker_runtime.subprocess, 'Popen', side_effect=popen):
            code = worker_runtime.execute(attempt, ['missing'], key='PROJ-1438', heartbeat=lambda k, u: pulse)
        self.assertEqual(code, 127)
        self.assertEqual(todo.working()[-1], ['working', 'tkt', '--off'])

    def test_no_key_runs_no_heartbeat(self):
        made = []
        attempt = worker_runtime.prepare(self.root)
        with contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO())):
            code = worker_runtime.execute(attempt, [sys.executable, '-c', ''], key=None,
                                          heartbeat=lambda k, u: made.append(k))
        self.assertEqual(code, 0)
        self.assertEqual(made, [])

    def test_unknown_working_command_is_logged_once_and_not_retried(self):
        todo = FakeTodo(fail={'working'})
        code, _ = self.run_worker(todo, 'import time; time.sleep(0.3)')
        self.assertEqual(code, 0)
        self.assertEqual(todo.working(), [['working', 'tkt', 'build']], '실패하면 갱신·끄기를 다시 부르지 않는다')
        self.assertEqual(self.stderr.getvalue().count('todo working'), 1)
        self.assertIn('unknown command', self.stderr.getvalue())

    def test_list_failure_does_not_affect_worker(self):
        todo = FakeTodo(fail={'list'})
        code, _ = self.run_worker(todo, 'raise SystemExit(3)')
        self.assertEqual(code, 3)
        self.assertEqual(todo.working(), [])
        self.assertEqual(self.stderr.getvalue().count('todo working list skipped'), 1)

    def test_slow_list_does_not_delay_worker_end(self):
        gate = threading.Event()

        def slow(args, timeout):
            gate.wait(5)
            return json.dumps(ROWS)
        pulse = worker_runtime.TodoHeartbeat('PROJ-1438', 'x', cli=slow).start()
        began = time.monotonic()
        pulse.stop()
        self.assertLess(time.monotonic() - began, 1)
        gate.set()
        pulse.thread.join(2)
        self.assertFalse(pulse.marked, '찾기가 끝난 뒤에도 멈춘 작업자에 표시하지 않는다')

    def test_slugged_item_is_marked_by_slug(self):
        rows = [dict(ROWS[0], slug='proj-1438-fix'), *ROWS[1:]]
        todo = FakeTodo(rows)
        code, _ = self.run_worker(todo, 'import time; time.sleep(0.2)')
        self.assertEqual(code, 0)
        self.assertEqual(todo.working()[0], ['working', 'proj-1438-fix', 'build'])
        self.assertEqual(todo.working()[-1], ['working', 'proj-1438-fix', '--off'])

    def test_missing_item_is_re_resolved_at_most_once_per_ten_minutes(self):
        """작업자가 시도 중에 항목에 slug를 붙이거나 제목을 바꾸면 옛 id는 'item not found'다."""
        now = [0.0]
        rows = [dict(ROWS[0])]

        class Moving(FakeTodo):
            ghosts = frozenset()

            def __call__(self, args, timeout):
                result = super().__call__(args, timeout)
                if args[0] == 'working' and (args[1] in self.ghosts
                                             or args[1] not in {r.get('slug') or r['id'] for r in self.rows}):
                    raise RuntimeError(f'item not found: {args[1]}')
                return result
        todo = Moving(rows)
        pulse = worker_runtime.TodoHeartbeat('PROJ-1438', 'PROJ-1438 build', cli=todo, clock=lambda: now[0])
        pulse.ids = pulse.item_ids()
        self.assertTrue(pulse.beat())

        def lists():
            return sum(1 for args, _ in todo.calls if args[0] == 'list')
        rows[0] = dict(rows[0], id='new', slug='proj-1438-fix')  # 작업자가 sdlc start --slug를 했다
        now[0] = 120
        self.assertTrue(pulse.beat(), '처음 못 찾으면 바로 다시 읽는다(180초 만료 전에 갱신)')
        self.assertEqual(lists(), 2)
        self.assertEqual(todo.working()[-1], ['working', 'proj-1438-fix', 'build'])
        todo.ghosts = frozenset({'newer'})  # 목록에는 있지만 working이 못 찾는다(목록과 쓰기 사이 경합)
        rows[0] = dict(rows[0], slug=None, id='newer')
        now[0] = 180
        self.assertTrue(pulse.beat())
        self.assertEqual(lists(), 3)
        now[0] = 600
        self.assertTrue(pulse.beat())
        self.assertEqual(lists(), 3, '다시 읽어도 못 찾았으면 10분 동안 다시 읽지 않는다')
        todo.ghosts = frozenset()
        now[0] = 780
        self.assertTrue(pulse.beat())
        self.assertEqual(lists(), 3, '경합이 풀리면 같은 식별자로 갱신된다')
        self.assertEqual(todo.working()[-1], ['working', 'newer', 'build'])
        pulse.stop()
        self.assertEqual(todo.working()[-1], ['working', 'newer', '--off'], '끄기는 마지막으로 찾은 식별자를 쓴다')
        self.assertEqual(self.stderr.getvalue(), '', 'not found는 실패로 기록하지 않는다')

    def test_lock_timeout_on_re_resolve_keeps_beating(self):
        """실제 통합 실행에서 다시 읽기가 할 일 잠금 대기로 실패하자 박동이 멈춰 표시가 꺼졌다."""
        now = [0.0]
        rows = [dict(ROWS[0])]
        locked = [False]

        class Busy(FakeTodo):
            def __call__(self, args, timeout):
                if args[0] == 'list' and locked[0]:
                    super().__call__(args, timeout)
                    raise RuntimeError('timed out waiting for lock: todo.md.lock')
                result = super().__call__(args, timeout)
                if args[0] == 'working' and args[1] not in {r.get('slug') or r['id'] for r in self.rows}:
                    raise RuntimeError(f'item not found: {args[1]}')
                return result
        todo = Busy(rows)
        pulse = worker_runtime.TodoHeartbeat('PROJ-1438', 'PROJ-1438 build', cli=todo, clock=lambda: now[0])
        pulse.ids = pulse.item_ids()
        rows[0] = dict(rows[0], id='new', slug='proj-1438-fix')
        locked[0] = True
        now[0] = 60
        self.assertTrue(pulse.beat(), '잠금 대기는 일시적이라 박동을 이어 간다')
        locked[0] = False
        now[0] = 120
        self.assertTrue(pulse.beat())
        self.assertEqual(todo.working()[-1], ['working', 'proj-1438-fix', 'build'])

    def test_transient_mark_error_keeps_beating(self):
        todo = FakeTodo([dict(ROWS[0])])
        calls = [0]
        real = todo.__call__

        def flaky(args, timeout):
            if args[0] == 'working':
                calls[0] += 1
                if calls[0] == 1:
                    raise worker_runtime.subprocess.TimeoutExpired(args, timeout)
            return real(args, timeout)
        pulse = worker_runtime.TodoHeartbeat('PROJ-1438', 'PROJ-1438 build', cli=flaky)
        pulse.ids = pulse.item_ids()
        self.assertTrue(pulse.beat(), '한 번의 시간 초과로 멈추지 않는다')
        self.assertTrue(pulse.beat())
        self.assertEqual(calls[0], 2)

    def test_cli_uses_kit_path_timeout_and_first_error_line(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs['timeout']))
            return worker_runtime.subprocess.CompletedProcess(argv, 2, '', 'unknown command: working\nUsage: ...\n')
        with mock.patch.dict('os.environ', {'SDLC_KIT': '/kit'}), \
                mock.patch.object(worker_runtime.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, r'^unknown command: working$'):
                worker_runtime.todo_cli(['working', 'tkt', 'build'], 10)
        self.assertEqual(calls, [(['node', '/kit/todo/todo.mjs', 'working', 'tkt', 'build'], 10)])

    def test_label_drops_key_and_fits_one_short_line(self):
        self.assertEqual(worker_runtime.working_label('PROJ-1438 build', 'PROJ-1438'), 'build')
        self.assertEqual(worker_runtime.working_label('PROJ-1438', 'PROJ-1438'), 'PROJ-1438')
        self.assertEqual(worker_runtime.working_label('morning\ncheck', None), 'morning check')
        self.assertEqual(len(worker_runtime.working_label('PROJ-1 ' + 'x' * 80, 'PROJ-1')), 40)


if __name__ == '__main__':
    unittest.main()
