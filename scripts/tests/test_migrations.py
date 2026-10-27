"""Offline migration checks; Git, configuration and DB are injected."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_migrations", ROOT / "check_migrations.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)


class Cursor:
    def __init__(self, host="stg-db001", present=True):
        self.host, self.present, self.calls = host, present, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return (self.host,) if self.calls[-1][0] == "SELECT @@hostname" else (int(self.present),)

    def close(self):
        pass


class Connection:
    def __init__(self, cursor):
        self.cur, self.closed = cursor, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class Migrations(unittest.TestCase):
    def setUp(self):
        self.files = {"docs/db/migration/add.sql": "ALTER TABLE tb_board ADD COLUMN OWNER_ID varchar(36);"}
        self.calls, self.connections = [], []
        self.cur = Cursor()
        self.configs = {"app_stg": SimpleNamespace(database="app_from_config"),
                        "cms_stg": SimpleNamespace(database="cms_from_config")}

    def command(self, argv, timeout):
        self.calls.append(argv)
        if "rev-parse" in argv:
            return SimpleNamespace(returncode=0, stdout=("a" if "origin/stg^{commit}" in argv else "b") * 40 + "\n")
        if "diff" in argv:
            return SimpleNamespace(returncode=0, stdout="\0".join(self.files) + ("\0" if self.files else ""))
        if "show" in argv:
            return SimpleNamespace(returncode=0, stdout=self.files[argv[-1].split(":", 1)[1]])
        self.fail(f"unexpected external command: {argv}")

    def connect(self, config):
        self.connections.append(config)
        self.conn = Connection(self.cur)
        return self.conn

    def check(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cm.main(["--repo", "/offline/core-api", "--head", "origin/fix/PROJ-1389", *extra],
                           command=self.command, load_configs=lambda: self.configs, connect=self.connect)
        return code, json.loads(output.getvalue())

    def test_missing_column_fails_and_queries_only_audit_metadata(self):
        self.cur.present = False
        code, data = self.check()
        self.assertEqual((code, data["status"]), (1, "FAIL"))
        self.assertFalse(data["checks"][0]["applied"])
        self.assertEqual(self.cur.calls[0], ("SELECT @@hostname", None))
        self.assertIn("information_schema.COLUMNS", self.cur.calls[1][0])
        self.assertEqual(self.cur.calls[1][1], ("app_from_config", "tb_board", "OWNER_ID"))
        self.assertTrue(self.conn.closed)
        self.assertTrue(all(sql.startswith("SELECT ") for sql, _ in self.cur.calls))

    def test_applied_column_and_create_table_pass(self):
        self.files["docs/db/migration/new.sql"] = "CREATE TABLE IF NOT EXISTS `cms`.`new_t` (id int);"
        code, data = self.check()
        self.assertEqual((code, data["status"]), (0, "PASS"))
        self.assertEqual(len(data["checks"]), 2)
        self.assertIn("information_schema.TABLES", self.cur.calls[2][0])
        self.assertEqual(self.cur.calls[2][1], ("cms", "new_t"))

    def test_missing_table_fails(self):
        self.files = {"docs/db/migration/new.sql": "create table `new_t` (id int);"}
        self.cur.present = False
        code, data = self.check()
        self.assertEqual((code, data["status"]), (1, "FAIL"))
        self.assertEqual(data["checks"][0]["kind"], "table")
        self.assertFalse(data["checks"][0]["applied"])

    def test_wrong_host_stops_before_information_schema(self):
        for host in ("dev-db001", "prd-db001", "stg-dbprod"):
            with self.subTest(host=host):
                self.cur = Cursor(host=host)
                code, data = self.check()
                self.assertEqual((code, data["status"]), (10, "ENV"))
                self.assertEqual(self.cur.calls, [("SELECT @@hostname", None)])

    def test_unparseable_sql_fails_closed_without_db(self):
        for sql in ("ALTER TABLE t DROP COLUMN c;", "PREPARE migration FROM @sql;", "ALTER TABLE t ADD;",
                    "ALTER TABLE t ADD COLUMN c int; UPDATE t SET c=1;", "ALTER TABLE t ADD c varchar(8) COMMENT 'unterminated;"):
            with self.subTest(sql=sql):
                self.files = {"docs/db/migration/bad.sql": sql}
                code, data = self.check()
                self.assertEqual((code, data["status"]), (3, "UNKNOWN"))
                self.assertEqual(self.connections, [])

    def test_use_qualified_prefix_and_service_config_default(self):
        self.files = {"docs/db/migration/one.sql": "USE `sso`; ALTER TABLE x ADD c int; ALTER TABLE `cms`.`y` ADD COLUMN d int;",
                      "docs/db/migration/two.sql": "CREATE TABLE z (id int);"}
        code, data = self.check("--config", "cms_stg")
        self.assertEqual(code, 0)
        self.assertEqual([c["schema"] for c in data["checks"]], ["sso", "cms", "cms_from_config"])

    def test_comments_literals_and_multiple_columns(self):
        self.files = {"docs/db/migration/add.sql": "-- ALTER TABLE fake ADD wrong int;\n/* CREATE TABLE fake (x int); */\nALTER TABLE t ADD c decimal(10,2), ADD COLUMN d varchar(20) COMMENT 'a; --b';"}
        code, data = self.check()
        self.assertEqual(code, 0)
        self.assertEqual([c["column"] for c in data["checks"]], ["c", "d"])

    def test_no_migrations_does_not_load_config_or_connect(self):
        self.files = {}
        def no_configs():
            self.fail("no migration must not load DB config")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cm.main(["--repo", "/offline/core-api", "--head", "ticket"],
                                     command=self.command, load_configs=no_configs, connect=self.connect), 0)
        code, data = self.check()
        self.assertEqual((code, data["checks"]), (0, []))
        self.assertEqual(self.connections, [])

    def test_git_range_uses_resolved_heads_and_added_modified_sql_filter(self):
        self.check()
        diff = next(c for c in self.calls if "diff" in c)
        self.assertIn("--diff-filter=AM", diff)
        self.assertIn("a" * 40 + "..." + "b" * 40, diff)
        self.assertIn("docs/db/migration/*.sql", diff)
        show = next(c for c in self.calls if "show" in c)
        self.assertEqual(show[-1], "b" * 40 + ":docs/db/migration/add.sql")

    def test_git_or_db_environment_failure_has_json_exit_10(self):
        def failed(*args, **kwargs):
            raise OSError("offline")
        for target in ("command", "connect"):
            with self.subTest(target=target):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = cm.main(["--repo", "/offline/core-api", "--head", "ticket"],
                                   command=failed if target == "command" else self.command,
                                   load_configs=lambda: self.configs,
                                   connect=failed if target == "connect" else self.connect)
                self.assertEqual((code, json.loads(out.getvalue())["status"]), (10, "ENV"))

    def test_unknown_service_or_missing_default_schema_needs_human(self):
        self.configs["app_stg"].database = None
        code, data = self.check()
        self.assertEqual((code, data["status"]), (3, "UNKNOWN"))
        self.assertEqual(self.connections, [])

    def test_config_selection_handles_service_and_worktree_names(self):
        mapping = [("cms-", "cms"), ("core-", "app"), ("content-", "app"), ("sso-", "sso"), ("demo", "sso")]
        self.addCleanup(setattr, cm, "SERVICE_DB", cm.SERVICE_DB)
        cm.SERVICE_DB = mapping
        for repo, expected in (("core-api", "app_stg"), ("PROJ-1389-content-api", "app_stg"),
                               ("cms-api", "cms_stg"), ("sso-api", "sso_stg")):
            with self.subTest(repo=repo):
                self.assertEqual(cm.config_name(repo), expected)
        with self.assertRaises(cm.HumanCheck):
            cm.config_name("unknown-service")

    def test_schema_prefix_and_use_work_even_without_default_database(self):
        self.configs["app_stg"].database = None
        self.files = {"docs/db/migration/one.sql": "USE other; ALTER TABLE t ADD c int; CREATE TABLE `cms`.x (id int);"}
        code, data = self.check()
        self.assertEqual(code, 0)
        self.assertEqual([c["schema"] for c in data["checks"]], ["other", "cms"])

    def test_non_audit_config_is_refused_before_connect(self):
        code, data = self.check("--config", "app_dev")
        self.assertEqual((code, data["status"]), (3, "UNKNOWN"))
        self.assertEqual(self.connections, [])

    def test_ticket_targets_use_carried_branch_and_skip_already_stg(self):
        rows = [{"repo": "core-api", "stg": False,
                 "branches": [{"branch": "fix/PROJ-1315", "own": False, "other_keys": ["PROJ-1315"]}]},
                {"repo": "content-api", "stg": True, "branches": []}]
        calls = []
        def refs_command(argv, timeout):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"repos": rows}))
        self.assertEqual(cm.ticket_targets("PROJ-1389", "/offline", refs_command),
                         [("/offline/core-api", "origin/fix/PROJ-1315")])
        self.assertEqual(calls[0][-3:], ["PROJ-1389", "--root", "/offline"])
        rows[0]["branches"].append({"branch": "fix/PROJ-1400", "own": False})
        with self.assertRaises(cm.HumanCheck):
            cm.ticket_targets("PROJ-1389", "/offline", refs_command)

    def test_key_mode_checks_every_pending_repository_offline(self):
        rows = [{"repo": "core-api", "stg": False, "branches": [{"branch": "fix/PROJ-1389", "own": True}]},
                {"repo": "content-api", "stg": False, "branches": [{"branch": "fix/PROJ-1315", "own": False}]}]
        def command(argv, timeout):
            if argv[0] == sys.executable:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"repos": rows}))
            return self.command(argv, timeout)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cm.main(["--key", "PROJ-1389", "--root", "/offline"], command=command,
                           load_configs=lambda: self.configs, connect=self.connect)
        self.assertEqual(code, 0)
        self.assertEqual([c["repo"] for c in json.loads(out.getvalue())["checks"]],
                         ["/offline/core-api", "/offline/content-api"])


class ApprovalGate(unittest.TestCase):
    def test_nonzero_checker_never_starts_approval_page(self):
        with tempfile.TemporaryDirectory() as temp:
            here = Path(temp)
            shutil.copy(ROOT / "stg-approval-request", here / "stg-approval-request")
            log = here / "calls"
            (here / "check_migrations.py").write_text("import os, sys\nfrom pathlib import Path\nPath(os.environ['CALLS']).write_text('check')\nsys.exit(int(os.environ['CHECK_CODE']))\n")
            (here / "decision-request").write_text('#!/bin/bash\nprintf page >> "$CALLS"\n')
            for code in (1, 3, 10):
                with self.subTest(code=code):
                    result = subprocess.run(["/bin/bash", str(here / "stg-approval-request"), "PROJ-1389"],
                                            capture_output=True, text=True,
                                            env={**os.environ, "CALLS": str(log), "CHECK_CODE": str(code)}, timeout=10)
                    self.assertEqual(result.returncode, code)
                    self.assertEqual(log.read_text(), "check")
                    self.assertIn("human-risk", result.stderr)
            result = subprocess.run(["/bin/bash", str(here / "stg-approval-request"), "PROJ-1389"],
                                    capture_output=True, text=True,
                                    env={**os.environ, "CALLS": str(log), "CHECK_CODE": "0"}, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(log.read_text(), "checkpage")


if __name__ == "__main__":
    unittest.main()
