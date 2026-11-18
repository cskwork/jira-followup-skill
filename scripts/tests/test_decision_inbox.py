"""할 일 결정 화면: 실제 sdlc-kit to-do CLI를 임시 todo.md로 돌리고, 사람 답글은 파일에 human 줄로 넣는다."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_inbox as inbox
import decision_page as dp
import followup_scan
import teams_room

CHOICE = {"kind": "choice", "humanReason": "product-scope", "key": "PROJ-1", "title": "처리 방법", "question": "어떻게 할까요?",
          "summary": "과거 기록", "recommendReason": "근거",
          "options": [{"id": "wait", "label": "기다림"}, {"id": "skip", "label": "제외", "detail": "이번 범위에서 뺀다"}],
          "recommended": "skip"}
SQL = {"kind": "sql-run", "key": "PROJ-2", "title": "보정 SQL", "config": "fake_stg",
       "expect_host": "fake-stg-", "file": "/tmp/fake.sql"}
KIT = Path(os.environ.get("SDLC_KIT", Path.home() / ".agents/skills/sdlc-kit"))


@unittest.skipUnless(shutil.which("node") and (KIT / "todo/todo.mjs").exists(), "sdlc-kit to-do 없음")
class TodoDecision(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.todo_file = self.directory / "todo.md"
        env = patch.dict(os.environ, SDLC_TODO=str(self.todo_file), JIRA_DECISION_SURFACE="app")
        env.start()
        self.addCleanup(env.stop)
        self.woken, self.ran = [], []

    def register(self, spec):
        path = self.directory / f"{spec['key']}-{spec['kind']}.json"
        path.write_text(json.dumps(spec, ensure_ascii=False))
        return inbox.register("spec", str(path), self.directory)

    def item(self, item_id):
        return next(r for r in inbox.todo_cli(["list", "--json"]) if r["id"] == item_id)

    def human_reply(self, item_id, words):
        """앱의 답글 버튼이 남기는 것과 같은 human 줄을 항목 기록 끝에 넣는다."""
        title = self.item(item_id)["title"]
        lines = self.todo_file.read_text().split("\n")
        start = next(i for i, l in enumerate(lines) if l == f"- [ ] {title}")
        end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith("  ")), len(lines))
        lines.insert(end, f"  - 2026-11-03 10:00 human: reply — {words}")
        # reply는 waiting 필드를 비운다(core.reply와 같게).
        lines = [l for i, l in enumerate(lines) if not (start < i < end and l.startswith("  - waiting: "))]
        self.todo_file.write_text("\n".join(lines))

    def decide(self, *args, **kwargs):
        def action(spec, stamp):
            self.ran.append((spec["kind"], spec.get("approvedVia")))
            return True, "runner output"
        return inbox.decide(*args, actions={"sql-run": action, "stg-approval": action}, wake=self.woken.append, **kwargs)

    def codes(self):
        return json.loads((self.directory / "decision-pages/codes.json").read_text())

    def test_choice_shows_in_needs_you_and_numbered_reply_records_once(self):
        code, item_id = self.register(CHOICE)
        row = self.item(item_id)
        self.assertEqual(row["status"], "waiting")
        self.assertTrue(row["title"].startswith(f"[결정 {code}] PROJ-1"))
        self.assertIn("2) 제외 (AI 권장)", row["waiting"])
        self.assertIsNone(self.codes()[code]["pid"])
        self.assertEqual(self.codes()[code]["todoId"], item_id)
        self.assertEqual(row["link"]["label"], "PROJ-1")
        brief = Path(row["brief"]).read_text()
        self.assertIn("- **Your call:** 어떻게 할까요?", brief)
        self.assertIn("- **What changes:** 하려는 것(TO-BE): 이번 범위에서 뺀다", brief)
        self.assertIn("- **AI pick:** AI 권장: 제외 — 근거", brief)
        self.assertIn("2. **제외** (AI 권장) — 이번 범위에서 뺀다", brief)

        self.human_reply(item_id, "2")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(code, "decided")])
        self.assertEqual(self.item(item_id)["status"], "done")
        rows = [json.loads(l) for l in (self.directory / "decisions.jsonl").read_text().splitlines()]
        self.assertEqual([(r["option"], r["by"]) for r in rows], [("skip", "todo-app")])
        self.assertEqual((self.codes()[code]["state"], self.woken), ("decided", ["PROJ-1"]))
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [])

    def test_free_text_reply_is_recorded_as_instruction_not_asked_again(self):
        code, item_id = self.register(CHOICE)
        self.human_reply(item_id, "셤 실패 해결 후 넘어가기")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(code, "decided")])
        self.assertEqual(self.item(item_id)["status"], "done")
        rows = [json.loads(l) for l in (self.directory / "decisions.jsonl").read_text().splitlines()]
        self.assertEqual([(r["option"], r["note"]) for r in rows], [("free-text", "셤 실패 해결 후 넘어가기")])
        self.assertEqual((self.codes()[code]["state"], self.woken), ("decided", ["PROJ-1"]))

    def test_invalid_reply_asks_again_and_hold_records_nothing(self):
        code, item_id = self.register(CHOICE)
        self.human_reply(item_id, "아마도")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(code, "invalid")])
        row = self.item(item_id)
        self.assertEqual(row["status"], "waiting")
        self.assertIn("1~2 번호로", row["waiting"])
        self.human_reply(item_id, "보류")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(code, "held")])
        self.assertFalse((self.directory / "decisions.jsonl").exists())
        self.assertEqual(self.codes()[code]["state"], "expired")

    def test_execution_approval_runs_action_from_app_reply(self):
        code, item_id = self.register(SQL)
        self.assertIn("'승인' 또는 '보류'", self.item(item_id)["waiting"])
        self.human_reply(item_id, "승인")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(code, "decided")])
        self.assertEqual(self.ran, [("sql-run", "답글 '승인' (할 일 앱")])
        rows = [json.loads(l) for l in (self.directory / "decisions.jsonl").read_text().splitlines()]
        self.assertEqual([(r["option"], r["by"], r["ok"]) for r in rows], [("approve", "todo-app", True)])

    def test_stg_batch_partial_then_all_with_temporary_todo_file(self):
        first, item = self.register({"kind": "stg-approval", "key": "PROJ-1429", "title": "첫 스테이징 승인"})
        second, shared = self.register({"kind": "stg-approval", "key": "PROJ-1431", "title": "둘째 스테이징 승인"})
        self.assertEqual(item, shared)
        self.assertEqual(self.item(item)["title"], "[스테이징 승인] PROJ-1429")
        self.human_reply(item, "승인 1429")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(first, "decided")])
        self.assertEqual(self.item(item)["status"], "waiting")
        self.assertIn("PROJ-1431", self.item(item)["waiting"])
        self.assertNotIn("PROJ-1429", self.item(item)["waiting"])
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [])
        self.human_reply(item, "승인")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(second, "decided")])
        self.assertEqual(self.item(item)["status"], "done")
        self.assertEqual(self.woken, ["PROJ-1429", "PROJ-1431"])

    def test_closed_code_closes_item_and_duplicate_reply_is_not_recorded_twice(self):
        code, item_id = self.register(CHOICE)
        self.human_reply(item_id, "1")
        message = inbox.reply_id(item_id, self.item(item_id)["replies"][-1])
        self.assertEqual(self.decide(self.directory, code, "wait", message)[1], "decided")
        self.assertEqual(self.decide(self.directory, code, "wait", message)[1], "duplicate")
        self.assertEqual(inbox.poll(self.directory, decider=self.decide), [(code, "closed")])
        self.assertEqual(self.item(item_id)["status"], "done")

    def test_alert_reuses_one_item_and_html_does_not_touch_todo(self):
        inbox.alert("[Jira follow-up line 조치 필요] 하나", self.directory)
        inbox.alert("[Jira follow-up line 조치 필요]\nSwitch VPN to stg; type OTP in Terminal: open -a Terminal /x/stg.command 시 진행", self.directory)
        rows = [r for r in inbox.todo_cli(["list", "--json"]) if r["title"] == inbox.ALERT_TITLE]
        self.assertEqual(rows[0].get("run", "open -a Terminal /x/stg.command"), "open -a Terminal /x/stg.command")  # 옛 kit은 필드 없음
        inbox.alert("[Jira follow-up line 조치 필요] 둘", self.directory)
        rows = [r for r in inbox.todo_cli(["list", "--json"]) if r["title"] == inbox.ALERT_TITLE]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["waiting"]), ("waiting", "[Jira follow-up line 조치 필요] 둘"))
        self.assertEqual(rows[0].get("suggest", "조치했어요"), "조치했어요")  # 옛 kit(v2.21.0 전)은 필드가 없다
        with patch.dict(os.environ, JIRA_DECISION_SURFACE="html"):
            self.assertFalse(inbox.alert("x", self.directory, todo=lambda *_: self.fail("todo touched")))


    def test_ticket_wait_reuses_ticket_item_and_scan_can_resume_it(self):
        _, decision_item = self.register(CHOICE)
        (self.directory / "followup-queue.json").write_text(json.dumps({"PROJ-1": {"waiting": "human-risk", "note": "첫 기록 | 기획 확인 필요"}}))
        inbox.alert("human-risk: 기획 확인", self.directory, key="PROJ-1", summary="처리 방법")
        inbox.alert("human-risk: 다시 확인", self.directory, key="PROJ-1")
        rows = [r for r in inbox.todo_cli(["list", "--json"]) if r["id"] != decision_item]
        self.assertEqual(len(rows), 1)
        # 같은 키의 결정 D1이 열려 있으므로 티켓 항목은 결정 항목을 가리킨다(2026-11-06 PROJ-1439 D62).
        where = followup_scan.decision_where("결정 D1")
        self.assertEqual((rows[0]["title"], rows[0]["waiting"]), ("PROJ-1 처리 방법", f"공장 대기 — {where} · human-risk: 다시 확인"))
        brief = Path(rows[0]["brief"]).read_text()
        self.assertIn(f"- **Your call:** {where} · 공장이 혼자 진행하면 위험해", brief)
        _, settled = followup_scan.select_todo(rows, set())
        self.assertEqual(followup_scan.todo_actions(settled, {}), [["resume", rows[0]["id"], "공장 큐 대기 해제"]])


class Parsing(unittest.TestCase):
    def test_surface_setting_file_env_and_invalid(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, clear=False) as env:
            env.pop("JIRA_DECISION_SURFACE", None)
            self.assertEqual(inbox.surface(temporary), "html")
            (Path(temporary) / "decision-surface").write_text("App\n")
            self.assertEqual(inbox.surface(temporary), "app")
            (Path(temporary) / "decision-surface").write_text("chrome\n")
            with self.assertRaises(ValueError):
                inbox.surface(temporary)

    def test_prompt_shows_options_within_app_width_and_plain_reason(self):
        long = {**CHOICE, "title": "PROJ-1 처리 방법", "recommendReason": "계획대로 하면 된다(PLAN.md:12). 근거 logs/x.json",
                "options": [{"id": f"o{n}", "label": f"선택지 {n} — " + "긴 설명 " * 8} for n in range(1, 4)],
                "recommended": "o2"}
        text = inbox.prompt(long)
        self.assertIn("1) 선택지 1 · 2) 선택지 2 (AI 권장) · 3) 선택지 3 — 번호로 답('네'는 AI 권장)", text[:inbox.SHOWN])
        self.assertTrue(text.endswith("— 번호로 답('네'는 AI 권장)"))
        self.assertEqual(inbox.plain(long["recommendReason"]), "계획대로 하면 된다.")
        self.assertEqual(inbox.heading(long, "D7"), "[결정 D7] PROJ-1 · 처리 방법")

    def test_brief_uses_ticket_intent_when_spec_has_no_background(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "decision-pages").mkdir()
            (directory / "followup-queue.json").write_text(json.dumps({"PROJ-1": {"slug": "proj-1-x"}}))
            work = Path(temporary) / "workspace" / ".sdlc/work/proj-1-x"
            work.mkdir(parents=True)
            (work / "intent.md").write_text("# x\n\n## At a glance\n- **Background:** 로그인 없이 지워진다.\n- **Why:** 막아야 한다.\n\n## Facts\n")
            with patch.object(dp, "WORKSPACE", Path(temporary) / "workspace"):
                text = inbox.brief(CHOICE, "D3", directory).read_text()
            self.assertIn("- **Background:** 지금(AS-IS): 로그인 없이 지워진다.", text)
            self.assertIn("- **Why:** 막아야 한다.", text)
            text = inbox.brief({**CHOICE, "background": "직접 쓴 배경", "intent": "직접 쓴 의도"}, "D4", directory).read_text()
            self.assertIn("- **Background:** 지금(AS-IS): 직접 쓴 배경", text)
            self.assertIn("- **What changes:** 하려는 것(TO-BE): 직접 쓴 의도", text)

    def test_words_strips_code_detail(self):
        self.assertEqual(inbox.words("web-api `GET /files/secure-url`을 막는다(:304) EvalResult.vue:705-720 참고 https://x.y/z"),
                         "web-api GET /files/secure-url을 막는다 참고")

    def test_option_forms(self):
        for words, expected in (("2", "skip"), ("2번", "skip"), ("D12 1", "wait"), ("제외", "skip"),
                                ("skip", "skip"), ("보류", None), ("네", "skip"), ("승인", "skip")):
            with self.subTest(words=words):
                self.assertEqual(inbox.option_for(CHOICE, words), expected)
        for words in ("3", "0", "글쎄"):
            with self.subTest(words=words), self.assertRaises(ValueError):
                inbox.option_for(CHOICE, words)
        self.assertEqual(inbox.option_for(CHOICE, "시험 실패 이슈도 해결"), "free-text")
        self.assertEqual(inbox.option_for(CHOICE, "이슈해결부터진행"), "free-text")
        self.assertEqual(inbox.option_for(SQL, "승인"), "approve")
        for words in ("2", "시험 실패도 고치고 실행"):
            with self.subTest(words=words), self.assertRaises(ValueError):
                inbox.option_for(SQL, words)

    def test_scan_skips_decision_items(self):
        rows = [{"id": "a", "title": "[결정 D3] PROJ-1 · 처리 방법", "replies": [{"who": "human", "what": "reply — 1"}]},
                {"id": "b", "title": "PROJ-1 일반 항목", "replies": [{"who": "human", "what": "reply — 계속"}]}]
        events, settled = followup_scan.select_todo(rows, set())
        self.assertEqual([e["todoId"] for e in events], ["b"])
        self.assertEqual(settled, [])

    def test_scan_links_factory_sdlc_tickets_to_todo(self):
        rows = [{"id": "a", "title": "PROJ-1 기존 항목", "link": {"label": "PROJ-1"}},
                {"id": "b", "title": "PROJ-2", "slug": "proj-2-x"},
                {"id": "c", "title": "[결정 D3] PROJ-3 · 결정", "link": {"label": "PROJ-3"}}]
        queue = {"PROJ-1": {"slug": "proj-1-x"}, "PROJ-2": {"slug": "proj-2-x"}, "PROJ-3": {"slug": "proj-3-x"},
                 "PROJ-4": {"waiting": "reporter"}}
        actions = followup_scan.slug_actions(rows, queue, lambda key: "제목")
        self.assertEqual(actions[0], ["start", "a", "--slug", "proj-1-x"])
        self.assertEqual(actions[1][:4], ["add", "PROJ-3 제목", "--slug", "proj-3-x"])
        self.assertEqual(len(actions), 2)

    def test_scan_shows_queue_repositories_on_ticket_and_decision_items(self):
        rows = [{"id": "a", "title": "PROJ-1 x", "link": {"label": "PROJ-1"}, "repos": []},
                {"id": "b", "title": "[결정 D3] PROJ-1 · 결정", "link": {"label": "PROJ-1"},
                 "repos": ["api", "web"]},
                {"id": "c", "title": "PROJ-2", "repos": []},
                {"id": "d", "title": "PROJ-1 끝", "status": "done", "repos": []}]
        queue = {"PROJ-1": {"service": "api,web"}, "PROJ-2": {"service": ""}}
        self.assertEqual(followup_scan.repo_actions(rows, queue),
                         [["repos", "a", "api,web"]])

    def test_teams_send_accepts_app_approval_row(self):
        record = {"draftId": "d1", "textSha256": "h"}
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "decisions.jsonl"
            log.write_text(json.dumps({"kind": "teams-reply", "option": "approve", "by": "todo-app",
                                       "draftId": "d1", "textSha256": "h"}) + "\n")
            self.assertIsNotNone(teams_room.approval_for(log, record))

    def test_load_spec_rejects_prod_sql_and_missing_recommendation(self):
        with tempfile.TemporaryDirectory() as temporary:
            for spec in ({**SQL, "config": "app_prod"}, {**CHOICE, "recommended": "zzz"}):
                path = Path(temporary) / "spec.json"
                path.write_text(json.dumps(spec))
                with self.subTest(kind=spec["kind"]), self.assertRaises(ValueError):
                    dp.load_spec("spec", str(path), Path(temporary))


if __name__ == "__main__":
    unittest.main()
