"""Caller-declared evidence gate; worker results cannot waive declared checks.

Manifest: key, stage, checks; optional event declares a judgement unit (Jira,
Teams, todo-event or intake), never inferred from a result or stage name.
event = {commentIds: [nonempty unique strings], stateArtifact: path,
         queueArtifact: optional path}. Paths are relative to the manifest.
The listed state artifact must contain followup.handled[id] with matching key,
action and at for every declared id. A done event may leave any waiting value.
Its result stage must match the declaration or the same key's stage in the
listed queueArtifact ({KEY: queue change}). All original checks still apply.
Non-event units retain strict stage identity and done + waiting=null.
"""
from copy import deepcopy
import json
import math
from pathlib import Path


def snapshot(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or not {'key', 'stage', 'checks'} <= set(value)
            or set(value) - {'key', 'stage', 'checks', 'event'}
            or not all(isinstance(value.get(k), str) and value[k] for k in ('key', 'stage'))
            or not isinstance(value['checks'], list) or not value['checks']):
        raise ValueError('invalid requirements manifest')
    if 'event' in value:
        event = value['event']
        if (not isinstance(event, dict) or set(event) - {'commentIds', 'stateArtifact', 'queueArtifact'}
                or not isinstance(event.get('commentIds'), list) or not event['commentIds']
                or not all(isinstance(cid, str) and cid.strip() for cid in event['commentIds'])
                or len(set(event['commentIds'])) != len(event['commentIds'])
                or not isinstance(event.get('stateArtifact'), str) or not event['stateArtifact'].strip()
                or 'queueArtifact' in event and (not isinstance(event['queueArtifact'], str)
                                                or not event['queueArtifact'].strip())):
            raise ValueError('invalid event declaration')
    names = set()
    for row in value['checks']:
        if (not isinstance(row, dict) or set(row) != {'name', 'artifact', 'resultField', 'environment'}
                or not all(isinstance(row.get(k), str) and row[k] for k in ('name', 'artifact', 'resultField'))
                or row['name'] in names or not isinstance(row['environment'], dict) or not row['environment']):
            raise ValueError('invalid required check')
        names.add(row['name'])
        for field in [row['resultField'], *row['environment']]:
            if not isinstance(field, str) or not field or any(not part for part in field.split('.')):
                raise ValueError('invalid JSON field binding')
        # Null, nested values and nonfinite numbers cannot establish an environment identity.
        for expected in row['environment'].values():
            if (expected is None or not isinstance(expected, (str, bool, int, float))
                    or isinstance(expected, str) and not expected
                    or isinstance(expected, float) and not math.isfinite(expected)):
                raise ValueError('invalid environment binding')
    return {'manifest': deepcopy(value), 'base': str(path.parent)}


def field(value, selector):
    for part in selector.split('.'):
        if not isinstance(value, dict) or part not in value:
            raise ValueError('missing verifier field')
        value = value[part]
    return value


def has_field(value, selector):
    try:
        field(value, selector)
    except ValueError:
        return False
    return True


def evaluate(declaration, value, key=None):
    manifest, base = declaration['manifest'], Path(declaration['base'])
    passed, failures = [], []
    event = manifest.get('event')
    artifacts = value.get('artifacts', []) if isinstance(value, dict) else []
    listed = {(base / p).resolve() for p in artifacts if isinstance(p, str)} if isinstance(artifacts, list) else set()

    def event_artifact(name):
        path = (base / event[name]).resolve()
        if path not in listed:
            raise ValueError('event artifact not listed')
        return json.loads(path.read_text())

    stages = [manifest['stage']]
    if event and event.get('queueArtifact'):
        try:
            queue_stage = field(event_artifact('queueArtifact'), manifest['key'] + '.stage')
            if isinstance(queue_stage, str) and queue_stage:
                stages.append(queue_stage)
        except (OSError, ValueError, TypeError):
            pass  # An unavailable queue change cannot grant another stage identity.
    if (not isinstance(value, dict) or value.get('key') != manifest['key']
            or key is not None and key != manifest['key'] or value.get('stage') not in stages):
        failures.append({'name': 'identity', 'reason': 'key-or-stage-mismatch'})
    if (not isinstance(value, dict) or value.get('status') != 'done'
            or not event and value.get('waiting') is not None):
        failures.append({'name': 'completion', 'reason': 'not-complete'})
    if event:
        try:
            handled = field(event_artifact('stateArtifact'), 'followup.handled')
            for cid in event['commentIds']:
                row = handled.get(cid) if isinstance(handled, dict) else None
                if (not isinstance(row, dict) or row.get('key') != manifest['key']
                        or not all(isinstance(row.get(f), str) and row[f].strip() for f in ('action', 'at'))):
                    raise ValueError('event not handled')
        except (OSError, ValueError, TypeError):
            failures.append({'name': 'completion', 'reason': 'event-not-handled'})
    checks = value.get('checks', []) if isinstance(value, dict) else []
    artifacts = value.get('artifacts', []) if isinstance(value, dict) else []
    if not isinstance(checks, list):
        checks = []
    if not isinstance(artifacts, list):
        artifacts = []
    for row in manifest['checks']:
        artifact = (base / row['artifact']).resolve()
        evidence = {'name': row['name'], 'artifact': str(artifact)}
        matches = [c for c in checks if isinstance(c, dict) and c.get('name') == row['name']]
        reason = None
        if len(matches) != 1:
            reason = 'missing-or-duplicate-check'
        elif matches[0].get('result') != 'PASS':
            reason = 'check-not-pass'
        elif artifact not in listed:
            reason = 'artifact-not-listed'
        else:
            try:
                saved = json.loads(artifact.read_text())
                missing = [name for name in [row['resultField'], *row['environment']] if not has_field(saved, name)]
                if missing:
                    reason = 'verifier-field-missing:' + ','.join(missing)
                elif field(saved, row['resultField']) != 'PASS':
                    reason = 'verifier-not-pass'
                elif any(type(field(saved, name)) is not type(expected) or field(saved, name) != expected
                         for name, expected in row['environment'].items()):
                    reason = 'environment-mismatch'
            except (OSError, ValueError, TypeError):
                reason = 'unreadable-or-invalid-verifier'
        if reason:
            failures.append({**evidence, 'reason': reason})
        else:
            passed.append(evidence)
    return {'passed': not failures, 'retryAllowed': False if failures else True,
            'key': manifest['key'], 'stage': manifest['stage'], 'passedChecks': passed, 'failures': failures}
