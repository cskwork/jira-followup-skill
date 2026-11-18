#!/usr/bin/env python3
"""사람 결정·경보를 sdlc-kit 할 일(앱·TUI·웹의 '확인 필요')에 올리고, 내 답글을 결정으로 기록한다.

사용자 지시 2026-11-03: 결정 화면은 설정(app|tui|web|html) 하나만 쓴다. html이 아니면 브라우저 페이지와
Teams 알림 없이 할 일 항목만 만든다. stg 승인·SQL 실행·Teams 회신 발송도 앱 답글 '승인'으로 받는다(사용자 선택).

설정: JIRA_DECISION_SURFACE 또는 <state>/decision-surface 첫 줄. 없으면 html.

Usage:
  decision_inbox.py surface [--dir D]
  decision_inbox.py open stg-approval <KEY> | open spec <spec.json> [--dir D]   # decision-request가 부른다
  decision_inbox.py alert --text TEXT [--key KEY [--summary S]] [--dir D]   # 경보 / 티켓 큐 대기

답글 형식: 선택 결정은 번호(`1`, `1번`, `D12 1`)·선택지 id·문구, 실행 승인은 `승인`(또는 `1`),
`보류`는 아무것도 기록하지 않고 닫는다. 그 밖의 답은 같은 항목에 다시 묻는다.
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import decision_page as dp
import followup_config as cfg
import followup_scan
from state_update import atomic_json, locked, read_object

SURFACES = ("app", "tui", "web", "html")
SDLC_KIT = cfg.SDLC_KIT
ALERT_TITLE = "[공장 알림] " + cfg.LINE_TITLE
# 판정 에이전트 켜기·끄기(사용자 지시 2026-11-09 "할일 앱에서 judge on/off, 기본은 on"). 버튼 답글을 poll이 모드 파일에 쓴다.
JUDGE_TITLE = "[공장 설정] 판정 에이전트(judge)"
JUDGE_CHOICES = "켜기 | 끄기"
JUDGE_WORDS = {"on": {"켜기", "켜", "켜줘", "켜기로", "on", "켬"}, "off": {"끄기", "꺼", "꺼줘", "끄기로", "off", "끔"},
               "shadow": {"shadow", "기록만", "섀도"}}
JUDGE_SHOWN = {"on": "켜짐 — 위험이 낮은 결정은 judge가 정합니다", "off": "꺼짐 — 모든 결정이 사람에게 옵니다",
               "shadow": "기록만 — judge 판정은 기록만 하고 사람이 정합니다(스테이징 승인 규칙은 적용)"}
YES = {"네", "예", "응", "ㅇㅇ", "좋아", "좋아요", "그래", "승인", "ok", "yes", "y", "권장", "권장대로", "추천대로"}
APPROVE = YES | {"approve", "1", "1번"}
# 선택 결정에 번호 대신 쓴 문장은 사람 지시다. 다시 묻지 않고 그 말 그대로 공장에 넘긴다(2026-11-03 D35:
# "시험 실패 이슈도 해결"·"셤 실패 해결 후 넘어가기"를 번호 아님으로 네 번 되물음).
FREE_TEXT = "free-text"


def free_text(words):
    return not re.fullmatch(r"\d+\s*번?", words) and (" " in words or len(words) >= 6)
HOLD = {"보류", "hold"}
BY = "todo-app"


def surface(directory=None):
    value = os.environ.get("JIRA_DECISION_SURFACE")
    if value is None:
        path = Path(directory or dp.STATE) / "decision-surface"
        value = path.read_text(encoding="utf-8").split("\n", 1)[0] if path.exists() else "html"
    value = value.strip().lower() or "html"
    if value not in SURFACES:
        raise ValueError(f"decision-surface must be one of {'|'.join(SURFACES)}: {value}")
    return value


def todo_cli(args, timeout=60):
    p = subprocess.run(["node", str(SDLC_KIT / "todo/todo.mjs"), *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300] or f"exit {p.returncode}")
    out = p.stdout.strip()
    return (json.loads(out) if out else None) if "--json" in args else out


def line(text, limit=900):
    return " ".join(str(text or "").split())[:limit]


SHOWN = 160  # 할 일 앱 상세 화면이 질문 줄을 자르는 길이(sdlc-kit todo/app `clip(&ask, 160)`). 나머지는 마우스를 올려야 보인다.


def prompt(spec, reask=""):
    """'에이전트가 묻고 있어요'의 마지막 줄: 고를 것과 답하는 법(앱이 160자에서 자른다). 질문과 AI 권장·이유는
    결정 요약(brief)의 Your call·What changes가 같은 칸 위에 보여 준다(사용자 지시 2026-11-03 "총 3개만")."""
    if spec["kind"] == "choice":
        rec = dp.recommendation(spec)
        def options(short):
            return " · ".join(f"{n}) {o['label'].split(' — ')[0] if short else o['label']}" + (" (AI 권장)" if o is rec else "")
                              for n, o in enumerate(spec["options"], 1))
        head = f"{reask}{options(False)} — 번호로 답('네'는 AI 권장)"
        if len(head) > SHOWN:
            head = f"{reask}{options(True)} — 번호로 답('네'는 AI 권장)"
    else:
        action = {"stg-approval": f"승인하면 {cfg.BRANCH_STG} push·stg 배포",
                  "sql-run": f"승인하면 {spec.get('config')}에 SQL 실행",
                  "teams-reply": f"승인하면 Teams {spec.get('chatName', '')} 방에 발송"}[spec["kind"]]
        head = f"{reask}{action} — '승인' 또는 '보류'로 답"
    return line(head)


def words(text, limit=300):
    """앱 요약 한 줄: 코드 내용을 줄인 쉬운 말(사용자 지시 2026-11-03 "코드 내용은 최소화").
    인라인 코드는 백틱만 벗기고, 괄호 속 근거·파일:줄·URL은 뺀다."""
    text = re.sub(r"`([^`]*)`", r"\1", line(text, 2000))
    text = re.sub(r"\s*\([^()]*\)", "", text)
    text = re.sub(r"\s*\S+\.(?:java|vue|ts|tsx|js|mjs|py|yml|yaml|json|md|xml|sql)(?::[\d,\-]+)?(?=\s|$)", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return line(text, limit)


def plain(reason):
    """권장 이유의 첫 문장에서 괄호 속 근거(파일:줄·증적 경로·코드명)를 뺀다. 근거는 결정 기록·노트에 그대로 남는다."""
    first = re.split(r"(?<=[.다])\s", line(reason), maxsplit=1)[0]
    return line(re.sub(r"\s*\([^()]*\)", "", first))


def kit_brief(title, glance, tail):
    """sdlc-kit `todo/brief.md`(앱이 읽는 하나뿐인 형식)를 채운다. 라벨과 순서는 템플릿이 정하고,
    여기서는 라벨별 값만 준다. 값이 없는 줄은 뺀다. At a glance 뒤는 공장 상세(tail)로 바꾼다."""
    template = SDLC_KIT / "todo/brief.md"
    if not template.exists():
        raise RuntimeError(f"sdlc-kit brief template missing: {template} (sdlc-kit v2.19.0 이상 필요)")
    lines = template.read_text(encoding="utf-8").splitlines()
    start = lines.index("## At a glance")
    end = next((n for n in range(start + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    rows = [f"# {title}", "", "## At a glance"]
    for text in lines[start + 1:end]:
        label = re.match(r"- \*\*([^*]+):\*\* ", text)
        if label and glance.get(label[1]):
            rows.append(f"- **{label[1]}:** {glance[label[1]]}")
    unknown = set(glance) - {m[1] for m in (re.match(r"- \*\*([^*]+):\*\* ", t) for t in lines[start + 1:end]) if m}
    if unknown:
        raise RuntimeError(f"brief labels not in sdlc-kit template: {sorted(unknown)}")
    return rows + tail


def heading(spec, code):
    """제목 = 결정할 것. 키가 앞에 이미 붙은 spec 제목은 중복을 뺀다."""
    title = spec["title"].removeprefix(spec["key"]).strip(" ·-—")
    return line(f"[결정 {code}] {spec['key']} · {title}", 200)


def ticket_glance(key, directory):
    """그 티켓 sdlc 작업 문서(intent.md)의 At a glance 줄. 큐의 slug로 찾고, 없으면 빈 dict."""
    try:
        slug = json.loads((Path(directory) / "followup-queue.json").read_text(encoding="utf-8")).get(key, {}).get("slug")
        text = (dp.WORKSPACE / ".sdlc/work" / slug / "intent.md").read_text(encoding="utf-8") if slug else ""
    except (OSError, ValueError, AttributeError):
        return {}
    sections = {m[1].strip(): m[2] for m in re.finditer(r"^## (.+)\n((?:(?!^## ).*\n?)*)", text, re.M)}
    glance = {m[1]: m[2].strip() for m in re.finditer(r"^- \*\*([^*]+):\*\* (.+)$", sections.get("At a glance", ""), re.M)}

    def first(*prefixes):
        """prefix 순서대로(앞이 우선) 제목이 그것으로 시작하는 절의 첫 항목(또는 첫 줄)."""
        for _, body in ((t, b) for prefix in prefixes for t, b in sections.items() if t.startswith(prefix)):
            rows = [re.sub(r"^\s*[-*]\s+|^\s*\d+\.\s+", "", r).strip() for r in body.splitlines()]
            found = next((r for r in rows if r and not r.startswith(("|", "<!--", "```"))), "")
            if found:
                return found
        return ""
    # 옛 intent.md는 At a glance에 배경·이유가 없다. 앱의 세 칸이 비지 않게 본문에서 채운다(사용자 지시 2026-11-03).
    glance.setdefault("Background", first("Understanding", "AS-IS 관찰", "Local AS-IS", "What was asked", "AS-IS", "Facts")) or glance.pop("Background")
    glance.setdefault("Why", first("Why this came up", "Why", "What was asked")) or glance.pop("Why")
    glance.setdefault("TO-BE", glance.get("What changes") or first("TO-BE", "What we will do", "Changes")) or glance.pop("TO-BE")
    return glance


def ticket_phase(key, directory, *, todo=todo_cli):
    """그 티켓 sdlc 작업의 현재 단계(intent·plan·build·evidence·deliver). 앱이 카드 위 단계 표시줄로 그린다."""
    try:
        slug = json.loads((Path(directory) / "followup-queue.json").read_text(encoding="utf-8")).get(key, {}).get("slug")
        return next((r.get("phase") or "" for r in todo(["list", "--json"]) or [] if slug and r.get("slug") == slug), "")
    except Exception:  # 단계 표시는 부가 정보다. 못 읽어도 요약은 만든다.
        return ""


def brief(spec, code, directory):
    """할 일 앱이 '정해 주실 것·한 줄로 보면·지금 상황·왜 필요한가요'와 '자세히 보기'로 그리는 결정 요약 문서
    (sdlc-kit item brief, At a glance 라벨). 사용자 지시 2026-11-03 "배경·의사결정 사유·의도를 알 수 있게"."""
    work = ticket_glance(spec["key"], directory)
    rec = dp.recommendation(spec) if spec["kind"] == "choice" else None
    action = {"stg-approval": f"승인하면 준비된 병합을 {cfg.BRANCH_STG}에 push하고 stg 배포를 실행합니다. 운영은 바뀌지 않습니다.",
              "sql-run": f"승인하면 SQL 파일을 {spec.get('config')}에 실행합니다(같은 연결 @@hostname이 {spec.get('expect_host')}로 시작할 때만). 운영 DB는 대상이 아닙니다.",
              "teams-reply": f"승인하면 아래 본문을 내 계정으로 Teams {spec.get('chatName', '')} 방에 한 번 보냅니다."}.get(spec["kind"])
    background = spec.get("background") or work.get("Background") or spec.get("summary") or ""
    purpose = work.get("Why") or spec.get("recommendReason") or ""
    # 앱 순서(사용자 지시 2026-11-03): 지금 상황(AS-IS) → 에이전트가 묻고 있어요(질문·하려는 것 TO-BE·AI 권장) → 왜 필요한가요.
    tobe = spec.get("intent") or work.get("TO-BE") or (rec.get("detail") if rec else action) or ""
    pick = f"AI 권장: {rec['label']} — {plain(spec.get('recommendReason')) or rec.get('detail', '')}" if rec else ""
    rows = ["", "## 무엇을 정하나요", spec.get("question") or spec["title"]]
    if spec.get("summary"):
        rows += ["", "## 지금까지", spec["summary"]]
    if work:
        names = {"Your call": "사람 확인", "What changes": "바뀌는 것", "Background": "배경", "Why": "이유", "TO-BE": "하려는 것"}
        rows += ["", "## 이 티켓에서 하려는 일 (sdlc intent)"] + [f"- {names.get(k, k)}: {v}" for k, v in work.items()]
    if spec["kind"] == "choice":
        rows += ["", "## 선택지 (번호로 답)"]
        rows += [f"{n}. **{o['label']}**{' (AI 권장)' if o is rec else ''} — {o.get('detail', '')}" for n, o in enumerate(spec["options"], 1)]
        rows += ["", "## AI 권장 이유", spec.get("recommendReason") or "(없음)"]
    else:
        rows += ["", "## 승인하면", action, "", "'승인' 또는 '보류'로 답합니다."]
        if spec["kind"] == "stg-approval":
            refs = spec.get("refs") or {}
            rows += ["", "## 저장소별 위치"] + [f"- {r['repo']}: {', '.join(b['branch'] for b in r['branches'])} (dev {'O' if r['dev'] else '-'}, stg {'O' if r['stg'] else '-'})" for r in refs.get("repos", [])]
            rows += ["", "## 병합 검수"] + [f"- {x}" for x in spec.get("review") or ["검수 기록 없음"]]
        elif spec["kind"] == "sql-run":
            sql = Path(spec["file"]).read_text(encoding="utf-8") if Path(spec["file"]).exists() else "(파일 없음)"
            if spec.get("guard_sql"):
                rows += ["", f"사전 조건: `{spec['guard_sql']}` = {spec.get('guard_expect')} 일 때만 실행"]
            rows += ["", "## 실행할 SQL", "```sql", sql, "```"]
        elif spec["kind"] == "teams-reply":
            rows += ["", f"## {spec.get('asker', '')}님의 메시지", spec.get("question", ""), "", "## 보낼 본문", spec["text"]]
    rows += ["", "## 답하는 법", "이 항목에 답글: 번호 또는 '승인', 기록 없이 닫으려면 '보류'.",
             "", f"티켓: {cfg.BROWSE}{spec['key']}"]
    rows = kit_brief(heading(spec, code), {
        "Your call": words(spec.get("question") or spec["title"]),
        "What changes": f"하려는 것(TO-BE): {words(tobe or spec['title'])}",
        "AI pick": words(pick) if pick else "",
        "Phase": ticket_phase(spec["key"], directory),
        "Background": f"지금(AS-IS): {words(background or spec['title'])}",
        "Why": words(purpose or spec["title"])}, rows)
    path = Path(directory) / "decision-pages" / f"{code}.brief.md"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


WAITS = {"human-risk": "공장이 혼자 진행하면 위험해 멈췄습니다. 아래 상황을 보고 어떻게 할지 답글로 알려 주세요.",
         "env-human": "VPN·DB·서버 같은 환경 조치가 있어야 이어갈 수 있습니다. 할 일에 적힌 조치를 하면 공장이 확인 뒤 스스로 이어갑니다(답글 필요 없음).",
         "stg-approval": "스테이징(stg) 배포 승인을 기다립니다. 결정 항목에서 '승인' 또는 '보류'로 답해 주세요.",
         "reporter": "요청자(기획·QA)의 회신을 기다립니다. 내가 할 일은 없습니다."}


def ticket_brief(key, directory, title="", decision=None):
    """공장 대기 티켓 항목의 결정 요약(sdlc-kit item brief). 큐 기록과 그 티켓 intent.md에서 만든다.
    사용자 지시 2026-11-03 "다른 티켓도 이렇게"."""
    queue = json.loads((Path(directory) / "followup-queue.json").read_text(encoding="utf-8")).get(key) or {}
    work = ticket_glance(key, directory)
    notes = [n.strip() for n in str(queue.get("note") or "").split(" | ") if n.strip()]
    waiting = queue.get("waiting") or ""
    # human-risk는 작업자가 쓴 질문(사용자가 정할 것)과 권장 답을 보인다. 없으면(옛 항목) 대기 종류 안내.
    ask = queue if waiting == "human-risk" else {}
    question, suggest = line(ask.get("question")), line(ask.get("suggest"))
    call = question or WAITS.get(waiting, f"공장 대기({waiting}): 답글로 다음 진행을 알려 주세요.")
    # 같은 키의 결정 항목이 열려 있으면 결정은 그 항목에서만 기록된다. 권장 답도 그 항목에 있다.
    decision = decision if waiting not in followup_scan.NOT_DECIDED_HERE else None
    if decision:
        call, suggest = f"{followup_scan.decision_where(decision)} · {call}", ""
    rows = []
    if work:
        names = {"Your call": "사람 확인", "What changes": "바뀌는 것", "Background": "배경", "Why": "이유", "TO-BE": "하려는 것"}
        rows += ["", "## 이 티켓에서 하려는 일 (sdlc intent)"] + [f"- {names.get(k, k)}: {v}" for k, v in work.items()]
    rows += ["", "## 공장 상태", f"- 단계: {queue.get('stage') or '-'}", f"- 대기: {waiting or '-'}"]
    if queue.get("slug"):
        rows.append(f"- sdlc 작업: {queue['slug']}")
    rows += ["", "## 최근 기록 (새 것부터)"] + [f"- {n}" for n in reversed(notes[-5:])]
    rows += ["", "## 답하는 법", f"'[{decision}]' 항목에서 선택지를 골라 답해 주세요. 이 항목 답글은 결정으로 기록되지 않습니다."
             if decision else "이 항목에 답글을 달면 공장이 다음 실행에서 읽고 이어갑니다.",
             "", f"티켓: {jira_link(key)}"]
    rows = kit_brief(f"{key} {title}".rstrip(), {
        "Your call": call,
        "What changes": f"하려는 것(TO-BE): {words(work.get('TO-BE') or f'{key} {title}')}",
        "AI pick": f"AI 권장: {suggest}" if suggest else "",
        "Phase": ticket_phase(key, directory),
        "Background": f"지금(AS-IS): {words(work.get('Background') or (notes[0] if notes else '') or f'{key} {title}')}",
        "Why": words(work.get('Why') if work.get('Why') and work.get('Why') != work.get('TO-BE') else f'요청: {key} {title}'.strip())}, rows)
    path = Path(directory) / "decision-pages" / f"ticket-{key}.brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def jira_link(key):
    """할 일 --link는 URL만 받는다(라벨은 키로 자동). '[KEY](url)'을 주면 링크가 빠진다."""
    return f"{cfg.BROWSE}{key}"


def short_label(option):
    return option["label"].split(" — ", 1)[0].replace("|", "/")


def suggested(spec):
    """결정 항목의 권장 답 한 줄: 선택 결정은 AI 권장 선택지의 짧은 이름, 실행 승인은 '승인'."""
    return short_label(dp.recommendation(spec)) if spec["kind"] == "choice" else "승인"


def offer_suggest(item, text, *, todo):
    """할 일 앱 답 칸에 권장 답을 미리 채운다(sdlc-kit v2.21.0 `suggest`). 옛 kit이면 무시한다."""
    try:
        todo(["suggest", item, text])
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"decision-inbox: suggest ignored: {error}", file=sys.stderr)


def offer_run(item, command, *, todo):
    """할 일 앱 '터미널에서 열기' 버튼의 명령(sdlc-kit v2.22.0 `run`). 옛 kit이면 무시한다."""
    try:
        todo(["run", item, command])
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"decision-inbox: run ignored: {error}", file=sys.stderr)


def offer_choices(item, spec, *, todo):
    """구형 kit에는 choices가 없다. 답글 대기는 그대로 유지한다."""
    if spec["kind"] == "choice":
        options = spec["options"][:6]
        recommended = dp.recommendation(spec)
        if recommended not in options:
            options[-1] = recommended
        labels = [short_label(o) for o in options]
        pick = str(next(n for n, o in enumerate(options, 1) if o is recommended))
    else:
        labels, pick = ["승인", "보류"], "1"
    args = ["choices", item, " | ".join(labels)]
    if pick:
        args += ["--pick", pick]
    try:
        todo(args)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"decision-inbox: choices ignored: {error}", file=sys.stderr)


def batch_entries(directory, item):
    return [(code, row) for code, row in read_object(Path(directory) / "decision-pages/codes.json").items()
            if isinstance(row, dict) and row.get("todoId") == item and row.get("state") == "open"]


def batch_prompt(entries, reask=""):
    keys = "·".join(dict.fromkeys(row["key"] for _, row in entries))
    return (f"{reask}승인하면 {keys} {cfg.BRANCH_STG} push·stg 배포 — "
            "'승인'(전부)·'승인 1429,1431'(일부)·'보류 1431'")


def wait_batch(item, entries, *, todo, reask=""):
    todo(["wait", item, batch_prompt(entries, reask)])
    offer_choices(item, {"kind": "stg-approval"}, todo=todo)
    offer_suggest(item, "승인", todo=todo)


def open_item(spec, code, directory, *, todo=todo_cli):
    """결정 항목을 만들고 '확인 필요'로 둔다. 항목 id를 코드 레지스트리에 남긴다."""
    if spec["kind"] == "stg-approval":
        # 여러 티켓 실행기가 동시에 요청해도 같은 묶음에 붙인다. codes 잠금과는 별개다.
        with locked(Path(directory) / "decision-pages/todo-batches"):
            registry = read_object(Path(directory) / "decision-pages/codes.json")
            candidates = {row["todoId"] for row in registry.values()
                          if isinstance(row, dict) and row.get("batch") is True
                          and row.get("kind") == "stg-approval" and row.get("state") == "open" and row.get("todoId")}
            rows = todo(["list", "--json"]) or []
            existing = next((r for r in rows if r.get("id") in candidates and r.get("status") != "done"), None)
            item = existing["id"] if existing else todo([
                "add", f"[스테이징 승인] {spec['key']}", "--project", str(dp.WORKSPACE), "--link", jira_link(spec["key"]),
                "--by", "factory", "--brief", str(brief(spec, code, directory))])
            # 붙이기 전에 달린 답은 새 티켓의 승인이 아니다.
            replies = [r for r in (existing or {}).get("replies") or [] if r.get("who") == "human"]
            fields = {"lastReplyId": reply_id(item, replies[-1])} if replies else {}
            dp.update_code(directory, code, todoId=item, batch=True, **fields)
            wait_batch(item, batch_entries(directory, item), todo=todo)
            return item
    title = heading(spec, code)
    item = todo(["add", title, "--project", str(dp.WORKSPACE), "--link", jira_link(spec["key"]), "--by", "factory",
                 "--brief", str(brief(spec, code, directory))])
    todo(["wait", item, prompt(spec)])
    offer_choices(item, spec, todo=todo)
    offer_suggest(item, suggested(spec), todo=todo)
    dp.update_code(directory, code, todoId=item)
    return item


def register(mode, target, directory, *, todo=todo_cli):
    """decision-request가 부른다: 서버 없이 코드를 잡고 spec을 남긴 뒤 할 일 항목을 연다."""
    directory = Path(directory)
    spec, name = dp.load_spec(mode, target, directory)
    code = dp.register_code(directory, spec, name, None, server=False)
    atomic_json(directory / "decision-pages" / f"{name}.spec.json", spec)
    try:
        return code, open_item(spec, code, directory, todo=todo)
    except Exception:
        dp.update_code(directory, code, "expired", reason="todo-open-failed")
        raise


def option_for(spec, text):
    """답글 → 선택지 id, None(보류), 또는 ValueError."""
    words = line(text).strip(" .")
    words = re.sub(r"^[Dd]\d+(?:\s*[.:-]\s*|\s+)", "", words)
    if words.lower() in HOLD:
        return None
    if spec["kind"] != "choice":
        if words.lower() in APPROVE:
            return "approve"
        raise ValueError("'승인' 또는 '보류'로 답해 주세요")
    options = spec["options"]
    if words.lower() in YES:  # '네'·'승인'처럼 동의만 한 답은 AI 권장 선택지다(2026-11-03 D16 '네'를 다시 물은 일).
        return dp.recommendation(spec)["id"]
    number = re.fullmatch(r"(\d+)\s*번?", words)
    if number and 1 <= int(number[1]) <= len(options):
        return options[int(number[1]) - 1]["id"]
    for o in options:
        if words in (o["id"], o["label"], short_label(o)):
            return o["id"]
    if free_text(words):
        return FREE_TEXT
    raise ValueError(f"1~{len(options)} 번호로 답해 주세요")


def decide(directory, code, option, message_id, *, actions=None, wake=None, words=""):
    """앱 답글 하나를 결정으로 기록하고 실행한다. 반환: (결과 문구, 상태)."""
    directory = Path(directory)
    pages = directory / "decision-pages"
    entry = read_object(pages / "codes.json")[code]
    spec = json.loads((pages / f"{entry['name']}.spec.json").read_text(encoding="utf-8"))
    log_path = directory / "decisions.jsonl"
    if actions is None or wake is None:
        default, default_wake = dp.default_actions(directory)
        actions, wake = actions or default, wake or default_wake
    stamp = datetime.now(dp.TZ).strftime("%Y-%m-%d %H:%M")
    with dp.audit_lock(log_path):
        if dp.has_message(log_path, message_id):
            return "이미 기록한 답글입니다.", "duplicate"
        with locked(pages):
            if read_object(pages / "codes.json")[code].get("state") != "open":
                return "이미 결정되었거나 닫힌 항목입니다.", "closed"
        if option is None:
            dp.update_code(directory, code, "expired", reason="hold", decidedBy=BY)
            return "보류했습니다. 아무것도 기록하지 않았습니다.", "held"
        if spec["kind"] == "choice":
            label = words if option == FREE_TEXT else next(o["label"] for o in spec["options"] if o["id"] == option)
            dp.append_audit(log_path, spec, option, True, label, BY, message_id)
        dp.update_code(directory, code, "decided", decidedBy=BY, messageId=message_id)

    def audit(option, ok, note):
        with dp.audit_lock(log_path):
            dp.append_audit(log_path, spec, option, ok, note, BY, message_id)
    if option == FREE_TEXT:
        wake(spec["key"])
        return f"사람 지시로 기록했습니다: \"{line(words, 80)}\" ({stamp}). 라인이 이 말대로 이어서 진행합니다.", "decided"
    result, _ = dp.carry_out(dict(spec, approvedVia="답글 '승인' (할 일 앱"), option, stamp, BY,
                             actions, wake, audit)
    return result, "decided"


def reply_id(item, reply):
    digest = hashlib.sha1(f"{reply.get('at')}\0{reply.get('what')}".encode()).hexdigest()[:10]
    return f"todo:{item}:{digest}"


def batch_options(entries, text):
    """묶음 답글은 전부 또는 명시한 열린 티켓만 선택한다. 모호하면 실행하지 않는다."""
    text = line(text).strip(" .")
    command = re.fullmatch(r"(승인|보류)\s+(.+)", text)
    if not command:
        if len(entries) > 1 and text not in {"승인", "보류"}:
            raise ValueError("'승인'·'보류' 또는 '승인 1429,1431'·'보류 1431'로 답해 주세요")
        option = option_for({"kind": "stg-approval"}, text)
        return [(code, option) for code, _ in entries]
    selected = set()
    tokens = re.split(r"[,\s·]+", command[2])
    if any(not token for token in tokens):
        raise ValueError("티켓 번호 또는 키를 쉼표로 구분해 주세요")
    for token in tokens:
        matches = [(code, row) for code, row in entries
                   if token.upper() == row["key"] or token == row["key"].rsplit("-", 1)[-1]]
        if not matches or len({row["key"] for _, row in matches}) > 1:
            raise ValueError(f"열린 스테이징 승인 티켓을 지정해 주세요: {token}")
        selected.update(code for code, _ in matches)
    return [(code, "approve" if command[1] == "승인" else None) for code, _ in entries if code in selected]


def poll_batch(directory, item, *, todo, decider):
    # open_item과 직렬화해 답글 처리 중 추가된 티켓이 이전 '전부 승인'에 섞이지 않게 한다.
    with locked(Path(directory) / "decision-pages/todo-batches"):
        entries = batch_entries(directory, item["id"])
        if not entries:
            todo(["done", item["id"], "결정 기록이 이미 닫혔습니다"])
            return []
        replies = [r for r in item.get("replies") or [] if r.get("who") == "human"]
        if not replies:
            return []
        reply = replies[-1]
        message = reply_id(item["id"], reply)
        fresh = [(code, row) for code, row in entries if row.get("lastReplyId") != message]
        if not fresh:
            return []
        text = re.sub(r"^reply — ", "", reply.get("what") or "")
        try:
            selected = batch_options(fresh, text)
        except ValueError as error:
            for code, _ in fresh:
                dp.update_code(directory, code, lastReplyId=message)
            wait_batch(item["id"], entries, todo=todo, reask=f"'{line(text, 20)}' 대신 {error} · ")
            return [(code, "invalid") for code, _ in fresh]
        handled = []
        for code, option in selected:
            # 한 답글이 여러 감사 줄을 남긴다. 코드별 id로 중복 방지를 유지한다.
            _, status = decider(directory, code, option, f"{message}:{code}")
            handled.append((code, status))
        for code, _ in fresh:
            dp.update_code(directory, code, lastReplyId=message)
        remaining = batch_entries(directory, item["id"])
        if remaining:
            wait_batch(item["id"], remaining, todo=todo)
        else:
            todo(["done", item["id"], "스테이징 승인 결정을 모두 처리했습니다"])
        return handled


def judge_title(current):
    """앱 카드는 접혀 있어도 제목은 보인다. 켜짐·꺼짐을 제목에 둔다(2026-11-09 사용자: '앱에서 안 보이는데')."""
    return f"{JUDGE_TITLE}: {JUDGE_SHOWN[current].split(' — ')[0]}"


def todo_item(args, timeout=60):
    """todo.mjs CLI에 없는 조작(retitle·top). sdlc-kit core의 잠금된 update를 쓴다."""
    p = subprocess.run(["node", str(Path(__file__).with_name("todo_item.mjs")), *args], capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "SDLC_KIT": str(SDLC_KIT)})
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300] or f"exit {p.returncode}")


def pin(item, todo):
    """앱 기본 탭(확인 필요)을 포함한 모든 탭 맨 위에 둔다(sdlc-kit v2.23.0 `pin`, 2026-11-09 사용자: '내가 킨 앱에서는 안보임').
    구형 kit는 pin 명령이 없으므로 실패해도 항목은 진행 중 탭에 남는다."""
    try:
        todo(["pin", item, "on"])
    except RuntimeError:
        pass


def judge_others(current):
    """설정 항목은 '진행 중' 탭에 둔다. 작업 항목(workable)이면 다른 세션의 `todo next`가 일감으로 가져갈 수 있고,
    '확인 필요'에 두면 사람 확인 수가 늘 1 이상이 된다(2026-11-09)."""
    return f"설정 · 판정 에이전트 {JUDGE_SHOWN[current].split(' — ')[0]} · 버튼으로 바꿈"


def judge_setting(directory, rows, *, todo=todo_cli, item_cli=todo_item):
    """할 일 앱의 judge 켜기·끄기 항목. 없으면 만들고, 새 사람 답글이면 모드 파일(<state>/judge)에 쓴다.

    항목은 대기(확인 필요)로 두지 않는다. 처리한 답글 id는 judge-state.json settingReply로 한 번만 처리한다."""
    import judge
    if surface(directory) == "html":
        return None
    directory = Path(directory)
    current = judge.mode(directory)
    row = next((r for r in rows if str(r.get("title") or "").startswith(JUDGE_TITLE) and r.get("status") != "done"), None)
    if row is None:
        item = todo(["add", judge_title(current), "--by", "factory"])
        todo(["choices", item, JUDGE_CHOICES, "--pick", "1" if current != "off" else "2"])
        todo(["note", item, f"지금: {JUDGE_SHOWN[current]}. 제목을 눌러 펼치면 켜기·끄기 버튼이 있습니다."])
        todo(["others", item, judge_others(current)])
        pin(item, todo)
        return current
    replies = [r for r in row.get("replies") or [] if r.get("who") == "human"]
    # 제목이 바뀌면 항목 id도 바뀌므로(sdlc-kit id=제목 기반) 답글은 시각·내용으로만 구분한다.
    message = reply_id("judge-setting", replies[-1]) if replies else None
    if not row.get("pin"):
        pin(row["id"], todo)
    if message is None or read_object(directory / "judge-state.json").get("settingReply") == message:
        if row.get("others") != judge_others(current):
            todo(["others", row["id"], judge_others(current)])
        if row.get("title") != judge_title(current):
            item_cli(["retitle", row["id"], judge_title(current)])
        return current
    reply = replies[-1]
    text = line(re.sub(r"^reply — ", "", reply.get("what") or ""), 40)
    wanted = next((m for m, words in JUDGE_WORDS.items() if text.lower().rstrip(".!") in words), None)
    if wanted is None:
        todo(["note", row["id"], f"'{text}'은(는) 알 수 없어요. '켜기' 또는 '끄기'로 답해 주세요. 지금: {JUDGE_SHOWN[current]}"])
    else:
        judge.set_mode(directory, wanted)
        todo(["choices", row["id"], JUDGE_CHOICES, "--pick", "1" if wanted != "off" else "2"])
        todo(["note", row["id"], f"{datetime.now(dp.TZ).strftime('%m-%d %H:%M')} 바꿈. 지금: {JUDGE_SHOWN[wanted]}"])
        current = wanted
    if row.get("others") != judge_others(current):
        todo(["others", row["id"], judge_others(current)])
    judge.patch(directory, "judge-state.json", [], {"settingReply": message})
    try:
        todo(["take", row["id"], "--json"])  # 답글을 읽음으로 표시한다. 대기 상태 반환(10·15)은 실패가 아니다.
    except RuntimeError:
        pass
    if row.get("title") != judge_title(current):
        item_cli(["retitle", row["id"], judge_title(current)])  # id가 바뀌므로 마지막에 한다.
    return current


def poll(directory, *, todo=todo_cli, decider=decide, item_cli=todo_item):
    """결정 항목의 새 답글을 처리한다. decision_replies.watch가 60초마다 부른다.

    답글은 마지막 대기 뒤의 사람 답(list --json의 replies)이다. 결정 → done 순서이고 같은 답글은
    messageId로 한 번만 기록되므로, 중간에 죽어도 다음 회차가 이어서 닫는다.
    끝에 반복 실패 결정 중 fix-now로 정한 것을 '[공장 수정]' 할 일로 만든다(lessons.track_fixes, 실패하면 다음 회차가 다시 한다)."""
    handled = _poll(directory, todo=todo, decider=decider, item_cli=item_cli)
    if surface(directory) != "html":
        import lessons
        lessons.track_fixes(directory, todo=todo)
    return handled


def _poll(directory, *, todo, decider, item_cli):
    directory = Path(directory)
    pages = directory / "decision-pages"
    registry = read_object(pages / "codes.json")
    items = {}
    for code, row in registry.items():
        if isinstance(row, dict) and row.get("todoId"):
            items.setdefault(row["todoId"], []).append((code, row))
    rows = None
    if surface(directory) != "html":
        rows = todo(["list", "--json"]) or []
        try:
            judge_setting(directory, rows, todo=todo, item_cli=item_cli)
        except (OSError, RuntimeError, ValueError) as error:  # 설정 항목 실패가 결정 수신을 막지 않는다.
            print(f"judge setting failed: {type(error).__name__}: {error}", file=sys.stderr)
    if not items:
        return []
    handled = []
    for item in rows if rows is not None else todo(["list", "--json"]) or []:
        entries = items.get(item.get("id"), [])
        if not entries or item.get("status") == "done":
            continue
        if any(row.get("batch") is True for _, row in entries):
            handled.extend(poll_batch(directory, item, todo=todo, decider=decider))
            continue
        code, entry = entries[0]
        if entry.get("state") != "open":
            todo(["done", item["id"], "결정 기록이 이미 닫혔습니다"])
            handled.append((code, "closed"))
            continue
        replies = [r for r in item.get("replies") or [] if r.get("who") == "human"]
        if not replies:
            continue
        reply = replies[-1]
        text = re.sub(r"^reply — ", "", reply.get("what") or "")
        spec = json.loads((pages / f"{entry['name']}.spec.json").read_text(encoding="utf-8"))
        try:
            option = option_for(spec, text)
        except ValueError as error:
            todo(["wait", item["id"], prompt(spec, reask=f"'{line(text, 20)}' 대신 {error} · ")])
            # 답글이 권장 답을 지웠다. 다시 묻는 김에 다시 채운다.
            offer_suggest(item["id"], suggested(spec), todo=todo)
            handled.append((code, "invalid"))
            continue
        extra = {"words": text} if option == FREE_TEXT else {}
        result, status = decider(directory, code, option, reply_id(item["id"], reply), **extra)
        todo(["done", item["id"], line(result, 300)])
        handled.append((code, status))
    return handled


def alert(text, directory=None, *, key=None, summary=None, todo=todo_cli):
    """사람 확인 필요를 할 일 '확인 필요'로 올린다. html이면 아무것도 하지 않는다(macOS 알림·로그는 호출자가 남긴다).

    key 없음: 공장 경보. '[공장 알림]' 항목 하나에 최신 경보를 둔다.
    key 있음: 큐 대기. 그 티켓의 할 일 항목(없으면 새로)에 '공장 대기 — <text>'를 둔다. 이후 갱신·해제와
    내 답글 전달은 followup_scan의 to-do 동기화가 맡는다."""
    if surface(directory) == "html":
        return False
    rows = [r for r in todo(["list", "--json"]) or [] if r.get("status") != "done"]
    if key is None:
        item = next((r["id"] for r in rows if r.get("title") == ALERT_TITLE), None)
        item = item or todo(["add", ALERT_TITLE, "--by", "factory"])
        todo(["wait", item, line(text)])
        offer_suggest(item, followup_scan.ENV_DONE, todo=todo)
        if followup_scan.terminal_command(text):
            offer_run(item, followup_scan.terminal_command(text), todo=todo)
        return True
    row = next((r for r in rows if followup_scan.todo_key(r) == key and not followup_scan.DECISION_ITEM.match(r.get("title") or "")), None)
    item = row["id"] if row else todo(["add", line(f"{key} {summary or ''}", 200), "--project", str(dp.WORKSPACE),
                                       "--link", jira_link(key), "--by", "factory"])
    # human-risk에 질문이 있으면 to-do 동기화(followup_scan.todo_actions)와 같은 문구·권장 답을 바로 보인다.
    queued = read_object(Path(directory or dp.STATE) / "followup-queue.json").get(key)
    waiting = queued.get("waiting") if isinstance(queued, dict) else None
    queued = queued if waiting == "human-risk" else {}
    suggest = line(queued.get("suggest"))
    asked = line(queued.get("question")) or text
    if followup_scan.human_action(queued):  # 사람만 할 수 있는 조치: 질문·권장 답 대신 할 일만 보인다.
        asked, suggest = followup_scan.human_action(queued), ""
    decision = followup_scan.open_decisions(rows).get(key) if waiting not in followup_scan.NOT_DECIDED_HERE else None
    if decision:
        asked, suggest = f"{followup_scan.decision_where(decision)} · {asked}", ""
    todo(["wait", item, line(followup_scan.WAIT_PREFIX + asked)])
    if suggest:
        offer_suggest(item, suggest, todo=todo)
    attach_ticket_brief(item, key, directory, (row or {}).get("title") or summary or "", todo=todo, decision=decision)
    return True


def attach_ticket_brief(item, key, directory=None, title="", *, todo=todo_cli, decision=None):
    """티켓 대기 항목에 최신 요약을 붙인다. 공장 대기를 새로 쓸 때마다(alert, followup_scan) 다시 만든다."""
    title = re.sub(rf"^{re.escape(key)}\s*", "", title)
    todo(["brief", item, str(ticket_brief(key, directory or dp.STATE, title, decision))])


def alert_detached(text):
    """호출자(가드·실행 잠금)를 붙잡지 않도록 alert를 분리된 프로세스로 띄운다. 결과는 기다리지 않는다."""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "alert", "--text", text],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["surface", "open", "alert"])
    parser.add_argument("mode", nargs="?", choices=["stg-approval", "spec"])
    parser.add_argument("target", nargs="?")
    parser.add_argument("--text")
    parser.add_argument("--key")
    parser.add_argument("--summary")
    parser.add_argument("--dir", type=Path, default=Path(os.environ.get("JIRA_WAKE_DIR", str(dp.STATE))))
    args = parser.parse_args(argv)
    if args.command == "surface":
        print(surface(args.dir))
    elif args.command == "open":
        if not args.target:
            parser.error("open needs stg-approval <KEY> or spec <spec.json>")
        code, item = register(args.mode, args.target, args.dir)
        print(f"{code} {item}")
    else:
        if not args.text:
            parser.error("alert needs --text")
        alert(args.text, args.dir, key=args.key, summary=args.summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
