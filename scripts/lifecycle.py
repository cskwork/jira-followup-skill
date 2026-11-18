#!/usr/bin/env python3
"""개발 단계는 sdlc-kit, 배포(delivery) 단계는 큐 `delivery`가 정본이다(사용자 결정 2026-11-16).

- sdlc "evidence"는 로컬 검증까지다. 첫 push·dev 배포부터는 공장의 delivery다.
- 큐 `delivery`는 닫힌 값(DELIVERY)이다. 없으면 옛 `stage`에서 순수 대응표로 끌어낸다(라이브 상태에 옮겨 쓰지 않는다).
  알 수 없는 옛 stage는 None(판정 불가)이며, 읽는 쪽은 그때 지금까지처럼 동작한다.
- `stage`는 단위 이름표(작업자·requirements·표시)일 뿐이다. 가드·judge·preflight·대조는 raw stage로 갈라지지 않는다.
- sdlc는 `bash <SDLC_KIT>/sdlc.sh status <slug> --json`(읽기 전용)을 작업 루트(JIRA_SDLC_ROOT, 기본 JIRA_FOLLOWUP_WORKSPACE)에서 부른다. find_store가 $PWD부터
  위로 .sdlc/config.md를 찾으므로 cwd가 필요하다. Sdlc 객체 하나가 한 tick 동안 slug별 결과를 캐시한다.
- JIRA_SDLC=off면 sdlc를 부르지 않는다(시험 안전망 기본값, 운영 비상 스위치).

CLI: lifecycle.py check-ship --key KEY --slug SLUG [--out PATH]
  `sdlc.sh check ship <slug>`(읽기 전용)로 첫 push·dev 배포 단위를 막는다. 출력은 requirements 검사 JSON
  ({check, result, key, slug, reason, queueWaiting, question, suggest}). exit 0 PASS, 1 FAIL, 2 잘못된 사용.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402

DELIVERY = ("none", "dev-deployed", "dev-verified", "result-posted", "stg-ready", "stg-merged",
            "stg-deployed", "stg-verified", "stg-commented", "shipped")
PHASES = ("intent", "plan", "build", "evidence", "deliver", "done", "legacy")
STATES = ("READY", "NEEDS-HUMAN", "BLOCKED", "DONE")
TIMEOUT = 10

# 옛 stage → delivery. 여기 없는 값(approved·merge-resolved 등 단위 판정값)은 None(판정 불가)이다.
_STAGE_DELIVERY = {
    **dict.fromkeys(("intake", "intent", "reproduce", "asis", "plan", "build", "built", "tobe", "evidence",
                     "evidence-local", "dev-deploy"), "none"),
    **dict.fromkeys(("dev-deployed", "dev-smoke"), "dev-deployed"),
    **dict.fromkeys(("dev-e2e", "dev-verified", "tobe-after-deploy"), "dev-verified"),
    **dict.fromkeys(("commented", "result-posted", "dev-qa-requested"), "result-posted"),
    **dict.fromkeys(("stg-prepare", "stg-ready"), "stg-ready"),
    **dict.fromkeys(("stg-merged", "stg-resolved"), "stg-merged"),
    **dict.fromkeys(("stg-deployed", "stg-e2e", "stg-smoked"), "stg-deployed"),
    "stg-verified": "stg-verified",
    **dict.fromkeys(("stg-commented", "stg-qa-requested"), "stg-commented"),
    **dict.fromkeys(("shipped", "closed"), "shipped"),
}
# 저장소별 부분 배포 이름표(dev-deployed-api 등).
_PREFIX_DELIVERY = (("dev-deployed-", "dev-deployed"), ("stg-deployed-", "stg-deployed"))
# sdlc가 없을 때(slug 없음·오류·꺼짐) 옛 stage로 보는 개발 단계. dev-deploy는 배포 직전(deliver)이다.
_STAGE_PHASE = {"plan": "plan", "build": "build", "tobe": "evidence", "evidence": "evidence", "dev-deploy": "deliver"}
# 첫 push·dev 배포 단위 이름표: requirements에 sdlc-ship 검사가 있어야 작업자를 띄운다.
SHIP_UNITS = ("push", "pushed", "dev-deploy", "dev-deployed")
SHIP_CHECK = "sdlc-ship"


class SdlcError(Exception):
    """sdlc를 부를 수 없거나 출력이 계약과 다르다. 가드는 보고만 하고 지금처럼 동작한다."""


def stage_delivery(stage):
    if not isinstance(stage, str):
        return None
    if stage in _STAGE_DELIVERY:
        return _STAGE_DELIVERY[stage]
    return next((value for prefix, value in _PREFIX_DELIVERY if stage.startswith(prefix)), None)


def delivery(item):
    """명시한 `delivery`가 정본이다. 없으면 옛 stage에서 끌어낸다. 닫힌 값 밖이면 ValueError."""
    if not isinstance(item, dict):
        raise ValueError("invalid queue item")
    if "delivery" in item:
        if item["delivery"] not in DELIVERY:
            raise ValueError("invalid delivery")
        return item["delivery"]
    return stage_delivery(item.get("stage"))


def legacy_phase(stage):
    return _STAGE_PHASE.get(stage) if isinstance(stage, str) else None


def is_ship_unit(stage):
    return isinstance(stage, str) and (stage in SHIP_UNITS or stage.startswith("dev-deployed-"))


def enabled():
    return os.environ.get("JIRA_SDLC", "on").strip().lower() != "off"


def kit():
    return Path(os.path.expanduser(os.environ.get("SDLC_KIT") or str(cfg.SDLC_KIT)))


def root():
    return Path(os.path.expanduser(os.environ.get("JIRA_SDLC_ROOT") or str(cfg.WORKSPACE)))


class Sdlc:
    """한 tick 동안 slug별 sdlc 상태를 한 번만 읽는다. 오류도 캐시해 같은 tick에 다시 부르지 않는다."""

    def __init__(self, *, runner=subprocess.run, timeout=None):
        self.runner, self.timeout, self.cache = runner, TIMEOUT if timeout is None else timeout, {}

    def call(self, *args):
        if not root().is_dir():
            raise SdlcError("sdlc root missing")
        try:
            return self.runner(["bash", str(kit() / "sdlc.sh"), *args], cwd=str(root()), capture_output=True,
                               stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                               timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise SdlcError("timeout") from None
        except OSError as error:
            raise SdlcError(type(error).__name__) from None

    def status(self, slug):
        """{"phase","state",...}. 꺼져 있으면 None. 실패는 SdlcError."""
        if not enabled():
            return None
        if slug not in self.cache:
            try:
                self.cache[slug] = self._status(slug)
            except SdlcError as error:
                self.cache[slug] = error
        value = self.cache[slug]
        if isinstance(value, SdlcError):
            raise value
        return value

    def _status(self, slug):
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise SdlcError("invalid slug")
        response = self.call("status", slug, "--json")
        if response.returncode:
            raise SdlcError(f"exit {response.returncode}")
        try:
            features = json.loads(response.stdout)["features"]
            row = next(f for f in features if isinstance(f, dict) and f.get("slug") == slug)
        except (ValueError, KeyError, TypeError, StopIteration):
            raise SdlcError("invalid status output") from None
        if row.get("phase") not in PHASES or row.get("state") not in STATES:
            raise SdlcError("invalid status output")
        return row

    def check_ship(self, slug):
        """(통과 여부, 사유 한 줄). 꺼져 있어도 부른다: 호출자가 enabled()를 먼저 본다."""
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            return False, "invalid slug"
        try:
            response = self.call("check", "ship", slug)
        except SdlcError as error:
            return False, f"sdlc check ship 실행 실패: {error}"
        text = " ".join((response.stdout or response.stderr or "").split())[:300]
        return response.returncode == 0, text or f"exit {response.returncode}"


def ship_report(key, slug, *, sdlc=None):
    ok, reason = (sdlc or Sdlc()).check_ship(slug)
    report = {"check": SHIP_CHECK, "result": "PASS" if ok else "FAIL", "key": key, "slug": slug, "reason": reason,
              "queueWaiting": None, "question": None, "suggest": None}
    if not ok:
        # human-risk 계약: 사용자가 정할 질문 한 문장과 그대로 보낼 권장 답 한 줄.
        report.update(queueWaiting="human-risk",
                      question=f"{key}의 sdlc 출고 점검(check ship)이 닫혀 dev push·배포를 멈췄습니다({reason[:160]}). 어떻게 할까요?",
                      suggest="sdlc 증거 단계(QA·score)를 지금 HEAD에서 다시 마쳐 check ship을 연 뒤 dev 배포를 이어 간다.")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ship = sub.add_parser("check-ship")
    ship.add_argument("--key", required=True)
    ship.add_argument("--slug", required=True)
    ship.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", args.key):
        parser.error("invalid key")
    report = ship_report(args.key, args.slug)
    if args.out:
        from state_update import atomic_json
        atomic_json(args.out, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
