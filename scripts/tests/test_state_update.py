"""공유 상태 직렬화와 실패 시 원본 보존을 오프라인으로 확인한다."""

import json
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import state_update as state


class StateUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="state update ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def read(self, filename):
        return state.read_object(self.directory / filename)

    def cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, str(ROOT / "state_update.py"),
                               "--dir", str(self.directory), *args],
                              capture_output=True, text=True, **kwargs)

    def test_empty_or_root_cli_directory_fails_before_patch(self):
        for value in ('', '/'):
            with self.subTest(value=value):
                stderr = io.StringIO()
                with patch.object(state, 'patch', return_value={}) as update, \
                        redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        state.main(['--dir', value, 'patch', 'state.json', '--json', '{}'])
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn('--dir', stderr.getvalue())
                update.assert_not_called()

    def test_nested_patch_retains_siblings_and_deletes_only_named_keys(self):
        state.atomic_json(self.directory / "state.json", {
            "morning": {"at": "keep"}, "followup": {
                "cursor": "keep", "handled": {"old": {"action": "human"}, "remove": {}}}})
        state.patch(self.directory, "state.json", ["followup", "handled"],
                    {"new": {"action": "posted"}}, delete=["remove"])
        self.assertEqual(self.read("state.json"), {
            "morning": {"at": "keep"}, "followup": {
                "cursor": "keep", "handled": {"old": {"action": "human"}, "new": {"action": "posted"}}}})

    def test_blank_note_cannot_erase_existing_note(self):
        state.atomic_json(self.directory / "followup-queue.json", {"PROJ-1": {"note": "원문 기록"}})
        result = self.cli("patch", "followup-queue.json", "--path", '["PROJ-1"]', "--json", '{"note": ""}')
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to blank", result.stderr)
        self.assertEqual(self.read("followup-queue.json")["PROJ-1"]["note"], "원문 기록")
        state.patch(self.directory, "followup-queue.json", ["PROJ-1"], {}, delete=["note"])
        self.assertNotIn("note", self.read("followup-queue.json")["PROJ-1"])

    def test_long_note_keeps_recent_tail_and_moves_head_to_notes_file(self):
        old = " | ".join(f"[2026-11-0{n % 9 + 1}] 기록 {n} " + "가" * 150 for n in range(60))
        state.atomic_json(self.directory / "followup-queue.json", {"PROJ-1": {"note": old}, "PROJ-2": {"note": "x" * 9000}})
        state.patch(self.directory, "followup-queue.json", ["PROJ-1"], {"note": old + " | 새 기록"})
        queue = self.read("followup-queue.json")
        note = queue["PROJ-1"]["note"]
        self.assertLessEqual(len(note), state.NOTE_MAX_CHARS)
        self.assertTrue(note.startswith(state.note_marker("PROJ-1")))
        self.assertTrue(note.endswith(" | 새 기록"))
        moved = (self.directory / "notes" / "PROJ-1.md").read_text()
        self.assertIn("기록 0 ", moved)
        # 잘린 기록은 notes 파일과 큐 둘 중 한 곳에 온전히 남는다.
        for n in range(60):
            self.assertTrue(f"기록 {n} " in moved or f"기록 {n} " in note, n)
        self.assertEqual(queue["PROJ-2"]["note"], "x" * 9000)  # 바뀌지 않은 항목은 손대지 않는다

    def test_note_line_cap_and_prepended_entry_moves_to_end(self):
        old = "\n".join(f"줄 {n}" for n in range(30))
        state.atomic_json(self.directory / "followup-queue.json", {"PROJ-1": {"note": old}})
        state.patch(self.directory, "followup-queue.json", ["PROJ-1"], {"note": "[새] 앞에 붙임 | " + old})
        note = self.read("followup-queue.json")["PROJ-1"]["note"]
        self.assertLessEqual(note.count("\n") + 1, state.NOTE_MAX_LINES)
        self.assertTrue(note.endswith("줄 29 | [새] 앞에 붙임"))
        self.assertIn("줄 0\n", (self.directory / "notes" / "PROJ-1.md").read_text())
        # 다시 덧붙여도 머리 표식은 하나다.
        state.patch(self.directory, "followup-queue.json", ["PROJ-1"], {"note": note + " | 다음"})
        again = self.read("followup-queue.json")["PROJ-1"]["note"]
        self.assertEqual(again.count(state.note_marker("PROJ-1")), 1)
        self.assertTrue(again.endswith(" | 다음"))

    def test_guard_transaction_note_append_is_capped_too(self):
        state.atomic_json(self.directory / "followup-queue.json", {"PROJ-1": {"note": "y" * 5000}})
        state.transaction(self.directory, "followup-queue.json",
                          lambda queue: queue["PROJ-1"].update(note=queue["PROJ-1"]["note"] + " | 가드 메모"))
        note = self.read("followup-queue.json")["PROJ-1"]["note"]
        self.assertLessEqual(len(note), state.NOTE_MAX_CHARS)
        self.assertTrue(note.endswith(" | 가드 메모"))
        self.assertTrue((self.directory / "notes" / "PROJ-1.md").exists())

    def test_queue_created_at_backfills_only_changed_items(self):
        state.atomic_json(self.directory / "followup-queue.json", {
            "untouched": {"waiting": None}, "changed": {"stage": "intent"},
            "dated": {"createdAt": "first", "stage": "intent"}})
        state.patch(self.directory, "followup-queue.json", ["changed"], {"stage": "plan"})
        state.patch(self.directory, "followup-queue.json", ["dated"], {"stage": "build"})
        queue = self.read("followup-queue.json")
        self.assertNotIn("createdAt", queue["untouched"])
        self.assertTrue(queue["changed"]["createdAt"])
        self.assertEqual(queue["dated"]["createdAt"], "first")

    def test_three_processes_fifty_updates_each_across_all_shared_files(self):
        program = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import state_update as state
directory, worker = Path(sys.argv[2]), sys.argv[3]
for index in range(50):
    key = worker + '-' + str(index)
    state.patch(directory, 'followup-queue.json', [], {key: {'waiting': None}})
    state.patch(directory, 'state.json', ['followup', 'handled'], {key: {'action': 'human'}})
    state.patch(directory, 'approvals.json', [], {key: {'stg': 'approved'}})
'''
        children = [subprocess.Popen([sys.executable, "-c", program, str(ROOT),
                                      str(self.directory), str(worker)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for worker in range(3)]
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stdout + stderr)
        expected = {f"{worker}-{index}" for worker in range(3) for index in range(50)}
        self.assertEqual(set(self.read("followup-queue.json")), expected)
        self.assertEqual(set(self.read("state.json")["followup"]["handled"]), expected)
        self.assertEqual(set(self.read("approvals.json")), expected)

    def test_waiting_item_content_change_resets_defer_count_without_touching_siblings(self):
        state.atomic_json(self.directory / "followup-queue.json", {
            "changed": {"waiting": "reporter", "stage": "intent", "deferCount": 3},
            "sibling": {"waiting": "reporter", "stage": "intent", "deferCount": 2},
            "legacy": {"waiting": "reporter", "stage": "intent"}})
        state.patch(self.directory, "followup-queue.json", ["changed"], {"stage": "plan"})
        state.patch(self.directory, "followup-queue.json", ["legacy"], {"stage": "plan"})
        queue = self.read("followup-queue.json")
        self.assertEqual(queue["changed"]["deferCount"], 0)
        self.assertEqual(queue["sibling"], {"waiting": "reporter", "stage": "intent", "deferCount": 2})
        self.assertNotIn("deferCount", queue["legacy"])

    def test_bookkeeping_only_change_keeps_defer_count(self):
        row = {"waiting": "reporter", "stage": "intent", "deferCount": 3}
        state.atomic_json(self.directory / "followup-queue.json", {"item": row})
        state.patch(self.directory, "followup-queue.json", ["item"], {
            "deferredAt": "new", "deferReason": "budget", "deferCount": 4,
            "updatedAt": "new", "createdAt": "first", "backoffUntil": "later", "backoffLevel": 1})
        current = self.read("followup-queue.json")["item"]
        self.assertEqual(current["deferCount"], 4)
        self.assertEqual(state.item_digest(current), state.item_digest(row))

    def test_replace_failure_keeps_original_and_removes_temporary(self):
        target = self.directory / "state.json"
        state.atomic_json(target, {"original": True})
        original = target.read_bytes()
        with patch.object(state.os, "replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaises(OSError):
                state.patch(self.directory, "state.json", [], {"new": True})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])
        # 예외 뒤에도 잠금이 해제되어 후속 갱신을 할 수 있어야 한다.
        self.assertEqual(self.cli("patch", "state.json", "--json", '{"after": true}').returncode, 0)

    def test_mutation_or_serialization_failure_never_changes_original(self):
        target = self.directory / "state.json"
        state.atomic_json(target, {"original": True})
        original = target.read_bytes()

        def fail(data):
            data["new"] = True
            raise ValueError("fixture callback failure")

        with self.assertRaises(ValueError):
            state.transaction(self.directory, "state.json", fail)
        with self.assertRaises(TypeError):
            state.patch(self.directory, "state.json", [], {"invalid": object()})
        self.assertEqual(target.read_bytes(), original)

    def test_corrupt_or_non_object_state_is_not_replaced(self):
        for payload in ("{broken", "[]"):
            with self.subTest(payload=payload):
                target = self.directory / "state.json"
                target.write_text(payload)
                result = self.cli("patch", "state.json", "--json", '{"new": true}')
                self.assertEqual(result.returncode, 1)
                self.assertEqual(target.read_text(), payload)

    def test_invalid_nested_path_is_not_silently_overwritten(self):
        state.atomic_json(self.directory / "state.json", {"followup": "invalid"})
        with self.assertRaises(ValueError):
            state.patch(self.directory, "state.json", ["followup", "handled"], {"new": {}})
        self.assertEqual(self.read("state.json"), {"followup": "invalid"})

    def test_cli_patch_and_approval_preserve_other_keys(self):
        self.assertEqual(self.cli("patch", "state.json", "--path", '["followup","handled"]',
                                  "--json", '{"100":{"action":"human"}}').returncode, 0)
        self.assertIn("100", self.read("state.json")["followup"]["handled"])
        home = self.directory / "home"
        approvals = home / ".jira-followup/approvals.json"
        state.atomic_json(approvals, {"PROJ-1": {"stg": "keep"}})
        result = subprocess.run([sys.executable, str(ROOT / "jira-followup-approve"),
                                 "proj-2", "stg", "승인"],
                                env={**os.environ, "HOME": str(home)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("approved PROJ-2 stg at "))
        self.assertEqual(state.read_object(approvals)["PROJ-1"], {"stg": "keep"})
        self.assertEqual(state.read_object(approvals)["PROJ-2"]["stg"], "승인")

    def test_pending_consumes_exact_occurrences_preserving_new_appends(self):
        old = '{"key":"PROJ-1"}'
        state.append_pending(self.directory, old)
        # 읽은 줄은 하나이고, 그 뒤 같은 내용과 다른 새 요청이 추가된 상황이다.
        captured = (self.directory / "pending-focus.jsonl").read_text().splitlines()
        state.append_pending(self.directory, old)
        state.append_pending(self.directory, '{"key":"PROJ-2"}')
        self.assertEqual(state.consume_pending(self.directory, captured), 1)
        self.assertEqual((self.directory / "pending-focus.jsonl").read_text().splitlines(),
                         [old, '{"key":"PROJ-2"}'])

    def test_pending_append_and_consume_share_serialization(self):
        state.append_pending(self.directory, "processed")
        program = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import state_update as state
for index in range(50):
    state.append_pending(Path(sys.argv[2]), 'new-' + str(index))
'''
        child = subprocess.Popen([sys.executable, "-c", program, str(ROOT), str(self.directory)])
        try:
            self.assertEqual(state.consume_pending(self.directory, ["processed"]), 1)
        finally:
            self.assertEqual(child.wait(timeout=15), 0)
        self.assertEqual(set((self.directory / "pending-focus.jsonl").read_text().splitlines()),
                         {f"new-{index}" for index in range(50)})

    def test_pending_cli_and_multiline_rejection(self):
        self.assertEqual(self.cli("append-pending", "--json", '{"key":"PROJ-1"}').returncode, 0)
        line = (self.directory / "pending-focus.jsonl").read_text().splitlines()[0]
        result = self.cli("consume-pending", "--lines", json.dumps([line]))
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "1"))
        self.assertEqual((self.directory / "pending-focus.jsonl").read_text(), "")
        with self.assertRaises(ValueError):
            state.append_pending(self.directory, "one\ntwo")


if __name__ == "__main__":
    unittest.main()
