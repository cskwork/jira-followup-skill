import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requirements  # noqa: E402


class RequirementsFieldTests(unittest.TestCase):
    def evaluate(self, environment, saved):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'check-deploy.json').write_text(json.dumps(saved))
            manifest = {'key': 'PROJ-1', 'stage': 'dev-deployed', 'checks': [
                {'name': 'deploy', 'artifact': 'check-deploy.json', 'resultField': 'status',
                 'environment': environment}]}
            result = {'key': 'PROJ-1', 'stage': 'dev-deployed', 'status': 'done', 'waiting': None,
                      'checks': [{'name': 'deploy', 'result': 'PASS'}], 'artifacts': ['check-deploy.json']}
            return requirements.evaluate({'manifest': manifest, 'base': str(base)}, result)

    def test_missing_environment_field_names_the_field(self):
        gate = self.evaluate({'key': 'PROJ-1'}, {'status': 'PASS', 'job': 'web-dev'})
        self.assertEqual(gate['failures'][0]['reason'], 'verifier-field-missing:key')

    def test_present_fields_pass(self):
        gate = self.evaluate({'key': 'PROJ-1', 'context': 'dev'},
                             {'status': 'PASS', 'key': 'PROJ-1', 'context': 'dev'})
        self.assertTrue(gate['passed'], gate)


if __name__ == '__main__':
    unittest.main()
