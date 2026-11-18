"""factory_workspace.py: 고정 workspace 재사용·재생성·실패 시 빈 출력. paseo는 stub."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "factory_workspace.py"


class FactoryWorkspaceChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "calls.log"
        paseo = self.bin / "paseo"
        paseo.write_text('#!/bin/bash\necho "$*" >> "$CALLS"\n'
                         'if [ "$2" = ls ]; then printf "%s" "$LS"; exit 0; fi\n'
                         'if [ "$2" = create ]; then printf "%s" "$CREATE"; exit "${CREATE_RC:-0}"; fi\n')
        paseo.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k != "JIRA_FACTORY_WORKSPACE"}
        self.env.update(PATH=f"{self.bin}:{os.environ['PATH']}", CALLS=str(self.calls), LS="[]",
                        CREATE=json.dumps({"workspaceId": "wks_new", "name": "Jira follow-up factory"}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, **extra):
        return subprocess.run(["python3", str(SCRIPT), "--dir", str(self.dir), "--path", "/product"],
                              env=dict(self.env, **extra), capture_output=True, text=True, timeout=10)

    def record(self):
        return self.dir / "factory-workspace"

    def test_reuses_saved_active_workspace(self):
        self.record().write_text("wks_saved\n")
        result = self.run_script(LS=json.dumps([{"workspaceId": "wks_saved"}]))
        self.assertEqual(result.stdout.strip(), "wks_saved")
        self.assertNotIn("create", self.calls.read_text())

    def test_creates_and_saves_when_saved_workspace_was_archived(self):
        self.record().write_text("wks_gone\n")
        result = self.run_script()
        self.assertEqual(result.stdout.strip(), "wks_new")
        self.assertEqual(self.record().read_text().strip(), "wks_new")
        self.assertIn("workspace create --isolation local --path /product --title Jira follow-up factory --json",
                      self.calls.read_text())

    def test_failure_prints_nothing_and_exits_zero(self):
        result = self.run_script(CREATE="boom", CREATE_RC="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("launching without --workspace", result.stderr)
        self.assertFalse(self.record().exists())

    def test_override_skips_paseo(self):
        result = self.run_script(JIRA_FACTORY_WORKSPACE="wks_fixed")
        self.assertEqual(result.stdout.strip(), "wks_fixed")
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
