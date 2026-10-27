"""Offline Claude/Codex JSONL fixtures; no real conversation content."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SessionDigestTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("session_digest", ROOT / "session_digest.py")
        self.digest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.digest)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.since = self.digest.parse_time("2026-10-21T00:00:00Z")
        self.until = self.digest.parse_time("2026-10-22T00:00:00Z")

    def write(self, path, rows):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return target

    def claude(self, content, hour=1, **extra):
        return {"type": "user", "sessionId": "claude-1", "timestamp": f"2026-10-21T{hour:02}:00:00Z",
                "message": {"content": content}, **extra}

    def render(self):
        return self.digest.build_digest(self.home, self.since, self.until)

    def test_claude_turns_errors_interrupts_and_all_hook_blocks(self):
        rows = [self.claude("first user"), self.claude([
            {"type": "text", "text": "second"}, {"type": "text", "text": "part"}]),
            self.claude("<system-reminder>hidden</system-reminder>"),
            self.claude("<task-notification>hidden</task-notification>"),
            self.claude("[Request interrupted by user for tool use]"),
            {"type": "assistant", "timestamp": "2026-10-21T02:00:00Z", "message": {"content": [
                {"type": "tool_use", "id": "t", "input": {"command": "git push " + "x" * 250}}]}}]
        for i in range(6):
            rows.append(self.claude([{"type": "tool_result", "tool_use_id": "t", "is_error": True,
                                     "content": "PreToolUse:Bash hook error: [/hooks/guard.sh]: reason " + str(i)}], 3))
        self.write(".claude/projects/p/main.jsonl", rows)
        report = self.render()
        self.assertIn("user_turns=2", report)
        self.assertIn("tool_errors=6", report)
        self.assertIn("interrupts=1", report)
        self.assertEqual(report.count("hook=guard.sh reason="), 6)
        self.assertIn("guard.sh=6", report)
        self.assertNotIn("hidden", report)
        self.assertIn("second part", report)
        self.assertNotIn("x" * 221, report)

    def test_range_uses_event_time_not_mtime_and_excludes_subagents(self):
        p = self.write(".claude/projects/p/main.jsonl", [
            {**self.claude("old"), "timestamp": "2026-10-20T23:59:59Z"},
            self.claude("boundary", 0), self.claude("inside", 23),
            {**self.claude("future"), "timestamp": "2026-10-22T00:00:00Z"}])
        os.utime(p, (1, 1))
        self.write(".claude/projects/p/subagents/agent-x.jsonl", [self.claude("nested subagent")])
        self.write(".claude/projects/p/agent-x.jsonl", [self.claude("flat subagent")])
        report = self.render()
        self.assertIn("user_turns=2", report)
        for excluded in ("old", "future", "nested subagent", "flat subagent"):
            self.assertNotIn(excluded, report)

    def test_codex_mirror_archive_dedup_and_event_user_fallback(self):
        rows = [
            {"type": "session_meta", "timestamp": "2026-10-21T01:00:00Z", "payload": {"id": "codex-1"}},
            {"type": "event_msg", "timestamp": "2026-10-21T01:00:01Z", "payload": {"type": "user_message", "message": "same turn"}},
            {"type": "response_item", "timestamp": "2026-10-21T01:00:02Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "same turn"}]}},
            {"type": "response_item", "timestamp": "2026-10-21T01:00:03Z", "payload": {
                "type": "function_call", "call_id": "call", "arguments": json.dumps({"cmd": "guarded"})}},
            {"type": "response_item", "timestamp": "2026-10-21T01:00:04Z", "payload": {
                "type": "function_call_output", "call_id": "call", "output": "Process exited with code 1\nPreToolUse:Bash hook error: [/x/g.sh]: denied"}},
            {"type": "event_msg", "timestamp": "2026-10-21T01:00:05Z", "payload": {"type": "turn_aborted"}},
            {"type": "response_item", "timestamp": "2026-10-21T01:00:06Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<turn_aborted>interrupted</turn_aborted>"}]}}]
        self.write(".codex/sessions/2026/10/21/live.jsonl", rows)
        self.write(".codex/archived_sessions/archive.jsonl", rows)
        self.write(".codex/archived_sessions/old-format.jsonl", [
            {"type": "session_meta", "timestamp": "2026-10-21T03:00:00Z", "payload": {"id": "old-format"}},
            {"type": "event_msg", "timestamp": "2026-10-21T03:00:01Z", "payload": {"type": "user_message", "message": "fallback user"}}])
        report = self.render()
        self.assertIn("sessions=2 factory=0 user_turns=2", report)
        self.assertEqual(report.count("same turn"), 1)
        self.assertIn("tool_errors=1", report)
        self.assertIn("interrupts=1", report)
        self.assertIn("command=guarded", report)
        self.assertTrue(report.rstrip().splitlines()[-1].startswith("Totals:"))

    def test_factory_is_detected_before_range_and_printed_as_one_line(self):
        for i, prefix in enumerate(self.digest.FACTORY_PREFIXES):
            self.write(f".claude/projects/p/factory{i}.jsonl", [
                {**self.claude(prefix + " internal secret"), "sessionId": f"factory{i}", "timestamp": "2026-10-20T23:00:00Z"},
                {**self.claude("later hidden", 1), "sessionId": f"factory{i}"},
                {**self.claude([{"type": "tool_result", "tool_use_id": "missing", "is_error": True,
                                 "content": "PreToolUse:Bash hook error: [/hooks/guard.sh]: blocked"}], 2), "sessionId": f"factory{i}"}])
        report = self.render()
        self.assertIn("sessions=2 factory=2 user_turns=2", report)
        self.assertEqual(sum("[factory]" in line for line in report.splitlines()), 2)
        self.assertNotIn("internal secret", report)
        self.assertNotIn("later hidden", report)
        self.assertEqual(report.count("command=unknown"), 2)

    def test_redaction_before_truncation_in_user_reason_and_command(self):
        secrets = ["Bearer -fake-bearer-secret", "ghp_abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnop", "xoxb-123456789-secret", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"]
        text = " ".join(secrets) + " tail"
        self.write(".claude/projects/p/redact.jsonl", [self.claude(text),
            {"type": "assistant", "timestamp": "2026-10-21T02:00:00Z", "message": {"content": [
                {"type": "tool_use", "id": "t", "input": {"command": text}}]}},
            self.claude([{"type": "tool_result", "tool_use_id": "t", "is_error": True,
                           "content": "PreToolUse:Bash hook error: [/x/g.sh]: " + text}], 3)])
        report = self.render()
        for secret in secrets:
            self.assertNotIn(secret, report)
        self.assertIn("[REDACTED]", report)
        self.assertIn("tail", report)
        self.assertEqual(len(self.digest.preview("x" * 400, 300)), 300)

    def test_codex_content_blocks_report_error_and_subagent_is_excluded(self):
        self.write(".codex/sessions/2026/10/21/main.jsonl", [
            {"type": "session_meta", "timestamp": "2026-10-21T01:00:00Z", "payload": {"id": "codex-main"}},
            {"type": "response_item", "timestamp": "2026-10-21T01:00:01Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "user"}]}},
            {"type": "response_item", "timestamp": "2026-10-21T01:00:02Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "call", "output": [{"type": "text", "text": json.dumps({"exit_code": 2, "output": "failed"})}]}}])
        self.write(".codex/sessions/2026/10/21/child.jsonl", [
            {"type": "session_meta", "timestamp": "2026-10-21T01:00:00Z", "payload": {"id": "codex-child", "source": {"subagent": {"parent_thread_id": "codex-main"}}}},
            {"type": "response_item", "timestamp": "2026-10-21T01:00:01Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "child hidden"}]}}])
        report = self.render()
        self.assertIn("sessions=1 factory=0 user_turns=1", report)
        self.assertIn("tool_errors=1", report)
        self.assertNotIn("child hidden", report)

    def test_codex_injected_context_precedes_factory_prompt(self):
        rows = [{"type": "session_meta", "timestamp": "2026-10-21T01:00:00Z", "payload": {"id": "codex-factory"}}]
        for i, text in enumerate(("# AGENTS.md instructions\nprivate setup", "<environment_context>private context</environment_context>", self.digest.FACTORY_PREFIXES[0])):
            rows.append({"type": "response_item", "timestamp": f"2026-10-21T01:00:0{i + 1}Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        self.write(".codex/sessions/2026/10/21/factory.jsonl", rows)
        report = self.render()
        self.assertIn("sessions=1 factory=1 user_turns=1", report)
        self.assertNotIn("private setup", report)

    def test_cli_out_and_invalid_range_and_malformed_line(self):
        path = self.write(".claude/projects/p/main.jsonl", [self.claude("user")])
        with path.open("a") as stream:
            stream.write("not json\n")
        out = self.home / "digest.md"
        env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(ROOT / "session_digest.py"), "--since", str(self.since), "--until", str(self.until), "--out", str(out)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("malformed_lines=1", out.read_text())
        result = subprocess.run([sys.executable, str(ROOT / "session_digest.py"), "--since", "bad"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ISO", result.stderr)
        with self.assertRaises(ValueError):
            self.digest.build_digest(self.home, self.until, self.since)


if __name__ == "__main__":
    unittest.main()
