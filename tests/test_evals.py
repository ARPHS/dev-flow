"""Check that eval fixtures and graders discriminate: untouched fixtures fail,
reference solutions pass. This runs without any agent and costs nothing."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
sys.path.insert(0, str(EVALS))
import lib  # noqa: E402
from lib import FIXTURES, Context  # noqa: E402


def git(ws, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=ws,
                          capture_output=True, text=True, check=True).stdout.strip()


def load_grader(name):
    spec = importlib.util.spec_from_file_location(f"grade_{name}", EVALS / "cases" / name / "grade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.grade


def fake_transcript(*tools, result="完成，退出码 0"):
    lines = []
    for name, data in tools:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": data}]}}, ensure_ascii=False))
    lines.append(json.dumps({"type": "result", "result": result}, ensure_ascii=False))
    return "\n".join(lines)


def required_failures(checks):
    return [c.name for c in checks if c.required and not c.ok]


class EvalCasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def workspace(self, fixture):
        ws = Path(tempfile.mkdtemp(dir=self.tmp.name)) / fixture
        shutil.copytree(FIXTURES / fixture, ws, ignore=shutil.ignore_patterns("__pycache__"))
        git(ws, "init", "-q")
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", "fixture")
        return ws, git(ws, "rev-parse", "HEAD")

    def context(self, fixture, ws, base, *transcripts):
        snaps = [sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*")
                        if p.is_file() and ".git" not in p.relative_to(ws).parts)]
        return Context(ws=ws, fixture=FIXTURES / fixture, base=base, transcripts=list(transcripts),
                       snapshots=snaps * max(1, len(transcripts)))

    def test_cases_well_formed(self):
        for case_dir in (EVALS / "cases").iterdir():
            case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
            self.assertTrue((FIXTURES / case["fixture"]).is_dir(), case_dir.name)
            self.assertTrue(case["turns"] and case["rubric"] and case["description"])
            self.assertTrue(callable(load_grader(case_dir.name)))

    def test_fixture_tests_pass(self):
        for fixture in ("ledger", "orders"):
            ws, _ = self.workspace(fixture)
            proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                  cwd=ws, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_untouched_fixtures_fail(self):
        for name, fixture in (("quick", "ledger"), ("feature", "ledger"), ("debug", "orders")):
            ws, base = self.workspace(fixture)
            checks = load_grader(name)(self.context(fixture, ws, base, fake_transcript()))
            self.assertTrue(required_failures(checks), f"{name} grader passes on untouched fixture")

    def test_quick_reference_passes(self):
        ws, base = self.workspace("ledger")
        readme = ws / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8").replace("recieve", "receive"), encoding="utf-8")
        checks = load_grader("quick")(self.context("ledger", ws, base, fake_transcript()))
        self.assertEqual(required_failures(checks), [])

    def test_feature_reference_passes(self):
        ws, base = self.workspace("ledger")
        cli = ws / "ledger/cli.py"
        text = cli.read_text(encoding="utf-8")
        text = text.replace('sub.add_parser("list", help="列出所有记录")',
                            'lst = sub.add_parser("list", help="列出所有记录")\n'
                            '    lst.add_argument("--json", action="store_true")')
        text = text.replace("        entries = store.load()\n",
                            "        entries = store.load()\n"
                            "        if args.json:\n"
                            "            import json\n"
                            "            print(json.dumps(entries, ensure_ascii=False))\n"
                            "            return 0\n")
        cli.write_text(text, encoding="utf-8")
        test = ws / "tests/test_cli.py"
        test.write_text(test.read_text(encoding="utf-8").replace(
            "\n\nif __name__",
            "\n    def test_list_json(self):\n        code, out = self.run_cli('list', '--json')\n"
            "        self.assertEqual(out.strip(), '[]')\n\n\nif __name__"), encoding="utf-8")
        transcript = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}),
                                     ("Edit", {"file_path": "ledger/cli.py"}),
                                     ("Bash", {"command": "python -m ledger list --json"}))
        checks = load_grader("feature")(self.context("ledger", ws, base, transcript))
        self.assertEqual(required_failures(checks), [])

    def test_debug_reference_passes(self):
        ws, base = self.workspace("orders")
        export = ws / "orders/export.py"
        export.write_text(export.read_text(encoding="utf-8").replace(
            "pages = total // page_size", "pages = -(-total // page_size)"), encoding="utf-8")
        test = ws / "tests/test_export.py"
        test.write_text(test.read_text(encoding="utf-8").replace(
            "\n\nif __name__",
            "\n    def test_partial_last_page(self):\n"
            "        self.assertEqual(len(export_orders(OrderSource(make_orders(25)))), 25)\n\n\nif __name__"),
            encoding="utf-8")
        transcript = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}))
        checks = load_grader("debug")(self.context("orders", ws, base, transcript))
        self.assertEqual(required_failures(checks), [])

    def test_debug_special_case_fails(self):
        ws, base = self.workspace("orders")
        export = ws / "orders/export.py"
        export.write_text(export.read_text(encoding="utf-8").replace(
            "pages = total // page_size", "pages = total // page_size + (1 if total == 25 else 0)"),
            encoding="utf-8")
        checks = load_grader("debug")(self.context("orders", ws, base, fake_transcript()))
        self.assertIn("隐藏用例：各种数量和页大小都完整导出", required_failures(checks))

    def test_project_first_turn_code_fails(self):
        ws, base = self.workspace("empty")
        (ws / "app.py").write_text("print(1)\n", encoding="utf-8")
        ctx = self.context("empty", ws, base, fake_transcript(), fake_transcript())
        checks = load_grader("project")(ctx)
        self.assertIn("第一轮停顿：未写实现代码", required_failures(checks))

    def test_codex_transcript_translated(self):
        def item(**data):
            return json.dumps({"type": "item.completed", "item": data}, ensure_ascii=False)
        pwsh = '"C:\\\\Program Files\\\\PowerShell\\\\7\\\\pwsh.exe" -Command '
        transcript = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            item(type="agent_message", text="先看技能"),
            item(type="command_execution", exit_code=0,
                 command=pwsh + "\"Get-Content -Raw 'C:/x/plugins/cache/dev-flow/dev-flow/0.3.0/skills/df-flow/SKILL.md'\""),
            item(type="command_execution", command=pwsh + "'python -m unittest discover -s tests'", exit_code=0),
            item(type="file_change", changes=[{"path": "C:/ws/ledger/cli.py", "kind": "update"}]),
            item(type="collab_tool_call", tool="spawn_agent", arguments={"message": "df-reviewer 审查"}),
            item(type="agent_message", text="完成，退出码 0"),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ])
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[transcript])
        self.assertEqual(ctx.skills_invoked(), ["dev-flow:df-flow"])
        self.assertTrue(ctx.ran(r"unittest"))
        self.assertEqual(ctx.final_text(), "完成，退出码 0")
        self.assertIn("df-reviewer", ctx.subagents()[0])
        dispatched, waited = lib.independent_review(ctx)
        self.assertEqual((dispatched.ok, waited.ok), (True, False))  # spawned but never waited
        test_index = ctx.first_index(lambda n, d: n == "Bash" and "unittest" in d["command"])
        edit_index = ctx.first_index(lambda n, d: n == "Edit")
        self.assertLess(test_index, edit_index)

    def test_evidence_calls_through_alias(self):
        def bash(command):
            return json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}})
        transcript = "\n".join([
            bash('EV="python /p/df-verify/scripts/evidence.py" && $EV run -l test -- python -m unittest'),
            bash("python /p/df-verify/scripts/evidence.py report"),
            bash("$EV run -l x -- true"),  # alias alone does not prove the evidence CLI ran
        ])
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[transcript])
        runs, reports = lib.evidence_recorded(ctx)
        self.assertEqual((runs.ok, runs.detail, reports.ok, reports.detail), (True, "1 次", True, "1 次"))


class RunTurnTests(unittest.TestCase):
    """The runner must not hang on CLIs whose children outlive the turn."""

    # parent prints a finished turn, leaves a grandchild holding stdout, then lingers
    LINGER = ("import subprocess, sys, time; "
              f"print(sys.stdin.read().strip()); print({json.dumps({'type': 'turn.completed'})!r}, flush=True); "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")

    def setUp(self):
        spec = importlib.util.spec_from_file_location("eval_run", EVALS / "run.py")
        self.run_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.run_module)

    def test_killed_after_turn_completed(self):
        start = time.time()
        out, err = self.run_module.run_turn([sys.executable, "-c", self.LINGER], ".", "hi", timeout=50, grace=1)
        self.assertLess(time.time() - start, 30)
        self.assertIn("turn.completed", out)
        self.assertTrue(out.startswith("hi"))
        self.assertIn("killed", err)

    def test_timeout_kills_tree(self):
        start = time.time()
        _, err = self.run_module.run_turn([sys.executable, "-c", "import time; time.sleep(60)"], ".", "", timeout=2)
        self.assertLess(time.time() - start, 30)
        self.assertIn("timeout after 2s", err)


if __name__ == "__main__":
    unittest.main()
