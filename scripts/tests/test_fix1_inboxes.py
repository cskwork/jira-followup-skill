"""손상된 inbox 줄은 보존·기록하고 정상 이벤트만 가드를 깨운다."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import followup_idle as idle


class InvalidInboxTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        for name, replacement in (
                ('run', self.command), ('lease_list', lambda *_: []),
                ('reconcile_reasons', lambda *_: []), ('blocked_by', lambda *_: [])):
            mocked = patch.object(idle, name, side_effect=replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.notify = patch.object(idle, 'notify').start()
        self.addCleanup(patch.stopall)

    def command(self, argv):
        self.assertEqual(argv[0], 'scan-stub', '외부 명령 실행 금지')
        Path(argv[argv.index('--out') + 1]).write_text(
            json.dumps({'events': [], 'errors': []}), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def inbox(self, content):
        path = self.directory / 'tickets' / 'PROJ-1.inbox.jsonl'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return path

    def decide(self):
        return idle.decide(self.directory, ['scan-stub'], ['preflight-forbidden'])

    def alerts(self):
        return [line for path in self.directory.glob('followup-*.md')
                for line in path.read_text(encoding='utf-8').splitlines()
                if 'liveness-alert: invalid tickets/PROJ-1.inbox.jsonl' in line]

    def test_malformed_only_inbox_stays_idle_and_alerts_once_across_ticks(self):
        content = b'{bad json\r\n'
        path = self.inbox(content)
        for _ in range(3):
            result, code = self.decide()
            self.assertEqual((code, result['idle']), (0, True))
            self.assertNotIn('internal-error', result['reasons'])
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(len(self.alerts()), 1)
        self.notify.assert_not_called()

    def test_invalid_rows_are_alerted_individually_and_handled_rows_stay_idle(self):
        rows = [None, [], 1, 'bad', {}, {'commentId': None},
                {'commentId': 7}, {'commentId': ''}, {'commentId': 'handled'}]
        content = ''.join(json.dumps(row) + '\n' for row in rows).encode()
        path = self.inbox(content)
        (self.directory / 'state.json').write_text(json.dumps({
            'followup': {'handled': {'handled': {'at': 'fixture'}}}}), encoding='utf-8')
        for _ in range(3):
            result, code = self.decide()
            self.assertEqual((code, result['idle']), (0, True))
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(len(self.alerts()), 8)
        for number in range(1, 9):
            self.assertEqual(sum(f' line {number} |' in row for row in self.alerts()), 1)
        self.notify.assert_not_called()

    def test_bad_rows_do_not_hide_following_unhandled_event_or_other_inbox(self):
        content = b'{bad\n{bad\n{"commentId":null}\n{"commentId":"new"}\n'
        path = self.inbox(content)
        other = path.with_name('PROJ-2.inbox.jsonl')
        other.write_text('{"commentId":"second"}\n', encoding='utf-8')
        for _ in range(2):
            result, code = self.decide()
            self.assertEqual(code, 1)
            self.assertNotIn('internal-error', result['reasons'])
            self.assertIn('ticket-inbox:PROJ-1', result['reasons'])
            self.assertIn('ticket-inbox:PROJ-2', result['reasons'])
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(len(self.alerts()), 3)
        self.notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
