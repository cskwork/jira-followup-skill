"""공장 셸 스크립트는 here-document와 here-string을 쓰지 않는다.

macOS 기본 bash 3.2는 둘 다 임시 파일로 만들고 지운다. 파일 삭제가 느린 기기에서는 한 번에 몇 초가 걸려
깨우기·작업자 시작이 그만큼 늦어진다(2026-11-10: 삭제 1회 5.4초, jira-worker 한 번에 6곳).
Python은 `python3 -c '...'`로, 반복문 입력은 `< <(printf '%s\\n' "$값")`로 준다.
"""
from pathlib import Path
import re
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
SHELL = re.compile(r"^#!.*\b(ba)?sh\b")
HEREDOC = re.compile(r"<<-?\s*['\"\\]?[A-Za-z_]")


class ShellNoTempFileTest(unittest.TestCase):
    def test_shell_scripts_use_no_heredoc_or_herestring(self):
        found = []
        checked = 0
        for path in sorted(SCRIPTS.iterdir()):
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").split("\n")
            except UnicodeDecodeError:
                continue
            if not SHELL.match(lines[0]):
                continue
            checked += 1
            for number, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                if "<<<" in line or HEREDOC.search(line):
                    found.append(f"{path.name}:{number}: {line.strip()[:80]}")
        self.assertGreater(checked, 3, "셸 스크립트를 찾지 못했다")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
