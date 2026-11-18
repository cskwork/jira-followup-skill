"""결정 게이트·묶음·버튼 계약. 모든 할 일·실행은 메모리 안의 가짜다."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import json
import os
from pathlib import Path
import subprocess

import pytest

# 다른 시험 모듈이 먼저 sys.path를 넣어 주던 것에 기대지 않는다(혼자 실행하면 ModuleNotFoundError).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_inbox as inbox  # noqa: E402
import decision_page as dp
import followup_scan


CHOICE = {"kind": "choice", "key": "PROJ-1429", "title": "보안 계획",
          "humanReason": "security-plan", "recommended": "review",
          "options": [{"id": "review", "label": "계획 검토 | 보완 — 설명"},
                      {"id": "approve", "label": "계획 승인 — 설명"}]}


class FakeTodo:
    def __init__(self, fail_choices=False):
        self.rows, self.calls = [], []
        self.fail_choices = fail_choices

    def __call__(self, args):
        self.calls.append(args)
        command = args[0]
        if command == "list":
            return self.rows
        if command == "add":
            item = f"item-{len(self.rows) + 1}"
            self.rows.append({"id": item, "title": args[1], "status": "active", "replies": []})
            return item
        row = next(r for r in self.rows if r["id"] == args[1])
        if command == "wait":
            row.update(status="waiting", waiting=args[2])
        elif command == "done":
            row.update(status="done")
        elif command == "choices":
            if self.fail_choices:
                raise RuntimeError('exit 2: unknown command "choices"')
        elif command == "suggest":
            # suggest는 sdlc-kit v2.21.0부터다. choices가 없는 옛 kit에는 suggest도 없다.
            if self.fail_choices:
                raise RuntimeError('exit 2: unknown command "suggest"')
            row.update(suggest=args[2])
        else:
            raise AssertionError(args)

    def reply(self, text, item=None):
        row = next(r for r in self.rows if r["id"] == item) if item else self.rows[0]
        row["replies"].append({"who": "human", "what": "reply — " + text,
                               "at": f"2026-11-03 10:{len(row['replies']):02d}"})


@pytest.fixture
def factory(tmp_path):
    todo, ran, woken = FakeTodo(), [], []

    def register(spec):
        path = tmp_path / f"{spec['key']}-{spec['kind']}.json"
        path.write_text(json.dumps(spec))
        return inbox.register("spec", str(path), tmp_path, todo=todo)

    def decider(*args, **kwargs):
        def action(spec, stamp):
            ran.append(spec["key"])
            return True, "fake deploy"
        return inbox.decide(*args, actions={"stg-approval": action}, wake=woken.append, **kwargs)

    def poll():
        return inbox.poll(tmp_path, todo=todo, decider=decider)

    return todo, register, poll, ran, woken


def stg(key):
    return {"kind": "stg-approval", "key": key, "title": "스테이징 배포 승인"}


def codes(directory):
    return json.loads((directory / "decision-pages/codes.json").read_text())


@pytest.mark.parametrize("reason,rule", [
    (None, "사람에게 묻지 않는 절차"), ("jira-status", "Jira 상태 이동"),
    ("evidence-comment", "증빙 댓글 보충"), ("stg-prepare", "stg 병합 준비"),
    ("baseline-fail", "기준선 시험 실패"), ("result-sql", "결과 댓글 참고용 SQL 첨부"),
    ("other", "사람에게 묻지 않는 절차"), ([], "사람에게 묻지 않는 절차")])
def test_choice_gate_rejects_before_registration(tmp_path, reason, rule):
    spec = dict(CHOICE)
    if reason is None:
        del spec["humanReason"]
    else:
        spec["humanReason"] = reason
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    for load in (lambda: dp.load_spec("spec", str(path), tmp_path),
                 lambda: inbox.register("spec", str(path), tmp_path, todo=lambda _: pytest.fail("todo touched"))):
        with pytest.raises(ValueError, match=f"RUNBOOK §0-3.*{rule}"):
            load()
    assert not (tmp_path / "decision-pages/codes.json").exists()


@pytest.mark.parametrize("reason", ["product-scope", "security-plan", "prod-data", "policy", "conflict"])
def test_valid_human_reason_opens_and_waits_for_human(factory, tmp_path, reason):
    todo, register, poll, ran, woken = factory
    code, item = register({**CHOICE, "humanReason": reason})
    assert todo.calls[-2:] == [["choices", item, "계획 검토 / 보완 | 계획 승인", "--pick", "1"],
                              ["suggest", item, "계획 검토 / 보완"]]
    assert codes(tmp_path)[code]["state"] == "open"
    assert todo.rows[0]["status"] == "waiting"
    assert poll() == []
    assert ran == woken == []
    assert not (tmp_path / "decisions.jsonl").exists()


@pytest.mark.parametrize("reason", [None, "jira-status", "evidence-comment", "stg-prepare", "baseline-fail", "result-sql"])
@pytest.mark.parametrize("surface", ["app", "html"])
def test_decision_request_reports_gate_to_caller(tmp_path, reason, surface):
    spec = {**CHOICE, "humanReason": reason}
    if reason is None:
        del spec["humanReason"]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    env = {**os.environ, "JIRA_WAKE_DIR": str(tmp_path), "JIRA_DECISION_SURFACE": surface}
    result = subprocess.run([str(Path(inbox.__file__).parent / "decision-request"), "spec", str(path)],
                            capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode == 1
    assert "RUNBOOK §0-3" in result.stderr + result.stdout
    assert "자동 규칙" in result.stderr + result.stdout
    assert not (tmp_path / "decision-pages/codes.json").exists()


def test_two_stg_approvals_share_item_and_partial_then_all(factory, tmp_path):
    todo, register, poll, ran, woken = factory
    first, item = register(stg("PROJ-1429"))
    second, other = register(stg("PROJ-1431"))
    assert item == other
    assert len(todo.rows) == 1
    assert todo.rows[0]["title"] == "[스테이징 승인] PROJ-1429"
    assert "PROJ-1429·PROJ-1431" in todo.rows[0]["waiting"]
    assert all(row["batch"] and row["todoId"] == item for row in codes(tmp_path).values())
    assert todo.calls[-2:] == [["choices", item, "승인 | 보류", "--pick", "1"], ["suggest", item, "승인"]]

    todo.reply("승인 1429")
    assert poll() == [(first, "decided")]
    assert ran == woken == ["PROJ-1429"]
    assert codes(tmp_path)[second]["state"] == "open"
    assert todo.rows[0]["status"] == "waiting"
    assert "PROJ-1431" in todo.rows[0]["waiting"]
    assert "PROJ-1429" not in todo.rows[0]["waiting"]
    assert poll() == []  # list에 예전 답이 남아 있어도 다시 묻거나 실행하지 않는다.

    todo.reply("승인")
    assert poll() == [(second, "decided")]
    assert todo.rows[0]["status"] == "done"
    assert ran == woken == ["PROJ-1429", "PROJ-1431"]
    audit = [json.loads(row) for row in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert [row["key"] for row in audit] == ran
    assert all(row["by"] == "todo-app" and row["ok"] for row in audit)


def test_all_approval_has_distinct_audit_ids_and_runs_each_code(factory, tmp_path):
    todo, register, poll, ran, _ = factory
    first, _ = register(stg("PROJ-1429"))
    second, _ = register(stg("PROJ-1431"))
    todo.reply("승인")
    assert poll() == [(first, "decided"), (second, "decided")]
    assert ran == ["PROJ-1429", "PROJ-1431"]
    audit = [json.loads(row) for row in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert len({row["messageId"] for row in audit}) == 2
    assert todo.rows[0]["status"] == "done"


@pytest.mark.parametrize("reply,states", [
    ("보류", ["expired", "expired"]), ("보류 PROJ-1431", ["open", "expired"]),
    ("승인 PROJ-1429,1431", ["decided", "decided"])])
def test_batch_hold_and_full_key_selection(factory, tmp_path, reply, states):
    todo, register, poll, ran, woken = factory
    first, _ = register(stg("PROJ-1429"))
    second, _ = register(stg("PROJ-1431"))
    todo.reply(reply)
    poll()
    assert [codes(tmp_path)[code]["state"] for code in (first, second)] == states
    assert todo.rows[0]["status"] == ("waiting" if "open" in states else "done")
    if "decided" not in states:
        assert ran == woken == []
        assert not (tmp_path / "decisions.jsonl").exists()


@pytest.mark.parametrize("reply", ["아마도", "승인 9999", "보류 1429,9999", "승인 1429,", "고친 뒤 진행",
                                 "1", "네", "ok", "approve", "hold"])
def test_invalid_batch_reply_reasks_once_without_partial_action(factory, tmp_path, reply):
    todo, register, poll, ran, _ = factory
    first, _ = register(stg("PROJ-1429"))
    second, _ = register(stg("PROJ-1431"))
    todo.reply(reply)
    assert poll() == [(first, "invalid"), (second, "invalid")]
    waits = len([call for call in todo.calls if call[0] == "wait"])
    assert poll() == []
    assert len([call for call in todo.calls if call[0] == "wait"]) == waits
    assert ran == []
    assert all(row["state"] == "open" for row in codes(tmp_path).values())


def test_new_code_does_not_inherit_previous_all_approval(factory, tmp_path):
    todo, register, poll, ran, _ = factory
    first, _ = register(stg("PROJ-1429"))
    todo.reply("승인")
    second, _ = register(stg("PROJ-1431"))
    assert poll() == [(first, "decided")]
    assert codes(tmp_path)[second]["state"] == "open"
    assert ran == ["PROJ-1429"]


@pytest.mark.parametrize("reply,state", [("1", "decided"), ("네", "decided"), ("hold", "expired")])
def test_single_stg_item_keeps_existing_reply_aliases(factory, tmp_path, reply, state):
    todo, register, poll, _, _ = factory
    code, _ = register(stg("PROJ-1429"))
    todo.reply(reply)
    poll()
    assert codes(tmp_path)[code]["state"] == state
    assert todo.rows[0]["status"] == "done"


def test_batch_reuse_requires_registry_flag_and_open_item(factory):
    todo, register, poll, _, _ = factory
    todo(["add", "[스테이징 승인] PROJ-1000"])
    _, item = register(stg("PROJ-1429"))
    assert item != todo.rows[0]["id"]
    todo.reply("보류", item)
    poll()
    _, next_item = register(stg("PROJ-1431"))
    assert next_item != item


def test_concurrent_stg_requests_create_one_batch(factory):
    todo, register, _, _, _ = factory
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(register, [stg("PROJ-1429"), stg("PROJ-1431")]))
    assert result[0][1] == result[1][1]
    assert len(todo.rows) == 1
    assert all(key in todo.rows[0]["waiting"] for key in ("PROJ-1429", "PROJ-1431"))


@pytest.mark.parametrize("kind", ["choice", "stg-approval"])
def test_older_choices_failure_keeps_item_open(factory, tmp_path, capsys, kind):
    todo, register, _, _, _ = factory
    todo.fail_choices = True
    code, _ = register(CHOICE if kind == "choice" else stg("PROJ-1429"))
    assert todo.rows[0]["status"] == "waiting"
    assert codes(tmp_path)[code]["state"] == "open"
    assert 'choices ignored: exit 2: unknown command "choices"' in capsys.readouterr().err


def test_choice_button_short_label_maps_to_original_option(factory, tmp_path):
    todo, register, poll, _, woken = factory
    code, _ = register(CHOICE)
    todo.reply("계획 검토 / 보완")
    assert poll() == [(code, "decided")]
    audit = json.loads((tmp_path / "decisions.jsonl").read_text())
    assert audit["option"] == "review"
    assert audit["note"] == CHOICE["options"][0]["label"]
    assert woken == ["PROJ-1429"]


def test_choice_buttons_limit_six_and_keep_recommendation(factory):
    todo, register, _, _, _ = factory
    options = [{"id": str(n), "label": f"선택 {n} — 상세"} for n in range(1, 8)]
    _, item = register({**CHOICE, "options": options, "recommended": "7"})
    assert todo.calls[-2:] == [["choices", item, "선택 1 | 선택 2 | 선택 3 | 선택 4 | 선택 5 | 선택 7", "--pick", "6"],
                              ["suggest", item, "선택 7"]]


def test_followup_scan_leaves_batch_reply_to_decision_receiver():
    row = {"id": "batch", "title": "[스테이징 승인] PROJ-1429", "status": "waiting",
           "replies": [{"who": "human", "what": "reply — 승인"}]}
    assert followup_scan.select_todo([row], set()) == ([], [])
    assert followup_scan.slug_actions([row], {"PROJ-1429": {"slug": "proj-1429-fix"}})[0][0] == "add"


class KitBriefTemplateTests(unittest.TestCase):
    def test_brief_follows_kit_template_order_and_drops_empty_lines(self):
        rows = inbox.kit_brief("PROJ-1 x", {"Why": "이유", "Your call": "고를까요?", "AI pick": ""}, ["", "## 꼬리"])
        self.assertEqual(rows, ["# PROJ-1 x", "", "## At a glance", "- **Your call:** 고를까요?", "- **Why:** 이유", "", "## 꼬리"])

    def test_unknown_label_or_missing_template_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            inbox.kit_brief("t", {"Summary": "x"}, [])
        with tempfile.TemporaryDirectory() as empty, patch.object(inbox, "SDLC_KIT", Path(empty)):
            with self.assertRaises(RuntimeError):
                inbox.kit_brief("t", {"Why": "x"}, [])
