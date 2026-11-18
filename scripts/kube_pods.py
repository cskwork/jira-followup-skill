#!/usr/bin/env python3
"""Read-only pod access shared by preflight and new-ERROR pod regex preparation.

Settings (config.env): KUBE_CONNECT_CMD runs one remote shell command on a cluster jump host,
`<KUBE_CONNECT_CMD> <host> "<kubectl command>"` (for example an SSH wrapper from `<kube 접속 스킬>`).
KUBE_HOST_DEV / KUBE_HOST_STG name the host per environment. With KUBE_CONNECT_CMD empty the check
is not configured: probe returns status SKIP (reason not-configured) and runs nothing.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402

ENVS = ("stg", "dev")
CONNECT = cfg.KUBE_CONNECT_CMD
HOSTS = {"dev": cfg.KUBE_HOST_DEV, "stg": cfg.KUBE_HOST_STG}
TIMEOUT = 30
RETRY_DELAY = 2
LABEL = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
POD = re.compile(r"pod/(" + LABEL + r"(?:\." + LABEL + r")*)")
REPLICASET = re.compile(r"replicaset\.apps/(" + LABEL + r"(?:\." + LABEL + r")*)")
NOT_CONFIGURED = "KUBE_CONNECT_CMD 미설정: kube 점검을 건너뜀(config.env에 접속 명령과 KUBE_HOST_<ENV>를 설정)"


def configured():
    return bool(CONNECT.strip())


def pod_command(env, *, include_replicasets=False):
    if env not in ENVS:
        raise ValueError("env must be stg or dev")
    resources = "rs,pods" if include_replicasets else "pods"
    return [*map(os.path.expanduser, CONNECT.split()), HOSTS[env], f"kubectl get {resources} -A -o name"]


def run_command(name, argv, timeout):
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def failure(reason, env, hint=None, *, timeout=TIMEOUT):
    host = HOSTS.get(env) or env
    hints = {
        "vpn": f"VPN을 {env}로 전환하고 터미널에서 OTP를 입력한 뒤 다시 확인하세요.",
        "host-key": (f"VPN이 {env}인지 확인하고 서버의 SSH 호스트 키 지문을 관리자에게 확인한 뒤 "
                     "해당 known_hosts 항목을 복구하세요."),
        "kube-auth": (f"{host} SSH 사용자의 kubeconfig와 클러스터 인증을 "
                      "복구·재인증한 뒤 pod 조회를 다시 확인하세요."),
        "ssh-auth": "SSH 비밀번호 인증이 일시 거부됨; 반복되면 계정 잠김·비밀번호 만료 확인",
        "timeout": f"SSH/VPN 응답이 {timeout:g}초 안에 오지 않음; 다시 시도하거나 VPN 상태 확인",
        "other": f"{host} SSH 연결·호스트 설정과 pod 조회 출력을 확인하세요.",
    }
    return {"status": "FAIL", "reason": reason, "hint": hint or hints[reason], "pods": []}


def not_configured():
    return {"status": "SKIP", "reason": "not-configured", "hint": NOT_CONFIGURED, "pods": [], "attempts": 0}


def classify(output, env):
    text = output.lower()
    vpn = re.search(r"vpn\s*[=:]\s*(stg|dev|none)", text)
    if any(marker in text for marker in ("wrong-vpn", "vpn not", "vpn required", "vpn is not")) or (
            vpn and vpn.group(1) != env):
        return "vpn"
    if any(marker in text for marker in ("remote host identification has changed", "host key verification",
                                        "host-key mismatch", "host key mismatch")):
        return "host-key"
    if any(marker in text for marker in ("localhost:8080", "127.0.0.1:8080", "kubeconfig", "unauthorized",
                                        "forbidden", "iam-authenticator", "getting credentials",
                                        "you must be logged in", "provide credentials",
                                        "no configuration has been provided", "client certificate")):
        return "kube-auth"
    if "permission denied" in text:
        return "ssh-auth"
    return "other"


def _probe_once(env, argv, timeout, *, command, include_replicasets):
    try:
        response = command(env + "-kube", argv, timeout)
    except subprocess.TimeoutExpired:
        return failure("timeout", env, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return failure("other", env)
    if response.returncode:
        return failure(classify((response.stdout or "") + "\n" + (response.stderr or ""), env), env)
    lines = [line.strip() for line in (response.stdout or "").splitlines() if line.strip()]
    if not lines:
        return failure("other", env)
    pods, replicasets = set(), set()
    for line in lines:
        pod = POD.fullmatch(line)
        replicaset = REPLICASET.fullmatch(line) if include_replicasets else None
        row = pod or replicaset
        if row is None or len(row.group(1)) > 253:
            return failure("other", env)
        (pods if pod else replicasets).add(row.group(1))
    result = {"status": "PASS", "reason": "ok", "hint": None, "pods": sorted(pods)}
    if include_replicasets:
        result["replicasets"] = sorted(replicasets)
    return result


def probe(env, timeout=TIMEOUT, *, command=run_command, include_replicasets=False,
          sleep=None, clock=None):
    if env not in ENVS:
        raise ValueError("env must be stg or dev")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if not configured():
        return not_configured()
    argv = pod_command(env, include_replicasets=include_replicasets)
    sleep = time.sleep if sleep is None else sleep
    clock = time.monotonic if clock is None else clock
    started = clock()
    result = _probe_once(env, argv, min(timeout, TIMEOUT), command=command,
                         include_replicasets=include_replicasets)
    attempts = 1
    # The caller's budget includes both SSH attempts and the backoff.
    if result["reason"] in ("ssh-auth", "timeout") and timeout - (clock() - started) > RETRY_DELAY:
        sleep(RETRY_DELAY)
        remaining = timeout - (clock() - started)
        if remaining > 0:
            result = _probe_once(env, argv, min(remaining, TIMEOUT), command=command,
                                 include_replicasets=include_replicasets)
            attempts = 2
    result["attempts"] = attempts
    return result


def service_name(value):
    if len(value) > 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value):
        raise argparse.ArgumentTypeError("service는 정확한 workload 이름 접두어여야 합니다")
    return value


def main(argv=None, *, command=run_command, sleep=None, clock=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("probe", "regex"):
        sub = modes.add_parser(mode)
        sub.add_argument("--env", choices=ENVS, required=True)
        if mode == "regex":
            sub.add_argument("--service", type=service_name, required=True,
                             help="정확한 workload 접두어 (이름이 같거나 접두어 뒤 '-'인 pod만 선택)")
    args = parser.parse_args(argv)
    result = probe(args.env, command=command, include_replicasets=args.mode == "regex",
                   sleep=sleep, clock=clock)
    if args.mode == "probe":
        print(json.dumps(result, ensure_ascii=False))
        # 미설정(SKIP)은 실패가 아니다: 출력의 status로 구분한다.
        return 0 if result["status"] in ("PASS", "SKIP") else 1
    if result["status"] == "PASS":
        workload = re.compile(re.escape(args.service) + r"-([a-z0-9]+)")
        hashes = sorted({match.group(1) for name in result["replicasets"]
                         if (match := workload.fullmatch(name))})
        if hashes:
            print("^" + re.escape(args.service) + "-(?:" + "|".join(re.escape(value) for value in hashes)
                  + ")-[a-z0-9]{5}$")
            return 0
        pods = [name for name in result["pods"] if name == args.service or name.startswith(args.service + "-")]
        if pods:
            print(f"경고: {args.service}에 일치하는 ReplicaSet이 없어 현재 pod 이름만 사용합니다. "
                  "baseline window가 불완전할 수 있습니다.", file=sys.stderr)
            print("^(?:" + "|".join(re.escape(name) for name in pods) + ")$")
            return 0
        result = {**failure("other", args.env,
                            f"{args.service}에 일치하는 pod가 없습니다. SSH 조회와 정확한 workload 접두어를 확인하세요."),
                  "attempts": result["attempts"]}
    print(result["reason"] + ": " + result["hint"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
