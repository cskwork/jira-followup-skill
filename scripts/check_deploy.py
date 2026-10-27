#!/usr/bin/env python3
"""Verify a completed Jenkins run against a commit already in a local repository.

Usage: check_deploy.py --job JOB --build NUMBER --repo DIR --expect COMMIT_OR_REF
                       [--context dev] [--run-json FILE] [--serving-json FILE]
Exit codes: 0 PASS; 1 deployment check FAIL; 2 usage, tool, JSON, or Git error.
The installed `jk --help` and `jk run view --help` were checked: --context/-c
is a global flag (JK_CONTEXT is its environment fallback). The supported read
command is `jk --context dev run view JOB NUMBER --json`. This script explicitly
selects its context, defaults to dev, and never changes global context or fetches.
--run-json reads saved output instead of invoking jk. A supplied run number must
match --build; older saved output without a number can still be checked.
SCM checkout identity must be a full 40- or 64-digit hexadecimal commit SHA.
--serving-json checks a saved kubectl Deployment separately from Jenkins. Neither
the serving check nor --run-json performs network calls. A serving PASS requires
a current, available rollout and commit-bearing image tags matching --expect.
Mutable tags such as latest and unrecognized tag formats report unknown, never
commit proof. A saved Deployment alone cannot establish ingress routing.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


TIMEOUT_SECONDS = 150  # jk idles ~60 s after printing JSON (measured)
MAX_JSON_BYTES = 8 * 1024 * 1024
JENKINS_RESULTS = {'SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT'}


class CheckError(Exception):
    """A reportable error with a fixed, non-sensitive reason."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckError('usage-error')


def positive_integer(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('must be a positive integer') from None
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def run_command(argv, timeout=TIMEOUT_SECONDS):
    """Single injection point for every external command; no shell or stderr."""
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, encoding='utf-8', errors='replace',
                          timeout=timeout, check=False)


def command(argv, runner):
    try:
        return runner(argv, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        raise CheckError('tool-error') from None


def read_run(args, runner):
    if args.run_json:
        try:
            with Path(args.run_json).open('rb') as source:
                raw = source.read(MAX_JSON_BYTES + 1)
            raw = raw.decode('utf-8')
        except (OSError, UnicodeError, ValueError):
            raise CheckError('invalid-run-json') from None
    else:
        completed = command(['jk', '--context', args.context, 'run', 'view',
                             args.job, str(args.build), '--json'], runner)
        if completed.returncode != 0:
            raise CheckError('tool-error')
        raw = completed.stdout
    if len(raw.encode('utf-8')) > MAX_JSON_BYTES:
        raise CheckError('invalid-run-json')
    try:
        run = json.loads(raw)
    except (ValueError, TypeError):
        raise CheckError('invalid-run-json') from None
    if not isinstance(run, dict):
        raise CheckError('invalid-run-json')
    if 'number' in run and (type(run['number']) is not int or run['number'] != args.build):
        raise CheckError('build-mismatch')
    return run


def resolve_commit(repo, ref, runner):
    return command(['git', '-C', repo, 'rev-parse', '--verify',
                    '--end-of-options', ref + '^{commit}'], runner)


def check(args, report, runner):
    run = read_run(args, runner)
    result = run.get('result')
    report['result'] = result if isinstance(result, str) and result in JENKINS_RESULTS else None
    scm = run.get('scm')
    checkout = scm.get('commit') if isinstance(scm, dict) else None
    valid_checkout = isinstance(checkout, str) and re.fullmatch(
        r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', checkout) is not None
    report['checkout_commit'] = checkout if valid_checkout else None

    # Running builds often have no SCM yet; their primary failure is completion.
    if run.get('status') != 'completed':
        return 1, 'not-completed'
    if result != 'SUCCESS':
        return 1, 'result-not-success'
    if not isinstance(checkout, str) or not checkout.strip():
        return 1, 'checkout-commit-missing'
    if not valid_checkout:
        return 1, 'checkout-commit-invalid'

    repository = command(['git', '-C', args.repo, 'rev-parse', '--git-dir'], runner)
    if repository.returncode != 0:
        raise CheckError('git-error')
    expected = resolve_commit(args.repo, args.expect, runner)
    if expected.returncode != 0:
        raise CheckError('expect-not-in-repo')
    report['expect_sha'] = expected.stdout.strip()
    checked_out = resolve_commit(args.repo, checkout, runner)
    if checked_out.returncode != 0:
        return 1, 'checkout-commit-not-in-repo'
    report['checkout_commit'] = checked_out.stdout.strip()
    ancestor = command(['git', '-C', args.repo, 'merge-base', '--is-ancestor',
                        report['expect_sha'], report['checkout_commit']], runner)
    if ancestor.returncode == 1:
        return 1, 'expect-not-contained'
    if ancestor.returncode != 0:
        raise CheckError('git-error')
    report['contains_expect'] = True
    return 0, 'contains-expect'


def image_commit_match(image, expected):
    # Service Jenkinsfile_N reads IMAGE from external properties; checked-in
    # values use :latest. Only an unambiguous bare Git SHA tag is comparable.
    name = image.rsplit('/', 1)[-1]
    if '@' in name or ':' not in name:
        return 'unknown'
    tag = name.rsplit(':', 1)[1]
    if not re.fullmatch(r'[0-9a-fA-F]{7,64}', tag):
        return 'unknown'
    return expected.lower().startswith(tag.lower())


def check_serving(args, report, runner):
    try:
        with Path(args.serving_json).open('rb') as source:
            raw = source.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError
        deployment = json.loads(raw)
        if not isinstance(deployment, dict) or deployment.get('kind') != 'Deployment':
            raise ValueError
        metadata = deployment['metadata']
        spec = deployment['spec']
        state = deployment.get('status', {})
        if not all(isinstance(value, dict) for value in (metadata, spec, state)):
            raise ValueError
        generation = metadata['generation']
        desired = spec.get('replicas', 1)
        observed = state.get('observedGeneration')
        counts = {key: state.get(field, 0) for key, field in (
            ('replicas', 'replicas'), ('ready', 'readyReplicas'),
            ('updated', 'updatedReplicas'), ('available', 'availableReplicas'))}
        if any(type(value) is not int or value < 0
               for value in (generation, desired, *counts.values())):
            raise ValueError
        if observed is not None and (type(observed) is not int or observed < 0):
            raise ValueError
        containers = spec['template']['spec']['containers']
        if not isinstance(containers, list) or not containers:
            raise ValueError
        images = []
        for container in containers:
            image = container['image']
            if not isinstance(image, str) or not image.strip():
                raise ValueError
            images.append({'name': container.get('name'), 'image': image})
        conditions = state.get('conditions', [])
        if not isinstance(conditions, list) or any(not isinstance(c, dict) for c in conditions):
            raise ValueError
        available = next((c for c in conditions if c.get('type') == 'Available'), None)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise CheckError('invalid-serving-json') from None

    report.update(name=metadata.get('name'), namespace=metadata.get('namespace'),
                  images=images, desired_replicas=desired, **counts,
                  available_condition=available, generation=generation,
                  observed_generation=observed,
                  generation_current=observed is not None and observed >= generation)
    healthy = (desired > 0 and all(count == desired for count in counts.values())
               and report['generation_current']
               and available is not None and available.get('status') == 'True')
    report['healthy'] = healthy
    expected = resolve_commit(args.repo, args.expect, runner)
    if expected.returncode != 0 or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', expected.stdout.strip()):
        raise CheckError('expect-not-in-repo')
    report['expect_sha'] = expected.stdout.strip()
    for item in images:
        item['commit_match'] = image_commit_match(item['image'], report['expect_sha'])
    matches = [item['commit_match'] for item in images]
    report['commit_match'] = (False if False in matches else
                              'unknown' if 'unknown' in matches else True)
    if not healthy:
        return 1, 'serving-not-ready'
    if report['commit_match'] == 'unknown':
        return 1, 'commit-match-unknown'
    if not report['commit_match']:
        return 1, 'serving-commit-mismatch'
    return 0, 'serving-ready-commit-matched'


def main(argv=None, command_runner=None):
    report = {'check': 'deploy', 'status': 'FAIL', 'job': None, 'build': None,
              'result': None, 'checkout_commit': None, 'expect': None,
              'expect_sha': None, 'contains_expect': False, 'reason': None}
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--job', required=True)
    parser.add_argument('--build', required=True, type=positive_integer)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--expect', required=True)
    parser.add_argument('--context', default='dev')
    parser.add_argument('--run-json')
    parser.add_argument('--serving-json', help='saved kubectl get deploy -o json; no kubectl is invoked')
    runner = command_runner or run_command
    args = None
    try:
        args = parser.parse_args(argv)
        report.update(job=args.job, build=args.build, expect=args.expect)
        if not all(value.strip() for value in (args.job, args.repo, args.expect, args.context)):
            raise CheckError('usage-error')
        code, reason = check(args, report, runner)
    except CheckError as error:
        code, reason = 2, str(error)
    report['jenkins'] = {key: report[key] for key in (
        'result', 'checkout_commit', 'expect_sha', 'contains_expect')}
    report['jenkins'].update(status='PASS' if code == 0 else 'FAIL', reason=reason)
    report['serving'] = {'status': 'NOT_REQUESTED'}
    if args is not None and args.serving_json:
        serving = {'status': 'FAIL', 'commit_match': 'unknown'}
        report['serving'] = serving
        try:
            serving_code, serving_reason = check_serving(args, serving, runner)
        except CheckError as error:
            serving_code, serving_reason = 2, str(error)
        serving.update(status='PASS' if serving_code == 0 else 'FAIL', reason=serving_reason)
        if code == 0 or serving_code > code:
            reason = serving_reason
        code = max(code, serving_code)
    report.update(status='PASS' if code == 0 else 'FAIL', reason=reason)
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
