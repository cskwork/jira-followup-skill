"""티켓 packet: 키 하나에 필요한 것만, 읽기 전용으로 모은다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import ticket_packet  # noqa: E402


class TicketPacketTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        note = " | ".join(f"기록 {n}" for n in range(9))
        self.write("followup-queue.json", {
            "PROJ-1": {"slug": "proj-1-x", "stage": "dev-deployed", "delivery": "dev-deployed", "waiting": None, "note": note},
            "PROJ-2": {"stage": "plan", "note": "다른 키"}})
        self.write("state.json", {"followup": {"handled": {
            "1": {"key": "PROJ-1", "action": "posted", "at": "2026-11-01T00:00:00+09:00"},
            "2": {"key": "PROJ-2", "action": "human", "at": "2026-11-02T00:00:00+09:00"}}}})
        self.write("approvals.json", {"PROJ-1": {"stg": "승인", "at": "2026-11-03"}, "PROJ-2": {"stg": "x"}})
        rows = [{"at": f"2026-11-0{n}", "key": "PROJ-1", "kind": "choice", "option": str(n)} for n in range(1, 6)]
        rows.insert(2, {"at": "2026-11-09", "key": "PROJ-2", "kind": "choice", "option": "z"})
        (self.state / "decisions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows) + "not json\n")
        (self.state / "tickets").mkdir()
        (self.state / "tickets" / "PROJ-1.inbox.jsonl").write_text(
            json.dumps({"commentId": "1", "key": "PROJ-1", "text": "처리됨"}) + "\n"
            + json.dumps({"commentId": "9", "key": "PROJ-1", "text": "새 댓글"}) + "\n")
        run = self.state / "runs" / ("d" * 32)
        run.mkdir(parents=True)
        self.write(run.relative_to(self.state) / "PROJ-1-dev-e2e.requirements.json",
                   {"key": "PROJ-1", "stage": "dev-verified", "checks": [{"name": "dev-e2e"}]})

    def write(self, name, value):
        path = self.state / name
        path.write_text(json.dumps(value, ensure_ascii=False))

    def test_packet_holds_only_this_key_and_note_tail(self):
        before = {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        packet = ticket_packet.build(self.state, "PROJ-1", notes=3, sdlc=False)
        self.assertEqual(packet["queue"]["noteTail"], ["기록 6", "기록 7", "기록 8"])
        self.assertEqual(packet["queue"]["noteHead"], "기록 0")
        self.assertEqual(packet["queue"]["noteOmitted"], 5)
        self.assertNotIn("note", packet["queue"])
        self.assertEqual(packet["lifecycle"], {"delivery": "dev-deployed", "sdlc": None})
        self.assertEqual(packet["handled"]["count"], 1)
        self.assertEqual([row["commentId"] for row in packet["inbox"]], ["9"])
        self.assertEqual(packet["approval"], {"stg": "승인", "at": "2026-11-03"})
        self.assertEqual([row["option"] for row in packet["decisions"]], ["3", "4", "5"])
        self.assertEqual(packet["requirements"]["checks"], ["dev-e2e"])
        self.assertIsNone(packet["lease"])
        self.assertNotIn("다른 키", json.dumps(packet, ensure_ascii=False))
        self.assertEqual(before, {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()})

    def test_cli_unknown_key_and_invalid_key(self):
        env = dict(os.environ, JIRA_SDLC="off")
        result = subprocess.run([sys.executable, str(SCRIPTS / "ticket_packet.py"), "PROJ-404", "--dir", str(self.state)],
                                capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(result.stdout)
        self.assertIsNone(packet["queue"])
        self.assertEqual(packet["decisions"], [])
        bad = subprocess.run([sys.executable, str(SCRIPTS / "ticket_packet.py"), "../x", "--dir", str(self.state)],
                             capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
