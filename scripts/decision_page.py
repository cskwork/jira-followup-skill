#!/usr/bin/env python3
"""사람 결정 페이지: 버튼 클릭 = 결정 기록 (+ 승인된 동작 실행).

Usage:
  decision_page.py stg-approval <KEY> [--open] [--url-file F] [--timeout S]
  decision_page.py spec <spec.json>  [--open] [--url-file F] [--timeout S]

결정 종류 (spec.json의 "kind"):
  stg-approval  승인 → jira-followup-approve <KEY> stg "<버튼 클릭 문구>" → 즉시 실행(wake --now --key)
  sql-run       승인 → sql_runner.py로 SQL 파일 실행 (dev/staging 전용, 같은 연결의 @@hostname·guard 확인)
                {"kind","key","title","summary","config":"app_stg","expect_host":"stg-db",
                 "file":"/abs/x.sql","guard_sql":"SELECT COUNT(*) ...","guard_expect":0}
  choice        선택 버튼 → 선택값 기록. 오케스트레이터가 다음 실행에서 decisions.jsonl을 읽는다.
                {"kind","key","title","summary","question","options":[{"id","label","detail"}],
                 "recommended":"<option id>","recommendReason":"<근거 한두 문장>","humanReason":"product-scope"}
                recommended는 필수다. 그 선택지에 "AI 권장" 레이블을 붙인다(사용자 지시 2026-10-30).
  teams-reply   승인 → 승인 줄(draftId·본문 해시)을 먼저 기록 → teams_room.py send로 Teams에 한 번 발송·verify.
                spec은 teams_room.py draft가 만든다. 승인 없이는 발송하지 않는다(채팅 답장은 사람이 승인한 초안만 보낸다).
                CHAT_SEND_CMD가 있으면 teams_room.py send 대신 `<CHAT_SEND_CMD> --dir D --draft <draft>`로 보낸다.
                {"kind","key","title","summary","question","options":[{"id","label","detail"}]}

공통: 127.0.0.1 임의 포트, URL의 1회용 토큰 없으면 403, 한 번 결정하면 409, "보류"는 기록 없음,
결정마다 $JIRA_FOLLOWUP_HOME/decisions.jsonl에 감사 줄을 남기고 jira-daily-wake --now --key <KEY>로 바로 이어간다.
승인 문구는 사람이 버튼을 눌렀다는 사실만 적는다. Jira 댓글·다른 사람의 말은 결정이 아니다.
결정 화면이 html이 아니면(decision_inbox.py) 이 서버 대신 할 일 항목을 열고, 기록·실행은 append_audit·carry_out을 함께 쓴다.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import socketserver
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import followup_config as cfg  # noqa: E402
from state_update import atomic_json, atomic_text, locked, read_object  # noqa: E402

STATE = Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR)
WORKSPACE = cfg.WORKSPACE
TZ = cfg.TZ
STG = cfg.BRANCH_STG
e = html.escape


def process_start_time(pid):
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=10)
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def register_code(directory, spec, name, url, server=True):
    """코드 번호와 레지스트리를 함께 직렬화한다. terminal 기록도 번호를 차지한다.
    server=False는 서버 없는 할 일 결정이다: 프로세스 신원이 없어 수신기가 만료시키지 않는다."""
    pages = Path(directory) / "decision-pages"
    pid = started = None
    if server:
        pid = os.getpid()
        started = process_start_time(pid)
        if not started:
            raise RuntimeError("decision page process start time unavailable")
    with locked(pages):
        codes = read_object(pages / "codes.json")
        with (pages / ".seq").open("a+") as sequence:
            fcntl.flock(sequence, fcntl.LOCK_EX)
            sequence.seek(0)
            previous = sequence.read().strip()
            number = max(int(previous or 0), max((int(c[1:]) for c in codes), default=0)) + 1
            code = f"D{number}"
            sequence.seek(0)
            sequence.truncate()
            sequence.write(str(number) + "\n")
            sequence.flush()
            os.fsync(sequence.fileno())
        spec["code"] = code
        codes[code] = {"name": name, "kind": spec["kind"], "key": spec["key"],
                       "optionIds": [o["id"] for o in spec.get("options") or []], "url": url,
                       "pid": pid, "pidStart": started,
                       "state": "open", "createdAt": datetime.now(TZ).isoformat()}
        atomic_json(pages / "codes.json", codes)
        atomic_text(pages / f"{name}.code", code + "\n")
    return code


def update_code(directory, code, state=None, **fields):
    pages = Path(directory) / "decision-pages"
    with locked(pages):
        codes = read_object(pages / "codes.json")
        row = codes[code]
        if state is not None:
            if row["state"] != "open":
                return
            row.update(state=state, terminalAt=datetime.now(TZ).isoformat())
        row.update(fields)
        atomic_json(pages / "codes.json", codes)


@contextmanager
def audit_lock(log_path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def has_message(log_path, message_id):
    if not log_path.exists():
        return False
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("messageId") == message_id:
                return True
        except (ValueError, AttributeError):
            continue
    return False


def stg_facts(key, directory=STATE):
    """Read-only facts for an stg approval: queue entry, ticket_refs, reviewer verdict lines."""
    queue = {}
    try:
        queue = json.loads((directory / "followup-queue.json").read_text()).get(key) or {}
    except (OSError, ValueError):
        pass
    refs = {}
    try:
        out = subprocess.run([sys.executable, str(HERE / "ticket_refs.py"), key],
                             capture_output=True, text=True, timeout=180).stdout
        refs = json.loads(out or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    review = []
    if queue.get("slug"):
        path = WORKSPACE / ".sdlc/work" / queue["slug"] / "logs/merge-review.md"
        if path.exists():
            review = [line.strip("# ").strip() for line in path.read_text().splitlines()
                      if line.startswith("## ") or "approved" in line or "rejected" in line][:8]
    return {"kind": "stg-approval", "key": key, "title": f"{key} 스테이징 배포를 승인할까요?",
            "summary": queue.get("note") or "큐 기록 없음", "refs": refs, "review": review}


def recommendation(spec):
    """choice의 AI 권장 선택지. 예전 spec이 쓰던 "default"도 받는다."""
    rec = spec.get("recommended") or spec.get("default")
    return next((o for o in spec.get("options") or [] if o["id"] == rec), None)


def body(spec):
    kind = spec["kind"]
    if kind == "stg-approval":
        refs = spec.get("refs") or {}
        rows = "".join(
            f"<tr><td>{e(r['repo'])}</td><td>{e(', '.join(b['branch'] for b in r['branches']))}</td>"
            f"<td>{e(', '.join(k for b in r['branches'] for k in b['other_keys']) or '-')}</td>"
            f"<td>{'O' if r['dev'] else '-'}</td><td>{'O' if r['stg'] else '-'}</td></tr>"
            for r in refs.get("repos", []))
        review = "".join(f"<li>{e(x)}</li>" for x in spec.get("review") or []) or "<li>검수 기록 없음</li>"
        return (f"<p class='warn'><b>승인하면</b> 준비된 병합을 <code>{e(STG)}</code>에 push하고 Jenkins stg 배포를 실행합니다. 운영(prd)은 바뀌지 않습니다.</p>"
                f"<h2>준비 상태</h2><p>{e(spec['summary'])}</p>"
                f"<h2>저장소별 위치 (ticket_refs: {e(refs.get('status', 'unknown'))})</h2>"
                f"<table><tr><th>저장소</th><th>브랜치</th><th>함께 올라가는 키</th><th>dev</th><th>stg</th></tr>{rows}</table>"
                f"<h2>병합 검수</h2><ul>{review}</ul>")
    if kind == "sql-run":
        sql = Path(spec["file"]).read_text() if Path(spec["file"]).exists() else "(파일 없음)"
        guard = f"<p>사전 조건: <code>{e(spec.get('guard_sql') or '없음')}</code> = <b>{e(str(spec.get('guard_expect')))}</b> 일 때만 실행</p>" if spec.get("guard_sql") else ""
        return (f"<p class='warn'><b>승인하면</b> 아래 SQL을 <b>{e(spec['config'])}</b>에 실행합니다. 같은 연결에서 "
                f"<code>@@hostname</code>이 <code>{e(spec['expect_host'])}</code>로 시작하지 않으면 실행하지 않습니다. 운영 DB는 대상이 아닙니다.</p>"
                f"<p>{e(spec.get('summary', ''))}</p>{guard}<h2>실행할 SQL</h2><pre>{e(sql)}</pre>")
    if kind == "choice":
        rec = recommendation(spec)
        reason = f" — {e(spec['recommendReason'])}" if spec.get("recommendReason") else ""
        return (f"<p>{e(spec.get('summary', ''))}</p><h2>{e(spec['question'])}</h2>"
                f"<p class='rec'><span class='badge'>AI 권장</span> <b>{e(rec['label'])}</b>{reason}</p>")
    if kind == "teams-reply":
        mentions = ", ".join(f"{label} → {member}" for label, member in spec.get("mentions") or []) or "없음"
        links = "".join(f"<li>{e(label)} → {e(url)}</li>" for label, url in spec.get("links") or []) or "<li>없음</li>"
        return (f"<p class='warn'><b>승인하면</b> 아래 본문을 내 계정으로 Teams <b>{e(spec.get('chatName', ''))}</b> 방에 한 번 보냅니다. "
                f"보류하면 아무것도 보내지 않습니다.</p><p>{e(spec.get('summary', ''))}</p>"
                f"<h2>{e(spec.get('asker', ''))}님의 메시지</h2><pre>{e(spec.get('question', ''))}</pre>"
                f"<h2>보낼 본문</h2><pre>{e(spec['text'])}</pre><p>멘션: {e(mentions)}</p><h2>링크</h2><ul>{links}</ul>")
    raise ValueError(f"unknown kind {kind}")


def buttons(spec, token):
    hidden = f"<input type='hidden' name='t' value='{e(token)}'>"
    if spec["kind"] == "choice":
        rec = recommendation(spec)
        opts = "".join(
            f"<form method='post' action='/decide'>{hidden}<input type='hidden' name='option' value='{e(o['id'])}'>"
            f"{'<span class=badge>AI 권장</span>' if o is rec else ''}"
            f"<button class='primary' type='submit'>{e(o['label'])}</button><div class='detail'>{e(o.get('detail', ''))}</div></form>"
            for o in spec["options"])
        return opts + f"<form method='post' action='/hold'>{hidden}<button type='submit'>보류</button></form>"
    label = {"stg-approval": "승인 (stg 배포 진행)", "teams-reply": "승인 (Teams 보내기)"}.get(spec["kind"], "승인 (SQL 실행)")
    return (f"<form method='post' action='/decide'>{hidden}<input type='hidden' name='option' value='approve'>"
            f"<button class='primary' type='submit'>{label}</button></form>"
            f"<form method='post' action='/hold'>{hidden}<button type='submit'>보류</button></form>")


def page(spec, token, message=None, detail=None):
    extra = f"<pre>{e(detail)}</pre>" if detail else ""
    actions = f"<p class='done'>{e(message)}</p>{extra}" if message else buttons(spec, token)
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>{e(spec['key'])} 결정</title>
<style>
body{{font-family:'Apple SD Gothic Neo','Noto Sans KR',system-ui,sans-serif;background:#f5f5f5;color:#2d3142;max-width:900px;margin:3rem auto;padding:0 1.5rem;line-height:1.6}}
.eyebrow{{font:500 .7rem ui-monospace,monospace;letter-spacing:.16em;color:#4f5d75;text-transform:uppercase}}
h1{{font-size:1.6rem;font-weight:600;margin:.2rem 0 1rem}} h2{{font-size:1rem;margin:1.6rem 0 .4rem}}
.warn{{border-left:3px solid #bf4520;padding:.4rem .8rem;background:#fff}} .done{{font-size:1.1rem;font-weight:600;color:#1f6f43}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:.88rem}} td,th{{border-bottom:1px solid #e3e3e3;padding:.4rem .6rem;text-align:left}}
pre{{background:#fff;border:1px solid #e3e3e3;padding:.8rem;font-size:.78rem;overflow-x:auto;max-height:420px}}
form{{display:inline-block;margin:1.4rem .6rem 0 0;vertical-align:top;max-width:260px}} .detail{{font-size:.78rem;color:#4f5d75;margin-top:.3rem}}
button{{font-size:1rem;padding:.7rem 1.4rem;border-radius:6px;border:1px solid #2d3142;background:#fff;cursor:pointer}}
button.primary{{background:#bf4520;border-color:#bf4520;color:#fff;font-weight:600}} code{{font-family:ui-monospace,monospace}}
.badge{{display:inline-block;font-size:.72rem;font-weight:700;color:#fff;background:#1f6f43;border-radius:4px;padding:.1rem .45rem;margin-bottom:.3rem}}
.rec{{border-left:3px solid #1f6f43;padding:.4rem .8rem;background:#fff}}
</style></head><body><p class="eyebrow">{e(cfg.LINE_TITLE)} · 사람 결정 · {e(spec['kind'])}</p>
<h1>{e(spec['title'])}</h1>{body(spec)}{actions}</body></html>"""


def default_actions(directory=STATE):
    bin_dir = Path.home() / ".local/bin"

    def approve_stg(spec, stamp):
        via = spec.get("approvedVia") or "승인 버튼 클릭 (결정 페이지"
        p = subprocess.run([str(bin_dir / "jira-followup-approve"), spec["key"], "stg",
                            f"{via}, {stamp})"], capture_output=True, text=True)
        return p.returncode == 0, (p.stdout + p.stderr).strip()

    def run_sql(spec, stamp):
        out = directory / "decisions" / f"{spec['key']}-{stamp.replace(' ', 'T').replace(':', '')}-sql.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(HERE / "sql_runner.py"), "--config", spec["config"],
               "--expect-host", spec["expect_host"], "--file", spec["file"], "--out", str(out)]
        if spec.get("guard_sql"):
            cmd += ["--guard-sql", spec["guard_sql"], "--guard-expect", str(spec.get("guard_expect"))]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        return p.returncode == 0, p.stdout.strip()[-4000:]

    def send_teams(spec, stamp):
        send = shlex.split(cfg.get("CHAT_SEND_CMD")) or [sys.executable, str(HERE / "teams_room.py"), "send"]
        p = subprocess.run([*send, "--dir", str(directory), "--draft", spec["draft"]],
                           capture_output=True, text=True, timeout=300)
        return p.returncode == 0, p.stdout.strip()[-2000:]

    def wake(key):
        subprocess.Popen(["/bin/bash", str(bin_dir / "jira-daily-wake"), "--now", "--key", key],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    return {"stg-approval": approve_stg, "sql-run": run_sql, "teams-reply": send_teams}, wake


def append_audit(log_path, spec, option, ok, note, by="button", message_id=None):
    """decisions.jsonl 감사 한 줄. 호출자가 audit_lock을 잡는다."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now(TZ).isoformat(), "key": spec["key"], "kind": spec["kind"],
           "title": spec["title"], "option": option, "ok": ok, "by": by, "note": note[:300]}
    if message_id:
        row["messageId"] = message_id
    if spec["kind"] == "choice":
        row["recommended"] = recommendation(spec)["id"]
    if spec["kind"] == "teams-reply":
        row.update(draftId=spec.get("draftId"), textSha256=spec.get("textSha256"))
    with log_path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def carry_out(spec, option, stamp, by, actions, wake, audit):
    """기록된 결정을 이어서 실행한다. 반환: (결과 문구, 상세). audit(option, ok, note)는 잠금을 스스로 잡는다."""
    if spec["kind"] == "choice":
        wake(spec["key"])
        label = next(o["label"] for o in spec["options"] if o["id"] == option)
        return f"'{label}'(으)로 결정을 기록했습니다 ({stamp}). 라인이 이어서 진행합니다.", None
    if spec["kind"] == "teams-reply":
        # 승인 줄이 먼저다: teams_room.py send는 이 줄(draftId·본문 해시)이 없으면 보내지 않는다.
        audit("approve", True, "승인 버튼 클릭" if by == "button" else "할 일 앱 답글 승인")
        ok, note = actions["teams-reply"](spec, stamp)
        audit("send", ok, note)
        return (f"Teams로 보냈고 서버 재조회까지 확인했습니다 ({stamp})." if ok else
                "발송이 거부되었거나 결과가 불확실합니다. 다시 보내지 말고 Teams 방을 먼저 확인하세요."), note
    ok, note = actions[spec["kind"]](spec, stamp)
    audit("approve", ok, note)
    if ok:
        wake(spec["key"])
        return f"승인·실행 완료 ({stamp}). 라인이 이어서 진행합니다.", note
    return "실행이 거부되었거나 실패했습니다. 아래 결과를 확인하세요. 아무것도 이어서 진행하지 않았습니다.", note


def make_server(spec, *, actions=None, wake=None, log_path=None, port=0, token=None, directory=None, name=None):
    token = token or secrets.token_urlsafe(18)
    directory = Path(directory) if directory else (Path(log_path).parent if log_path else STATE)
    if actions is None or wake is None:
        default, default_wake = default_actions(directory)
        actions = default if actions is None else actions
        wake = default_wake if wake is None else wake
    log_path = Path(log_path) if log_path else directory / "decisions.jsonl"
    state = {"used": False, "result": None, "detail": None}
    lock = threading.Lock()
    code = None

    def audit(option, ok, note, *, already_locked=False):
        if already_locked:
            append_audit(log_path, spec, option, ok, note)
        else:
            with audit_lock(log_path):
                append_audit(log_path, spec, option, ok, note)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # keep tokens out of logs
            del format, args

        def send(self, code, text):
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not secrets.compare_digest(parse_qs(urlparse(self.path).query).get("t", [""])[0], token):
                return self.send(403, "forbidden")
            self.send(200, page(spec, token, state["result"], state["detail"]))

        def do_POST(self):
            self.accepted = False
            try:
                self.decide()
            except Exception as error:
                if not self.accepted:
                    raise
                state["result"] = "처리 결과가 불명확합니다. 결정 기록과 실행 결과를 PC에서 확인하세요."
                state["detail"] = type(error).__name__
                self.send(500, page(spec, token, state["result"], state["detail"]))
            finally:
                # 감사 후 클라이언트가 연결을 끊거나 실행이 실패해도 서버를 남기지 않는다.
                if self.accepted:
                    server.cancel_timer()
                    threading.Thread(target=server.shutdown, daemon=True).start()

        def decide(self):
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            if not secrets.compare_digest(form.get("t", [""])[0], token):
                return self.send(403, "forbidden")
            if self.path not in ("/decide", "/hold"):
                return self.send(404, "not found")
            with lock:
                stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
                option = form.get("option", [""])[0]
                with audit_lock(log_path):
                    if state["used"]:
                        return self.send(409, page(spec, token, state["result"] or "이미 처리됨"))
                    if self.path == "/decide" and spec["kind"] == "choice":
                        labels = {o["id"]: o["label"] for o in spec["options"]}
                        if option not in labels:
                            return self.send(400, "unknown option")
                        audit(option, True, labels[option], already_locked=True)
                    state["used"] = True
                    self.accepted = True
                server.cancel_timer()
                if self.path == "/decide":
                    update_code(directory, code, "decided", decidedBy="button")
                if self.path == "/hold":
                    state["result"] = "보류했습니다. 아무것도 기록하지 않았습니다."
                    update_code(directory, code, "expired")
                else:
                    state["result"], state["detail"] = carry_out(spec, option, stamp, "button", actions, wake, audit)
            self.send(200, page(spec, token, state["result"], state["detail"]))

    class DecisionServer(ThreadingHTTPServer):
        timeout_timer = None

        def server_bind(self):
            """HTTPServer.server_bind는 socket.getfqdn(host)로 역방향 DNS를 조회한다. 127.0.0.1만 듣는 이 페이지에는
            필요 없고, 느린 DNS에서는 시작을 막는다(2026-11-10 GitHub macOS 러너: getfqdn('127.0.0.1') 35초 →
            URL 파일 5초 안에 못 씀). server_name은 CGI 환경 변수에만 쓰인다."""
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

        def cancel_timer(self):
            if self.timeout_timer is not None:
                self.timeout_timer.cancel()

        def serve_forever(self, poll_interval=0.1):
            try:
                super().serve_forever(poll_interval)
            finally:
                self.cancel_timer()
                update_code(directory, code, "expired")

        def server_close(self):
            self.cancel_timer()
            try:
                if code is not None:
                    update_code(directory, code, "expired")
            finally:
                super().server_close()

    server = DecisionServer(("127.0.0.1", port), Handler)
    name = name or f"{spec['key']}-{spec['kind']}"
    if Path(name).name != name:
        server.socket.close()
        raise ValueError("decision name must be a filename")
    try:
        code = register_code(directory, spec, name, f"http://127.0.0.1:{server.server_address[1]}/?t={token}")
    except Exception:
        server.socket.close()
        raise
    return server, token, state


def load_spec(mode, target, directory):
    """결정 spec과 이름. 결정 페이지와 할 일 결정이 같은 검사를 거친다."""
    spec = stg_facts(target.upper(), directory) if mode == "stg-approval" else json.loads(Path(target).read_text())
    if spec.get("kind") == "sql-run" and not str(spec.get("config", "")).endswith(
            (cfg.DB_CONFIG_SUFFIX["dev"], cfg.DB_CONFIG_SUFFIX["stg"])):
        raise ValueError("sql-run config must be a dev/staging config")
    if spec.get("kind") == "choice":
        reasons = {"product-scope", "security-plan", "prod-data", "policy", "conflict"}
        reason = spec.get("humanReason")
        if not isinstance(reason, str) or reason not in reasons:
            rules = {"jira-status": "Jira 상태 이동", "evidence-comment": "증빙 댓글 보충",
                     "stg-prepare": "stg 병합 준비", "baseline-fail": "기준선 시험 실패",
                     "result-sql": "결과 댓글 참고용 SQL 첨부"}
            rule = rules.get(reason, "사람에게 묻지 않는 절차") if isinstance(reason, str) else "사람에게 묻지 않는 절차"
            raise ValueError(f"choice humanReason={reason!r}은 허용되지 않습니다. "
                             f"RUNBOOK §0-3 '사람에게 묻지 않는 절차'의 '{rule}' 자동 규칙을 따르세요. "
                             f"사람 결정은 {', '.join(sorted(reasons))}만 허용합니다.")
        if not recommendation(spec):
            raise ValueError("choice spec needs recommended=<one of the option ids>")
    return spec, f"{target.upper()}-stg" if mode == "stg-approval" else Path(target).stem


def main(argv=None):
    parser = argparse.ArgumentParser(description="사람 결정 페이지")
    parser.add_argument("mode", choices=["stg-approval", "spec"])
    parser.add_argument("target", help="KEY (stg-approval) 또는 spec.json 경로")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--timeout", type=int, default=28800)
    parser.add_argument("--url-file")
    parser.add_argument("--dir", type=Path,
                        default=Path(os.environ.get("JIRA_WAKE_DIR", str(STATE))))
    args = parser.parse_args(argv)
    directory = args.dir.expanduser()
    try:
        spec, name = load_spec(args.mode, args.target, directory)
    except ValueError as error:
        parser.error(str(error))
    name = Path(args.url_file).stem if args.url_file else name
    server, token, state = make_server(spec, directory=directory, name=name)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    timer = server.timeout_timer = threading.Timer(args.timeout, server.shutdown)
    timer.daemon = True
    old_term = signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        if args.url_file:
            atomic_text(Path(args.url_file), url + "\n")
        if args.open:
            # 채팅 알림은 하지 않는다(사용자 지시 2026-11-03: 결정 페이지는 브라우저로만 연다).
            subprocess.run(shlex.split(cfg.get("BROWSER_OPEN_CMD", "open")) + [url], check=False)
        timer.start()
        server.serve_forever()
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
        server.server_close()
        signal.signal(signal.SIGTERM, old_term)
    print(f"decision {spec['key']} {spec['kind']}: {state['result'] or 'timeout'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
