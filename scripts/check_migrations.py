#!/usr/bin/env python3
"""Read-only staging schema gate before requesting stg approval.

Usage: check_migrations.py --repo DIR --base origin/stg --head SOURCE
       check_migrations.py --key PROJ-123 [--root WORKSPACE]
Exit: 0 applied/no migrations, 1 missing, 3 needs human interpretation,
10 Git/configuration/VPN/DB unavailable. Git and DB are injectable for offline tests.
Only ALTER TABLE ADD [COLUMN] and CREATE TABLE are verified; other writes need a human.

Settings (config.env): DB_TOOL_DIR is a MySQL helper directory exposing
scripts/config.py:get_all_configs() and scripts/db_connector.py:MySQLConnector(config=...)
(the same contract sql_runner.py uses). DB_SERVICE_MAP maps repo-name prefixes to DB names
(`cms-=cms,core-=app`); empty means every repo uses DB_PRIMARY. Staging config names are
<db>DB_CONFIG_SUFFIX_STG and the connection's @@hostname must start with DB_HOST_PREFIX_STG.
MIGRATION_GLOB (default docs/db/migration/*.sql) selects migration files.
"""

import argparse
import fnmatch
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import followup_config as cfg  # noqa: E402

MYSQL = Path(cfg.get("DB_TOOL_DIR", "~/.agents/skills/mysql-tool")).expanduser()
WORKSPACE = str(cfg.WORKSPACE)
STG_SUFFIX = cfg.DB_CONFIG_SUFFIX["stg"]
STG_HOST = cfg.DB_HOST_PREFIX["stg"]
MIGRATION_GLOB = cfg.get("MIGRATION_GLOB", "docs/db/migration/*.sql")
SERVICE_DB = [tuple(part.split("=", 1)) for part in cfg.get_list("DB_SERVICE_MAP", "") if "=" in part]
IDENT = r"(?:`[A-Za-z0-9_$]+`|[A-Za-z_][A-Za-z0-9_$]*)"
TABLE = rf"{IDENT}(?:\s*\.\s*{IDENT})?"


class HumanCheck(ValueError):
    pass


class MigrationEnvError(RuntimeError):
    pass


def run_command(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def git(repo, *args, command):
    result = command(["git", "--no-pager", "-C", str(repo), *args], timeout=30)
    if result.returncode:
        raise MigrationEnvError("local Git range unavailable")
    return result.stdout


def mysql_module(name):
    spec = importlib.util.spec_from_file_location("migration_mysql_" + name, MYSQL / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_mysql_configs():
    # Same read-only configuration loader as the DB tool CLI; never log credentials.
    return mysql_module("config").get_all_configs()


def connect_mysql(config):
    return mysql_module("db_connector").MySQLConnector(config=config).get_connection()


def sql_parts(text, delimiter=";"):
    """Split outside literals/comments/parentheses; reject incomplete SQL."""
    parts, current, quote, depth, i = [], [], None, 0, 0
    while i < len(text):
        char = text[i]
        if quote:
            current.append(char)
            if char == "\\" and i + 1 < len(text):
                i += 1
                current.append(text[i])
            elif char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 1
                    current.append(text[i])
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
            current.append(char)
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0 or text.startswith("/*!", i):
                raise HumanCheck("unterminated or executable SQL comment")
            current.append(" ")
            i = end + 1
        elif char == "#" or (text.startswith("--", i) and (i + 2 == len(text) or text[i + 2].isspace())):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            current.append(" ")
        elif char == delimiter and depth == 0:
            if "".join(current).strip():
                parts.append("".join(current).strip())
            current = []
        else:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise HumanCheck("unbalanced SQL parentheses")
            current.append(char)
        i += 1
    if quote or depth:
        raise HumanCheck("incomplete SQL literal or parentheses")
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def requirements(text, default_schema):
    schema, result = default_schema, []
    for statement in sql_parts(text):
        use = re.fullmatch(rf"USE\s+({IDENT})", statement, re.I)
        if use:
            schema = use[1].strip("`")
            continue
        create = re.fullmatch(rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({TABLE})\s*\(.+\)(?:\s+.*)?", statement, re.I | re.S)
        alter = re.fullmatch(rf"ALTER\s+TABLE\s+({TABLE})\s+(.+)", statement, re.I | re.S)
        if not create and not alter:
            # SELECT cannot change the schema; dynamic SQL, DML and other DDL stay explicit.
            if re.match(r"^SELECT\b", statement, re.I):
                continue
            raise HumanCheck("unsupported migration statement")
        name = re.split(r"\s*\.\s*", (create or alter)[1])
        target_schema, table = (name[0].strip("`"), name[1].strip("`")) if len(name) == 2 else (schema, name[0].strip("`"))
        if not target_schema:
            raise HumanCheck("no USE, schema prefix or configured service database")
        if create:
            result.append({"kind": "table", "schema": target_schema, "table": table})
            continue
        for clause in sql_parts(alter[2], ","):
            added = re.fullmatch(rf"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?({IDENT})\s+(.+)", clause, re.I | re.S)
            if not added or added[1].strip("`").upper() in ("INDEX", "KEY", "CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "PARTITION"):
                raise HumanCheck("unsupported ALTER TABLE clause")
            result.append({"kind": "column", "schema": target_schema, "table": table, "column": added[1].strip("`")})
    return result


def config_name(repo):
    # Worktrees also use names such as PROJ-123-core-api.
    service = re.sub(r"^[A-Z][A-Z0-9]+-\d+-", "", Path(repo).name)
    if not SERVICE_DB:
        return cfg.DB_PRIMARY + STG_SUFFIX
    for prefix, database in SERVICE_DB:
        if service == prefix or (prefix.endswith("-") and service.startswith(prefix)):
            return database + STG_SUFFIX
    raise HumanCheck("unknown service database; specify --config <name" + STG_SUFFIX + ">")


def check_repo(repo, base, head, *, config, command, load_configs, connect):
    def resolve(ref):
        sha = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", command=command).strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise MigrationEnvError("invalid resolved Git commit")
        return sha

    base_sha, head_sha = resolve(base), resolve(head)
    paths = git(repo, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=AM", base_sha + "..." + head_sha,
                "--", MIGRATION_GLOB, command=command).split("\0")
    paths = [path for path in paths if path]
    if not paths:
        return []
    configs = load_configs()
    name = config or config_name(repo)
    if not name.endswith(STG_SUFFIX):
        raise HumanCheck("only staging DB configurations are allowed")
    if name not in configs:
        raise MigrationEnvError("staging DB configuration unavailable")
    settings = configs[name]
    checks = []
    for path in paths:
        text = git(repo, "show", head_sha + ":" + path, command=command)
        try:
            checks.extend({"file": path, **entry} for entry in requirements(text, settings.database))
        except HumanCheck as error:
            raise HumanCheck(path + ": " + str(error)) from None
    if not checks:
        return []
    conn = connect(settings)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT @@hostname")
            row = cur.fetchone()
            host = row[0] if row else None
            if not isinstance(host, str) or not host.startswith(STG_HOST) or any(x in host.lower() for x in ("prd", "prod")):
                raise MigrationEnvError("wrong DB hostname; staging VPN required")
            for entry in checks:
                sql = "SELECT COUNT(*) FROM information_schema."
                params = (entry["schema"], entry["table"])
                if entry["kind"] == "column":
                    sql += "COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s"
                    params += (entry["column"],)
                else:
                    sql += "TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'"
                cur.execute(sql, params)
                entry["applied"] = bool(cur.fetchone()[0])
        finally:
            cur.close()
    finally:
        conn.close()
    return checks


def ticket_targets(key, root, command):
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
        raise HumanCheck("invalid ticket key")
    response = command([sys.executable, str(HERE / "ticket_refs.py"), key, "--root", root], timeout=180)
    if response.returncode:
        raise MigrationEnvError("ticket promotion refs unavailable")
    rows = json.loads(response.stdout).get("repos")
    if not rows:
        raise HumanCheck("no ticket promotion repositories found")
    targets = []
    for row in rows:
        if row.get("stg"):
            continue
        branches = row.get("branches", [])
        own = [b for b in branches if b.get("own")]
        selected = own if own else branches
        if len(selected) != 1:
            raise HumanCheck("ambiguous source branch; use --repo and --head for each repository")
        repo = row["repo"]
        if Path(repo).name != repo or not fnmatch.fnmatch(repo, cfg.REPO_GLOB):
            raise HumanCheck("invalid ticket repository")
        targets.append((str(Path(root) / repo), "origin/" + selected[0]["branch"]))
    return targets


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise HumanCheck("invalid arguments; see --help")


def main(argv=None, *, command=run_command, load_configs=load_mysql_configs, connect=connect_mysql):
    output = {"check": "migrations", "status": "UNKNOWN", "checks": []}
    try:
        parser = Parser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--repo")
        parser.add_argument("--head")
        parser.add_argument("--base", default="origin/" + cfg.BRANCH_STG)
        parser.add_argument("--config")
        parser.add_argument("--key")
        parser.add_argument("--root", default=WORKSPACE)
        args = parser.parse_args(argv)
        if args.key and not args.repo and not args.head:
            targets = ticket_targets(args.key, args.root, command)
        elif args.repo and args.head and not args.key:
            targets = [(args.repo, args.head)]
        else:
            raise HumanCheck("specify --repo with --head, or --key")
        for repo, head in targets:
            output["checks"].extend({"repo": repo, **entry} for entry in check_repo(
                repo, args.base, head, config=args.config, command=command, load_configs=load_configs, connect=connect))
        code = 0 if all(c["applied"] for c in output["checks"]) else 1
        output["status"] = "PASS" if code == 0 else "FAIL"
    except HumanCheck as error:
        output["error"], code = str(error), 3
    except Exception:
        # Driver exceptions can contain connection credentials; never echo them.
        output["status"], output["error"], code = "ENV", "Git/configuration/DB unavailable or wrong staging VPN", 10
    print(json.dumps(output, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
