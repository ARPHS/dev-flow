import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins/dev-flow/skills/df-verify/scripts/evidence.py"
sys.path.insert(0, str(CLI.parent))
sys.dont_write_bytecode = True  # keep __pycache__ out of the plugin dir (local installs copy it)
import evidence  # noqa: E402



def py(code):
    """argv for `evidence.py run`: -- <python> -c <code>"""
    return ["--", sys.executable, "-c", code]


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def cli(cwd, *args):
    return subprocess.run([sys.executable, str(CLI), *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8")


def object_count(repo):
    return sum(1 for p in (repo / ".git/objects").rglob("*") if p.is_file())


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / ".dev-flow").mkdir()
        (self.repo / ".dev-flow/.gitignore").write_text("*\n", encoding="utf-8")  # what df-flow creates

    def tearDown(self):
        self.tmp.cleanup()

    def fp(self):
        return evidence.fingerprint(self.repo)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # ---- snapshot ----

    def test_fingerprint_tracks_code_and_ignores_state_and_ignored_files(self):
        first = self.fp()
        self.assertEqual(self.fp(), first, "same tree must give same id")
        self.write(".dev-flow/notes.md", "state")
        self.write("ignored/cache.bin", "cache")
        self.write("__pycache__/a.cpython-313.pyc", "pyc")  # not gitignored, yet rewritten by every run
        self.write("pkg/__pycache__/b.pyc", "pyc")
        self.write(".pytest_cache/v/cache/lastfailed", "{}")
        self.assertEqual(self.fp(), first)
        self.write("a.py", "x = 2\n")
        modified = self.fp()
        self.assertNotEqual(modified, first)
        self.write("b[1].py", "y = 1\n")  # glob characters must be taken literally
        self.assertNotEqual(self.fp(), modified)

    def test_racily_clean_edit_is_detected(self):
        # Same-size edit whose mtime equals the index mtime: git must rehash it ("racy git").
        # Copying the index without its mtime would make the stale entry look clean.
        target, index = self.repo / "a.py", self.repo / ".git/index"
        stamp = target.stat().st_mtime - 100
        self.write("a.py", "x = 5\n")
        os.utime(target, (stamp, stamp))
        git(self.repo, "add", "a.py")
        os.utime(index, (stamp, stamp))
        self.write("a.py", "x = 6\n")
        os.utime(target, (stamp, stamp))
        store = evidence.object_store(self.repo)
        tree, _ = evidence.snapshot(self.repo, store)
        content = evidence.git(["cat-file", "-p", f"{tree}:a.py"], self.repo, evidence.object_env(self.repo, store))
        self.assertIn(b"x = 6", content)

    def test_commit_without_content_change_keeps_fingerprint(self):
        self.write("a.py", "x = 2\n")
        before = self.fp()
        git(self.repo, "commit", "-qam", "change")
        self.assertEqual(self.fp(), before)

    def test_project_git_objects_and_index_untouched(self):
        self.write("a.py", "x = 3\n")
        self.write("new.py", "z = 1\n")
        count, status = object_count(self.repo), git(self.repo, "status", "--porcelain")
        cli(self.repo, "run", *py("print(1)"))
        cli(self.repo, "report")
        self.assertEqual(object_count(self.repo), count)
        self.assertEqual(git(self.repo, "status", "--porcelain"), status)
        self.assertTrue(any((self.repo / ".dev-flow/objects").rglob("*")))

    def test_no_state_dir_leaves_nothing_behind(self):
        shutil.rmtree(self.repo / ".dev-flow")
        self.write("a.py", "x = 4\n")
        count = object_count(self.repo)
        proc = cli(self.repo, "run", *py("print(1)"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("未记录", proc.stdout)
        fp = cli(self.repo, "fingerprint")
        self.assertEqual(fp.returncode, 1)
        self.assertIn("没有 .dev-flow/", fp.stderr)
        self.assertFalse((self.repo / ".dev-flow").exists())
        self.assertEqual(object_count(self.repo), count)

    def test_writes_only_inside_state_dir(self):
        # Codex's Windows sandbox forbids writing to %TEMP%: nothing may be created there.
        self.write("a.py", "x = 13\n")
        with tempfile.TemporaryDirectory() as fake_tmp:
            env = {**os.environ, "TEMP": fake_tmp, "TMP": fake_tmp, "TMPDIR": fake_tmp}
            for args in (["run", *py("print(1)")], ["report"], ["fingerprint"]):
                proc = subprocess.run([sys.executable, str(CLI), *args], cwd=self.repo, env=env,
                                      capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(os.listdir(fake_tmp), [])
        leftovers = [p.name for p in (self.repo / ".dev-flow").iterdir() if p.name.startswith("index-")]
        self.assertEqual(leftovers, [])

    def test_repo_without_commits(self):
        repo = Path(self.tmp.name) / "fresh"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".dev-flow").mkdir()
        (repo / "main.py").write_text("print(1)\n", encoding="utf-8")
        first = evidence.fingerprint(repo)  # no index file at all
        (repo / "main.py").write_text("print(2)\n", encoding="utf-8")
        self.assertNotEqual(evidence.fingerprint(repo), first)
        git(repo, "add", "main.py")
        git(repo, "rm", "-q", "--cached", "main.py")  # index file exists but has no entries
        self.assertNotEqual(evidence.fingerprint(repo), first)

    def test_dubious_ownership_explained(self):
        # Codex's elevated sandbox runs commands as another Windows user than the repo owner
        error = evidence.GitError("fatal: detected dubious ownership in repository at 'C:/repo'")
        with mock.patch.object(evidence, "repo_root", side_effect=error):
            code, _, record, notes = evidence.run(py("print(1)")[1:], cwd=self.repo)
            self.assertEqual((code, record), (0, None))
            self.assertIn("safe.directory", notes[0])
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(evidence.main(["report"]), 1)
            self.assertIn("GIT_CONFIG_KEY_0=safe.directory", err.getvalue())

    def test_large_untracked_files_left_out(self):
        with mock.patch.object(evidence, "LARGE_FILE", 10):
            base = self.fp()
            self.write("data.bin", "x" * 100)
            tree, large = evidence.snapshot(self.repo, evidence.object_store(self.repo))
        self.assertEqual(tree[:evidence.SHORT], base)
        self.assertEqual(large, ["data.bin"])

    # ---- run / report ----

    def test_run_records_exit_code_and_passes_it_through(self):
        ok = cli(self.repo, "run", "-l", "AC1", *py("print('12 passed')"))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("12 passed", ok.stdout)
        bad = cli(self.repo, "run", *py("import sys; print('boom'); sys.exit(3)"))
        self.assertEqual(bad.returncode, 3)
        records = evidence.load_records(self.repo)
        self.assertEqual([r["exit"] for r in records], [0, 3])
        self.assertEqual(records[0]["label"], "AC1")
        self.assertEqual(records[0]["snapshot"][:evidence.SHORT], self.fp())
        self.assertIn("boom", records[1]["tail"])

    def test_report_lists_changes_since_each_record(self):
        cli(self.repo, "run", "-l", "unit", *py("print('ok')"))
        self.assertIn("当前有效", cli(self.repo, "report").stdout)
        self.write("ledger.json", "[]")
        out = cli(self.repo, "report").stdout
        self.assertIn("之后改动：ledger.json", out)
        cli(self.repo, "run", "-l", "unit", *py("print('ok')"))
        out = cli(self.repo, "report").stdout
        self.assertEqual(out.count("| unit |"), 1, "only the latest run of a command is listed")
        self.assertIn("当前有效", out)

    def test_runs_program_directly_without_shell(self):
        # Arguments arrive exactly as the caller's shell split them: no second shell parse.
        proc = cli(self.repo, "run", *py("import sys; print(sys.argv[1:])"), "a b", "$HOME", "x&&y")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("['a b', '$HOME', 'x&&y']", proc.stdout)

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_shell_features_via_explicit_wrapper(self):
        proc = cli(self.repo, "run", "--", "bash", "-c", "FOO=1 && echo \"foo=$FOO\" | tr a-z A-Z")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FOO=1", proc.stdout)

    @unittest.skipUnless(os.name == "nt", "PATHEXT resolution is Windows-only")
    def test_windows_cmd_wrappers_resolve(self):
        script = self.repo / "tool.cmd"
        script.write_text("@echo from-cmd\r\n", encoding="utf-8")
        env_path = f"{self.repo}{os.pathsep}{os.environ['PATH']}"
        with mock.patch.dict(os.environ, {"PATH": env_path}):
            code, output, _, _ = evidence.run(["tool"], cwd=self.repo)
        self.assertEqual(code, 0, output)
        self.assertIn("from-cmd", output)

    def test_whole_command_in_one_argument_is_rejected(self):
        proc = cli(self.repo, "run", "--", "python -m pytest -q")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("去掉外层引号", proc.stderr)
        self.assertEqual(evidence.load_records(self.repo), [])

    def test_missing_program_is_not_recorded(self):
        proc = cli(self.repo, "run", "--", "definitely-not-a-program-xyz")
        self.assertEqual(proc.returncode, 127)
        self.assertEqual(evidence.load_records(self.repo), [])

    def test_baseline_kept_apart_from_later_run_of_same_command(self):
        cmd = py("print('ok')")
        cli(self.repo, "run", "-l", "baseline", *cmd)
        self.write("a.py", "x = 12\n")
        cli(self.repo, "run", "-l", "AC1", *cmd)
        out = cli(self.repo, "report").stdout
        self.assertIn("| baseline |", out)
        self.assertIn("之后改动：a.py", out)
        self.assertIn("| AC1 |", out)

    def test_report_without_records_fails(self):
        self.assertEqual(cli(self.repo, "report").returncode, 1)

    def test_cleared_objects_mark_records_uncomparable(self):
        self.write("a.py", "x = 8\n")  # snapshot content that exists only in .dev-flow/objects
        cli(self.repo, "run", *py("print(1)"))
        evidence.remove_tree(self.repo / ".dev-flow/objects")
        self.write("a.py", "x = 9\n")
        self.assertIn("无法对比", cli(self.repo, "report").stdout)

    def test_objects_cap_clears_store(self):
        self.write("a.py", "x = 7\n")
        evidence.run(py("print(1)")[1:], cwd=self.repo)
        self.write("a.py", "x = 10\n")
        with mock.patch.object(evidence, "OBJECTS_CAP", 0):
            _, _, record, notes = evidence.run(py("print(1)")[1:], cwd=self.repo)
        self.assertTrue(record)
        self.assertTrue(any("已清空" in n for n in notes), notes)
        old = evidence.load_records(self.repo)[0]["snapshot"]
        with self.assertRaises(evidence.GitError):  # old snapshot objects are really gone
            evidence.git(["cat-file", "-e", old], self.repo, evidence.object_env(self.repo, self.repo / ".dev-flow/objects"))

    def test_worktree(self):
        other = Path(self.tmp.name) / "wt"
        git(self.repo, "worktree", "add", "-q", str(other), "-b", "wt")
        (other / ".dev-flow").mkdir()
        (other / "a.py").write_text("x = 11\n", encoding="utf-8")
        proc = cli(other, "run", *py("print(1)"))
        self.assertIn("已记录", proc.stdout, proc.stderr)
        self.assertIn("当前有效", cli(other, "report").stdout)

    def test_single_arg_executable_path_with_spaces_allowed(self):
        spaced_dir = Path(self.tmp.name) / "dir with spaces"
        spaced_dir.mkdir()
        if os.name == "nt":
            script = spaced_dir / "run tool.cmd"
            script.write_text("@echo spaced-ok\r\n", encoding="utf-8")
        else:
            script = spaced_dir / "run tool.sh"
            script.write_text("#!/bin/sh\necho spaced-ok\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        proc = cli(self.repo, "run", "-l", "spaced", "--", str(script))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("spaced-ok", proc.stdout)
        records = evidence.load_records(self.repo)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["exit"], 0)

    def test_chinese_and_space_filenames_in_report(self):
        cli(self.repo, "run", "-l", "unit", *py("print('ok')"))
        self.write("中文 目录/测试 文件.py", "x = 1\n")
        self.write("数据[1].json", "{}\n")
        md = cli(self.repo, "report").stdout
        self.assertIn("中文 目录/测试 文件.py", md)
        self.assertIn("数据[1].json", md)
        self.assertNotIn("\\344", md)
        raw_json = cli(self.repo, "report", "--json")
        self.assertEqual(raw_json.returncode, 0, raw_json.stderr)
        data = json.loads(raw_json.stdout)
        self.assertEqual(data["records"][0]["status"], "changed")
        self.assertIn("中文 目录/测试 文件.py", data["records"][0]["changes"])

    def test_report_json_structure_and_valid_status(self):
        cli(self.repo, "run", "-l", "AC1", *py("print('all good')"))
        proc = cli(self.repo, "report", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data["records"]), 1)
        rec = data["records"][0]
        self.assertEqual(rec["label"], "AC1")
        self.assertEqual(rec["exit"], 0)
        self.assertEqual(rec["status"], "valid")
        self.assertEqual(rec["changes"], [])
        self.assertIsNotNone(rec["duration_s"])
        self.assertEqual(rec["argv"], [sys.executable, "-c", "print('all good')"])

    def test_snapshot_diff_is_readonly_and_separates_existing_edits(self):
        self.write("a.py", "user_change = 2\n")
        before = self.fp()
        self.write("a.py", "user_change = 2\ntask_change = 3\n")
        self.write("new.py", "task_added = True\n")
        after = self.fp()
        index = (self.repo / ".git/index").read_bytes()
        count = object_count(self.repo)
        objects = self.repo / ".dev-flow/objects"
        saved = {path.relative_to(objects).as_posix(): path.read_bytes()
                 for path in objects.rglob("*") if path.is_file()}
        proc = cli(self.repo, "diff", before, after)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("+task_change = 3", proc.stdout)
        self.assertIn("+task_added = True", proc.stdout)
        self.assertNotIn("+user_change", proc.stdout)
        self.assertNotIn("-user_change", proc.stdout)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(object_count(self.repo), count)
        self.assertEqual({path.relative_to(objects).as_posix(): path.read_bytes()
                          for path in objects.rglob("*") if path.is_file()}, saved)

    def test_snapshot_diff_without_objects_does_not_create_state(self):
        shutil.rmtree(self.repo / ".dev-flow")
        proc = cli(self.repo, "diff", "0" * 40, "1" * 40)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse((self.repo / ".dev-flow").exists())

    def test_huge_output_summarized_and_saved_to_log(self):
        lines_code = "for i in range(400): print(f'line-{i:04d}-' + 'x' * 40)"
        proc = cli(self.repo, "run", "-l", "verbose", *py(lines_code))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已省略中间", proc.stdout)
        self.assertIn(".dev-flow/logs/", proc.stdout)
        records = evidence.load_records(self.repo)
        log_rel = records[-1]["log"]
        full_text = (self.repo / log_rel).read_text(encoding="utf-8")
        self.assertIn("line-0200-", full_text)
        # --full-output bypasses truncation
        full_proc = cli(self.repo, "run", "--full-output", "-l", "verbose2", *py(lines_code))
        self.assertEqual(full_proc.returncode, 0, full_proc.stdout)
        self.assertIn("line-0200-", full_proc.stdout)
        self.assertNotIn("已省略中间", full_proc.stdout)
        # Without .dev-flow/ (no saved log), stdout must NOT be truncated
        shutil.rmtree(self.repo / ".dev-flow")
        no_state_proc = cli(self.repo, "run", "-l", "verbose3", *py(lines_code))
        self.assertEqual(no_state_proc.returncode, 0, no_state_proc.stderr)
        self.assertIn("line-0200-", no_state_proc.stdout)
        self.assertNotIn("已省略中间", no_state_proc.stdout)

    def test_huge_output_preserved_when_log_save_fails(self):
        lines_code = "for i in range(400): print(f'line-{i:04d}-' + 'x' * 40)"
        old_cwd = os.getcwd()
        try:
            os.chdir(self.repo)
            with mock.patch.object(evidence, "save_log", side_effect=OSError("disk full")), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = evidence.main(["run", "-l", "verbose", *py(lines_code)])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, 0)
        self.assertIn("line-0200-", out.getvalue())
        self.assertNotIn("已省略中间", out.getvalue())

    def test_doctor_fails_when_git_status_fails(self):
        # Corrupt .git/index so repo_root succeeds but git status fails
        (self.repo / ".git/index").write_bytes(b"corrupted-index")
        proc = cli(self.repo, "doctor", "--json")
        self.assertEqual(proc.returncode, 1)
        info = json.loads(proc.stdout)
        self.assertFalse(info["ok"])
        repo_check = next(c for c in info["checks"] if c["name"] == "git_repo")
        self.assertFalse(repo_check["ok"])

    def test_cross_stack_cache_files_ignored(self):
        base = self.fp()
        self.write(".coverage", "binary")
        self.write("web/.vitest/cache.json", "{}")
        self.write("web/tsconfig.tsbuildinfo", "{}")
        self.assertEqual(self.fp(), base)

    def test_doctor_readonly_diagnostics(self):
        proc = cli(self.repo, "doctor", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = json.loads(proc.stdout)
        self.assertTrue(info["ok"])
        self.assertTrue(info["state"]["present"])
        self.assertTrue(info["state"]["gitignore_ok"])
        # Without .dev-flow/, doctor must stay strictly read-only and never create it
        shutil.rmtree(self.repo / ".dev-flow")
        proc2 = cli(self.repo, "doctor")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("dev-flow 环境诊断", proc2.stdout)
        self.assertFalse((self.repo / ".dev-flow").exists())

    def test_relative_cwd_in_subdirectory(self):
        sub = self.repo / "pkg" / "sub"
        sub.mkdir(parents=True)
        proc = cli(sub, "run", "-l", "sub", *py("print('ok')"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = evidence.load_records(self.repo)
        self.assertEqual(records[-1]["cwd"], "pkg/sub")

    def test_doctor_flags_missing_state_gitignore_and_allows_shared_ledger(self):
        (self.repo / ".dev-flow/.gitignore").unlink()
        proc = cli(self.repo, "doctor", "--json")
        self.assertEqual(proc.returncode, 1)
        info = json.loads(proc.stdout)
        self.assertFalse(info["ok"])
        state_check = next(c for c in info["checks"] if c["name"] == "state_dir")
        self.assertFalse(state_check["ok"])
        self.assertEqual(set(info["state"]["unignored"]), {"objects/", "evidence.jsonl", "logs/", "index-*"})
        self.assertTrue(any(".gitignore" in tip for tip in info["tips"]))

        # Shared-ledger ignore config documented in state.md must pass doctor
        (self.repo / ".dev-flow/.gitignore").write_text(
            "objects/\nevidence.jsonl\nlogs/\nindex-*\n", encoding="utf-8",
        )
        shared_proc = cli(self.repo, "doctor", "--json")
        self.assertEqual(shared_proc.returncode, 0, shared_proc.stdout + shared_proc.stderr)
        shared_info = json.loads(shared_proc.stdout)
        self.assertTrue(shared_info["ok"])
        self.assertTrue(shared_info["state"]["gitignore_ok"])
        self.assertEqual(shared_info["state"]["unignored"], [])

        # Missing `logs/` in shared-ledger rules must be flagged specifically
        (self.repo / ".dev-flow/.gitignore").write_text("objects/\nevidence.jsonl\nindex-*\n", encoding="utf-8")
        partial_proc = cli(self.repo, "doctor", "--json")
        self.assertEqual(partial_proc.returncode, 1)
        partial_info = json.loads(partial_proc.stdout)
        self.assertEqual(partial_info["state"]["unignored"], ["logs/"])

        # Repo-level .gitignore ignoring .dev-flow/ also satisfies Git ignore behavior
        (self.repo / ".dev-flow/.gitignore").unlink()
        (self.repo / ".gitignore").write_text("ignored/\n.dev-flow/\n", encoding="utf-8")
        root_gi = cli(self.repo, "doctor", "--json")
        self.assertEqual(root_gi.returncode, 0, root_gi.stdout + root_gi.stderr)
        self.assertTrue(json.loads(root_gi.stdout)["ok"])

    def test_summarize_output_caps_wide_lines_and_prunes_superseded_logs_first(self):
        wide = ("X" * 20000 + "\n") + "\n".join(f"line-{i}" for i in range(80)) + "\n"
        summary = evidence.summarize_output(wide, 0, log_rel=".dev-flow/logs/wide.log")
        self.assertLess(len(summary), evidence.MAX_STDOUT_CHARS)
        self.assertIn(".dev-flow/logs/wide.log", summary)

        # Two-tier pruning: superseded logs are dropped before logs referenced by latest records
        big_code = "print('x' * 13000)"
        code1, _, rec1, _ = evidence.run([sys.executable, "-c", big_code], label="AC1", cwd=self.repo)
        self.assertEqual(code1, 0)
        code2, _, rec2, _ = evidence.run([sys.executable, "-c", big_code], label="AC2", cwd=self.repo)
        self.assertEqual(code2, 0)
        # Re-run AC2 so rec2's log becomes superseded, and make AC1 older than rec2 to verify tier priority
        os.utime(self.repo / rec1["log"], (1000, 1000))
        os.utime(self.repo / rec2["log"], (2000, 2000))
        ac1_size = (self.repo / rec1["log"]).stat().st_size
        with mock.patch.object(evidence, "LOGS_CAP", ac1_size * 2 + 500):
            code3, _, rec3, notes3 = evidence.run([sys.executable, "-c", big_code], label="AC2", cwd=self.repo)
        self.assertEqual(code3, 0)
        self.assertTrue((self.repo / rec1["log"]).is_file(), "active latest log should survive while superseded log is dropped")
        self.assertFalse((self.repo / rec2["log"]).exists(), "superseded log should be pruned first")
        self.assertTrue((self.repo / rec3["log"]).is_file())
        self.assertFalse(any("完整日志" in n for n in notes3), notes3)

        # When cap forces pruning an active latest log, note it and mark report entry as pruned (no dangling path)
        with mock.patch.object(evidence, "LOGS_CAP", ac1_size + 500):
            code4, _, rec4, notes4 = evidence.run([sys.executable, "-c", big_code], label="AC3", cwd=self.repo)
        self.assertEqual(code4, 0)
        self.assertFalse((self.repo / rec1["log"]).exists())
        self.assertTrue((self.repo / rec4["log"]).is_file())
        self.assertTrue(any("已清理" in n and "完整日志" in n for n in notes4), notes4)

        data = evidence.report_data(self.repo)
        by_label = {r["label"]: r for r in data["records"]}
        self.assertIsNone(by_label["AC1"]["log"])
        self.assertEqual(by_label["AC1"]["log_status"], "pruned")
        self.assertEqual(by_label["AC3"]["log_status"], "present")
        self.assertEqual(by_label["AC3"]["log"], rec4["log"])
        md, ok = evidence.report(self.repo)
        self.assertTrue(ok)
        self.assertIn("完整日志已清理", md)


if __name__ == "__main__":
    unittest.main()
