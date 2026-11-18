"""판정 에이전트(judge, 사용자 지시 2026-11-09). 임시 상태·가짜 모델·가짜 Jira만 쓴다."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import followup_idle as idle  # noqa: E402
import judge  # noqa: E402
import state_update  # noqa: E402

ME = judge.ME
QA = {"accountId": "qa-1", "displayName": "김철수"}


def verdict(option="a", decision="decide", risk="low", confidence=0.9):
    return {"decision": decision, "option": option, "risk": risk, "humanNeeded": "", "against": "반대",
            "why": "근거", "confidence": confidence}


def spec(reason="policy", key="PROJ-1"):
    return {"kind": "choice", "key": key, "title": "제목", "summary": "s", "question": "q", "humanReason": reason,
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "recommended": "a"}


def comment(cid, author, created, text="본문", media=False):
    content = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
    if media:
        content.append({"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m", "type": "file"}}]})
    return {"id": cid, "author": {"accountId": author, "displayName": "x"}, "created": created,
            "body": {"type": "doc", "content": content}}


class FakeJira:
    def __init__(self, status, comments, changelog, reporter=QA):
        self.data = {"fields": {"status": {"name": status}, "reporter": reporter, "summary": "요약",
                                "comment": {"comments": comments}}, "changelog": changelog}
        self.posted = []

    def facts(self, key):
        return self.data

    def comment(self, key, text):
        self.posted.append(text)
        return f"c{len(self.posted)}"


def moved(created, to=judge.DEV_DONE, by="qa-1"):
    return {"created": created, "author": {"accountId": by},
            "items": [{"field": "status", "fromString": "개발 QA 요청", "toString": to}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.woken = []
        env = patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("JIRA_JUDGE_MODE", None)
        self.set_mode("off")

    def set_mode(self, value):
        (self.dir / "judge").write_text(value + "\n")

    def jsonl(self, name):
        path = self.dir / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Mode(Base):
    def test_missing_is_on_invalid_is_off(self):
        (self.dir / "judge").unlink()
        self.assertEqual(judge.mode(self.dir), "on")
        self.set_mode("maybe")
        self.assertEqual(judge.mode(self.dir), "off")
        judge.set_mode(self.dir, "shadow")
        self.assertEqual(judge.mode(self.dir), "shadow")
        with self.assertRaises(ValueError):
            judge.set_mode(self.dir, "maybe")

    def test_env_override_wins(self):
        os.environ["JIRA_JUDGE_MODE"] = "off"
        self.set_mode("on")
        self.assertEqual(judge.mode(self.dir), "off")


class FakeTodo:
    def __init__(self, rows=()):
        self.rows, self.calls = list(rows), []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "list":
            return self.rows
        if args[0] == "add":
            return "t1"
        return ""


class Setting(Base):
    def setUp(self):
        super().setUp()
        (self.dir / "decision-surface").write_text("app\n")
        os.environ.pop("JIRA_DECISION_SURFACE", None)
        import decision_inbox
        self.inbox = decision_inbox
        self.items = []

    def row(self, *replies):
        return {"id": "t1", "title": self.inbox.judge_title("on"), "status": "others", "others": self.inbox.judge_others("on"), "pin": True,
                "replies": [{"who": "human", "at": f"2026-11-09T10:0{i}", "what": f"reply — {w}"} for i, w in enumerate(replies)]}

    def test_creates_item_with_buttons_once(self):
        (self.dir / "judge").unlink()
        todo = FakeTodo()
        self.assertEqual(self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[], todo=todo), "on")
        self.assertEqual(todo.calls[1], ["choices", "t1", "켜기 | 끄기", "--pick", "1"])
        self.assertEqual(todo.calls[0][:2], ["add", "[공장 설정] 판정 에이전트(judge): 켜짐"])
        self.assertEqual(todo.calls[-2:], [["others", "t1", "설정 · 판정 에이전트 켜짐 · 버튼으로 바꿈"], ["pin", "t1", "on"]])
        self.assertEqual(self.items, [])
        todo = FakeTodo()
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[self.row()], todo=todo)
        self.assertEqual(todo.calls, [])

    def test_button_reply_switches_mode_once(self):
        self.set_mode("on")
        todo = FakeTodo()
        self.assertEqual(self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[self.row("끄기")], todo=todo), "off")
        self.assertEqual(judge.mode(self.dir), "off")
        self.assertIn(["choices", "t1", "켜기 | 끄기", "--pick", "2"], todo.calls)
        self.assertIn(["others", "t1", "설정 · 판정 에이전트 꺼짐 · 버튼으로 바꿈"], todo.calls)
        self.assertEqual(self.items[-1], ["retitle", "t1", "[공장 설정] 판정 에이전트(judge): 꺼짐"])
        # 같은 답글은 다시 처리하지 않는다: 사람이 파일로 다시 켜도 덮어쓰지 않는다.
        self.set_mode("on")
        todo = FakeTodo()
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[self.row("끄기")], todo=todo)
        self.assertEqual((judge.mode(self.dir), todo.calls), ("on", []))
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[self.row("끄기", "켜기")], todo=FakeTodo())
        self.assertEqual(judge.mode(self.dir), "on")

    def test_existing_item_moves_to_in_progress(self):
        todo = FakeTodo()
        self.set_mode("on")
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[dict(self.row(), status="workable", others="")], todo=todo)
        self.assertEqual(todo.calls, [["others", "t1", "설정 · 판정 에이전트 켜짐 · 버튼으로 바꿈"]])

    def test_old_title_is_renamed_and_reply_survives_id_change(self):
        # 제목이 바뀌면 sdlc-kit 항목 id도 바뀐다. 같은 답글을 다시 처리하면 안 된다.
        self.set_mode("on")
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[self.row("끄기")], todo=FakeTodo())
        self.set_mode("on")
        renamed = dict(self.row("끄기"), id="t2", title=self.inbox.judge_title("on"))
        todo = FakeTodo()
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[renamed], todo=todo)
        self.assertEqual(judge.mode(self.dir), "on")
        legacy = dict(self.row(), title=self.inbox.JUDGE_TITLE)
        self.items.clear()
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[legacy], todo=FakeTodo())
        self.assertEqual(self.items, [["retitle", "t1", "[공장 설정] 판정 에이전트(judge): 켜짐"]])

    def test_unpinned_item_is_pinned_and_old_kit_is_tolerated(self):
        self.set_mode("on")
        todo = FakeTodo()
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[dict(self.row(), pin=False)], todo=todo)
        self.assertIn(["pin", "t1", "on"], todo.calls)

        def old_kit(args):
            if args[0] == "pin":
                raise RuntimeError("unknown command: pin")
            return "t1"
        self.assertEqual(self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[], todo=old_kit), "on")

    def test_unknown_reply_keeps_mode(self):
        self.set_mode("on")
        todo = FakeTodo()
        self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[self.row("음")], todo=todo)
        self.assertEqual(judge.mode(self.dir), "on")
        self.assertTrue(any(c[0] == "note" and "알 수 없어요" in c[2] for c in todo.calls))

    def test_html_surface_does_nothing(self):
        (self.dir / "decision-surface").write_text("html\n")
        self.assertIsNone(self.inbox.judge_setting(self.dir, item_cli=self.items.append, rows=[], todo=lambda a: self.fail("todo touched")))

    def test_poll_runs_setting_without_open_decisions(self):
        todo = FakeTodo([self.row("끄기")])
        self.set_mode("on")
        self.assertEqual(self.inbox.poll(self.dir, todo=todo, item_cli=self.items.append), [])
        self.assertEqual(judge.mode(self.dir), "off")


class Choice(Base):
    def run_choice(self, data, model):
        path = self.dir / "spec.json"
        path.write_text(json.dumps(data, ensure_ascii=False))
        return judge.choice(path, self.dir, model=model, wake=self.woken.append)

    def test_off_never_asks_the_model(self):
        self.assertEqual(self.run_choice(spec(), lambda d: self.fail("model called")), judge.ESCALATE)

    def test_human_only_reasons_skip_the_model(self):
        self.set_mode("on")
        for reason in ("security-plan", "product-scope", "prod-data"):
            self.assertEqual(self.run_choice(spec(reason), lambda d: self.fail("model called")), judge.ESCALATE)
        self.assertEqual(self.jsonl("decisions.jsonl"), [])

    def test_on_applies_low_risk_decision_and_wakes(self):
        self.set_mode("on")
        seen = []
        self.assertEqual(self.run_choice(spec(), lambda d: seen.append(d) or verdict("b")), judge.APPROVED)
        row = self.jsonl("decisions.jsonl")[-1]
        self.assertEqual((row["by"], row["option"], row["recommended"]), ("judge", "b", "a"))
        self.assertEqual(self.woken, ["PROJ-1"])
        self.assertEqual(seen[0]["humanReason"], "policy")

    def test_shadow_logs_only(self):
        self.set_mode("shadow")
        self.assertEqual(self.run_choice(spec(), lambda d: verdict()), judge.ESCALATE)
        self.assertEqual(self.jsonl("decisions.jsonl"), [])
        log = self.jsonl("judge.jsonl")[-1]
        self.assertEqual((log["accepted"], log["applied"]), (True, False))

    def test_low_confidence_high_risk_or_unknown_option_escalate(self):
        self.set_mode("on")
        for bad in (verdict(confidence=0.5), verdict(risk="high"), verdict("zzz"), verdict(decision="escalate")):
            self.assertEqual(self.run_choice(spec(), lambda d, v=bad: v), judge.ESCALATE)
        self.assertEqual(self.jsonl("decisions.jsonl"), [])

    def test_history_is_given_to_the_model(self):
        self.set_mode("on")
        (self.dir / "decisions.jsonl").write_text(json.dumps(
            {"at": "2026-11-04T15:37", "key": "PROJ-1", "kind": "choice", "title": "재리뷰", "option": "rereview-again",
             "recommended": "approve-build", "by": "todo-app", "note": "", "ok": True}, ensure_ascii=False) + "\n")
        seen = []
        self.run_choice(spec(), lambda d: seen.append(d) or verdict(decision="escalate"))
        self.assertEqual(seen[0]["history"][0]["option"], "rereview-again")

    def test_model_failure_escalates(self):
        def broken(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="{}", stderr="")
        self.assertEqual(judge.ask({}, runner=broken)["decision"], "escalate")


class Async(Base):
    def write_spec(self, data):
        path = self.dir / "lesson-x.json"
        path.write_text(json.dumps(data, ensure_ascii=False))
        return path

    def codes(self):
        return json.loads((self.dir / "decision-pages" / "codes.json").read_text())

    def test_applies_is_quick_and_respects_mode_skip_and_human_only(self):
        path = self.write_spec(spec())
        self.assertFalse(judge.applies(path, self.dir))
        self.set_mode("on")
        self.assertTrue(judge.applies(path, self.dir))
        self.assertFalse(judge.applies(self.write_spec(spec("security-plan")), self.dir))
        os.environ["JIRA_JUDGE_SKIP"] = "1"
        self.assertFalse(judge.applies(path, self.dir))

    def test_decided_closes_the_held_code(self):
        self.set_mode("on")
        path = self.write_spec(spec())
        result = judge.choice_async(path, self.dir, model=lambda d: verdict("b"), wake=self.woken.append,
                                    escalate=lambda *a: self.fail("escalated"))
        self.assertEqual(result, judge.APPROVED)
        (code, row), = self.codes().items()
        self.assertEqual((row["state"], row["by"], row["name"]), ("decided", "judge", "lesson-x"))
        self.assertEqual(self.jsonl("decisions.jsonl")[-1]["messageId"], f"judge:{code}")

    def test_escalation_reuses_the_code_without_process_identity(self):
        self.set_mode("on")
        path = self.write_spec(spec())
        seen = []
        result = judge.choice_async(path, self.dir, model=lambda d: verdict(decision="escalate"),
                                    escalate=lambda s, name, code, d: seen.append((name, code)))
        self.assertEqual(result, judge.ESCALATE)
        (code, row), = self.codes().items()
        self.assertEqual(seen, [("lesson-x", code)])
        self.assertEqual((row["state"], row["pid"], row["pidStart"]), ("open", None, None))
        self.assertTrue((self.dir / "decision-pages" / "lesson-x.spec.json").exists())

    def test_model_crash_escalates(self):
        self.set_mode("on")
        def boom(data):
            raise RuntimeError("x")
        seen = []
        judge.choice_async(self.write_spec(spec()), self.dir, model=boom, escalate=lambda *a: seen.append(a))
        self.assertEqual(len(seen), 1)


class Stg(Base):
    RESULT_AT = "2026-11-09T10:00:00.000+0900"

    def run_stg(self, jira, model=lambda d: verdict("approve")):
        return judge.stg("PROJ-9", self.dir, jira=jira, model=model, wake=self.woken.append)

    def result(self):
        return comment("100", ME, self.RESULT_AT, "개발계 결과", media=True)

    def test_off_does_nothing(self):
        jira = FakeJira(judge.DEV_DONE, [self.result()], [moved("2026-11-09T11:00:00.000+0900")])
        self.assertEqual(self.run_stg(jira), judge.ESCALATE)
        self.assertFalse((self.dir / "approvals.json").exists())

    def test_dev_done_after_result_approves(self):
        self.set_mode("shadow")  # 사용자 결정 규칙이라 shadow에서도 적용한다.
        jira = FakeJira(judge.DEV_DONE, [self.result()], [moved("2026-11-09T11:00:00.000+0900")])
        self.assertEqual(self.run_stg(jira, model=lambda d: self.fail("model called")), judge.APPROVED)
        approval = json.loads((self.dir / "approvals.json").read_text())["PROJ-9"]
        self.assertTrue(approval["stg"].startswith("judge: Jira '개발 검수 완료'"))
        self.assertEqual(self.jsonl("decisions.jsonl")[-1]["by"], "judge")
        self.assertEqual(self.woken, ["PROJ-9"])

    def test_dev_done_by_my_account_goes_to_human(self):
        # 공장도 내 계정으로 상태를 옮기므로 사람의 QA 완료로 볼 수 없다(실데이터 PROJ-1442 11-03 12:57).
        self.set_mode("on")
        jira = FakeJira(judge.DEV_DONE, [self.result()], [moved("2026-11-09T11:00:00.000+0900", by=ME)])
        self.assertEqual(self.run_stg(jira), judge.ESCALATE)
        self.assertFalse((self.dir / "approvals.json").exists())
        self.assertEqual(jira.posted, [])

    def test_dev_done_before_newer_result_asks_again(self):
        self.set_mode("on")
        jira = FakeJira(judge.DEV_DONE, [self.result()], [moved("2026-11-09T09:00:00.000+0900")])
        self.assertEqual(self.run_stg(jira), judge.WAITING_QA)
        self.assertFalse((self.dir / "approvals.json").exists())
        self.assertEqual(len(jira.posted), 1)
        state = json.loads((self.dir / "judge-state.json").read_text())["stgAsked"]["PROJ-9"]
        self.assertEqual(state["status"], judge.DEV_DONE)
        self.assertIn("@[김철수](qa-1)", jira.posted[0])

    def test_no_reply_posts_one_request_per_result(self):
        self.set_mode("shadow")
        jira = FakeJira("개발 QA 요청", [self.result()], [])
        self.assertEqual(self.run_stg(jira), judge.WAITING_QA)
        self.assertEqual(self.run_stg(jira), judge.WAITING_QA)
        self.assertEqual(len(jira.posted), 1)
        jira.data["fields"]["comment"]["comments"].append(comment("101", ME, "2026-11-09T12:00:00.000+0900", "새 결과", True))
        self.run_stg(jira)
        self.assertEqual(len(jira.posted), 2)

    def test_self_reported_ticket_goes_to_human(self):
        self.set_mode("on")
        jira = FakeJira("개발 QA 요청", [self.result()], [], reporter={"accountId": ME, "displayName": "나"})
        self.assertEqual(self.run_stg(jira), judge.ESCALATE)
        self.assertEqual(jira.posted, [])

    def test_qa_ok_comment_approves_when_on(self):
        self.set_mode("on")
        ok = comment("102", "qa-1", "2026-11-09T11:00:00.000+0900", "개발계 확인 완료했습니다. 스테이징 반영 부탁드려요")
        jira = FakeJira("개발 QA 요청", [self.result(), ok], [])
        seen = []
        self.assertEqual(self.run_stg(jira, model=lambda d: seen.append(d) or verdict("approve")), judge.APPROVED)
        self.assertEqual(seen[0]["comments"][0]["text"].strip(), "개발계 확인 완료했습니다. 스테이징 반영 부탁드려요")

    def test_qa_defect_comment_waits_without_request(self):
        self.set_mode("on")
        bad = comment("102", "qa-1", "2026-11-09T11:00:00.000+0900", "버튼이 안 눌려요")
        jira = FakeJira("개발 QA 요청", [self.result(), bad], [])
        self.assertEqual(self.run_stg(jira, model=lambda d: verdict(decision="escalate")), judge.WAITING_QA)
        self.assertEqual(jira.posted, [])
        self.assertFalse((self.dir / "approvals.json").exists())

    def test_defect_comment_after_old_dev_done_records_status_so_guard_does_not_rewake(self):
        self.set_mode("on")
        bad = comment("102", "qa-1", "2026-11-09T11:00:00.000+0900", "버튼이 안 눌려요")
        jira = FakeJira(judge.DEV_DONE, [self.result(), bad], [moved("2026-11-09T09:00:00.000+0900")])
        self.assertEqual(self.run_stg(jira, model=lambda d: verdict(decision="escalate")), judge.WAITING_QA)
        state = json.loads((self.dir / "judge-state.json").read_text())["stgAsked"]["PROJ-9"]
        self.assertEqual(state["status"], judge.DEV_DONE)

    def test_judge_failure_on_comments_goes_to_human(self):
        self.set_mode("on")
        other = comment("102", "qa-1", "2026-11-09T11:00:00.000+0900", "확인")
        failed = dict(verdict(decision="escalate"), humanNeeded="judge-failed:TimeoutExpired")
        jira = FakeJira("개발 QA 요청", [self.result(), other], [])
        self.assertEqual(self.run_stg(jira, model=lambda d: failed), judge.ESCALATE)

    def test_comment_before_result_is_not_qa_signal(self):
        self.set_mode("on")
        old = comment("99", "qa-1", "2026-11-09T09:00:00.000+0900", "확인 완료")
        jira = FakeJira("개발 QA 요청", [old, self.result()], [])
        self.assertEqual(self.run_stg(jira, model=lambda d: self.fail("model called")), judge.WAITING_QA)
        self.assertEqual(len(jira.posted), 1)


class Risk(Base):
    def item(self, **extra):
        return dict(stage="plan", waiting="human-risk", note="작업 기록", question="계획 리뷰를 한 번 더 할까요?",
                    suggest="지적 2건을 반영하고 구현한다", **extra)

    def write_queue(self, queue):
        state_update.atomic_json(self.dir / "followup-queue.json", queue)

    def queue(self):
        return json.loads((self.dir / "followup-queue.json").read_text())

    def test_on_accept_resumes_with_judge_note(self):
        self.set_mode("on")
        self.write_queue({"PROJ-5": self.item(), "PROJ-6": dict(stage="x", waiting="reporter")})
        self.assertEqual(judge.risk(self.dir, model=lambda d: verdict("accept")), 1)
        item = self.queue()["PROJ-5"]
        self.assertIsNone(item["waiting"])
        self.assertNotIn("question", item)
        self.assertIn("judge] 사람 대기 대신 판정: '지적 2건을 반영하고 구현한다'", item["note"])
        self.assertEqual(self.jsonl("decisions.jsonl")[-1]["by"], "judge")

    def test_same_question_is_judged_once(self):
        self.set_mode("on")
        self.write_queue({"PROJ-5": self.item()})
        judge.risk(self.dir, model=lambda d: verdict(decision="escalate"))
        self.assertEqual(judge.risk(self.dir, model=lambda d: self.fail("judged twice")), 0)
        self.assertEqual(self.queue()["PROJ-5"]["waiting"], "human-risk")

    def test_shadow_and_leased_do_not_change_queue(self):
        self.set_mode("shadow")
        self.write_queue({"PROJ-5": self.item()})
        judge.risk(self.dir, model=lambda d: verdict("accept"))
        self.assertEqual(self.queue()["PROJ-5"]["waiting"], "human-risk")
        self.set_mode("on")
        self.write_queue({"PROJ-7": self.item()})
        self.assertEqual(judge.risk(self.dir, model=lambda d: self.fail("leased"), leased={"PROJ-7"}), 0)

    def test_concurrent_change_is_not_overwritten(self):
        self.set_mode("on")
        self.write_queue({"PROJ-5": self.item()})

        def model(data):
            self.write_queue({"PROJ-5": dict(self.item(), note="다른 작성자")})
            return verdict("accept")
        judge.risk(self.dir, model=model)
        self.assertEqual(self.queue()["PROJ-5"]["waiting"], "human-risk")

    def test_daily_cap_per_key(self):
        self.set_mode("on")
        for n in range(judge.RISK_PER_KEY_PER_DAY + 1):
            self.write_queue({"PROJ-5": dict(self.item(), question=f"질문 {n}")})
            judge.risk(self.dir, model=lambda d: verdict("accept"))
        self.assertEqual(self.queue()["PROJ-5"]["waiting"], "human-risk")


class GuardDevDone(Base):
    def setUp(self):
        super().setUp()
        self.statuses = {}
        self.addCleanup(patch.stopall)
        patch("lessons.open_request").start()
        patch.object(idle, "run", side_effect=self.command).start()
        patch.object(idle, "notify", return_value=True).start()
        patch.object(idle, "blocked_by", return_value=[]).start()
        patch.object(idle, "reconcile_reasons", return_value=[]).start()
        self.popen = patch.object(idle.subprocess, "Popen").start()

    def command(self, argv):
        if "--out" in argv:
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(
                {"events": [], "errors": [], "queueStatuses": self.statuses}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_dev_done_resumes_stg_only_when_judge_is_enabled(self):
        queue = {"PROJ-1": dict(stage="dev-deployed", waiting="reporter", note="n"),
                 "PROJ-2": dict(stage="build", waiting="reporter", note="n"),
                 "PROJ-3": dict(stage="stg-ready", waiting="stg-approval", note="n"),
                 "PROJ-4": dict(stage="dev-deployed", waiting="reporter", note="n")}
        state_update.atomic_json(self.dir / "followup-queue.json", queue)
        # PROJ-4: judge가 이미 '개발 검수 완료'를 보고도 기다리기로 함(새 결과 댓글이 전환보다 뒤).
        state_update.atomic_json(self.dir / "judge-state.json", {"stgAsked": {"PROJ-4": {"status": judge.DEV_DONE}}})
        self.statuses = {k: judge.DEV_DONE for k in queue}
        idle.decide(self.dir, ["scan"], ["preflight"])
        self.assertEqual(json.loads((self.dir / "followup-queue.json").read_text()), queue)
        self.set_mode("shadow")
        result, code = idle.decide(self.dir, ["scan"], ["preflight"])
        after = json.loads((self.dir / "followup-queue.json").read_text())
        self.assertIsNone(after["PROJ-1"]["waiting"])
        self.assertIn("스테이징 반영 재개", after["PROJ-1"]["note"])
        self.assertEqual((after["PROJ-2"]["waiting"], after["PROJ-3"]["waiting"], after["PROJ-4"]["waiting"]),
                         ("reporter", "stg-approval", "reporter"))
        self.assertEqual(result["queueReconcile"]["stg"], {"PROJ-1": judge.DEV_DONE})
        self.assertIn("queue-runnable:PROJ-1", result["reasons"])
        self.assertEqual(code, 1)

    def test_dev_done_resume_reads_delivery_not_stage_label(self):
        # delivery가 정본이다: 이름표(stage)가 무엇이든 delivery=dev-deployed면 재개하고, result-posted면 그대로 둔다.
        queue = {"PROJ-1": dict(stage="dev-fix", delivery="dev-deployed", waiting="reporter", note="n"),
                 "PROJ-2": dict(stage="dev-deployed", delivery="result-posted", waiting="reporter", note="n")}
        state_update.atomic_json(self.dir / "followup-queue.json", queue)
        self.statuses = {k: judge.DEV_DONE for k in queue}
        self.set_mode("on")
        idle.decide(self.dir, ["scan"], ["preflight"])
        after = json.loads((self.dir / "followup-queue.json").read_text())
        self.assertEqual((after["PROJ-1"]["waiting"], after["PROJ-2"]["waiting"]), (None, "reporter"))

    def test_human_risk_launches_detached_judge(self):
        state_update.atomic_json(self.dir / "followup-queue.json",
                                 {"PROJ-3": dict(stage="plan", waiting="human-risk", note="n", question="q", suggest="s")})
        idle.decide(self.dir, ["scan"], ["preflight"])
        self.popen.assert_not_called()
        self.set_mode("on")
        result, _ = idle.decide(self.dir, ["scan"], ["preflight"])
        self.assertEqual(result["judge"], "risk-launched")
        self.assertIn("risk", self.popen.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
