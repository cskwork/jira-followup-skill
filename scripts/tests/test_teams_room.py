"""Offline Teams room watch contracts: trigger, high-water mark, idle wiring, approval-gated reply.

No test reaches Teams or a browser: the reader is injected and the CLI is a local stub script.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import decision_page as dp  # noqa: E402
import followup_idle as idle  # noqa: E402
import teams_room as tr  # noqa: E402

OTHER = "8:user:00000000-0000-0000-0000-0000000000aa"
NOW = datetime(2026, 10, 23, 10, 30, tzinfo=timezone(timedelta(hours=9)))


def msg(n, text, *, author="김철수(Kim Chul su) 선임", mri=OTHER, mentions=()):
    return {"id": str(1761190000000 + n), "time": f"2026-10-23T01:{n:02d}:00.000Z",
            "author": author, "authorMri": mri, "text": text, "mentions": list(mentions)}


def mine(n, text="네 확인할게요"):
    return msg(n, text, author=tr.ME_NAME, mri=tr.ME_MRI)


def entities(text):
    return "".join(f"&#{ord(c)};" for c in text)


# 가상의 감시 방과 내 계정(config.env의 TEAMS_ROOM_CHAT_ID·TEAMS_ME_* 자리). 시험마다 모듈 값을 바꿔 넣는다.
IDENTITY = {"CHAT_ID": "19:room@example", "CHAT_NAME": "기획+개발 방",
            "ME_MRI": "8:user:00000000-0000-0000-0000-000000000001", "ME_NAME": "홍길동(Hong Gil dong) 선임",
            "TRIGGER": tr.trigger_pattern(["홍 ?선임", "홍 ?대리", "홍댈", "홍길동", "길동 ?(?:선임|대리)", "Hong Gil ?dong"])}


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="teams room ")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        patcher = patch.multiple(tr, **IDENTITY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self, messages):
        return tr.scan(self.dir, fetch=lambda: list(reversed(messages)), now=NOW)  # CLI order: newest first

    def room(self):
        return json.loads((self.dir / "teams-room.json").read_text())[tr.CHAT_ID]

    def handle(self, cid, action="drafted"):
        path = self.dir / "state.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        state.setdefault("followup", {}).setdefault("handled", {})[cid] = {"key": "TEAMS-ROOM", "action": action}
        path.write_text(json.dumps(state, ensure_ascii=False))


class Trigger(Fixture):
    def test_positive_names_and_mri_mention(self):
        for text in ("홍선임 이거 봐주세요", "홍 선임님 확인 부탁", "홍대리 배포요", "홍 대리님", "홍댈 ㅎㅎ",
                     "홍길동 선임님", "길동선임이 키우는 고양이", "길동 대리님", "@Hong Gil dong please", "hong gildong check", entities("홍선임") + " 확인"):
            with self.subTest(text=text):
                self.assertEqual(tr.trigger_of(msg(1, text)), "name")
        self.assertEqual(tr.trigger_of(msg(1, "이거 봐주세요", mentions=[tr.ME_MRI])), "mention")

    def test_negatives_and_own_messages_are_ignored(self):
        for text in ("김철수 선임님 확인", "홍보방 공지", "홍  선임", "선임님들 회의", "hong gil  dong"):
            with self.subTest(text=text):
                self.assertIsNone(tr.trigger_of(msg(1, text)))
        self.assertIsNone(tr.trigger_of(msg(1, "다른 분 멘션", mentions=[OTHER])))
        self.assertIsNone(tr.trigger_of(mine(1, "홍길동입니다 홍선임")))
        self.assertIsNone(tr.trigger_of(msg(1, "홍선임", author="x", mri=tr.ME_MRI)))
        self.assertIsNone(tr.trigger_of(msg(1, "홍선임", author=tr.ME_NAME, mri="")))
        self.assertIsNone(tr.trigger_of(msg(1, "홍선임", mri=tr.ME_MRI, mentions=[tr.ME_MRI])))


class HighWaterMark(Fixture):
    def test_first_scan_is_baseline_only(self):
        out = self.scan([msg(1, "홍선임 예전 요청")])
        self.assertTrue(out["baseline"])
        self.assertEqual(out["events"], [])
        self.assertEqual(self.room()["mark"]["id"], msg(1, "")["id"])

    def test_new_trigger_emits_once_with_context_until_handled(self):
        history = [msg(i, f"잡담 {i}") for i in range(1, 8)]
        self.scan(history)
        trigger = msg(8, "<p>홍 선임님 PROJ-1405 dev 배포 &amp; 확인 부탁드려요</p>")
        out = self.scan(history + [trigger])
        self.assertEqual(len(out["events"]), 1)
        event = out["events"][0]
        self.assertEqual((event["commentId"], event["key"], event["keys"]), ("teams:" + trigger["id"], "PROJ-1405", ["PROJ-1405"]))
        self.assertEqual(event["text"], "홍 선임님 PROJ-1405 dev 배포 & 확인 부탁드려요")
        self.assertEqual([c["text"] for c in event["context"]], [f"잡담 {i}" for i in range(3, 8)])
        self.assertEqual((event["mentionsMe"], event["assignedToMe"], event["answeredByMeAfter"]), (True, False, False))
        # 같은 메시지를 다시 읽어도 새 이벤트가 생기지 않는다(미처리 이벤트 하나만 남는다).
        again = self.scan(history + [trigger, msg(9, "일반 대화")])
        self.assertEqual([e["commentId"] for e in again["events"]], [event["commentId"]])
        self.handle(event["commentId"])
        done = self.scan(history + [trigger, msg(9, "일반 대화")])
        self.assertEqual(done["events"], [])
        self.assertEqual(self.room()["pending"], {})
        self.assertEqual(self.room()["mark"]["id"], msg(9, "")["id"])

    def test_handled_trigger_never_returns_even_if_reread(self):
        self.scan([msg(1, "x")])
        self.scan([msg(1, "x"), msg(2, "홍댈 이거")])
        self.handle("teams:" + msg(2, "")["id"])
        self.assertEqual(self.scan([msg(1, "x"), msg(2, "홍댈 이거")])["events"], [])
        self.assertEqual(self.scan([msg(2, "홍댈 이거"), msg(3, "ok")])["events"], [])

    def test_state_keys_are_preserved(self):
        (self.dir / "teams-room.json").write_text(json.dumps({"19:other@example": {"mark": {"id": "1"}},
                                                               tr.CHAT_ID: {"note": "keep"}}))
        state = {"morning": {"at": "x"}, "followup": {"cursor": "keep", "handled": {"49001": {"key": "PROJ-1"}}}}
        (self.dir / "state.json").write_text(json.dumps(state))
        before = (self.dir / "state.json").read_bytes()
        self.scan([msg(1, "x")])
        data = json.loads((self.dir / "teams-room.json").read_text())
        self.assertEqual(data["19:other@example"], {"mark": {"id": "1"}})
        self.assertEqual(data[tr.CHAT_ID]["note"], "keep")
        self.assertEqual((self.dir / "state.json").read_bytes(), before)

    def test_my_later_message_marks_answered_by_me(self):
        self.scan([msg(1, "x")])
        out = self.scan([msg(1, "x"), msg(2, "홍길동 선임님 확인요"), mine(3)])
        self.assertTrue(out["events"][0]["answeredByMeAfter"])
        # 내 메시지는 이벤트가 아니다.
        self.assertEqual(len(out["events"]), 1)

    def test_full_page_of_new_messages_reports_possible_gap(self):
        self.scan([msg(0, "x")])
        page = [msg(i, f"m{i}") for i in range(1, tr.LIMIT + 1)]
        self.assertEqual(self.scan(page)["errors"], [{"scope": "teams", "error": "possible-gap"}])


class Unavailable(Fixture):
    def test_unreadable_room_keeps_pending_events_and_mark(self):
        self.scan([msg(1, "x")])
        self.scan([msg(1, "x"), msg(2, "홍선임 질문")])
        mark = self.room()["mark"]

        def down():
            raise tr.Unavailable("BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED")
        out = tr.scan(self.dir, fetch=down, now=NOW)
        self.assertEqual((out["status"], out["error"]), ("unavailable", "BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED"))
        self.assertEqual(len(out["events"]), 1)
        self.assertEqual(self.room()["mark"], mark)
        self.assertEqual(self.room()["lastError"]["error"], "BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED")

    def test_missing_cli_and_cli_errors_become_codes(self):
        with self.assertRaisesRegex(tr.Unavailable, "TEAMS_CLI_NOT_FOUND"):
            tr.fetch_messages(cli=str(self.dir / "absent"))
        fake = self.dir / "fake.py"
        fake.write_text("import json; print(json.dumps({'status':'BLOCKED','error':'RUN_CONNECT_FIRST'}))")
        with self.assertRaisesRegex(tr.Unavailable, "RUN_CONNECT_FIRST"):
            tr.fetch_messages(cli=str(fake))
        fake.write_text("print('token=abc not json')")
        with self.assertRaisesRegex(tr.Unavailable, "TEAMS_CLI_INVALID_OUTPUT"):
            tr.fetch_messages(cli=str(fake))

    def test_lost_task_space_reconnects_then_reads(self):
        # 2026-10-29: 브라우저 재시작으로 저장된 공간이 사라져 32회 연속 읽기 실패.
        fake = self.dir / "fake.py"
        log = self.dir / "calls"
        fake.write_text(
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log)!r})\n"
            "calls = log.read_text().split() if log.exists() else []\n"
            "log.write_text(' '.join(calls + [sys.argv[1]]))\n"
            "if sys.argv[1] == 'connect': print(json.dumps({'status':'PASS'}))\n"
            "elif 'connect' in calls: print(json.dumps({'status':'PASS','messages':[]}))\n"
            "else: print(json.dumps({'status':'BLOCKED','error':'TASK_SPACE_NOT_FOUND'}))\n")
        self.assertEqual(tr.fetch_messages(cli=str(fake)), [])
        self.assertEqual(log.read_text().split(), ["messages", "connect", "messages"])

    def test_cli_scan_writes_output_and_exits_zero_when_unavailable(self):
        out = self.dir / "teams-scan.json"
        env = {"HOME": str(self.dir), "PATH": "/usr/bin:/bin", "TEAMS_NATIVE_CLI": str(self.dir / "absent"),
               "TEAMS_ROOM_CHAT_ID": IDENTITY["CHAT_ID"]}
        process = subprocess.run([sys.executable, str(ROOT / "teams_room.py"), "scan", "--dir", str(self.dir),
                                  "--out", str(out)], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0)
        self.assertIn("status=unavailable", process.stdout)
        self.assertEqual(json.loads(out.read_text())["error"], "TEAMS_CLI_NOT_FOUND")

    def test_cli_scan_without_room_is_clean_not_configured(self):
        out = self.dir / "teams-scan.json"
        env = {"HOME": str(self.dir), "PATH": "/usr/bin:/bin", "TEAMS_NATIVE_CLI": str(self.dir / "absent")}
        process = subprocess.run([sys.executable, str(ROOT / "teams_room.py"), "scan", "--dir", str(self.dir),
                                  "--out", str(out)], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout.strip(), "teams status=ok events=0 errors=0 configured=false")
        data = json.loads(out.read_text())
        self.assertEqual((data["status"], data["configured"], data["events"], data["errors"]), ("ok", False, [], []))
        self.assertFalse((self.dir / "teams-room.json").exists())


class SuccessTelemetry(Fixture):
    def unavailable(self):
        raise tr.Unavailable("RUN_CONNECT_FIRST")

    def test_first_failure_records_null_without_inferring_success_from_mark(self):
        (self.dir / "teams-room.json").write_text(json.dumps({
            tr.CHAT_ID: {"mark": {"id": "1", "time": NOW.isoformat()}}}))
        out = tr.scan(self.dir, fetch=self.unavailable, now=NOW)
        self.assertIsNone(out["monitoring"]["lastSuccessAt"])
        self.assertIsNone(self.room()["monitoring"]["lastSuccessAt"])
        self.assertEqual(out["error"], "RUN_CONNECT_FIRST")

    def test_empty_success_failure_and_recovery_preserve_actual_success_time(self):
        out = tr.scan(self.dir, fetch=lambda: [], now=NOW)
        self.assertEqual(out["monitoring"]["lastSuccessAt"], NOW.isoformat())
        self.assertNotIn("mark", self.room())
        failed_at = NOW + timedelta(days=2)
        down = tr.scan(self.dir, fetch=self.unavailable, now=failed_at)
        self.assertEqual(down["monitoring"]["lastSuccessAt"], NOW.isoformat())
        self.assertEqual(self.room()["monitoring"]["lastSuccessAt"], NOW.isoformat())
        self.assertEqual(self.room()["lastError"]["at"], failed_at.isoformat())
        recovered_at = failed_at + timedelta(minutes=30)
        recovered = tr.scan(self.dir, fetch=lambda: [], now=recovered_at)
        self.assertEqual(recovered["monitoring"]["lastSuccessAt"], recovered_at.isoformat())
        self.assertEqual(self.room()["monitoring"]["lastSuccessAt"], recovered_at.isoformat())
        self.assertNotIn("lastError", self.room())


class IdleWiring(Fixture):
    def decide(self, teams_payload=None, *, classifier=None, teams_writes=True):
        calls = []

        def run(argv, *args, **kwargs):
            calls.append(argv)
            output = Path(argv[argv.index("--out") + 1])
            if argv[0] == "fixture-scan":
                output.write_text(json.dumps({"events": [], "errors": []}))
            elif argv[0] == "fixture-teams" and teams_writes:
                output.write_text(json.dumps(teams_payload))
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(idle, "run", side_effect=run), patch.object(idle, "notify", return_value=True) as notify:
            result, code = idle.decide(self.dir, ["fixture-scan"], ["fixture-preflight"],
                                       classifier=classifier or Mock(return_value={"predictions": []}),
                                       teams_cmd=["fixture-teams"])
        self.calls, self.notify = calls, notify
        return result, code

    def teams_event(self, **over):
        return {"source": "teams", "key": "TEAMS-ROOM", "commentId": "teams:1", "text": "홍선임 질문",
                "mentionsMe": True, "assignedToMe": False, "answeredByMeAfter": False, **over}

    def predict(self, **over):
        return Mock(return_value={"predictions": [{"commentId": "teams:1", "class": "정보 공유",
                    "needs_response": False, "confidence": 0.95, "reason": "fixture", **over}]})

    def test_no_new_teams_trigger_is_idle_and_teams_gets_dir(self):
        result, code = self.decide({"status": "ok", "events": [], "errors": []})
        self.assertEqual((result["idle"], code, result["teams"]), (True, 0, {"status": "ok", "events": 0}))
        teams = [argv for argv in self.calls if argv[0] == "fixture-teams"][0]
        self.assertEqual(teams[teams.index("--dir") + 1], str(self.dir))

    def test_new_trigger_wakes_in_shadow_with_one_luna_call_and_recipient_context(self):
        classifier = self.predict()
        result, code = self.decide({"status": "ok", "events": [self.teams_event()], "errors": []}, classifier=classifier)
        self.assertEqual((result["idle"], code, result["events"]), (False, 1, 1))
        self.assertIn("teams-new-events", result["reasons"])
        sent = classifier.call_args.args[0]
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["source"], sent[0]["mentionsMe"]), ("teams", True))
        self.assertEqual(len((self.dir / "triage-shadow.jsonl").read_text().splitlines()), 1)

    def test_enforce_skips_only_confident_information(self):
        (self.dir / "triage-mode").write_text("enforce\n")
        result, code = self.decide({"status": "ok", "events": [self.teams_event()], "errors": []}, classifier=self.predict())
        self.assertEqual((result["idle"], code), (True, 0))
        handled = json.loads((self.dir / "state.json").read_text())["followup"]["handled"]
        self.assertEqual(handled["teams:1"]["action"], "skipped-luna")
        result, _ = self.decide({"status": "ok", "events": [self.teams_event()], "errors": []},
                                classifier=self.predict(needs_response=True, **{"class": "질문 답변"}))
        self.assertFalse(result["idle"])

    def test_my_reply_after_trigger_is_human_and_idle_without_model(self):
        classifier = Mock(side_effect=AssertionError("must not classify"))
        result, code = self.decide({"status": "ok", "events": [self.teams_event(answeredByMeAfter=True)], "errors": []},
                                   classifier=classifier)
        self.assertEqual((result["idle"], code), (True, 0))
        self.assertEqual(json.loads((self.dir / "state.json").read_text())["followup"]["handled"]["teams:1"]["action"], "human")

    def test_unavailable_is_reported_not_idle_silent_and_notifies_once_a_day(self):
        payload = {"status": "unavailable", "error": "BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED", "events": [], "errors": []}
        result, code = self.decide(payload)
        self.assertEqual((result["idle"], code), (True, 0))
        self.assertEqual(result["reasons"], ["teams-unavailable:BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED"])
        self.assertEqual(result["teams"]["status"], "unavailable")
        self.notify.assert_called_once()
        self.assertIn("방 감시 불가", self.notify.call_args.args[0])
        self.decide(payload)
        self.notify.assert_not_called()
        last = json.loads((self.dir / "guard-last.json").read_text())
        self.assertIn("teams-unavailable:BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED", last["reasons"])

    def test_unavailable_with_pending_trigger_still_wakes(self):
        payload = {"status": "unavailable", "error": "CLI_BUSY", "events": [self.teams_event()], "errors": []}
        result, code = self.decide(payload, classifier=self.predict(needs_response=True))
        self.assertEqual((result["idle"], code), (False, 1))
        self.assertIn("teams-unavailable:CLI_BUSY", result["reasons"])

    def test_teams_scan_crash_does_not_crash_the_tick(self):
        result, code = self.decide(teams_writes=False)
        self.assertEqual((result["idle"], code), (True, 0))
        self.assertEqual(result["reasons"], ["teams-unavailable:TEAMS_SCAN_FAILED"])

    def test_malformed_teams_output_is_unavailable_without_waking_model(self):
        result,code=self.decide([])
        self.assertEqual((result['idle'],code),(True,0))
        self.assertEqual(result['monitoringStatus'],'unavailable')
        self.assertIn('teams-unavailable:TEAMS_SCAN_FAILED',result['reasons'])

    def test_missing_cli_does_not_notify(self):
        self.decide({"status": "unavailable", "error": "TEAMS_CLI_NOT_FOUND", "events": [], "errors": []})
        self.notify.assert_not_called()


FAKE_CLI = r'''
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_CALLS"]).open("a").write(json.dumps(sys.argv[1:]) + "\n")
cmd = sys.argv[1]
if cmd == "send":
    text = Path(sys.argv[sys.argv.index("--text-file") + 1]).read_text()
    Path(os.environ["FAKE_CALLS"] + ".text").write_text(text)
    print(json.dumps({"status": os.environ.get("FAKE_SEND", "PASS"), "messageId": "1761199999999", "verified": True}))
elif cmd == "verify":
    print(json.dumps({"status": os.environ.get("FAKE_VERIFY", "PASS"), "verified": True}))
elif cmd == "status":
    print(os.environ.get("FAKE_STATUS", json.dumps({"status": "PASS", "account": {
        "id": "00000000-0000-0000-0000-000000000001",
        "tenantId": "00000000-0000-0000-0000-000000000002"}})))
elif cmd == "messages":
    print(os.environ["FAKE_MESSAGES"])
else:
    print(json.dumps({"status": "FAIL"}))
'''


class Reply(Fixture):
    def setUp(self):
        super().setUp()
        self.scan([msg(1, "x")])
        self.scan([msg(1, "x"), msg(2, "홍선임 PROJ-1405 dev에 반영됐나요?")])
        self.event_id = "teams:" + msg(2, "")["id"]
        self.cli = self.dir / "fake-cli.py"
        self.cli.write_text(FAKE_CLI)
        self.calls = self.dir / "cli-calls.jsonl"
        patcher = patch.dict("os.environ", FAKE_CALLS=str(self.calls))
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def make_draft(self, text="김철수 선임님 PROJ-1405 dev 반영 확인했습니다."):
        opened = []
        result = tr.draft(self.dir, self.event_id, text, "김철수 선임님", opener=opened.append)
        self.assertEqual(len(opened), 1)
        return result

    def approve(self, draft_id, sha):
        with (self.dir / "decisions.jsonl").open("a") as f:
            f.write(json.dumps({"kind": "teams-reply", "option": "approve", "by": "button",
                                "draftId": draft_id, "textSha256": sha, "at": "2026-10-23T10:40"}) + "\n")

    def test_draft_writes_spec_with_mention_and_real_jira_link_and_never_sends(self):
        result = self.make_draft()
        record = json.loads(Path(result["draft"]).read_text())
        spec = json.loads(Path(result["spec"]).read_text())
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["mentions"], [["김철수 선임님", "김철수(Kim Chul su) 선임"]])
        self.assertEqual(record["links"], [["PROJ-1405", "https://jira.example.com/browse/PROJ-1405"]])
        self.assertEqual((spec["kind"], spec["draftId"], spec["textSha256"]), ("teams-reply", record["draftId"], record["textSha256"]))
        self.assertEqual(self.cli_calls(), [])
        with self.assertRaises(ValueError):
            tr.draft(self.dir, self.event_id, "본문에 호칭 없음", "김철수 선임님", opener=lambda _: True)

    def test_send_without_approval_never_calls_teams(self):
        result = self.make_draft()
        out, code = tr.send(self.dir, result["draft"], cli=str(self.cli))
        self.assertEqual((out["error"], code), ("NO_APPROVAL", 1))
        self.approve(result["draftId"], "other-hash")
        self.approve("other-draft", json.loads(Path(result["draft"]).read_text())["textSha256"])
        out, code = tr.send(self.dir, result["draft"], cli=str(self.cli))
        self.assertEqual((out["error"], code), ("NO_APPROVAL", 1))
        self.assertEqual(self.cli_calls(), [])
        self.assertEqual(json.loads(Path(result["draft"]).read_text())["status"], "pending")

    def test_text_changed_after_approval_is_refused(self):
        result = self.make_draft()
        record = json.loads(Path(result["draft"]).read_text())
        self.approve(record["draftId"], record["textSha256"])
        record["text"] = "김철수 선임님 운영도 배포했습니다."
        Path(result["draft"]).write_text(json.dumps(record, ensure_ascii=False))
        out, _ = tr.send(self.dir, result["draft"], cli=str(self.cli))
        self.assertEqual(out["error"], "DRAFT_INVALID_OR_CHANGED")
        self.assertEqual(self.cli_calls(), [])

    def test_approved_send_executes_once_then_verifies_and_never_resends(self):
        result = self.make_draft()
        record = json.loads(Path(result["draft"]).read_text())
        self.approve(record["draftId"], record["textSha256"])
        out, code = tr.send(self.dir, result["draft"], cli=str(self.cli))
        self.assertEqual((out["draftStatus"], code), ("sent", 0))
        calls = self.cli_calls()
        self.assertEqual([c[0] for c in calls], ["send", "verify"])
        send = calls[0]
        self.assertIn("--execute", send)
        self.assertEqual(send[send.index("--chat-id") + 1], tr.CHAT_ID)
        self.assertEqual(send[send.index("--mention") + 1:send.index("--mention") + 3], ["김철수 선임님", "김철수(Kim Chul su) 선임"])
        self.assertEqual(send[send.index("--link") + 1:send.index("--link") + 3], ["PROJ-1405", "https://jira.example.com/browse/PROJ-1405"])
        self.assertEqual(calls[1][calls[1].index("--message-id") + 1], "1761199999999")
        self.assertEqual(Path(str(self.calls) + ".text").read_text(), record["text"])
        again, code = tr.send(self.dir, result["draft"], cli=str(self.cli))
        self.assertEqual((again["error"], code), ("DRAFT_NOT_PENDING", 1))
        self.assertEqual(len(self.cli_calls()), 2)
        with self.assertRaisesRegex(ValueError, "already has a sent reply"):
            self.make_draft("김철수 선임님 다시")

    def test_uncertain_send_is_not_retried(self):
        result = self.make_draft()
        record = json.loads(Path(result["draft"]).read_text())
        self.approve(record["draftId"], record["textSha256"])
        with patch.dict("os.environ", FAKE_SEND="SEND_OUTCOME_UNKNOWN"):
            out, code = tr.send(self.dir, result["draft"], cli=str(self.cli))
        self.assertEqual((out["draftStatus"], code), ("unknown", 1))
        self.assertEqual([c[0] for c in self.cli_calls()], ["send"])
        out, _ = tr.send(self.dir, result["draft"], cli=str(self.cli))
        self.assertEqual(out["error"], "DRAFT_NOT_PENDING")

    def test_redraft_supersedes_held_pending_draft(self):
        first = self.make_draft()
        second = self.make_draft("김철수 선임님 고친 답입니다")
        self.assertEqual(json.loads(Path(first["draft"]).read_text())["status"], "superseded")
        old = json.loads(Path(first["draft"]).read_text())
        self.approve(old["draftId"], old["textSha256"])
        out, _ = tr.send(self.dir, first["draft"], cli=str(self.cli))
        self.assertEqual(out["error"], "DRAFT_NOT_PENDING")
        self.assertEqual(json.loads(Path(second["draft"]).read_text())["status"], "pending")


class ReplyPage(Reply):
    def start(self, spec):
        self.log = self.dir / "decisions.jsonl"
        self.seen_before_action, self.woken = [], []

        def action(spec_, stamp):
            rows = [json.loads(l) for l in self.log.read_text().splitlines()]
            self.seen_before_action.append(rows)
            out, code = tr.send(self.dir, spec_["draft"], cli=str(self.cli))
            return code == 0, json.dumps(out)

        with patch.object(dp, 'process_start_time', return_value='fake-process-start'):
            self.server, _, _ = dp.make_server(spec, actions={"teams-reply": action}, wake=self.woken.append,
                                               log_path=self.log, token="tok")
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, **fields):
        data = urllib.parse.urlencode({"t": "tok", **fields}).encode()
        return urllib.request.urlopen(self.base + path, data=data, timeout=20).read().decode()

    def spec(self):
        return json.loads(Path(self.make_draft()["spec"]).read_text())

    def test_page_shows_draft_and_approve_records_approval_before_sending(self):
        spec = self.spec()
        self.start(spec)
        page = urllib.request.urlopen(self.base + "/?t=tok", timeout=20).read().decode()
        self.assertIn("승인 (Teams 보내기)", page)
        self.assertIn("김철수 선임님 PROJ-1405 dev 반영 확인했습니다.", page)
        result = self.post("/decide", option="approve")
        self.assertIn("Teams로 보냈고", result)
        before = self.seen_before_action[0]
        self.assertEqual([(r["option"], r["draftId"], r["textSha256"]) for r in before],
                         [("approve", spec["draftId"], spec["textSha256"])])
        rows = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([(r["option"], r["ok"]) for r in rows], [("approve", True), ("send", True)])
        import summary_metrics
        measured = summary_metrics.aggregate([], self.dir, 0, datetime.now(timezone.utc).timestamp() + 1)
        self.assertEqual(measured['humanInterventions']['buttonDecisions'], 1,
                         'one approval click plus automatic send receipt is one human action')
        self.assertEqual([c[0] for c in self.cli_calls()], ["send", "verify"])
        self.assertEqual(self.woken, [])

    def test_hold_sends_nothing_and_records_nothing(self):
        self.start(self.spec())
        self.post("/hold")
        self.assertFalse(self.log.exists())
        self.assertEqual(self.cli_calls(), [])


class SelfNotify(Reply):
    # 사용자 지시 2026-10-30: 승인·사용자 결정 필요는 본인 Teams 자기 대화로 보낸다.
    def test_notify_sends_to_self_chat_then_verifies(self):
        out = tr.notify("[Jira follow-up line 결정 필요] PROJ-1 · 스테이징 배포를 승인할까요?", cli=str(self.cli), directory=self.dir)
        self.assertEqual(out["status"], "sent")
        calls = self.cli_calls()
        self.assertEqual([c[0] for c in calls], ["send", "verify", "status"])
        self.assertEqual(calls[0][calls[0].index("--chat-id") + 1], "48:notes")
        self.assertIn("--execute", calls[0])
        self.assertEqual(Path(str(self.calls) + ".text").read_text(), "[Jira follow-up line 결정 필요] PROJ-1 · 스테이징 배포를 승인할까요?")

    def test_notify_failure_is_reported_not_raised(self):
        with patch.dict("os.environ", FAKE_SEND="FAIL"):
            self.assertEqual(tr.notify("x", cli=str(self.cli), directory=self.dir)["status"], "failed")
        with patch.dict("os.environ", FAKE_SEND="SEND_UNVERIFIED"):
            self.assertEqual(tr.notify("x", cli=str(self.cli), directory=self.dir)["status"], "sent")
        self.assertEqual([c[0] for c in self.cli_calls()].count("send"), 2)
        self.assertEqual(tr.notify("x", cli=str(self.dir / "absent"), directory=self.dir)["status"], "unknown")

    def test_identity_is_pinned_only_after_first_verified_notification(self):
        pin = self.dir / "teams-self.json"
        with patch.dict("os.environ", FAKE_VERIFY="FAIL"):
            self.assertEqual(tr.notify("x", cli=str(self.cli), directory=self.dir)["status"], "unknown")
        self.assertFalse(pin.exists())
        self.assertNotIn("status", [c[0] for c in self.cli_calls()])
        self.assertEqual(tr.notify("x", cli=str(self.cli), directory=self.dir)["status"], "sent")
        self.assertEqual(json.loads(pin.read_text()), {
            "accountId": "00000000-0000-0000-0000-000000000001",
            "tenantId": "00000000-0000-0000-0000-000000000002"})
        before = pin.read_bytes()
        with patch.dict("os.environ", FAKE_STATUS='{"status":"FAIL"}'):
            self.assertEqual(tr.notify("x", cli=str(self.cli), directory=self.dir)["status"], "sent")
        self.assertEqual(pin.read_bytes(), before)
        self.assertEqual([c[0] for c in self.cli_calls()].count("status"), 1)

    def test_invalid_status_never_creates_pin_or_exposes_raw_fields(self):
        for status in ({"status": "PASS"}, {"status": "PASS", "account": {"id": "secret-token"}},
                       {"status": "FAIL", "error": "secret-token"},
                       {"status": "PASS", "account": {"id": "00000000-0000-0000-0000-000000000001",
                                                           "tenantId": []}}):
            with self.subTest(status=status), patch.dict("os.environ", FAKE_STATUS=json.dumps(status)):
                result = tr.notify("x", cli=str(self.cli), directory=self.dir)
                self.assertEqual(result["status"], "sent")
                self.assertIn("identityError", result)
                self.assertNotIn("secret-token", json.dumps(result))
                self.assertFalse((self.dir / "teams-self.json").exists())
        with patch.dict("os.environ", FAKE_STATUS="secret-token is not JSON"):
            result = tr.notify("x", cli=str(self.cli), directory=self.dir)
        self.assertEqual(result["identityError"], "TEAMS_CLI_INVALID_OUTPUT")
        self.assertFalse((self.dir / "teams-self.json").exists())

    def test_racing_mismatched_pin_is_never_overwritten(self):
        pin = self.dir / "teams-self.json"
        existing = {"accountId": "00000000-0000-0000-0000-000000000003",
                    "tenantId": "00000000-0000-0000-0000-000000000004"}
        original = tr.call_cli

        def cli(argv, cli=None):
            result = original(argv, cli)
            if argv == ["status"]:
                pin.write_text(json.dumps(existing))
            return result

        with patch.object(tr, "call_cli", side_effect=cli):
            result = tr.notify("x", cli=str(self.cli), directory=self.dir)
        self.assertEqual(result["identityError"], "TEAMS_IDENTITY_MISMATCH")
        self.assertEqual(json.loads(pin.read_text()), existing)

    def test_malformed_existing_pin_is_preserved(self):
        pin = self.dir / "teams-self.json"
        pin.write_text('{"accountId":"secret-token"}')
        before = pin.read_bytes()
        result = tr.notify("x", cli=str(self.cli), directory=self.dir)
        self.assertEqual(result["identityError"], "TEAMS_IDENTITY_INVALID")
        self.assertEqual(pin.read_bytes(), before)
        self.assertEqual([c[0] for c in self.cli_calls()], ["send", "verify"])

    def test_notify_cli_dir_and_environment_default_keep_pin_in_test_directory(self):
        with patch.dict("os.environ", TEAMS_NATIVE_CLI=str(self.cli), JIRA_WAKE_DIR=str(self.dir)):
            proc = subprocess.run([sys.executable, str(ROOT / "teams_room.py"), "notify", "--text", "x"],
                                  capture_output=True, text=True)
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["status"]), (0, "sent"))
        self.assertTrue((self.dir / "teams-self.json").exists())
        explicit = self.dir / "explicit"
        with patch.dict("os.environ", TEAMS_NATIVE_CLI=str(self.cli)):
            proc = subprocess.run([sys.executable, str(ROOT / "teams_room.py"), "notify", "--text", "x",
                                   "--dir", str(explicit)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((explicit / "teams-self.json").exists())


class SelfMessages(Fixture):
    def setUp(self):
        super().setUp()
        self.cli = self.dir / "fake-cli.py"
        self.cli.write_text(FAKE_CLI)
        self.calls = self.dir / "calls.jsonl"
        self.account = {"id": "00000000-0000-0000-0000-000000000001",
                        "tenantId": "00000000-0000-0000-0000-000000000002"}
        patcher = patch.dict("os.environ", FAKE_CALLS=str(self.calls), TEAMS_NATIVE_CLI=str(self.cli))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_self_messages_sanitizes_account_and_message_fields(self):
        row = {"id": "1", "time": "2026-10-30T01:00:00Z", "text": "D1 A",
               "authorMri": "8:user:" + self.account["id"]}
        payload = {"status": "PASS", "account": {**self.account, "token": "secret-token"},
                   "messages": [{**row, "content": "<secret-html>", "properties": {"token": "secret-token"}}],
                   "raw": "secret-token"}
        with patch.dict("os.environ", FAKE_MESSAGES=json.dumps(payload)):
            proc = subprocess.run([sys.executable, str(ROOT / "teams_room.py"), "self-messages", "--limit", "50"],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"status": "PASS", "account": self.account, "messages": [row]})
        self.assertNotIn("secret", proc.stdout)
        self.assertEqual([json.loads(line) for line in self.calls.read_text().splitlines()],
                         [["messages", "--chat-id", "48:notes", "--limit", "50"]])

    def test_missing_or_malformed_identity_is_rejected_without_raw_output(self):
        for account in (None, [], {}, {"id": self.account["id"]},
                        {**self.account, "tenantId": "secret-token"}, {**self.account, "id": 1}):
            with self.subTest(account=account), patch.dict("os.environ", FAKE_MESSAGES=json.dumps({
                    "status": "PASS", "account": account, "messages": [], "raw": "secret-token"})):
                proc = subprocess.run([sys.executable, str(ROOT / "teams_room.py"), "self-messages"],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(json.loads(proc.stdout), {"status": "FAIL", "error": "TEAMS_IDENTITY_INVALID"})
                self.assertNotIn("secret", proc.stdout + proc.stderr)

    def test_message_read_uses_current_payload_identity_and_never_changes_pin(self):
        pin = self.dir / "teams-self.json"
        pin.write_text(json.dumps({"accountId": "00000000-0000-0000-0000-000000000003",
                                   "tenantId": "00000000-0000-0000-0000-000000000004"}))
        before = pin.read_bytes()
        with patch.dict("os.environ", FAKE_MESSAGES=json.dumps({"status": "PASS", "account": self.account,
                                                               "messages": []})):
            self.assertEqual(tr.self_messages(cli=str(self.cli))["account"], self.account)
        self.assertEqual(pin.read_bytes(), before)

    def test_read_failure_exposes_only_error_code_and_never_reconnects(self):
        with patch.dict("os.environ", FAKE_MESSAGES=json.dumps({"status": "FAIL", "error": "secret-token"})):
            proc = subprocess.run([sys.executable, str(ROOT / "teams_room.py"), "self-messages"],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout), {"status": "FAIL", "error": "TEAMS_READ_FAILED"})
        self.assertNotIn("secret", proc.stdout + proc.stderr)
        self.assertEqual([json.loads(line)[0] for line in self.calls.read_text().splitlines()], ["messages"])

    def test_malformed_message_payload_and_invalid_limits_fail_closed(self):
        for messages in (None, {}, [None], [{"id": "1", "text": []}]):
            with self.subTest(messages=messages), patch.dict("os.environ", FAKE_MESSAGES=json.dumps({
                    "status": "PASS", "account": self.account, "messages": messages})):
                with self.assertRaisesRegex(tr.Unavailable, "TEAMS_SELF_MESSAGES_INVALID"):
                    tr.self_messages(cli=str(self.cli))
        before = self.calls.read_bytes()
        for limit in (0, 51, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                tr.self_messages(cli=str(self.cli), limit=limit)
        self.assertEqual(self.calls.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
