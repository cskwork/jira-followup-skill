"""아침 점검의 의존성 상태와 조치 이월(2026-11-10).

11-06·11-09·11-10 보고서는 Teams 감시 52회 연속 실패(마지막 성공 11-02 10:01)와 스테이징 kube 인증 실패(11-06 17:28 스테이징
승인 미사용) 속에서 'BLOCKED·오류 0건', '오류 없음'을 썼다. 조치 필요 수는 12 → 13 → 19건으로 늘었는데 같은 조치를
올리는 장치가 없었다. Jira·git·결정 화면은 가짜로 받는다.
"""
import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_page  # noqa: E402
import kube_pods  # noqa: E402
import morning_check as m  # noqa: E402

NOW = datetime.fromisoformat('2026-11-10T08:09:00+09:00')
TEAMS_DOWN = {'status': 'unavailable', 'error': 'BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED',
              'monitoring': {'lastSuccessAt': '2026-11-02T10:01:08.051909+09:00',
                             'error': 'BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED', 'failures': 52}}
QUEUE = {'PROJ-1501': {'stage': 'stg-ready', 'waiting': 'env-human', 'nextStep': 'stg-smoke',
                      'needs': 'stg-web,stg-kube'}}
APPROVALS = {'PROJ-1501': {'stg': "답글 '승인' (할 일 앱, 2026-11-06 17:28)", 'at': '2026-11-06T17:28:08.412604+09:00'},
             'PROJ-1442': {'stg': '승인', 'at': '2026-11-03T09:00:00+09:00', 'usedAt': '2026-11-03T10:00:00+09:00'}}


def ok(argv, out=''):
    return subprocess.CompletedProcess(argv, 0, out, '')


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.base, self.root, self.skills = self.tmp / 'daily', self.tmp / 'root', self.tmp / 'skills'
        self.base.mkdir()
        self.root.mkdir()
        self.runs = 0
        # kube 점검이 설정된 환경(KUBE_CONNECT_CMD). 미설정이면 스테이징 kube 행은 표시하지 않는다.
        self.addCleanup(setattr, kube_pods, 'CONNECT', kube_pods.CONNECT)
        kube_pods.CONNECT = 'kube-connect-fixture'

    def write(self, name, value):
        m.dump(self.base / name, value)

    def runner(self, argv, timeout):
        if argv[0] == 'git':
            if argv[3] == 'remote':
                return ok(argv, 'https://example.invalid/repo')
            return ok(argv)  # fetch·for-each-ref(회고 제안 없음)
        self.assertEqual(argv[:4], ['acli', 'jira', 'workitem', 'search'])
        return ok(argv, '[]')

    def run_main(self, execute=False, **kwargs):
        self.runs += 1
        out = self.tmp / f'out-{self.runs}'
        args = ['--base', str(self.base), '--root', str(self.root), '--out-dir', str(out)]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = m.main(args + (['--execute'] if execute else []), runner=self.runner, rest=lambda path: {},
                        note=lambda date: ok([], 'devnote=ok item=NOTE-1\n'), now=kwargs.pop('now', NOW),
                        skills_repo=self.skills, **kwargs)
        return rc, (out / 'report.md').read_text(), json.loads((out / 'errors.json').read_text())


def headline(report):
    return next(line for line in report.splitlines() if line.startswith('| BLOCKED·오류 |'))


def health_row(report, name):
    return next(line for line in report.splitlines() if line.startswith(f'| {name} |'))


class DependencyHealth(Fixture):
    def test_recorded_1110_failures_are_counted_and_never_reported_as_no_errors(self):
        self.write('teams-scan.json', TEAMS_DOWN)
        self.write('followup-queue.json', QUEUE)
        self.write('approvals.json', APPROVALS)
        self.write('guard-last.json', {
            'at': '2026-11-10T08:03:00+09:00', 'reasons': ['teams-unavailable:BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED'],
            'internalErrors': [], 'waitingHuman': {kube_pods.failure('kube-auth', 'stg')['hint']: ['PROJ-1501']},
            'monitoring': {'jira': {'failureCount': 0, 'lastSuccessAt': '2026-11-10T08:03:00+09:00'}}})
        rc, report, errors = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn('| BLOCKED·오류 | 2건 (의존성 실패 2건 포함)', headline(report))
        for wording in ('오류 없음', '오류 0건', '| BLOCKED·오류 | 0건', '## 오류\n\n없음'):
            self.assertNotIn(wording, report)
        teams = health_row(report, 'Teams 방 감시')
        for part in ('| FAIL |', '마지막 성공 2026-11-02 10:01', '| 52회 |', 'BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED'):
            self.assertIn(part, teams)
        kube = health_row(report, '스테이징 kube 인증')
        self.assertIn('| FAIL |', kube)
        self.assertIn('kube-auth', kube)
        # 환경 때문에 쓰지 못한 승인은 그 의존성 아래에 남는다. 이미 쓴 승인은 나오지 않는다.
        self.assertIn('PROJ-1501(env-human) 스테이징 승인 2026-11-06 17:28 미사용', kube)
        self.assertNotIn('PROJ-1442', report)
        self.assertEqual(sorted(e['key'] for e in errors if e['scope'] == '의존성'), ['Teams 방 감시', '스테이징 kube 인증'])
        # 기록이 없는 것은 정상이 아니라 미확인이고, 오류 수에 넣지 않는다.
        self.assertIn('| UNKNOWN |', health_row(report, '서비스 저장소 git fetch'))
        self.assertIn('미확인 2(서비스 저장소 git fetch, dev·스테이징 사설망(VPN))', report)

    def test_vpn_missing_is_its_own_failure_and_kube_stays_unknown(self):
        # 11-10 08:03 VPN 미연결: preflight는 stg-kube를 'vpn' 이유로 막는다. kube 인증은 확인하지 못했다.
        self.write('followup-queue.json', QUEUE)
        self.write('approvals.json', APPROVALS)
        self.write('guard-last.json', {'at': '2026-11-10T08:03:00+09:00', 'reasons': [],
                                       'waitingHuman': {kube_pods.failure('vpn', 'stg')['hint']: ['PROJ-1501']}})
        _, report, _ = self.run_main()
        vpn = health_row(report, 'dev·스테이징 사설망(VPN)')
        self.assertIn('| FAIL |', vpn)
        self.assertIn('stg VPN 연결 필요', vpn)
        self.assertIn('PROJ-1501(env-human) 스테이징 승인 2026-11-06 17:28 미사용', vpn)
        self.assertIn('| UNKNOWN |', health_row(report, '스테이징 kube 인증'))
        self.assertIn('| BLOCKED·오류 | 1건 (의존성 실패 1건 포함)', headline(report))

    def test_stale_guard_record_is_unknown_not_healthy(self):
        self.write('followup-queue.json', QUEUE)
        self.write('guard-last.json', {'at': '2026-11-05T18:30:00+09:00', 'reasons': [],
                                       'waitingHuman': {kube_pods.failure('kube-auth', 'stg')['hint']: ['PROJ-1501']}})
        _, report, errors = self.run_main()
        self.assertEqual(errors, [])
        self.assertIn('| UNKNOWN |', health_row(report, '공장 가드 실행'))
        self.assertIn('| UNKNOWN |', health_row(report, '환경 대기(stg-web,stg-kube)'))
        self.assertNotIn('| PASS |', health_row(report, '스테이징 kube 인증'))

    def test_all_dependencies_healthy_reports_zero_errors(self):
        (self.root / 'api' / '.git').mkdir(parents=True)
        self.write('teams-scan.json', {'status': 'ok', 'monitoring': {'lastSuccessAt': NOW.isoformat(), 'failures': 0}})
        self.write('followup-queue.json', {'PROJ-1442': {'waiting': 'env-human', 'needs': 'stg-web,stg-kube'}})
        self.write('guard-last.json', {'at': '2026-11-10T08:03:00+09:00', 'reasons': ['environment-ready:PROJ-1442'],
                                       'internalErrors': [], 'waitingHuman': {}})
        rc, report, errors = self.run_main()
        self.assertEqual((rc, errors), (0, []))
        self.assertIn('| BLOCKED·오류 | 0건 / 댓글 모델 판정 대기 0건 |', report)
        self.assertIn('| 의존성 | 실패 0 / 미확인 0 / 경고 0 / 정상 7 |', report)
        self.assertIn('## 오류\n\n없음', report)
        for name in ('Teams 방 감시', 'Jira 인증', '서비스 저장소 git fetch', '공장 가드 실행', 'dev·스테이징 사설망(VPN)',
                     '스테이징 kube 인증', '스테이징 웹'):
            self.assertIn('| PASS |', health_row(report, name))


class CarryOver(Fixture):
    """하루 = 보고서 하나. report → save_carry → (실행이면) carry_decision."""

    def setUp(self):
        super().setUp()
        self.day = datetime.fromisoformat('2026-11-03T08:00:00+09:00')  # 첫 보고서는 다음 날(수)
        self.opener = Mock(side_effect=lambda base, path: decision_page.register_code(
            base, *decision_page.load_spec('spec', str(path), base), None, server=False))

    def report(self, keys, *, teams=False, advance=True):
        if advance:
            self.day += timedelta(days=3 if self.day.weekday() == 4 else 1)
        self.write('teams-scan.json', TEAMS_DOWN if teams else {'status': 'ok', 'monitoring': {}})
        issues = [{'key': key, 'summary': '요약', 'status': '진행 중', 'previousStatus': '진행 중', 'newlyObserved': False,
                   'newlyAssigned': False, 'history': [], 'freshComments': [], 'subtasks': [], 'refs': {'repos': []},
                   'idleBusinessDays': 10 + self.day.day} for key in keys]
        self.runs += 1
        morning = m.Morning(self.base, self.root, self.tmp / f'out-{self.runs}', runner=self.runner)
        text = morning.report({'startedAt': self.day.isoformat(), 'since': self.day.isoformat(), 'issues': issues,
                               'repoFetch': []}, [], {})[0]
        morning.save_carry()
        return morning, text

    def answer(self, code, option):
        decision_page.update_code(self.base, code, 'decided', messageId='todo:test:' + code)
        with (self.base / 'decisions.jsonl').open('a') as log:
            log.write(json.dumps({'key': 'FACTORY', 'option': option, 'messageId': 'todo:test:' + code}) + '\n')

    def test_third_report_opens_one_decision_and_same_set_is_not_reasked(self):
        for _ in range(2):
            morning, _ = self.report(['PROJ-1422', 'OTHER-139'], teams=True)
            self.assertEqual(morning.carry_decision(self.opener), 'none')
        morning, text = self.report(['PROJ-1422', 'OTHER-139'], teams=True)
        self.assertIn('| PROJ-1422 | 업무일 미갱신 | 3회 | 2026-11-04 |', text)
        self.assertIn('| FACTORY | Teams 감시 복구 | 3회 | 2026-11-04 |', text)
        self.assertEqual(morning.carry_decision(self.opener), 'opened')
        spec = json.loads(self.opener.call_args.args[1].read_text())
        self.assertEqual(spec['title'], '[아침 점검] 3회 넘긴 조치 3건')
        self.assertEqual([o['id'] for o in spec['options']], ['now', 'defer', 'drop'])
        self.assertIn(spec['recommended'], [o['id'] for o in spec['options']])
        for part in ('PROJ-1422', 'OTHER-139', 'Teams 감시 복구', '3회째, 2026-11-04부터'):
            self.assertIn(part, spec['summary'])
        # 4번째 보고서: 같은 묶음은 결정이 열려 있든 정해졌든 다시 묻지 않는다.
        morning, _ = self.report(['PROJ-1422', 'OTHER-139'], teams=True)
        self.assertEqual(morning.carry_decision(self.opener), 'already-asked')
        self.answer('D1', 'now')
        morning, _ = self.report(['PROJ-1422', 'OTHER-139'], teams=True)
        self.assertEqual(morning.carry_decision(self.opener), 'already-asked')
        self.assertEqual(self.opener.call_count, 1)

    def test_changed_set_waits_for_open_decision_then_asks(self):
        for keys in (['PROJ-1422'], ['PROJ-1422', 'PROJ-1385'], ['PROJ-1422', 'PROJ-1385']):
            morning, _ = self.report(keys)
        self.assertEqual(morning.carry_decision(self.opener), 'opened')
        morning, _ = self.report(['PROJ-1422', 'PROJ-1385'])  # PROJ-1385 3회째: 묶음이 바뀌었다
        self.assertEqual(morning.carry_decision(self.opener), 'already-asked')  # D1 답을 기다린다
        self.answer('D1', 'now')
        self.assertEqual(morning.carry_decision(self.opener), 'opened')
        self.assertEqual(json.loads(self.opener.call_args.args[1].read_text())['title'], '[아침 점검] 3회 넘긴 조치 2건')

    def test_disappearing_item_resets_and_same_day_rerun_does_not_count_twice(self):
        self.report(['PROJ-1422'])
        self.report(['PROJ-1422'])
        _, text = self.report(['PROJ-1422'], advance=False)  # 같은 날 재실행은 횟수를 올리지 않는다
        self.assertIn('| PROJ-1422 | 업무일 미갱신 | 2회 |', text)
        _, text = self.report([])
        self.assertIn('## 조치 연속 횟수', text)
        morning, text = self.report(['PROJ-1422'])
        self.assertIn('| PROJ-1422 | 업무일 미갱신 | 1회 |', text)
        self.assertEqual(morning.carry_decision(self.opener), 'none')

    def test_defer_and_drop_answers_suppress_from_count(self):
        for option in ('defer', 'drop'):
            with self.subTest(option=option):
                self.setUp()
                for _ in range(3):
                    morning, _ = self.report(['PROJ-1422', 'OTHER-139'])
                self.assertEqual(morning.carry_decision(self.opener), 'opened')
                self.answer('D1', option)
                morning, text = self.report(['PROJ-1422', 'OTHER-139'])
                self.assertIn('| 조치 필요 수 | 0건 (미룸·뺌 2건 제외) |', text)
                self.assertIn('| 오늘 먼저 할 일 | 추가 조치 없음 |', text)
                self.assertIn('(D1)', health_row(text, 'PROJ-1422 | 업무일 미갱신'))
                if option == 'defer':
                    until = json.loads((self.base / 'morning-carry.json').read_text())['decisions']
                    self.day = datetime.fromisoformat(next(iter(until.values()))['deferUntil'] + 'T08:00:00+09:00')
                    _, text = self.report(['PROJ-1422', 'OTHER-139'], advance=False)
                    self.assertIn('미룸·뺌 2건 제외', text)  # 정한 날까지는 뺀다
                    _, text = self.report(['PROJ-1422', 'OTHER-139'])
                    self.assertIn('| 조치 필요 수 | 2건 |', text)  # 그 뒤로는 다시 센다
                else:
                    self.report([])  # 사라졌다 다시 나오면 다시 센다
                    _, text = self.report(['PROJ-1422', 'OTHER-139'])
                    self.assertIn('| 조치 필요 수 | 2건 |', text)

    def test_main_opens_only_with_execute_on_default_base(self):
        self.write('teams-scan.json', TEAMS_DOWN)
        self.write('morning-carry.json', {'date': '2026-11-09', 'previous': {}, 'decisions': {},
                                          'current': {'items': {'FACTORY|teams-watch': {'count': 2, 'since': '2026-11-06'}}}})
        before = (self.base / 'morning-carry.json').read_text()
        opener = Mock()
        _, report, _ = self.run_main(carry_opener=opener)  # 사용자 지정 --base
        self.assertIn('| FACTORY | Teams 감시 복구 | 3회 | 2026-11-06 |', report)
        self.assertIn('3회 넘긴 조치 1건: --execute 기본 상태 폴더에서만 연다.', report)
        self.run_main(True, carry_opener=opener)
        self.assertFalse(opener.called)
        with patch.object(m, 'BASE', self.base):
            (self.base / 'morning-carry.json').write_text(before)
            self.run_main(carry_opener=opener)  # dry-run: 열지도, 기록하지도 않는다
            self.assertFalse(opener.called)
            self.assertEqual((self.base / 'morning-carry.json').read_text(), before)
            _, report, _ = self.run_main(True, carry_opener=opener)
        self.assertEqual(opener.call_count, 1)
        self.assertIn('3회 넘긴 조치 1건: 결정을 열었다.', report)
        saved = json.loads((self.base / 'morning-carry.json').read_text())
        self.assertEqual(saved['current']['items']['FACTORY|teams-watch'], {'count': 3, 'since': '2026-11-06'})
        self.assertEqual(list(saved['decisions'].values())[0]['items'], {'FACTORY|teams-watch': '2026-11-06'})


if __name__ == '__main__':
    unittest.main()
