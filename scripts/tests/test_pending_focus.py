"""잠금 중 수동·결정 페이지 요청은 외부 호출 없이 pending focus에 보존한다."""
from datetime import date
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import followup_idle as idle
import morning_check as morning


class PendingFocusIdleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.pending = self.directory / 'pending-focus.jsonl'
        self.scan = self.directory / 'scan.py'
        self.scan.write_text('import pathlib,sys\npathlib.Path(sys.argv[-1]).write_text(\'{"events":[],"errors":[]}\')\n')

    def decide(self, blocked, reconcile=()):
        with patch.object(idle, 'blocked_by', side_effect=lambda directory, key: blocked.get(key, [])), \
                patch.object(idle, 'reconcile_reasons', return_value=list(reconcile)):
            return idle.decide(self.directory, [sys.executable, str(self.scan)], [])

    def test_all_blocked_pending_keys_are_report_only_and_preserved(self):
        original = '{"key":"PROJ-1414"}\n{"focus":"PROJ-1423"}\n{"key":"PROJ-1414"}\n'
        self.pending.write_text(original)
        result, code = self.decide({'PROJ-1414': ['lost'], 'PROJ-1423': ['held']})
        self.assertEqual(code, 0)
        self.assertTrue(result['idle'])
        self.assertIn('pending-focus-blocked:PROJ-1414,PROJ-1423', result['reasons'])
        self.assertNotIn('pending-focus', result['reasons'])
        self.assertEqual(result['blockedKeys'], {'PROJ-1414': ['lost'], 'PROJ-1423': ['held']})
        self.assertEqual(self.pending.read_text(), original)
        self.assertEqual(json.loads((self.directory / 'guard-last.json').read_text()), result)

    def test_unblocked_pending_key_still_wakes_and_reports_blocked_keys(self):
        self.pending.write_text('{"key":"PROJ-1414"}\n{"key":"PROJ-1423"}\n')
        result, code = self.decide({'PROJ-1414': ['lost']})
        self.assertEqual(code, 1)
        self.assertIn('pending-focus', result['reasons'])
        self.assertIn('pending-focus-blocked:PROJ-1414', result['reasons'])

    def test_reconciliation_still_wakes_with_all_pending_keys_blocked(self):
        self.pending.write_text('{"key":"PROJ-1414"}\n')
        result, code = self.decide({'PROJ-1414': ['lost']}, ['reconcile:lost'])
        self.assertEqual(code, 1)
        self.assertIn('reconcile:lost', result['reasons'])

    def test_malformed_pending_focus_is_an_explicit_internal_error(self):
        for payload in ('{', '[]\n', 'null\n', '{"key":null}\n', '{"key":""}\n'):
            with self.subTest(payload=payload):
                self.pending.write_text(payload)
                result, code = self.decide({})
                self.assertEqual(code, 2)
                self.assertIn('internal-error', result['reasons'])

    def test_morning_report_surfaces_blocked_pending_keys_as_needs_action(self):
        (self.directory / 'guard-last.json').write_text(json.dumps({
            'reasons': ['pending-focus-blocked:PROJ-1414,PROJ-1423']}))
        now = '2026-10-28T08:00:00+09:00'
        instance = morning.Morning(self.directory, self.directory, self.directory / 'out')
        report, moves, needs, pending = instance.report({
            'issues': [], 'repoFetch': [], 'startedAt': now, 'since': now}, [], {})
        self.assertIn('pending-focus-blocked:PROJ-1414,PROJ-1423', report)
        self.assertIn('조치 필요', report)
        self.assertEqual((moves, needs, pending), (0, 1, []))


class PendingFocusWakeTests(unittest.TestCase):
    def test_locked_manual_focus_is_queued_in_the_selected_state_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            guard = directory / 'guard'
            guard.write_text('#!/bin/bash\necho "jira-daily-guard: skipping duplicate (running)"\n')
            state = directory / 'custom state'
            env = dict(os.environ, JIRA_WAKE_GUARD=str(guard))
            for _ in range(2):
                result = subprocess.run(['/bin/bash', str(ROOT / 'jira-daily-wake'), '--now',
                                         '--key', 'PROJ-1404', '--dir', str(state)],
                                        capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'jira-daily-guard: skipping duplicate (running)')
            rows = [json.loads(line) for line in (state / 'pending-focus.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row['key'] == 'PROJ-1404' and row['trigger'] == 'manual' for row in rows))

    def test_idle_skip_does_not_enqueue_an_unrelated_focus(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            guard = directory / 'guard'
            guard.write_text('#!/bin/bash\necho "jira-daily-guard: skipping duplicate (idle: events=0, no runnable work)"\n')
            result = subprocess.run(['/bin/bash', str(ROOT / 'jira-daily-wake'), '--now',
                                     '--key', 'PROJ-1404', '--dir', str(directory)],
                                    capture_output=True, text=True,
                                    env=dict(os.environ, JIRA_WAKE_GUARD=str(guard)), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((directory / 'pending-focus.jsonl').exists())

    def test_real_guard_wakes_on_pending_focus_with_no_events_or_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / '.local/bin'
            binary.mkdir(parents=True)
            once = binary / 'paseo-once'
            once.write_text('#!/bin/sh\necho run\n')
            once.chmod(0o755)
            state = directory / 'state'
            state.mkdir()
            (state / 'done-today').write_text(date.today().isoformat())
            (state / 'pending-focus.jsonl').write_text('{"key":"PROJ-1404"}\n')
            scan = directory / 'scan.py'
            scan.write_text('import pathlib,sys\npathlib.Path(sys.argv[-1]).write_text(\'{"events":[],"errors":[]}\')\n')
            result = subprocess.run(['/bin/bash', str(ROOT / 'jira-daily-guard'), '--dir', str(state)],
                                    env=dict(os.environ, HOME=str(directory),
                                             FOLLOWUP_IDLE_SCAN_CMD=shlex.join([sys.executable, str(scan)])),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('mode=tick', result.stdout)
            self.assertTrue((state / 'running.json').exists())
            self.assertIn('pending-focus', json.loads((state / 'guard-last.json').read_text())['reasons'])
