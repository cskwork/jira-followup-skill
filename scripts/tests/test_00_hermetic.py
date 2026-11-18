"""Make the offline suite independent of the operator's own config.

The clearing itself happens in _harness.py, which test_000_harness.py imports before any other test
module (unittest discover imports modules in sorted order). This module checks the result: no
jira-followup setting inherited from the shell, the config loader reads an empty file, and only the
forced safety values are set.
"""
import os
import unittest

import _harness


class HermeticEnvironment(unittest.TestCase):
    def test_no_inherited_settings(self):
        leaked = [key for key in _harness.HERMETIC_KEYS
                  if key in os.environ and key not in _harness.SAFETY]
        self.assertEqual(leaked, [])
        self.assertEqual(os.environ["JIRA_FOLLOWUP_CONFIG"], os.devnull)

    def test_safety_values_are_forced(self):
        for key, value in _harness.SAFETY.items():
            self.assertEqual(os.environ.get(key), value, key)


if __name__ == "__main__":
    unittest.main()
