"""Event judgement completion is independent of the resulting queue wait."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requirements


class EventRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.manifest = {'key': 'PROJ-1505', 'stage': 'intake', 'checks': [
            {'name': 'judgement', 'artifact': 'judge.json', 'resultField': 'status',
             'environment': {'key': 'PROJ-1505'}}],
            'event': {'commentIds': ['intake:PROJ-1505', 'todo:1454'],
                      'stateArtifact': 'state.json', 'queueArtifact': 'queue.json'}}
        self.save('judge.json', {'status': 'PASS', 'key': 'PROJ-1505'})
        self.save('state.json', {'followup': {'handled': {
            identity: {'key': 'PROJ-1505', 'action': 'queued', 'at': '2026-11-05T09:25:00+09:00'}
            for identity in self.manifest['event']['commentIds']}}})
        self.save('queue.json', {'PROJ-1505': {'stage': 'reproduce', 'waiting': 'reporter'}})
        self.result = {'key': 'PROJ-1505', 'stage': 'intake', 'status': 'done', 'waiting': 'reporter',
                       'checks': [{'name': 'judgement', 'result': 'PASS'}],
                       'artifacts': ['judge.json', 'state.json', 'queue.json']}

    def save(self, name, value):
        (self.base / name).write_text(json.dumps(value))

    def gate(self):
        return requirements.evaluate({'manifest': self.manifest, 'base': str(self.base)}, self.result)

    def test_manifest_explicitly_declares_event_without_changing_legacy_schema(self):
        self.save('requirements.json', self.manifest)
        self.assertEqual(requirements.snapshot(self.base / 'requirements.json')['manifest'], self.manifest)

    def test_1105_handled_judgements_pass_with_remaining_wait(self):
        for waiting in ('reporter', 'human-risk', 'env-human', 'stg-approval', None):
            with self.subTest(waiting=waiting):
                self.result['waiting'] = waiting
                self.assertTrue(self.gate()['passed'], self.gate())

    def test_1505_intake_can_report_the_recorded_queue_stage(self):
        self.result['stage'] = 'reproduce'
        self.assertTrue(self.gate()['passed'], self.gate())
        self.result['stage'] = 'build'
        self.assertIn({'name': 'identity', 'reason': 'key-or-stage-mismatch'}, self.gate()['failures'])

    def test_every_declared_event_must_be_handled_for_the_same_key(self):
        for row in (None, {}, {'key': 'PROJ-1501', 'action': 'queued', 'at': 'today'}):
            with self.subTest(row=row):
                self.save('state.json', {'followup': {'handled': {'intake:PROJ-1505': row}}})
                self.assertIn({'name': 'completion', 'reason': 'event-not-handled'}, self.gate()['failures'])

    def test_unlisted_or_unreadable_handled_evidence_cannot_pass(self):
        self.result['artifacts'].remove('state.json')
        self.assertFalse(self.gate()['passed'])
        self.result['artifacts'].append('state.json')
        (self.base / 'state.json').write_text('invalid')
        self.assertFalse(self.gate()['passed'])

    def test_non_events_keep_strict_completion_and_stage(self):
        del self.manifest['event']
        self.assertIn({'name': 'completion', 'reason': 'not-complete'}, self.gate()['failures'])
        self.result['waiting'] = None
        self.result['stage'] = 'reproduce'
        self.assertFalse(self.gate()['passed'])

    def test_events_still_require_done_and_all_original_checks(self):
        self.result['status'] = 'waiting'
        self.assertFalse(self.gate()['passed'])
        self.result['status'] = 'done'
        self.result['checks'][0]['result'] = 'FAIL'
        self.assertFalse(self.gate()['passed'])

    def test_event_declaration_cannot_have_empty_or_duplicate_ids(self):
        for ids in ([], ['x', 'x'], [''], 'x'):
            with self.subTest(ids=ids):
                manifest = copy.deepcopy(self.manifest)
                manifest['event']['commentIds'] = ids
                self.save('requirements.json', manifest)
                with self.assertRaises(ValueError):
                    requirements.snapshot(self.base / 'requirements.json')
