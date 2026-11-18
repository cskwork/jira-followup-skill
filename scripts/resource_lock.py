#!/usr/bin/env python3
"""작업자 PID와 attempt 실행을 함께 확인하는 자원별 잠금. TTL은 쓰지 않는다."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import followup_config as cfg
from run_lock import atomic_json, call, has_running, invalid_alert, journal
from state_update import directory_argument


RETRY_SECONDS = 15


def identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('invalid resource or attempt name')
    return value


@contextmanager
def locked(directory, *, wait_until=None):
    """획득 마감이 있으면 flock 대기도 같은 마감에 묶고, 만료 시 None을 준다."""
    root = directory / 'locks'
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.lock').open('a') as stream:
        if wait_until is None:
            fcntl.flock(stream, fcntl.LOCK_EX)
        else:
            while True:
                remaining = wait_until - time.time()
                if remaining <= 0:
                    yield None
                    return
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(.05, remaining))
        yield root


def read_lock(path):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('name') != path.stem
            or not isinstance(value.get('token'), str) or not isinstance(value.get('key'), str)
            or type(value.get('pid')) is not int or value['pid'] <= 0
            or not isinstance(value.get('at'), (int, float))
            or not isinstance(value.get('pidStart'), str) or not value['pidStart'].strip()):
        raise ValueError('invalid resource lock')
    identifier(value.get('name', ''))
    identifier(value.get('attempt', ''))
    return value


def pid_start(pid, *, timeout=None):
    response = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True,
                              text=True, check=True,
                              timeout=15 if timeout is None else min(15, max(.001, timeout)))
    started = response.stdout.strip()
    if not started:
        raise ValueError('missing worker process start')
    return started


def pid_alive(pid, started, *, timeout=None):
    if not isinstance(started, str) or not started.strip():
        raise ValueError('missing worker process start')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return pid_start(pid, timeout=timeout) == started


def attempt_running(attempt, *, timeout=None):
    argv = ('agent', 'ls', '--json', '-a', '--label', 'attempt=' + attempt)
    response = call(*argv) if timeout is None else subprocess.run(
        [os.environ.get('PASEO_BIN', 'paseo'), *argv], capture_output=True, text=True, check=True, timeout=min(15, max(.001, timeout)))
    rows = json.loads(response.stdout)
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
            or row.get('status') not in ('running', 'idle', 'closed', 'error') for row in rows):
        raise ValueError('invalid agent list')
    return has_running(rows)


def notify_alert(directory, data):
    message = 'resource-alert: ' + data['name'] + ' attempt=' + data['attempt']
    journal(directory, message)
    try:
        import decision_inbox
        decision_inbox.alert_detached(f'[{cfg.LINE_TITLE} 조치 필요]\n자원 잠금 6시간 초과: ' + message)
    except ImportError:
        pass


def alert(directory, path, data, now):
    if now - data['at'] > 21600 and not data.get('alertedAt'):
        data['alertedAt'] = now
        # 알림 재시도에 의한 중복을 막기 위해 먼저 기록한다.
        atomic_json(path, data)
        notify_alert(directory, data)


def acquire(directory, name, token, key, attempt, wait_until, *, pid=None):
    name, attempt = identifier(name), identifier(attempt)
    pid = os.getppid() if pid is None else pid
    if type(pid) is not int or pid <= 0 or not math.isfinite(wait_until):
        raise ValueError('invalid worker pid')
    while True:
        now = time.time()
        if now >= wait_until:
            return 4
        with locked(directory, wait_until=wait_until) as root:
            if root is None:
                return 4
            path = root / (name + '.json')
            try:
                data = read_lock(path)
            except (OSError, ValueError, TypeError):
                invalid_alert(directory, path)
                return 4
            if data and data['token'] == token and data['attempt'] == attempt and data['pid'] == pid:
                try:
                    if pid_alive(pid, data['pidStart'], timeout=wait_until-time.time()):
                        return 0
                except (OSError, ValueError, subprocess.SubprocessError):
                    return 4
            reclaim = data is None
            if data:
                alert(directory, path, data, now)
                try:
                    if not pid_alive(data['pid'], data['pidStart'], timeout=wait_until-time.time()):
                        reclaim = not attempt_running(data['attempt'], timeout=wait_until-time.time())
                except (OSError, ValueError, subprocess.SubprocessError):
                    reclaim = False
            if reclaim:
                try:
                    started = pid_start(pid, timeout=wait_until-time.time())
                except (OSError, ValueError, subprocess.SubprocessError):
                    return 4
                if time.time() >= wait_until:
                    return 4
                atomic_json(path, dict(name=name, token=token, key=key, attempt=attempt, at=now,
                                       pid=pid, pidStart=started))
                return 0
        time.sleep(min(RETRY_SECONDS, max(0, wait_until - time.time())))


def release(directory, name, token, attempt):
    name, attempt = identifier(name), identifier(attempt)
    with locked(directory) as root:
        path = root / (name + '.json')
        try:
            data = read_lock(path)
        except (OSError, ValueError, TypeError):
            invalid_alert(directory, path)
            return 4
        if data is None:
            return 0
        if data['token'] != token or data['attempt'] != attempt:
            return 3
        try:
            if attempt_running(attempt):
                return 4
        except (OSError, ValueError, subprocess.SubprocessError):
            return 4
        path.unlink()
        return 0


def list_locks(directory):
    with locked(directory) as root:
        result = []
        for path in sorted(root.glob('*.json')):
            try:
                data = read_lock(path)
            except (OSError, ValueError, TypeError):
                invalid_alert(directory, path)
                continue
            if data is None:
                continue
            alert(directory, path, data, time.time())
            result.append(data)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('acquire', 'release', 'list'))
    parser.add_argument('name', nargs='?')
    parser.add_argument('--dir', type=directory_argument, required=True)
    parser.add_argument('--token')
    parser.add_argument('--key', default='')
    parser.add_argument('--attempt')
    parser.add_argument('--pid', type=int)
    parser.add_argument('--wait-until', type=float)
    args = parser.parse_args(argv)
    if args.action != 'list' and (not args.name or args.token is None or not args.attempt):
        parser.error('name, --token and --attempt are required')
    if args.action == 'acquire' and args.wait_until is None:
        parser.error('acquire requires --wait-until')
    try:
        if args.action == 'list':
            print(json.dumps(list_locks(args.dir)))
            return 0
        if args.action == 'release':
            return release(args.dir, args.name, args.token, args.attempt)
        return acquire(args.dir, args.name, args.token, args.key, args.attempt, args.wait_until, pid=args.pid)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('resource_lock: state or paseo query failed; lock preserved', file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())
