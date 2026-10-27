"""Saved Jenkins and Deployment fixtures; no cluster or Jenkins access."""

import copy
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest

import test_checks as fixtures


class ServingChecks(fixtures.GitFixture):
    def setUp(self):
        super().setUp()
        self.expect = self.commit('fix: serving fixture [PROJ-1388]')
        self.run = {'number': 662, 'status': 'completed', 'result': 'SUCCESS',
                    'scm': {'commit': self.expect}}
        self.deployment = {
            'apiVersion': 'apps/v1', 'kind': 'Deployment',
            'metadata': {'name': 'core-api', 'namespace': 'app', 'generation': 3},
            'spec': {'replicas': 2, 'template': {'spec': {'containers': [
                {'name': 'core-api', 'image': 'registry.invalid:5000/core-api:' + self.expect[:12]}]}}},
            'status': {'observedGeneration': 3, 'replicas': 2, 'readyReplicas': 2,
                       'updatedReplicas': 2, 'availableReplicas': 2,
                       'conditions': [{'type': 'Available', 'status': 'True',
                                       'reason': 'MinimumReplicasAvailable'}]}}

    def check(self, deployment=None, run=None):
        self.run_path = Path(self.temp.name) / 'run.json'
        self.serving_path = Path(self.temp.name) / 'serving.json'
        self.run_path.write_text(json.dumps(self.run if run is None else run))
        self.serving_path.write_text(json.dumps(self.deployment if deployment is None else deployment))
        return self.cli('check_deploy.py', '--job', 'core-api-stg', '--build', 662,
                        '--repo', self.repo, '--expect', self.expect,
                        '--run-json', self.run_path, '--serving-json', self.serving_path)

    def test_healthy_matching_serving_and_successful_jenkins_pass(self):
        code, data = self.check()
        self.assertEqual((code, data['status'], data['jenkins']['status'], data['serving']['status']),
                         (0, 'PASS', 'PASS', 'PASS'))
        serving = data['serving']
        self.assertIs(serving['commit_match'], True)
        self.assertEqual([serving[k] for k in ('replicas', 'ready', 'updated', 'available')], [2] * 4)
        self.assertEqual((serving['observed_generation'], serving['generation']), (3, 3))
        self.assertEqual(serving['available_condition']['status'], 'True')
        self.assertEqual(serving['images'][0]['image'], 'registry.invalid:5000/core-api:' + self.expect[:12])

    def test_jenkins_failure_does_not_erase_healthy_serving(self):
        self.run['result'] = 'FAILURE'
        code, data = self.check()
        self.assertEqual((code, data['jenkins']['status'], data['serving']['status']), (1, 'FAIL', 'PASS'))
        self.assertEqual(data['reason'], 'result-not-success')

    def test_running_jenkins_still_checks_serving(self):
        self.run.update(status='running', result=None, scm={})
        code, data = self.check()
        self.assertEqual((code, data['jenkins']['reason'], data['serving']['status']), (1, 'not-completed', 'PASS'))

    def test_invalid_jenkins_input_does_not_erase_serving_result(self):
        code, data = self.check(run=[])
        self.assertEqual((code, data['jenkins']['reason'], data['serving']['status']),
                         (2, 'invalid-run-json', 'PASS'))

    def test_successful_jenkins_does_not_prove_pending_serving(self):
        self.deployment['status'].update(readyReplicas=0, availableReplicas=0)
        self.deployment['status']['conditions'][0]['status'] = 'False'
        code, data = self.check()
        self.assertEqual((code, data['jenkins']['status'], data['serving']['reason']),
                         (1, 'PASS', 'serving-not-ready'))
        self.assertFalse(data['serving']['healthy'])

    def test_each_rollout_signal_must_pass(self):
        for field, value in [('observedGeneration', 2), ('observedGeneration', None),
                             ('replicas', 3), ('readyReplicas', 1), ('updatedReplicas', 1),
                             ('availableReplicas', 1), ('conditions', [])]:
            with self.subTest(field=field, value=value):
                deployment = copy.deepcopy(self.deployment)
                deployment['status'][field] = value
                code, data = self.check(deployment)
                self.assertEqual((code, data['serving']['reason']), (1, 'serving-not-ready'))

    def test_zero_scaled_deployment_is_not_serving(self):
        self.deployment['spec']['replicas'] = 0
        self.deployment['status'].update(replicas=0, readyReplicas=0, updatedReplicas=0, availableReplicas=0)
        code, data = self.check()
        self.assertEqual((code, data['serving']['healthy']), (1, False))

    def test_healthy_older_serving_is_separate_from_jenkins_success(self):
        self.deployment['spec']['template']['spec']['containers'][0]['image'] = 'repo:' + self.base[:12]
        code, data = self.check()
        self.assertEqual((code, data['jenkins']['status'], data['serving']['healthy'],
                          data['serving']['commit_match']), (1, 'PASS', True, False))

    def test_unrecognized_tags_and_digests_are_unknown_not_commit_proof(self):
        for image in ('repo:latest', 'repo', 'repo:stg-20261020', 'repo:abc123',
                      'repo@sha256:' + 'a' * 64, 'repo:' + self.expect[:12] + '@sha256:' + 'a' * 64):
            with self.subTest(image=image):
                self.deployment['spec']['template']['spec']['containers'][0]['image'] = image
                code, data = self.check()
                self.assertEqual((code, data['serving']['commit_match'], data['serving']['healthy']),
                                 (1, 'unknown', True))
                self.assertEqual(data['serving']['reason'], 'commit-match-unknown')

    def test_multi_container_images_are_all_reported_and_checked(self):
        containers = self.deployment['spec']['template']['spec']['containers']
        containers.append({'name': 'sidecar', 'image': 'sidecar:latest'})
        code, data = self.check()
        self.assertEqual((code, len(data['serving']['images']), data['serving']['commit_match']), (1, 2, 'unknown'))
        containers[1]['image'] = 'sidecar:' + self.expect
        self.assertEqual(self.check()[0], 0)

    def test_malformed_deployments_are_json_errors_preserving_jenkins(self):
        invalid = [[], {}, {'kind': 'Pod'}]
        for path, value in [(('status', 'readyReplicas'), '2'), (('status', 'updatedReplicas'), True),
                            (('status', 'conditions'), [None]), (('spec', 'replicas'), -1),
                            (('metadata', 'generation'), None), (('status',), [])]:
            deployment = copy.deepcopy(self.deployment)
            target = deployment
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid.append(deployment)
        for deployment in invalid:
            with self.subTest(deployment=deployment):
                code, data = self.check(deployment)
                self.assertEqual((code, data['jenkins']['status'], data['serving']['reason']),
                                 (2, 'PASS', 'invalid-serving-json'))

    def test_unreadable_and_malformed_files_preserve_other_check(self):
        self.check()
        for content in (None, '{broken', '[]'):
            if content is None:
                self.serving_path.unlink()
            else:
                self.serving_path.write_text(content)
            code, data = self.cli('check_deploy.py', '--job', 'x', '--build', 662,
                                  '--repo', self.repo, '--expect', self.expect,
                                  '--run-json', self.run_path, '--serving-json', self.serving_path)
            self.assertEqual((code, data['jenkins']['status'], data['serving']['reason']),
                             (2, 'PASS', 'invalid-serving-json'))

    def test_saved_inputs_never_invoke_jenkins_or_kubectl(self):
        self.check()
        spec = importlib.util.spec_from_file_location('deploy_serving', fixtures.ROOT / 'check_deploy.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        commands = []

        def runner(argv, **kwargs):
            commands.append(argv)
            self.assertEqual(argv[0], 'git')
            return subprocess.run(argv, **kwargs, capture_output=True, text=True, env=self.env)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = module.main(['--job', 'x', '--build', '662', '--repo', str(self.repo),
                                '--expect', self.expect, '--run-json', str(self.run_path),
                                '--serving-json', str(self.serving_path)], command_runner=runner)
        self.assertEqual(code, 0)
        self.assertTrue(commands)


if __name__ == '__main__':
    unittest.main()
