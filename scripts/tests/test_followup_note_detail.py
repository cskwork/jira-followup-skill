"""Offline tests for followup_note_detail.py (journal → dev-note detail lines)."""

import datetime
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import followup_note_detail as fkd  # noqa: E402

JOURNAL = """\
2026-10-21T09:56:34+09:00 | - | - | 완료 | - | followup events=1 posted=0 | none | followup-scan.json
10:14:13 | PROJ-1389 | 49708 → 49710 | 확인·조치 요청 | 92% | posted 49710 / waiting:reporter | proj-1389-sweep·stg-qa | .sdlc/logs/a.json; drafts/b.md
2026-10-21T10:15:23+0900 | PROJ-1389 | - | 큐 상태 | - | env-human 해제 → waiting:reporter | proj-1389-sweep·stg-qa | followup-queue.json
2026-10-21T10:15:23+0900 | PROJ-1334 | - | 기존 큐 | - | waiting:human-risk 유지 | none·intent | followup-queue.json
2026-10-21T10:15:23+0900 | PROJ-1353 | - | 기존 큐 | - | waiting:reporter 유지 | x·intent | followup-queue.json
2026-10-21T10:15:23+0900 | - | - | 완료 | - | followup events=0 posted=1 | none | followup-scan.json
2026-10-21T11:33:36+0900 | — | — | 스캔 | — | idle-skip(guard): no-events | none | followup-scan.json
"""


class DetailChecks(unittest.TestCase):
    def run_detail(self, since):
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / "followup-2026-10-21.md"
            journal.write_text(JOURNAL)
            buf = io.StringIO()
            with redirect_stdout(buf):
                fkd.main(["--date", "2026-10-21", "--journal", str(journal), "--since", str(since)])
            return buf.getvalue().splitlines()

    def epoch(self, hhmm):
        h, m = map(int, hhmm.split(":"))
        return datetime.datetime(2026, 10, 21, h, m, tzinfo=fkd.KST).timestamp()

    def test_only_this_run_with_event_queue_and_kept_summary(self):
        lines = self.run_detail(self.epoch("10:00"))
        self.assertEqual(lines[0], "10:14 **PROJ-1389** 댓글 49708 → 49710 · 확인·조치 요청, 명확도 92% → "
                                   "posted 49710 / waiting:reporter [proj-1389-sweep·stg-qa] — 증적 `.sdlc/logs/a.json; drafts/b.md`")
        self.assertEqual(lines[1], "10:15 **PROJ-1389** · 큐 상태 → env-human 해제 → waiting:reporter "
                                   "[proj-1389-sweep·stg-qa] — 증적 `followup-queue.json`")
        self.assertEqual(lines[2], "그대로 대기: PROJ-1334(human-risk), PROJ-1353(reporter)")
        self.assertEqual(len(lines), 3)  # 완료·idle 줄은 마지막 줄과 겹쳐 빠진다

    def test_rows_before_run_start_are_excluded(self):
        self.assertEqual(self.run_detail(self.epoch("10:15") + 60), [])

    def test_real_timestamp_forms(self):
        day = datetime.date(2026, 10, 22)
        for text, hhmm in (("2026-10-22T08:03+0900", "08:03"), ("19:23", "19:23"),
                           ("2026-10-20T17:09:21.544232+09:00", "17:09"),
                           ("2026-10-22T08:03:21", "08:03")):
            with self.subTest(text=text):
                at = fkd.parse_at(text, day)
                self.assertIsNotNone(at)
                self.assertEqual(at.strftime("%H:%M"), hhmm)
                self.assertEqual(at.utcoffset(), datetime.timedelta(hours=9))

    def test_unknown_time_survives_detail_line_limit(self):
        rows = [fkd.Verbatim(f"unknown-{n} | PROJ-1 | — | スキャン | — | raw | none | proof")
                for n in range(fkd.MAX_LINES + 1)]
        self.assertEqual(fkd.render(rows), rows)

    def test_short_time_and_unknown_time_are_kept(self):
        unknown = "  unknown-at | PROJ-1405 | — | 큐 상태 | — | " + "원문" * 250 + " | none | proof  "
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / "followup.md"
            journal.write_text("시각 | 티켓 | 댓글 | 분류 | 명확도 | 조치 | stage | 증적\n"
                               "2026-10-22T08:03+0900 | PROJ-1 | — | 큐 상태 | — | older | none | a\n"
                               "19:23 | PROJ-1405 | — | 큐 상태 | — | resumed | none | b\n" + unknown + "\n")
            since = datetime.datetime(2026, 10, 22, 19, 0, tzinfo=fkd.KST).timestamp()
            lines = fkd.render(fkd.read_rows(journal, since, datetime.date(2026, 10, 22)))
            self.assertEqual(len(lines), 2)
            self.assertIn("19:23 **PROJ-1405**", lines[0])
            self.assertEqual(lines[1], unknown)


if __name__ == "__main__":
    unittest.main()
