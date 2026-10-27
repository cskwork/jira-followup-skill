#!/usr/bin/env python3
"""사람이 결정 페이지에서 승인한 SQL 파일을 dev/staging DB에 실행한다.

Usage: sql_runner.py --config app_stg --expect-host stg-db --file X.sql
                     [--guard-sql "SELECT COUNT(*) ..." --guard-expect 0] [--out result.json]
Exit: 0 all statements ran, 1 refused (host/guard) or a statement failed, 2 usage.

Safety (why each check exists):
- env는 dev/staging 설정(<db>_dev, <db>_stg — DB_CONFIG_SUFFIX_*)만 쓴다. 운영 접속 정보는 이 PC에 두지 않는다.
- 같은 연결에서 먼저 SELECT @@hostname을 읽고 --expect-host로 시작하지 않으면 아무것도 하지 않는다.
  dev와 staging이 같은 VIP를 쓰면 설정 이름만으로는 어느 DB인지 알 수 없다(반복 실측).
- DB 접속은 DB_TOOL_DIR(scripts/config.py:get_all_configs, scripts/db_connector.py:MySQLConnector)을 쓴다.
- --guard-sql이 있으면 그 결과(첫 행 첫 열)가 --guard-expect와 같을 때만 실행한다.
  예: 비멱등 ALTER 전에 "컬럼 없음(0)"을 확인한다.
- 문장은 파일 순서대로 하나씩 실행하고 첫 오류에서 멈춘다. '--' 주석 줄은 건너뛴다.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402

SKILL = Path(cfg.get("DB_TOOL_DIR", "~/.agents/skills/mysql-tool")).expanduser()
SUFFIXES = (cfg.DB_CONFIG_SUFFIX["dev"], cfg.DB_CONFIG_SUFFIX["stg"])


def statements(sql_text):
    body = "\n".join(line for line in sql_text.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def connect(config_name):
    sys.path.insert(0, str(SKILL))
    from scripts.config import get_all_configs  # type: ignore
    from scripts.db_connector import MySQLConnector  # type: ignore
    configs = get_all_configs()
    if config_name not in configs:
        raise ValueError(f"unknown config {config_name}")
    return MySQLConnector(config=configs[config_name]).get_connection()


def run(conn, *, expect_host, sql_text, guard_sql=None, guard_expect=None):
    result = {"host": None, "guard": None, "steps": [], "status": "refused"}
    cur = conn.cursor()
    cur.execute("SELECT @@hostname")
    host = str(cur.fetchone()[0])
    result["host"] = host
    if not expect_host or not host.startswith(expect_host) or "prd" in host or "prod" in host:
        result["reason"] = f"host {host} does not match {expect_host}"
        return result
    if guard_sql:
        cur.execute(guard_sql)
        value = cur.fetchone()[0]
        result["guard"] = value
        if str(value) != str(guard_expect):
            result["reason"] = f"guard returned {value}, expected {guard_expect}"
            return result
    for stmt in statements(sql_text):
        step: dict = {"sql": stmt[:160]}
        try:
            cur.execute(stmt)
            if cur.description:
                cols = [d[0] for d in cur.description]
                step["columns"], step["rows"] = cols, [list(map(str, r)) for r in cur.fetchall()][:50]
            else:
                step["rowcount"] = cur.rowcount
        except Exception as error:  # report the DB error text, then stop
            step["error"] = str(error)[:300]
            result["steps"].append(step)
            result["status"] = "failed"
            conn.rollback()
            return result
        result["steps"].append(step)
    conn.commit()
    result["status"] = "done"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="승인된 SQL 파일 실행 (dev/staging 전용)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--expect-host", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--guard-sql")
    parser.add_argument("--guard-expect")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    if not args.config.endswith(SUFFIXES):
        parser.error("config must be a dev/staging config (…%s, …%s)" % SUFFIXES)
    conn = connect(args.config)
    try:
        result = run(conn, expect_host=args.expect_host, sql_text=Path(args.file).read_text(),
                     guard_sql=args.guard_sql, guard_expect=args.guard_expect)
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if result["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
