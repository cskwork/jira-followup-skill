"""Make the offline suite independent of the operator's own config.

`unittest discover` imports test modules in sorted order, so this module runs before any
other test imports followup_config. It removes every jira-followup setting inherited from
the shell and points the config loader at an empty file; fixtures then see the defaults
(PROJ, dev/stg/main, ~/.jira-followup under each test's HOME).
"""
import os
import re
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[2] / "config.example.env"
KEYS = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", EXAMPLE.read_text(encoding="utf-8"), re.M))
KEYS |= {"JIRA_FOLLOWUP_HOME", "JIRA_WAKE_DIR", "JIRA_WAKE_CWD", "JIRA_WORKER_CWD", "JIRA_API_TOKEN",
         "FOLLOWUP_IDLE_SCAN_CMD", "FOLLOWUP_IDLE_PREFLIGHT_CMD"}
for key in KEYS:
    os.environ.pop(key, None)
os.environ["JIRA_FOLLOWUP_CONFIG"] = os.devnull


class HermeticEnvironment(unittest.TestCase):
    def test_no_inherited_settings(self):
        self.assertEqual([key for key in KEYS if key in os.environ], [])
        self.assertEqual(os.environ["JIRA_FOLLOWUP_CONFIG"], os.devnull)
