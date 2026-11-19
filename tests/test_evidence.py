import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
