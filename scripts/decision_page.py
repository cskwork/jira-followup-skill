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
  teams-reply   승인 → 승인 줄(draftId·본문 해시)을 먼저 기록 → CHAT_SEND_CMD --draft <draft>로 채팅에 한 번 발송·verify.
                spec은 채팅 감시 도구가 초안과 함께 만든다. 승인 없이는 발송하지 않는다(채팅 답장은 사람이 승인한 초안만 보낸다).
                CHAT_SEND_CMD가 없으면 발송하지 않고 실패로 기록한다.
                {"kind","key","title","summary","question","options":[{"id","label","detail"}]}

공통: 127.0.0.1 임의 포트, URL의 1회용 토큰 없으면 403, 한 번 결정하면 409, "보류"는 기록 없음,
결정마다 $JIRA_FOLLOWUP_HOME/decisions.jsonl에 감사 줄을 남기고 jira-daily-wake --now --key <KEY>로 바로 이어간다.
승인 문구는 사람이 버튼을 눌렀다는 사실만 적는다. Jira 댓글·다른 사람의 말은 결정이 아니다.
"""

import argparse
from datetime import datetime
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import os
import secrets
import shlex
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import followup_config as cfg  # noqa: E402

STATE = Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR)
WORKSPACE = cfg.WORKSPACE
TZ = cfg.TZ
STG = cfg.BRANCH_STG
e = html.escape


def stg_facts(key):
    """Read-only facts for an stg approval: queue entry, ticket_refs, reviewer verdict lines."""
    queue = {}
    try:
        queue = json.loads((STATE / "followup-queue.json").read_text()).get(key) or {}
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
        return f"<p>{e(spec.get('summary', ''))}</p><h2>{e(spec['question'])}</h2>"
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
        opts = "".join(
            f"<form method='post' action='/decide'>{hidden}<input type='hidden' name='option' value='{e(o['id'])}'>"
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
</style></head><body><p class="eyebrow">{e(cfg.LINE_TITLE)} · 사람 결정 · {e(spec['kind'])}</p>
<h1>{e(spec['title'])}</h1>{body(spec)}{actions}</body></html>"""


def default_actions():
    bin_dir = Path.home() / ".local/bin"

    def approve_stg(spec, stamp):
        p = subprocess.run([str(bin_dir / "jira-followup-approve"), spec["key"], "stg",
                            f"승인 버튼 클릭 (결정 페이지, {stamp})"], capture_output=True, text=True)
        return p.returncode == 0, (p.stdout + p.stderr).strip()

    def run_sql(spec, stamp):
        out = STATE / "decisions" / f"{spec['key']}-{stamp.replace(' ', 'T').replace(':', '')}-sql.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(HERE / "sql_runner.py"), "--config", spec["config"],
               "--expect-host", spec["expect_host"], "--file", spec["file"], "--out", str(out)]
        if spec.get("guard_sql"):
            cmd += ["--guard-sql", spec["guard_sql"], "--guard-expect", str(spec.get("guard_expect"))]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        return p.returncode == 0, p.stdout.strip()[-4000:]

    def send_teams(spec, stamp):
        send = shlex.split(cfg.get("CHAT_SEND_CMD"))
        if not send:
            return False, "CHAT_SEND_CMD not configured; nothing sent"
        p = subprocess.run([*send, "--draft", spec["draft"]],
                           capture_output=True, text=True, timeout=300)
        return p.returncode == 0, p.stdout.strip()[-2000:]

    def wake(key):
        subprocess.Popen(["/bin/bash", str(bin_dir / "jira-daily-wake"), "--now", "--key", key],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    return {"stg-approval": approve_stg, "sql-run": run_sql, "teams-reply": send_teams}, wake


def make_server(spec, *, actions=None, wake=None, log_path=None, port=0, token=None):
    token = token or secrets.token_urlsafe(18)
    if actions is None or wake is None:
        default, default_wake = default_actions()
        actions, wake = actions or default, wake or default_wake
    log_path = Path(log_path) if log_path else STATE / "decisions.jsonl"
    state = {"used": False, "result": None, "detail": None}
    lock = threading.Lock()

    def audit(option, ok, note):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(TZ).isoformat(), "key": spec["key"], "kind": spec["kind"],
               "title": spec["title"], "option": option, "ok": ok, "by": "button", "note": note[:300]}
        if spec["kind"] == "teams-reply":
            row.update(draftId=spec.get("draftId"), textSha256=spec.get("textSha256"))
        with log_path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

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
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            if not secrets.compare_digest(form.get("t", [""])[0], token):
                return self.send(403, "forbidden")
            if self.path not in ("/decide", "/hold"):
                return self.send(404, "not found")
            with lock:
                if state["used"]:
                    return self.send(409, page(spec, token, state["result"] or "이미 처리됨"))
                state["used"] = True
                stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
                option = form.get("option", [""])[0]
                if self.path == "/hold":
                    state["result"] = "보류했습니다. 아무것도 기록하지 않았습니다."
                elif spec["kind"] == "choice":
                    labels = {o["id"]: o["label"] for o in spec["options"]}
                    if option not in labels:
                        state["used"] = False
                        return self.send(400, "unknown option")
                    audit(option, True, labels[option])
                    wake(spec["key"])
                    state["result"] = f"'{labels[option]}'(으)로 결정을 기록했습니다 ({stamp}). 라인이 이어서 진행합니다."
                elif spec["kind"] == "teams-reply":
                    # 승인 줄이 먼저다: 발송 도구(CHAT_SEND_CMD)는 이 줄(draftId·본문 해시)이 없으면 보내지 않아야 한다.
                    audit("approve", True, "승인 버튼 클릭")
                    ok, note = actions["teams-reply"](spec, stamp)
                    audit("send", ok, note)
                    state["result"] = (f"Teams로 보냈고 서버 재조회까지 확인했습니다 ({stamp})." if ok else
                                       "발송이 거부되었거나 결과가 불확실합니다. 다시 보내지 말고 Teams 방을 먼저 확인하세요.")
                    state["detail"] = note
                else:
                    ok, note = actions[spec["kind"]](spec, stamp)
                    audit("approve", ok, note)
                    if ok:
                        wake(spec["key"])
                        state["result"] = f"승인·실행 완료 ({stamp}). 라인이 이어서 진행합니다."
                    else:
                        state["result"] = "실행이 거부되었거나 실패했습니다. 아래 결과를 확인하세요. 아무것도 이어서 진행하지 않았습니다."
                    state["detail"] = note
            self.send(200, page(spec, token, state["result"], state["detail"]))
            threading.Thread(target=server.shutdown, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server, token, state


def main(argv=None):
    parser = argparse.ArgumentParser(description="사람 결정 페이지")
    parser.add_argument("mode", choices=["stg-approval", "spec"])
    parser.add_argument("target", help="KEY (stg-approval) 또는 spec.json 경로")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--timeout", type=int, default=28800)
    parser.add_argument("--url-file")
    args = parser.parse_args(argv)
    spec = stg_facts(args.target.upper()) if args.mode == "stg-approval" else json.loads(Path(args.target).read_text())
    if spec.get("kind") == "sql-run" and not str(spec.get("config", "")).endswith(
            (cfg.DB_CONFIG_SUFFIX["dev"], cfg.DB_CONFIG_SUFFIX["stg"])):
        parser.error("sql-run config must be a dev/staging config")
    server, token, state = make_server(spec)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    if args.url_file:
        Path(args.url_file).write_text(url + "\n")
    if args.open:
        subprocess.run(shlex.split(cfg.get("BROWSER_OPEN_CMD", "open")) + [url], check=False)
    threading.Timer(args.timeout, server.shutdown).start()
    server.serve_forever()
    print(f"decision {spec['key']} {spec['kind']}: {state['result'] or 'timeout'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
