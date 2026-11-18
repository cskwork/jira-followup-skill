import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker_runtime  # noqa: E402


class LeftoverServerTests(unittest.TestCase):
    def test_stops_only_this_tickets_worktree_listeners_started_during_attempt(self):
        since = time.time() - 100
        rows = [
            (1, 50, 'node /w/product/worktrees/PROJ-1470-web/node_modules/.bin/vite preview'),
            (2, 50, 'node /w/product/.scratch/proj-1470-eval/web/node_modules/.bin/vite'),
            (3, 50, 'node /w/product/worktrees/PROJ-1449-api/gradlew bootRun'),
            (4, 500, 'node /w/product/worktrees/PROJ-1470-web/node_modules/.bin/vite'),
            (5, 50, 'node /w/product/web/node_modules/.bin/vite'),
            (6, 50, 'node /w/product/worktrees/PROJ-14700-x/vite'),
        ]
        sent = []
        stopped = worker_runtime.stop_leftover_servers(
            Path('/w/product'), 'PROJ-1470', since, processes=lambda: rows,
            signal_process=lambda pid, sig: sent.append((pid, sig)))
        self.assertEqual(sent, [(1, signal.SIGTERM), (2, signal.SIGTERM)])
        self.assertEqual([row['pid'] for row in stopped], [1, 2])

    def test_elapsed_parses_ps_etime(self):
        self.assertEqual(worker_runtime.elapsed_seconds('05'), 5)
        self.assertEqual(worker_runtime.elapsed_seconds('01:02'), 62)
        self.assertEqual(worker_runtime.elapsed_seconds('2-01:00:00'), 2 * 86400 + 3600)

    def test_listing_spawns_one_ps_for_all_listeners(self):
        calls = []

        def run(argv, **_):
            calls.append(argv)
            if argv[0] == 'lsof':
                return subprocess.CompletedProcess(argv, 0, ''.join(f'p{n}\nf3\n' for n in range(100, 180)), '')
            return subprocess.CompletedProcess(argv, 1, '  100    01:02 node /w/product/worktrees/PROJ-1/vite --port 5\n'
                                                     '  101 2-01:00:00 java -jar app.jar\n', '')
        rows = list(worker_runtime.listening_processes(run))
        self.assertEqual(rows, [(100, 62, 'node /w/product/worktrees/PROJ-1/vite --port 5'),
                                (101, 2 * 86400 + 3600, 'java -jar app.jar')])
        self.assertEqual([c[0] for c in calls], ['lsof', 'ps'])
        self.assertEqual(calls[1][-1], ','.join(str(n) for n in range(100, 180)))


if __name__ == '__main__':
    unittest.main()
