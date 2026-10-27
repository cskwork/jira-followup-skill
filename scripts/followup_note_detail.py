#!/usr/bin/env python3
"""이번 follow-up 실행의 일지 줄을 개발자 노트 보드용 사람이 읽는 목록으로 바꾼다 (읽기 전용, §7-7).

일지 `followup-YYYY-MM-DD.md` 한 줄 = `시각 | 티켓 | 댓글 id | 분류 | 명확도 | 조치 | slug·stage | 증적`.
--since(epoch, 기본 잠금 파일 `running`의 값) 이후 줄만 쓴다. 완료 줄·idle 줄은 마지막 줄과 겹쳐 뺀다.
변화 없는 '기존 큐' 줄은 한 줄로 모은다. stdout을 실행 보고 도구(RUN_REPORT_CMD --detail-file -)로 넘긴다.
"""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402

HOME = Path.home()
DIR = Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR)
KST = datetime.timezone(datetime.timedelta(hours=9))
MAX_LINES = 20
MAX_CHARS = 400


def parse_at(text, day):
    text = text.strip()
    try:
        at = datetime.datetime.fromisoformat(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text))
        return at if at.tzinfo else at.replace(tzinfo=KST)
    except ValueError:
        pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.datetime.strptime(text, fmt).time()
            return datetime.datetime.combine(day, t, KST)
        except ValueError:
            pass
    return None


def blank(v):
    return v in ("", "-", "—", "none")


def short(path):
    return path.replace(str(HOME), "~")


class Verbatim(str):
    """A journal line whose time is unknown; preserve it for human review."""


def render(rows):
    out, kept = [], []
    for row in rows:
        if isinstance(row, str):
            out.append(row)  # Unknown timestamps must remain reviewable, verbatim.
            continue
        at, key, cid, kind, clarity, action, stage, evidence = row
        if kind in ("완료", "스캔") or action.startswith("idle-skip"):
            continue
        hhmm = at.strftime("%H:%M")
        if kind == "기존 큐":
            kept.append("%s(%s)" % (key, action.replace("waiting:", "").replace(" 유지", "")))
            continue
        head = "%s **%s**" % (hhmm, key) if not blank(key) else hhmm
        if not blank(cid):
            head += " 댓글 %s" % cid
        what = kind if blank(clarity) else "%s, 명확도 %s" % (kind, clarity)
        line = "%s · %s → %s" % (head, what, action)
        if not blank(stage):
            line += " [%s]" % stage
        if not blank(evidence):
            line += " — 증적 `%s`" % short(evidence)
        out.append(line)
    if kept:
        out.append("그대로 대기: " + ", ".join(kept))
    return [l if isinstance(l, Verbatim) or len(l) <= MAX_CHARS else l[:MAX_CHARS - 1] + "…"
            for index, l in enumerate(out) if isinstance(l, Verbatim) or index < MAX_LINES]


def read_rows(journal, since, day):
    rows = []
    for raw in journal.read_text(errors="replace").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 8:
            continue
        at = parse_at(parts[0], day)
        if at is None:
            if parts[0] and not parts[0].startswith(("#", "시각", "시간", "---")) and set(parts[0]) != {"-"}:
                rows.append(Verbatim(raw))
            continue
        if at.timestamp() < since:
            continue
        rows.append([at] + parts[1:7] + [" | ".join(parts[7:])])
    return rows


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.add_argument("--journal")
    p.add_argument("--since", type=float, help="epoch; 기본은 잠금 파일 running의 값")
    a = p.parse_args(argv)
    day = datetime.date.fromisoformat(a.date)
    journal = Path(a.journal) if a.journal else DIR / ("followup-%s.md" % a.date)
    since = a.since
    if since is None:
        try:
            since = float((DIR / "running").read_text().strip())
        except (OSError, ValueError):
            since = 0.0
    if not journal.exists():
        return
    for line in render(read_rows(journal, since, day)):
        print(line)


if __name__ == "__main__":
    main()
