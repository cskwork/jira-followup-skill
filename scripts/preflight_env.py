#!/usr/bin/env python3
"""Check evidence prerequisites without changing VPN, auth, or Jenkins state.

Usage: preflight_env.py --need dev-db,stg-db,dev-ai,stg-ai,dev-web,stg-web,jira,jenkins-dev
                        [--timeout 20] [--fail-fast]
Exit codes: 0 all PASS, 10 HUMAN (VPN/OTP/auth/setup), 20 DOWN (service),
2 invalid usage. Output is one JSON object; --help prints CLI help instead.

Locally verified CLI help: `acli jira auth status` is read-only. `jk` has a
global --context flag; `jk --context dev job ls --json` lists jobs remotely
without changing the saved context. No context-use or production calls occur.
Tests inject command(name, argv, timeout) and http(url, timeout) into
check_environment or main; no subprocess or HTTP request is needed in tests.
--timeout bounds each external operation, not the entire collection. jk is the
exception: it prints its JSON and then idles ~60 s before exiting (measured
against a dev controller), so the jenkins-dev probe gets at least JK_TIMEOUT.

Settings (followup_config.py / config.env): DB_QUERY_CMD, DB_PRIMARY, DB_CONFIG_SUFFIX_*,
DB_HOST_PREFIX_*, AI_HEALTH_URL_DEV/STG, WEB_HEALTH_URL_DEV/STG, VPN_STATUS_CMD
(`none` when dev/staging need no VPN), JK_CONTEXT_DEV, JIRA_REAUTH_HINT.
"""

import argparse
from http.client import HTTPException
import json
import math
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import followup_config as cfg

NEEDS = ("dev-db", "stg-db", "dev-ai", "stg-ai", "dev-web", "stg-web", "jira", "jenkins-dev")
PROFILES = ("dev", "stg")
JK_TIMEOUT = 120


def next_step_item(item):
    """큐 쓰기는 호출자가 한다. 과거 needs나 note에서 다음 동작을 추측하지 않는다."""
    if not isinstance(item, dict):
        raise ValueError("invalid queue item")
    result = dict(item)
    established = item.get("causeEstablished") is True or item.get("stage") in (
        "plan", "build", "tobe", "evidence", "dev-deploy", "dev-e2e", "dev-smoke", "dev-deployed",
    )
    step = item.get("nextStep")
    if step is None and item.get("stage") in ("plan", "build", "tobe", "evidence"):
        step = "local"
    if step is None:
        return result
    if not isinstance(step, str):
        raise ValueError("invalid nextStep")
    needs = {"local": "", "reproduce-web": "dev-web", "dev-deploy": "jenkins-dev",
             "dev-e2e": "dev-web", "jira": "jira"}
    if step == "reproduce-db":
        profile = item.get("profile")
        if profile not in PROFILES:
            raise ValueError("DB reproduction requires dev/stg profile")
        value = profile + "-db"
    elif step in needs:
        if step == "local" and not established:
            raise ValueError("local arbitrary-data path requires established cause")
        value = needs[step]
        if step == "reproduce-web":
            profile = item.get("profile", "dev")
            if profile not in PROFILES:
                raise ValueError("invalid reproduction profile")
            value = profile + "-web"
    else:
        raise ValueError("invalid nextStep")
    # Additional *current-step* requirements (e.g. AI) must be explicit.
    extra = item.get("stepNeeds", "")
    if not isinstance(extra, str) or any(n not in NEEDS for n in extra.split(",") if n):
        raise ValueError("invalid stepNeeds")
    result.update(nextStep=step, needs=",".join(dict.fromkeys(n for n in (value + "," + extra).split(",") if n)))
    if not result["needs"] and result.get("waiting") in ("env-human", "env-down"):
        result["waiting"] = None
    return result

# AI 엔진 등 VPN 안쪽 의존 서비스의 health URL(선택).
AI_URLS = {
    "dev": cfg.get("AI_HEALTH_URL_DEV", "https://ai.dev.example.com/docs"),
    "stg": cfg.get("AI_HEALTH_URL_STG", "https://ai.stg.example.com/docs"),
}
# 공개 도메인의 웹/API health(VPN 무관). 스테이징 웹이 본문 0B 503을 낼 때 재현이 막혔는데
# 점검 항목이 없으면 가드가 자동 재개하지 못한다.
WEB_URLS = {
    "dev": cfg.get("WEB_HEALTH_URL_DEV", "https://dev.example.com/actuator/health"),
    "stg": cfg.get("WEB_HEALTH_URL_STG", "https://stg.example.com/actuator/health"),
}
VPN_STATUS_CMD = cfg.get("VPN_STATUS_CMD", "vpn --status")
JK_CONTEXT_DEV = cfg.get("JK_CONTEXT_DEV", "dev")


def run_command(name, argv, timeout):
    """Capture external output for classification; never echo it into logs."""
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def http_status(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def vpn_action(profile):
    # Path.resolve follows symlinks like readlink -f, without depending on GNU tools.
    skill_dir = (Path.home() / ".local/bin/vpn").resolve().parent
    command_file = skill_dir / (profile + ".command")
    return "Switch VPN to %s; type OTP in Terminal: %s" % (
        profile, shlex.join(["open", "-a", "Terminal", str(command_file)])
    )


def check_environment(needs, timeout=20, *, command=run_command, http=http_status, fail_fast=False):
    results = []

    def record(name, ok, observed, reason, action=None):
        results.append({"name": name, "status": "PASS" if ok else "FAIL",
                        "observed": observed, "human_action": action, "reason": reason})

    def execute(name, argv):
        try:
            response = command(name, argv, max(timeout, JK_TIMEOUT) if name == "jenkins-dev" else timeout)
            if response.returncode:
                if name == "jenkins-dev":
                    details = (response.stdout + (response.stderr or "")).lower()
                    if any(marker in details for marker in (
                        "401", "403", "unauthorized", "forbidden", "authentication",
                        "credentials", "no active context", "context not found", "unknown context",
                    )) or re.search(r"context.*(?:not found|does not exist)|no .*context", details):
                        return None, "auth-or-context-required"
                return None, "command-failed"
            return response.stdout, None
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except OSError:
            return None, "command-unavailable"

    def parse_object(raw):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None

    if VPN_STATUS_CMD == "none":
        raw, error = json.dumps({"active": []}), None
    else:
        raw, error = execute("vpn", shlex.split(VPN_STATUS_CMD))
    state = parse_object(raw)
    active = state.get("active") if state else None
    valid = isinstance(active, list) and all(
        isinstance(p, dict) and isinstance(p.get("profile"), str)
        and isinstance(p.get("state"), str) for p in active
    )
    connected = {p["profile"] for p in active if p["state"] == "CONNECTED"} if valid else set()
    known = connected <= set(PROFILES)
    vpn = next(iter(connected)) if known and len(connected) == 1 else "none"
    vpn_ok = not error and valid and known and len(connected) <= 1
    record("vpn", vpn_ok, vpn,
           "ok" if vpn_ok else error or ("vpn-conflict" if len(connected) > 1 else "invalid-vpn-status"),
           None if vpn_ok else "Check vpn status; connect only the dev or stg VPN using Terminal and OTP.")
    # Public checks still record VPN diagnostics, but do not depend on its CLI or OTP.
    results[-1]["required"] = any(name.endswith(("-db", "-ai")) for name in needs)

    # Always check VPN first; preserve the caller's order for requested checks.
    for name in needs:
        if fail_fast and any(r["status"] == "FAIL" and r.get("required", True) for r in results):
            results.append({"name": name, "status": "SKIP", "observed": None,
                            "human_action": None, "reason": "fail-fast"})
            continue
        if name.endswith("-db"):
            profile = name.split("-")[0]
            if VPN_STATUS_CMD != "none" and (not vpn_ok or vpn != profile):
                record(name, False, None, "wrong-vpn", vpn_action(profile))
                continue
            query = cfg.DB_QUERY_CMD or "DB_QUERY_CMD"
            raw, error = execute(name, [*shlex.split(query), cfg.DB_PRIMARY + cfg.DB_CONFIG_SUFFIX[profile],
                                        "SELECT @@hostname h"])
            data = parse_object(raw)
            rows = data.get("rows") if data else None
            host = rows[0].get("h") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
            # Only export a hostname-shaped value, never a raw DB response/error.
            observed = host if isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,248}", host) else None
            host_ok = observed is not None and observed.startswith(cfg.DB_HOST_PREFIX[profile])
            if not error and host is not None and not host_ok:
                record(name, False, observed, "hostname-mismatch", vpn_action(profile))
            elif error == "command-unavailable":
                record(name, False, None, error, "Set DB_QUERY_CMD in config.env (currently: " + query + ")")
            elif error or not host_ok:
                record(name, False, observed, error or "invalid-db-response")
            else:
                record(name, True, observed, "ok")
        elif name.endswith("-ai"):
            profile = name.split("-")[0]
            if VPN_STATUS_CMD != "none" and (not vpn_ok or vpn != profile):
                record(name, False, None, "wrong-vpn", vpn_action(profile))
                continue
            try:
                status = http(AI_URLS[profile], timeout)
                record(name, status == 200, status, "ok" if status == 200 else "http-not-200")
            except (OSError, ValueError, urllib.error.URLError, HTTPException):
                record(name, False, None, "http-unreachable")
        elif name.endswith("-web"):
            try:
                status = http(WEB_URLS[name.split("-")[0]], timeout)
                record(name, status == 200, status, "ok" if status == 200 else "http-not-200")
            except (OSError, ValueError, urllib.error.URLError, HTTPException):
                record(name, False, None, "http-unreachable")
        elif name == "jira":
            _, error = execute(name, ["acli", "jira", "auth", "status"])
            record(name, not error, "authenticated" if not error else None,
                   "ok" if not error else error, cfg.JIRA_REAUTH_HINT if error else None)
        elif name == "jenkins-dev":
            raw, error = execute(name, ["jk", "--context", JK_CONTEXT_DEV, "job", "ls", "--json"])
            if not error:
                try:
                    if not isinstance(json.loads(raw), (dict, list)):
                        error = "invalid-jenkins-response"
                except (ValueError, TypeError):
                    error = "invalid-jenkins-response"
            action = None
            if error == "command-unavailable":
                action = "Install jk and configure the dev context."
            elif error == "auth-or-context-required":
                action = "Check Jenkins dev context credentials: jk --context dev auth --help"
            record(name, not error, "reachable" if not error else None, "ok" if not error else error, action)

    required = [r for r in results if r.get("required", True)]
    human = any(r["status"] == "FAIL" and r["human_action"] for r in required)
    failed = any(r["status"] == "FAIL" for r in required)
    status, code = ("HUMAN", 10) if human else (("DOWN", 20) if failed else ("PASS", 0))
    return {"check": "preflight", "status": status, "vpn": vpn, "results": results}, code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Don't echo arbitrary user arguments or external output into unattended logs.
        print(json.dumps({"check": "preflight", "status": "HUMAN", "vpn": "none",
                          "results": [{"name": "usage", "status": "FAIL", "observed": None,
                                       "human_action": "Run preflight_env.py --help", "reason": "invalid-arguments"}]}))
        raise SystemExit(2)


def main(argv=None, *, command=run_command, http=http_status):
    parser = Parser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--need", help="Comma-separated checks: " + ",".join(NEEDS))
    mode.add_argument("--queue-item", type=Path, help="Print normalized next-step queue item (read-only, no probes)")
    parser.add_argument("--timeout", type=float, default=20, help="Seconds per operation (default 20)")
    parser.add_argument("--fail-fast", action="store_true", help="Skip remaining probes after the first required failure")
    args = parser.parse_args(argv)
    if args.queue_item is not None:
        try:
            item = next_step_item(json.loads(args.queue_item.expanduser().read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            parser.error("invalid queue item")
        print(json.dumps({"check": "needs", "item": item}, ensure_ascii=False))
        return 0
    needs = list(dict.fromkeys(args.need.split(",")))
    if not needs or any(n not in NEEDS for n in needs) or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("invalid checks or timeout")
    payload, code = check_environment(needs, args.timeout, command=command, http=http, fail_fast=args.fail_fast)
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
