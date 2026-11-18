#!/usr/bin/env python3
"""Attempt-local checkpoint and process receipt. Never releases factory locks."""
import argparse
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

import followup_config as cfg
import lifecycle
import requirements
from state_update import atomic_json


def human_ask_complete(value):
    """human-risk 대기의 question·suggest가 둘 다 비지 않은 문자열인가."""
    return all(isinstance(value.get(name), str) and value[name].strip() for name in ('question', 'suggest'))


def one_line(text):
    return isinstance(text, str) and bool(text.strip()) and '\n' not in text and '\r' not in text


def human_action_valid(action):
    """humanAction은 null이거나 {line: 한 줄, runSheet: 경로 한 줄 또는 null}이다."""
    return action is None or (isinstance(action, dict) and set(action) == {'line', 'runSheet'} and one_line(action['line'])
                              and (action['runSheet'] is None or one_line(action['runSheet'])))


def human_fields(value):
    """검증한 결과를 큐 human-risk 필드로 바꾼다(state_update patch --from-result): 사람이 할 일 또는 질문·권장 답."""
    if value.get('waiting') != 'human-risk':
        return {}
    action = value.get('humanAction')
    if action:
        return {'action': action['line'], **({'runSheet': action['runSheet']} if action['runSheet'] else {})}
    return {'question': value['question'], 'suggest': value['suggest']}


def valid_result(path, *, key=None, started_at=0):
    try:
        if path.stat().st_mtime < started_at:
            return False
        value = json.loads(path.read_text())
        schema = json.loads(Path(__file__).with_name('worker-result.schema.json').read_text())
        if not isinstance(value, dict):
            return False
        # humanAction이 생기기 전 스키마로 낸 결과는 null로 본다. 그 밖의 필드는 빠짐·추가 모두 거부한다.
        value = {'humanAction': None, **value}
        if set(value) != set(schema['required']):
            return False
        if key is not None and value['key'] != key:
            return False
        for name, spec in schema['properties'].items():
            item = value[name]
            if 'enum' in spec and item not in spec['enum']:
                return False
            types = spec['type'] if isinstance(spec['type'], list) else [spec['type']]
            if not any((t == 'null' and item is None) or (t == 'string' and isinstance(item, str))
                       or (t == 'array' and isinstance(item, list)) or (t == 'object' and isinstance(item, dict))
                       for t in types):
                return False
        if not all(isinstance(a, str) for a in value['artifacts']):
            return False
        if not all(isinstance(c, dict) and set(c) == {'name', 'result'}
                   and isinstance(c['name'], str) and c['result'] in ('PASS', 'FAIL', 'SKIP')
                   for c in value['checks']):
            return False
        # 사람 확인 대기는 사용자가 정할 질문과 권장 답 한 줄이 있어야 할 일 앱에 보일 수 있다(2026-11-05).
        # 사람만 할 수 있는 선행 조치는 질문 대신 humanAction 하나로 보고한다. 질문·권장 답과 함께 쓰거나 human-risk 밖이면 거부한다.
        action = value['humanAction']
        if not human_action_valid(action):
            return False
        if action is not None:
            return value['waiting'] == 'human-risk' and value['question'] is None and value['suggest'] is None
        if value['waiting'] == 'human-risk' and not human_ask_complete(value):
            return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def timeout_seconds(value):
    match = re.fullmatch(r"(\d+)([smh]?)", value)
    if not match:
        raise argparse.ArgumentTypeError("invalid timeout")
    return int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]] + 30


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix='attempt-', dir=root)).resolve()


def listening_processes(run=subprocess.run):
    """(pid, 경과 초, 명령줄) — 이 사용자의 TCP LISTEN 프로세스.

    ps는 PID 목록으로 한 번만 부른다. PID마다 부르면 LISTEN 프로세스 수만큼 느려진다(2026-11-10: 이 기기 77개에서
    시도마다 8.5초, jira-worker 시험이 10~15초 제한을 넘김). 그 사이 끝난 PID는 ps 출력에서 빠질 뿐이다."""
    found = run(['lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-Fp'], capture_output=True, text=True, timeout=30)
    pids = sorted({int(line[1:]) for line in found.stdout.splitlines() if line[:1] == 'p' and line[1:].isdigit()})
    if not pids:
        return
    rows = run(['ps', '-o', 'pid=,etime=,command=', '-p', ','.join(map(str, pids))],
               capture_output=True, text=True, timeout=30)
    for line in rows.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) in pids:
            yield int(parts[0]), elapsed_seconds(parts[1]), parts[2]


def elapsed_seconds(value):
    days, _, clock = value.rpartition('-')
    seconds = 0
    for part in clock.split(':'):
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86400


def stop_leftover_servers(workspace, key, since, *, processes=listening_processes, signal_process=os.kill):
    """작업자가 worktree에서 띄우고 끄지 못한 로컬 서버를 멈춘다(2026-11-03 vite 6개 잔류).

    대상은 이번 시도 시작 뒤에 시작했고, 명령줄이 이 티켓 키로 시작하는 workspace의 worktrees·.scratch 아래 경로를
    가리키며 포트를 연 프로세스뿐이다. 다른 티켓(병렬 lanes)의 서버는 건드리지 않는다.
    작업자 에이전트는 kill이 막혀 있어 정리를 실행기가 맡는다. 멈춘 것은 영수증 stoppedServers에 남긴다.
    """
    roots = '|'.join(re.escape(str(Path(workspace).resolve() / name)) for name in ('worktrees', '.scratch'))
    owned = re.compile(rf'(?:{roots})/{re.escape(key)}[-/]', re.IGNORECASE)
    now, stopped = time.time(), []
    try:
        for pid, elapsed, command in processes():
            if now - elapsed >= since - 1 and owned.search(command):
                try:
                    signal_process(pid, signal.SIGTERM)
                    stopped.append({'pid': pid, 'command': command[:200]})
                except OSError:
                    pass
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        stopped.append({'error': type(error).__name__})
    return stopped


def cancel_attempt_agents(attempt_label, *, run_token=None, key=None):
    """Stop only this launch's labelled agents; a stop acknowledgement is not proof of idle."""
    cancelled, messages = [], []

    def call(*args, check=True):
        environment = {name: value for name, value in os.environ.items()
                       if name not in ('PASEO_AGENT_ID', 'PASEO_WORKSPACE_ID')}
        response = subprocess.run([os.environ.get('PASEO_BIN') or 'paseo', *args], capture_output=True, text=True, timeout=15,
                                  env=environment)
        if check and response.returncode:
            raise ValueError((response.stderr or response.stdout).strip() or f'exit {response.returncode}')
        return response

    def agents():
        labels = ['--label', 'factory=jira-followup', '--label', 'attempt=' + attempt_label]
        if run_token:
            labels += ['--label', 'run=' + run_token]
        if key:
            labels += ['--label', 'key=' + key]
        response = call('agent', 'ls', '-a', '-g', '--json', *labels)
        rows = json.loads(response.stdout)
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
                or row.get('status') not in ('running', 'idle', 'closed', 'error') for row in rows):
            raise ValueError('invalid agent list')
        return rows

    try:
        rows = agents()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        messages.append(f'cancel failed: attempt={attempt_label}: {error}')
    else:
        for row in rows:
            if row['status'] != 'running':
                continue
            identity = row['id']
            try:
                call('stop', identity, '--json')
                # Wait is bounded, then independently verify the same attempt's status.
                call('wait', identity, '--timeout', '5', '--json', check=False)
                confirmed = next((item for item in agents() if item['id'] == identity), None)
                if confirmed is None or confirmed['status'] == 'running':
                    raise ValueError('agent still running or stop unconfirmed')
                cancelled.append(identity)
                messages.append(f'cancelled: {identity}')
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                messages.append(f'cancel failed: {identity}: {error}')
    for message in messages:
        print('jira-worker: ' + message, file=sys.stderr)
    return {'cancelledAgents': cancelled, 'cancellationFailed': any(
        message.startswith('cancel failed:') for message in messages), 'summary': '; '.join(messages)}


def todo_cli(args, timeout):
    kit = Path(os.path.expanduser(os.environ.get('SDLC_KIT') or str(cfg.SDLC_KIT)))
    response = subprocess.run(['node', str(kit / 'todo/todo.mjs'), *args], capture_output=True, text=True,
                              timeout=timeout)
    if response.returncode:
        # 첫 줄만 남긴다: 모르는 명령이면 kit이 사용법 전체를 stderr에 낸다.
        lines = [line.strip() for line in (response.stderr or response.stdout).splitlines() if line.strip()]
        raise RuntimeError((lines[0] if lines else f'exit {response.returncode}')[:200])
    return response.stdout


def working_label(unit, key):
    """할 일 앱의 '작업 중' 문구: 단위 이름에서 앞의 티켓 키를 뺀 한 줄, 40자 이내('PROJ-1438 build' → 'build')."""
    text = ' '.join(str(unit or '').split())
    rest = text[len(key):].strip(' -:·') if key and text.upper().startswith(key) else text
    return (rest or text)[:40]


class TodoHeartbeat:
    """작업자가 도는 동안 그 키의 열린 티켓 할 일 항목에 '작업 중' 표시를 유지한다(sdlc-kit `todo working`, 180초 만료).

    모든 호출은 데몬 스레드에서 하고 실패는 한 번만 기록한다. 할 일 CLI가 없거나 `working`을 모르거나 느려도
    작업자 실행·시간 제한·종료는 기다리지 않는다. 결정 항목('[결정 Dn]'·스테이징 승인 항목)은 사람 차례라 표시하지 않는다.
    항목은 slug가 있으면 slug로 부른다. 작업자가 시도 중에 `start --slug`·제목 바꾸기를 하면 항목 id가 바뀌므로,
    갱신이 'item not found'면 바로 목록을 새로 읽어 다시 찾는다. 다시 읽어도 못 찾으면(목록은 전체 동기화) 다음은 10분 뒤다."""
    INTERVAL = 60
    LIST_TIMEOUT = 30
    CALL_TIMEOUT = 10
    STOP_WAIT = 11
    RESOLVE_GAP = 600

    def __init__(self, key, unit, *, cli=todo_cli, clock=time.monotonic):
        self.key, self.what, self.cli, self.clock = key, working_label(unit, key), cli, clock
        self.ids, self.listing, self.marked, self.failed = [], True, False, False
        self.retry_at = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='todo-working', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def fail(self, step, error):
        if not self.failed:
            self.failed = True
            print(f'jira-worker: todo working {step} skipped: {type(error).__name__}: {error}', file=sys.stderr)

    def item_ids(self):
        """그 키의 열린 티켓 항목 식별자(slug 우선, 없으면 id). 목록은 한 번 읽는다."""
        from followup_scan import DECISION_ITEM, todo_key
        self.listing = True
        try:
            rows = json.loads(self.cli(['list', '--json'], self.LIST_TIMEOUT) or '[]')
        finally:
            self.listing = False
        return [row.get('slug') or row['id'] for row in rows
                if isinstance(row, dict) and row.get('status') != 'done'
                and isinstance(row.get('id'), str) and todo_key(row) == self.key
                and not DECISION_ITEM.match(row.get('title') or '')]

    def mark(self):
        """모든 항목을 갱신한다. 반환: 'ok', 항목을 못 찾으면 'missing', 그 밖의 실패는 'failed'."""
        missing = False
        for identity in self.ids:
            try:
                self.cli(['working', identity, self.what], self.CALL_TIMEOUT)
                self.marked = True
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                if 'not found' in str(error):
                    missing = True
                    continue
                self.fail('mark', error)
                # 설치된 kit이 `working`을 모르거나 node가 없으면 매분 같은 실패만 나므로 멈춘다. 잠금 대기·시간 초과는 다음 박동에 다시 한다.
                if 'unknown command' in str(error) or isinstance(error, OSError):
                    return 'failed'
        return 'missing' if missing else 'ok'

    def beat(self):
        state = self.mark()
        # 처음 못 찾으면 바로 다시 찾는다: kit이 표시를 새 id로 옮겨 두지만 180초 안에 갱신하지 않으면 꺼진다.
        # 다시 읽어도 못 찾은 항목(삭제 등)이 남으면 다음 다시 읽기는 10분 뒤다.
        if state == 'missing' and (self.retry_at is None or self.clock() >= self.retry_at):
            try:
                self.ids = self.item_ids()
            except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
                # 할 일 잠금 대기·동기화 시간 초과는 일시적이다: 다음 박동에 다시 읽는다.
                self.fail('list', error)
                self.retry_at = self.clock() + self.INTERVAL
                return True
            if self.stop_event.is_set():
                return False
            state = self.mark()
            self.retry_at = self.clock() + self.RESOLVE_GAP if state == 'missing' else None
        return state != 'failed'

    def run(self):
        try:
            self.ids = self.item_ids()
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            self.fail('list', error)
            return
        # 영구 실패(kit이 `working`을 모름·node 없음)면 더 부르지 않는다. 표시는 180초 뒤 저절로 꺼진다.
        while not self.stop_event.is_set() and self.beat() and not self.stop_event.wait(self.INTERVAL):
            pass

    def stop(self):
        """작업자가 끝나면(성공·실패·시간 초과·취소) 마지막으로 찾은 식별자로 표시를 끈다. 길어도 STOP_WAIT+CALL_TIMEOUT초만
        기다린다. 목록을 읽는 중이면 기다리지 않는다: 끄지 못한 표시는 180초 뒤 저절로 꺼진다."""
        self.stop_event.set()
        if self.thread.is_alive() and not self.listing:
            self.thread.join(self.STOP_WAIT)
        if not self.marked or self.thread.is_alive():
            return
        for identity in self.ids:
            try:
                self.cli(['working', identity, '--off'], self.CALL_TIMEOUT)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                self.fail('off', error)


def execute(attempt, command, *, key=None, result=None, declaration=None, timeout=None, workspace=None,
            agent_attempt=None, agent_run=None, unit=None, heartbeat=TodoHeartbeat):
    started = time.time()
    monotonic_started = time.monotonic()
    checkpoint = attempt / 'checkpoint.json'
    output = attempt / 'stdout.json'
    receipt = {'startedAt': started, 'key': key, 'checkpointPath': str(checkpoint),
               'resultPath': str(output), 'usage': None,
               'requirementsSnapshot': str(attempt / 'requirements-snapshot.json') if declaration else None}
    # Snapshot before starting the child. The child cannot waive this declaration.
    if declaration:
        atomic_json(attempt / 'requirements-snapshot.json', declaration)
    process = None
    # 키 없는 실행(아침 점검)은 표시할 할 일 항목이 없다.
    pulse = heartbeat(key, unit).start() if key and heartbeat else None
    try:
        with output.open('w') as stream:
            process = subprocess.Popen(command, stdout=stream,
                env={**os.environ, "JIRA_WORKER_CHECKPOINT": str(checkpoint), "JIRA_WORKER_ATTEMPT": str(attempt)})
            timed_out = False
            try:
                code = process.wait(timeout=timeout)
                timed_out = code == 124
            except subprocess.TimeoutExpired:
                timed_out = True
                # Do not kill descendants or claim their factory lock is released.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                code = 124
        # Stdout remains the worker's exact output, including failed output.
        raw = output.read_bytes()
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        if result:
            Path(result).write_bytes(raw)
        final_valid = valid_result(output, key=key, started_at=started)
        checkpoint_valid = valid_result(checkpoint, key=key, started_at=started)
        value = json.loads(output.read_text()) if final_valid else None
        gate = None
        if declaration:
            gate = requirements.evaluate(declaration, value, key=key)
            atomic_json(attempt / 'gate.json', gate)
            if code == 0 and not gate['passed'] and (not isinstance(value, dict) or value.get('status') == 'done'):
                code = 4
        receipt.update(processExitCode=process.returncode, exitCode=code,
            outcome='timeout' if timed_out else 'failed' if code else 'completed',
            resultValid=final_valid, checkpointValid=checkpoint_valid,
            recoveryEvidence=str(checkpoint) if checkpoint_valid else None,
            stageComplete=(code == 0 and final_valid and value.get('status') == 'done'
                           and value.get('waiting') is None and (gate is None or gate['passed'])))
        return code
    except OSError:
        receipt.update(exitCode=127, processExitCode=None, outcome='launch-failed',
                       resultValid=False, checkpointValid=valid_result(checkpoint, key=key, started_at=started),
                       recoveryEvidence=str(checkpoint) if valid_result(checkpoint, key=key, started_at=started) else None,
                       stageComplete=False)
        return 127
    finally:
        if pulse:
            pulse.stop()
        if agent_attempt and process is not None and (receipt.get('exitCode') or not receipt.get('resultValid')):
            receipt.update(cancel_attempt_agents(agent_attempt, run_token=agent_run, key=key))
        if workspace and key:
            receipt['stoppedServers'] = stop_leftover_servers(workspace, key, started)
        receipt.update(endedAt=time.time())
        receipt['durationSeconds'] = max(0, time.monotonic() - monotonic_started)
        atomic_json(attempt / 'worker.json', receipt)
        # Stable pointer for existing callers; every attempt's receipt stays immutable.
        if result:
            atomic_json(Path(str(result) + '.worker.json'), receipt)


RESOURCE_HINT = ('runtime-local 또는 deploy-<서비스>(저장소 디렉터리 '
                 f'{cfg.SERVICE_REPO_PREFIX}<서비스>, 예: deploy-web)')
REQUIREMENTS_HINT = ('requirements는 key·stage·checks만 두고, checks 행마다 name·artifact·resultField·'
                     'environment(비지 않은 객체) 네 필드가 필수다')


def ship_gate_errors(manifest, *, sdlc=None):
    """첫 push·dev 배포 단위는 sdlc 출고 점검(check ship)이 열려 있어야 띄운다(사용자 결정 2026-11-16).

    단위 이름표(stage)가 lifecycle.SHIP_UNITS면 requirements에 `sdlc-ship` 검사(environment에 key·slug)가 있어야 하고,
    작업자를 띄우기 직전 `sdlc.sh check ship <slug>`를 다시 돌려 exit 0이어야 한다. JIRA_SDLC=off면 다시 돌리지 않는다.
    닫혔으면 호출자는 `lifecycle.py check-ship` 출력의 queueWaiting·question·suggest로 큐를 human-risk로 둔다."""
    if not lifecycle.is_ship_unit(manifest['stage']):
        return []
    row = next((r for r in manifest['checks'] if r['name'] == lifecycle.SHIP_CHECK), None)
    slug = row['environment'].get('slug') if row else None
    if (not row or row['environment'].get('key') != manifest['key'] or not isinstance(slug, str) or not slug):
        return [f"requirements: 첫 push·dev 배포 단위({manifest['stage']})는 {lifecycle.SHIP_CHECK} 검사"
                f"(artifact=lifecycle.py check-ship --out 파일, resultField=result, environment={{key, slug}})가 필요하다"]
    if not lifecycle.enabled():
        return []
    ok, reason = (sdlc or lifecycle.Sdlc()).check_ship(slug)
    if ok:
        return []
    return [f"sdlc check ship {slug} 닫힘: {reason} — 띄우지 않는다. lifecycle.py check-ship 출력대로 waiting=human-risk"]


def declaration_errors(skill, brief, workspace, *, requirements_path=None, key=None, resources=None):
    """작업자를 띄우기 전에 호출자 선언(brief 절 인용·requirements·자원 이름)을 검사한다."""
    from runbook_section import worker_sections
    errors = []
    try:
        worker_sections(skill, brief.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as error:
        errors.append(f'brief: {error} (절 번호에는 RUNBOOK/ORCHESTRATOR 접두어를 붙인다)')
    if requirements_path:
        try:
            declaration = requirements.snapshot(requirements_path)
            if key is not None and declaration['manifest']['key'] != key:
                errors.append('requirements: key가 --key와 다르다')
            errors.extend(ship_gate_errors(declaration['manifest']))
        except (OSError, ValueError, TypeError) as error:
            errors.append(f'requirements: {error} ({REQUIREMENTS_HINT})')
    for name in filter(None, (resources or '').split(',')):
        service = name[len('deploy-'):] if name.startswith('deploy-') else None
        if name != 'runtime-local' and not (service and (Path(workspace) / (cfg.SERVICE_REPO_PREFIX + service)).is_dir()):
            errors.append(f'resources: 모르는 자원 {name} ({RESOURCE_HINT})')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('root', type=Path)
    run = sub.add_parser('execute')
    run.add_argument('--attempt', type=Path, required=True)
    run.add_argument('--result', type=Path)
    run.add_argument('--key')
    run.add_argument('--requirements', type=Path)
    run.add_argument('--timeout', type=timeout_seconds)
    run.add_argument('--workspace', type=Path)
    run.add_argument('--agent-attempt', help='Exact attempt label used by this Paseo launch')
    run.add_argument('--agent-run', help='Factory run label used by this Paseo launch')
    run.add_argument('--unit', help='Unit title shown as the to-do working label')
    run.add_argument('command', nargs=argparse.REMAINDER)
    check = sub.add_parser('declare')
    check.add_argument('--skill', type=Path, required=True)
    check.add_argument('--brief', type=Path, required=True)
    check.add_argument('--workspace', type=Path, required=True)
    check.add_argument('--requirements', type=Path)
    check.add_argument('--key')
    check.add_argument('--resources')
    args = parser.parse_args()
    if args.action == 'declare':
        errors = declaration_errors(args.skill, args.brief, args.workspace, requirements_path=args.requirements,
                                    key=args.key, resources=args.resources)
        for error in errors:
            print('jira-worker: ' + error, file=sys.stderr)
        return 2 if errors else 0
    try:
        if args.action == 'prepare':
            print(prepare(args.root))
            return 0
        declaration = requirements.snapshot(args.requirements) if args.requirements else None
        if declaration and args.key is not None and declaration['manifest']['key'] != args.key:
            raise ValueError('manifest key mismatch')
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        return execute(args.attempt, command, key=args.key, result=args.result,
                       declaration=declaration, timeout=args.timeout, workspace=args.workspace,
                       agent_attempt=args.agent_attempt, agent_run=args.agent_run, unit=args.unit)
    except (ValueError, OSError):
        print('jira-worker: invalid execution declaration', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
