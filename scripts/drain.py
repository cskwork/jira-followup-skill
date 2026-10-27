#!/usr/bin/env python3
"""순차 drain의 시간·선정·장부를 모델과 같은 규칙으로 계산한다."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

import followup_config as cfg
import run_lock
import state_update

from state_update import BOOKKEEPING, item_digest


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    raise ValueError('invalid timestamp')


def iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def backoff_active(item, now):
    return 'backoffUntil' in item and epoch(item['backoffUntil']) > epoch(now)


def duration(value):
    match = re.fullmatch(r'(\d+)([smh]?)', str(value))
    if not match:
        raise ValueError('invalid timeout')
    seconds = int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]]
    if seconds <= 0:
        raise ValueError('invalid timeout')
    return seconds


def next_budget(directory, token, default_timeout, now=None, units=None):
    now = time.time() if now is None else epoch(now)
    data = run_lock.read_lock(directory)
    if not data or data['token'] != token:
        raise PermissionError('run ownership lost')
    if units is None:
        summary = state_update.read_object(run_lock.run_path(directory, token) / 'summary.json', optional=True)
        units = summary.get('runtimeUnitCount', 0)
    if type(units) is not int or units < 0:
        raise ValueError('invalid unit count')
    deadline = epoch(data['startedAt']) + 165 * 60
    remaining = deadline - now
    enabled = not (directory / 'drain').exists() or (directory / 'drain').read_text().strip() != '0'
    timeout = max(0, min(duration(default_timeout), int(remaining - 600)))
    return dict(deadline=deadline, remainingSeconds=remaining, timeoutSeconds=timeout,
                timeout=str(timeout) + 's', canStart=remaining >= 1800 and (enabled or units == 0),
                drainEnabled=enabled)


def validate_queue(queue):
    if not isinstance(queue, dict):
        raise ValueError('invalid queue')
    for key, item in queue.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9]+-\d+', key) or not isinstance(item, dict):
            raise ValueError('invalid queue item')
        for field in ('slug', 'stage', 'service', 'branch', 'commentIds', 'waiting', 'note'):
            if field not in item:
                raise ValueError('missing queue field: ' + field)
        if not isinstance(item['stage'], str) or not item['stage']:
            raise ValueError('invalid queue stage')
        if not isinstance(item['commentIds'], list) or any(not isinstance(v, str) for v in item['commentIds']):
            raise ValueError('invalid commentIds')
        if item['waiting'] is not None and not isinstance(item['waiting'], str):
            raise ValueError('invalid waiting')
    return queue


def pending_keys(directory):
    path = directory / 'pending-focus.jsonl'
    if not path.exists():
        return []
    keys = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = row.get('key', row.get('focus'))
        if not isinstance(key, str):
            raise ValueError('invalid pending focus')
        if key not in keys:
            keys.append(key)
    return keys


def rank(queue, *, approvals=None, handled=None, pending=(), focus=None,
         eligible=None, blocked=(), now=None):
    """환경 복구 등 모델이 확인한 후보는 eligible로 명시한다. backoff·키 차단은 유지한다."""
    now = time.time() if now is None else epoch(now)
    approvals, handled = approvals or {}, handled or {}
    allowed = None if eligible is None else set(eligible)
    blocked = set(blocked)
    pending = list(pending)
    def unused(key):
        approval = approvals.get(key, {})
        return isinstance(approval, dict) and 'stg' in approval and not approval.get('usedAt')
    def stamp(value):
        return epoch(value) if value is not None else float('inf')
    def order(key):
        item = queue[key]
        category = 0 if unused(key) else 1 if key in pending else 2 if key == focus else 3
        comments = item.get('commentIds') or []
        entry = handled.get(comments[0], {}) if comments else {}
        return (category, pending.index(key) if category == 1 else 0,
                stamp(item.get('createdAt')), stamp(entry.get('at')),
                int(key.rsplit('-', 1)[1]), key)
    keys = []
    for key, item in queue.items():
        if key in blocked or backoff_active(item, now) or (allowed is not None and key not in allowed):
            continue
        if unused(key) or key in pending or key == focus or (
                key in allowed if allowed is not None else item.get('waiting') is None):
            keys.append(key)
    return sorted(keys, key=order)


def summary_init(directory, token, runnable, lane_eligible, now):
    path = run_lock.run_path(directory, token) / 'summary.json'
    # 재시작해도 이미 위임한 작업자 수를 잃지 않는다.
    value = state_update.read_object(path, optional=True)
    if not value:
        value = dict(token=token, startedAt=run_lock.read_lock(directory)['startedAt'],
                     runnable=runnable, laneEligible=lane_eligible, units=[], runtimeUnits=[],
                     runtimeUnitCount=0, workers=0, durationSeconds=0)
    value['updatedAt'] = iso(now)
    state_update.atomic_json(path, value)
    return value


def summary_record(directory, token, key, unit, started_at, ended_at, workers, runtime):
    if workers < 0 or ended_at < started_at:
        raise ValueError('invalid unit duration or workers')
    path = run_lock.run_path(directory, token) / 'summary.json'
    value = state_update.read_object(path)
    if not value or not isinstance(value.get('units'), list):
        raise ValueError('summary-init required')
    record = dict(key=key, unit=unit, startedAt=started_at, endedAt=ended_at,
                  durationSeconds=ended_at - started_at, workers=workers, runtime=runtime)
    identity = (key, unit, started_at)
    previous = next((r for r in value['units'] if (r['key'], r['unit'], r['startedAt']) == identity), None)
    if previous is not None:
        if previous != record:
            raise ValueError('conflicting unit record')
        return value
    value['units'].append(record)
    value['runtimeUnits'] = [r for r in value['units'] if r['runtime']]
    value['runtimeUnitCount'] = len(value['runtimeUnits'])
    value['workers'] = sum(r['workers'] for r in value['units'])
    value['durationSeconds'] = sum(r['durationSeconds'] for r in value['units'])
    value['updatedAt'] = iso(ended_at)
    state_update.atomic_json(path, value)
    return value


def update_item(directory, token, key, action, now, reason=None):
    started = epoch(run_lock.read_lock(directory)['startedAt'])
    def change(queue):
        if key not in queue or not isinstance(queue[key], dict):
            raise ValueError('missing queue key')
        item = queue[key]
        if not item.get('createdAt'):
            item['createdAt'] = iso(now)
        if action == 'processed':
            item['deferCount'] = 0
            for field in ('deferredAt', 'deferReason', 'backoffUntil', 'backoffLevel'):
                item.pop(field, None)
        else:
            guard = state_update.read_object(directory / 'guard-state.json', optional=True)
            prior = guard.get('runnable', {}).get(key, {})
            count = item.get('deferCount', 0)
            if type(count) is not int or count < 0:
                raise ValueError('invalid defer count')
            hash_changed = bool(prior.get('hash') and prior['hash'] != item_digest(item))
            if hash_changed:
                count = 0
            same_run = (item.get('deferredAt') is not None
                        and epoch(item['deferredAt']) >= started)
            already_limited = (same_run and item.get('deferReason') == 'rate-limit'
                               and 'backoffUntil' in item)
            already = not hash_changed and same_run
            item['deferCount'] = count if already else count + 1
            item.update(deferredAt=iso(now), deferReason=reason)
            if action == 'rate-limit' and not already_limited:
                old_level = item.get('backoffLevel', -1)
                if type(old_level) is not int or old_level < -1:
                    raise ValueError('invalid backoff level')
                level = min(old_level + 1, 2)
                item.update(backoffLevel=level, backoffUntil=iso(now + min(1800 * 2 ** level, 7200)))
        item['updatedAt'] = iso(now)
        return dict(item)
    return state_update.transaction(directory, 'followup-queue.json', change)


def rate_limited(*paths):
    text = '\n'.join(Path(path).read_text(encoding='utf-8', errors='replace')
                     for path in paths if path is not None)
    return bool(re.search(r'rate[ -]?limit|\b429\b|\boverloaded\b', text, re.I))


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--dir', type=Path, default=argparse.SUPPRESS)
    common.add_argument('--token', default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest='command', required=True)
    budget = sub.add_parser('next-budget', parents=[common])
    budget.add_argument('--default-timeout', default='90m')
    budget.add_argument('--units', '--runtime-units', type=int)
    budget.add_argument('--now', type=float)
    ranking = sub.add_parser('rank', parents=[common])
    ranking.add_argument('--focus')
    ranking.add_argument('--eligible', nargs='*')
    ranking.add_argument('--blocked', nargs='*', default=[])
    ranking.add_argument('--now', type=float)
    sub.add_parser('validate-queue', parents=[common])
    init = sub.add_parser('summary-init', parents=[common])
    init.add_argument('--runnable', type=int, required=True)
    init.add_argument('--lane-eligible', type=int, required=True)
    init.add_argument('--now', type=float)
    record = sub.add_parser('summary-record', parents=[common])
    record.add_argument('--key', required=True)
    record.add_argument('--unit', required=True)
    record.add_argument('--started-at', type=float, required=True)
    record.add_argument('--ended-at', type=float, required=True)
    record.add_argument('--workers', type=int, required=True)
    record.add_argument('--runtime', action='store_true')
    for name in ('defer', 'processed', 'rate-limit'):
        command = sub.add_parser(name, parents=[common])
        command.add_argument('--key', required=True)
        command.add_argument('--now', type=float)
        if name == 'defer':
            command.add_argument('--reason', required=True)
    check = sub.add_parser('check-result', parents=[common])
    check.add_argument('--key', required=True)
    check.add_argument('--result', type=Path, required=True)
    check.add_argument('--stderr', type=Path)
    check.add_argument('--now', type=float)
    args = parser.parse_args(argv)
    directory = getattr(args, 'dir', Path(os.environ.get('JIRA_WAKE_DIR') or cfg.HOME_DIR))
    token = getattr(args, 'token', None)
    now = getattr(args, 'now', None)
    now = time.time() if now is None else now
    try:
        if args.command == 'next-budget':
            result = next_budget(directory, token, args.default_timeout, now, args.units)
        elif args.command in ('rank', 'validate-queue'):
            queue = validate_queue(state_update.read_object(directory / 'followup-queue.json'))
            if args.command == 'validate-queue':
                result = dict(valid=True, items=len(queue))
            else:
                state = state_update.read_object(directory / 'state.json', optional=True)
                result = rank(queue, approvals=state_update.read_object(directory / 'approvals.json', optional=True),
                              handled=state.get('followup', {}).get('handled', {}), pending=pending_keys(directory),
                              focus=args.focus, eligible=args.eligible, blocked=args.blocked, now=now)
        else:
            # 주인 확인과 쓰기 사이에 잠금이 교체되지 않게 같은 잠금을 잡는다.
            with run_lock.locked(directory):
                if not run_lock.owns(directory, token):
                    raise PermissionError('run ownership lost')
                if args.command == 'summary-init':
                    if args.runnable < 0 or args.lane_eligible < 0:
                        raise ValueError('invalid summary count')
                    with state_update.locked(directory):
                        result = summary_init(directory, token, args.runnable, args.lane_eligible, now)
                elif args.command == 'summary-record':
                    with state_update.locked(directory):
                        result = summary_record(directory, token, args.key, args.unit, args.started_at,
                                                args.ended_at, args.workers, args.runtime)
                elif args.command == 'check-result':
                    limited = rate_limited(args.result, args.stderr)
                    result = dict(rateLimited=limited, retryAllowed=not limited)
                    if limited:
                        update_item(directory, token, args.key, 'rate-limit', now, 'rate-limit')
                        result.update(failureType='infra', deferReason='rate-limit')
                else:
                    result = update_item(directory, token, args.key, args.command, now,
                                         'rate-limit' if args.command == 'rate-limit' else getattr(args, 'reason', None))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except PermissionError:
        print('drain: run ownership lost', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print('drain: invalid state or arguments', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
