"""Check that eval fixtures, graders, and runner integrity checks discriminate:
untouched fixtures fail, reference solutions pass, timeouts/unverified claims fail,
and plugin vs --no-plugin compliance gating works cleanly. Runs without any agent."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
EVIDENCE_CLI = ROOT / "plugins/dev-flow/skills/df-verify/scripts/evidence.py"
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


def load_run_module():
    spec = importlib.util.spec_from_file_location("eval_run", EVALS / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_transcript(*tools, result="完成，退出码 0", include_end=True):
    lines = []
    for idx, entry in enumerate(tools):
        name, data = entry[:2]
        is_error = entry[2] if len(entry) > 2 else False
        content = entry[3] if len(entry) > 3 else ("err" if is_error else "ok")
        tid = f"t{idx}"
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": data}]}}, ensure_ascii=False))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": content}]}},
            ensure_ascii=False))
    if include_end:
        lines.append(json.dumps({"type": "result", "result": result}, ensure_ascii=False))
    return "\n".join(lines)


def required_failures(checks):
    return [c.name for c in checks if c.required and not c.ok]


def record_evidence(ws, label, *cmd, env=None):
    (ws / ".dev-flow").mkdir(exist_ok=True)
    (ws / ".dev-flow/.gitignore").write_text("*\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(EVIDENCE_CLI), "run", "-l", label, "--", *cmd],
                          cwd=ws, capture_output=True, text=True, encoding="utf-8", env={**os.environ, **(env or {})})
    assert proc.returncode == 0, proc.stdout + proc.stderr


class EvalCasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_module = load_run_module()

    def tearDown(self):
        self.tmp.cleanup()

    def workspace(self, fixture):
        ws = Path(tempfile.mkdtemp(dir=self.tmp.name)) / fixture
        shutil.copytree(FIXTURES / fixture, ws, ignore=shutil.ignore_patterns("__pycache__"))
        git(ws, "init", "-q")
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", "fixture")
        return ws, git(ws, "rev-parse", "HEAD")

    def context(self, fixture, ws, base, *transcripts, plugin=False, snapshots=None):
        snaps = snapshots or ([sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*")
                                      if p.is_file() and ".git" not in p.relative_to(ws).parts)]
                              * max(1, len(transcripts)))
        return Context(ws=ws, fixture=FIXTURES / fixture, base=base, transcripts=list(transcripts),
                       snapshots=snaps, plugin=plugin)

    def _apply_feature_solution(self, ws):
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

    def _apply_project_solution(self, ws):
        shutil.copytree(FIXTURES / "ledger" / "ledger", ws / "ledger",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(FIXTURES / "ledger" / "tests", ws / "tests",
                        ignore=shutil.ignore_patterns("__pycache__"))

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

    def test_feature_reference_passes_no_plugin_and_requires_compliance_with_plugin(self):
        ws, base = self.workspace("ledger")
        record_evidence(ws, "baseline", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        self._apply_feature_solution(ws)
        transcript = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}),
                                     ("Edit", {"file_path": "ledger/cli.py"}),
                                     ("Bash", {"command": "python -m ledger list --json"}))
        # Baseline (--no-plugin): functional checks pass; plugin-only compliance is not required
        no_plugin_checks = load_grader("feature")(self.context("ledger", ws, base, transcript, plugin=False))
        self.assertEqual(required_failures(no_plugin_checks), [])
        # Plugin mode without evidence.py must fail compliance
        plugin_missing = load_grader("feature")(self.context("ledger", ws, base, transcript, plugin=True))
        self.assertIn("用 evidence.py run 记录验证", required_failures(plugin_missing))
        # Plugin mode WITH real on-disk evidence passes all required checks
        record_evidence(ws, "unit", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        record_evidence(ws, "json", sys.executable, "-m", "ledger", "list", "--json")
        plugin_transcript = fake_transcript(
            ("Bash", {"command": "python evidence.py run -l baseline -- python -m unittest discover -s tests"}),
            ("Edit", {"file_path": "ledger/cli.py"}),
            ("Bash", {"command": "python evidence.py run -l unit -- python -m unittest discover -s tests"}),
            ("Bash", {"command": "python evidence.py run -l json -- python -m ledger list --json"}),
            ("Bash", {"command": "python evidence.py report"}),
        )
        plugin_ok = load_grader("feature")(self.context("ledger", ws, base, plugin_transcript, plugin=True))
        self.assertEqual(required_failures(plugin_ok), [])

    def test_feature_strengthened_assert_vs_skipped_test(self):
        ws, base = self.workspace("ledger")
        self._apply_feature_solution(ws)
        test_path = ws / "tests/test_cli.py"
        # Strengthening assertIn -> assertEqual: functional compatibility passes, advisory notes diff
        test_path.write_text(
            test_path.read_text(encoding="utf-8").replace('self.assertIn("暂无记录", out)',
                                                         'self.assertEqual(out.strip(), "暂无记录")'),
            encoding="utf-8",
        )
        transcript = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}),
                                     ("Edit", {"file_path": "ledger/cli.py"}),
                                     ("Bash", {"command": "python -m ledger list --json"}))
        checks = load_grader("feature")(self.context("ledger", ws, base, transcript, plugin=False))
        self.assertEqual(required_failures(checks), [])
        advisory = next(c for c in checks if c.name == "原有断言行未改动")
        self.assertFalse(advisory.ok)
        self.assertFalse(advisory.required)

        # Breaking production behavior and hiding it behind @unittest.skip MUST be caught
        cli = ws / "ledger/cli.py"
        cli.write_text(cli.read_text(encoding="utf-8").replace("暂无记录", "BROKEN"), encoding="utf-8")
        test_path.write_text(
            test_path.read_text(encoding="utf-8").replace("    def test_list_empty",
                                                         "    @unittest.skip('cheat')\n    def test_list_empty"),
            encoding="utf-8",
        )
        cheated = load_grader("feature")(self.context("ledger", ws, base, transcript, plugin=False))
        self.assertIn("原始夹具测试兼容通过", required_failures(cheated))

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
        checks = load_grader("debug")(self.context("orders", ws, base, transcript, plugin=False))
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

    def test_project_broken_tests_fail_and_valid_solution_passes(self):
        ws, base = self.workspace("empty")
        first_snap = self.run_module.snapshot(ws)
        self._apply_project_solution(ws)
        test = ws / "tests/test_cli.py"
        passing_tests = test.read_text(encoding="utf-8")
        test.write_text(
            "import unittest\nclass TestCLI(unittest.TestCase):\n"
            "    def test_fail(self): self.assertEqual(1, 2)\n", encoding="utf-8",
        )
        final_snap = self.run_module.snapshot(ws)
        t2 = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}),
                             ("Bash", {"command": "python -m ledger list"}))
        bad_ctx = self.context("empty", ws, base, fake_transcript(), t2, plugin=False,
                               snapshots=[first_snap, final_snap])
        self.assertIn("项目测试实际运行并通过", required_failures(load_grader("project")(bad_ctx)))

        test.write_text(passing_tests, encoding="utf-8")
        ok_ctx = self.context("empty", ws, base, fake_transcript(), t2, plugin=False,
                              snapshots=[first_snap, final_snap])
        self.assertEqual(required_failures(load_grader("project")(ok_ctx)), [])

        test.write_text(
            "import unittest\nclass TestCLI(unittest.TestCase):\n"
            "    @unittest.skip('skipped')\n    def test_bad(self): self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        skipped_ctx = self.context("empty", ws, base, fake_transcript(), t2, plugin=False,
                                   snapshots=[first_snap, final_snap])
        self.assertIn("项目测试实际运行并通过", required_failures(load_grader("project")(skipped_ctx)))

        test.write_text(passing_tests, encoding="utf-8")
        t2_compile_only = fake_transcript(
            ("Bash", {"command": "python -m unittest discover -s tests"}),
            ("Bash", {"command": "python -m compileall ."}),
            ("Bash", {"command": "python --version"}),
        )
        static_only_ctx = self.context("empty", ws, base, fake_transcript(), t2_compile_only, plugin=False,
                                       snapshots=[first_snap, final_snap])
        self.assertIn("真实运行了 CLI 入口验证", required_failures(load_grader("project")(static_only_ctx)))

        # In plugin mode, must ALSO record valid evidence & dispatch independent review
        missing_compliance = load_grader("project")(
            self.context("empty", ws, base, fake_transcript(), t2, plugin=True, snapshots=[first_snap, final_snap])
        )
        self.assertIn("派发了独立审查子代理", required_failures(missing_compliance))
        self.assertIn("用 evidence.py run 记录验证", required_failures(missing_compliance))

    def test_timeout_and_missing_end_event_fail_grade_case(self):
        case_dir = Path(self.tmp.name) / "project_timeout"
        ws = case_dir / "workspace"
        case_dir.mkdir()
        shutil.copytree(FIXTURES / "empty", ws)
        git(ws, "init", "-q")
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", "fixture")
        base = git(ws, "rev-parse", "HEAD")
        first_snapshot = self.run_module.snapshot(ws)
        self._apply_project_solution(ws)
        (case_dir / "turn1.jsonl").write_text(fake_transcript(result="请确认方案"), encoding="utf-8")
        # Turn 2 timed out without turn.completed / result
        (case_dir / "turn2.jsonl").write_text(
            fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}),
                            ("Bash", {"command": "python -m ledger list"}), include_end=False),
            encoding="utf-8",
        )
        (case_dir / "turn2.stderr.txt").write_text("timeout after 600s\n", encoding="utf-8")
        meta = {
            "case": "project", "fixture": "empty", "base": base,
            "snapshots": [first_snapshot, self.run_module.snapshot(ws)],
            "expected_turns": 2,
            "stats": {"cost_usd": None, "agent_turns": 1, "seconds": 610.0, "tokens": None},
            "plugin": False, "model": None, "agent": "codex",
        }
        (case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        res = self.run_module.grade_case(case_dir)
        self.assertFalse(res["passed"])
        self.assertFalse(res["dimensions"]["execution"]["passed"])
        self.assertTrue(res["dimensions"]["functional"]["passed"])

    def test_error_result_event_and_abnormal_exit_fail_execution(self):
        # Claude error_max_turns returns type=result with is_error=True and exit_code=1
        ws, base = self.workspace("ledger")
        readme = ws / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8").replace("recieve", "receive"), encoding="utf-8")
        case_dir = Path(self.tmp.name) / "quick_max_turns"
        case_dir.mkdir()
        shutil.copytree(ws, case_dir / "workspace")
        error_result = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns", "result": ""})
        (case_dir / "turn1.jsonl").write_text(error_result + "\n", encoding="utf-8")
        meta = {
            "case": "quick", "fixture": "ledger", "base": base,
            "snapshots": [["README.md"]], "expected_turns": 1,
            "turns": [{"index": 1, "started": True, "skipped": False, "exit_code": 1,
                       "timed_out": False, "killed_after_done": False, "has_end_event": True}],
            "stats": {"cost_usd": 0.1, "agent_turns": 5, "seconds": 20.0, "tokens": None},
            "plugin": True, "model": None, "agent": "claude",
        }
        (case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        res = self.run_module.grade_case(case_dir)
        self.assertFalse(res["passed"])
        self.assertFalse(res["dimensions"]["execution"]["passed"])

        # killed_after_done with clean result event remains a valid execution
        (case_dir / "turn1.jsonl").write_text(fake_transcript(result="完成"), encoding="utf-8")
        meta["turns"][0]["killed_after_done"] = True
        meta["turns"][0]["exit_code"] = -9
        (case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        res_ok = self.run_module.grade_case(case_dir)
        self.assertTrue(res_ok["dimensions"]["execution"]["passed"])

    def test_missing_baseline_evidence_is_unknown_even_with_tool_order(self):
        ws, base = self.workspace("ledger")
        t_state_init = fake_transcript(
            ("Bash", {"command": "mkdir -p .dev-flow && printf '*\\n' > .dev-flow/.gitignore"}),
            ("Bash", {"command": "python -c \"from pathlib import Path; Path('.dev-flow/task.md').write_text('# t')\""}),
            ("Bash", {"command": "cat <<'EOF' > .dev-flow/progress.md\n# P\n> note\nEOF"}),
            ("Bash", {"command": "python -m unittest discover -s tests"}),
            ("Edit", {"file_path": "ledger/cli.py"}),
        )
        chk_ok = lib.baseline_before_edit(self.context("ledger", ws, base, t_state_init, plugin=True))
        self.assertFalse(chk_ok.ok)
        self.assertEqual(chk_ok.status, "unknown")

        # Shell edit to code (even combined with .dev-flow write) before test must fail
        t_shell_edit = fake_transcript(
            ("Bash", {"command": "printf '*\\n' > .dev-flow/.gitignore && echo 'x=1' > ledger/cli.py"}),
            ("Bash", {"command": "python -m unittest discover -s tests"}),
        )
        chk = lib.baseline_before_edit(self.context("ledger", ws, base, t_shell_edit, plugin=True))
        self.assertFalse(chk.ok)

        # Workspace modified without any detectable edit event must be marked unknown (not pass!)
        (ws / "ledger/cli.py").write_text("# changed\n", encoding="utf-8")
        t_mystery = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}))
        chk2 = lib.baseline_before_edit(self.context("ledger", ws, base, t_mystery, plugin=True))
        self.assertFalse(chk2.ok)
        self.assertEqual(chk2.status, "unknown")

    def test_ran_tests_fails_when_last_run_failed(self):
        ws, base = self.workspace("ledger")
        t = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}, True))
        chk = lib.ran_tests(self.context("ledger", ws, base, t))
        self.assertFalse(chk.ok)

    def test_changed_files_handles_chinese_filenames(self):
        ws, base = self.workspace("ledger")
        (ws / "中文说明.md").write_text("内容\n", encoding="utf-8")
        ctx = self.context("ledger", ws, base, fake_transcript())
        self.assertIn("中文说明.md", ctx.changed_files())

    def test_codex_transcript_translated_and_wait_agent_output_checked(self):
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
            item(type="collab_tool_call", tool="spawn_agent",
                 arguments={"task_name": "df-reviewer", "fork_turns": "none", "message": "df-reviewer 审查"},
                 agent_id="reviewer", output_present=True, output_error=False),
            item(type="agent_message", text="完成，退出码 0"),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ])
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[transcript])
        self.assertEqual(ctx.skills_invoked(), ["dev-flow:df-flow"])
        self.assertTrue(ctx.ran(r"unittest"))
        self.assertEqual(ctx.final_text(), "完成，退出码 0")
        self.assertIn("df-reviewer", ctx.subagents()[0])
        self.assertEqual(ctx.command_stats(), (2, 0))
        dispatched, waited = lib.independent_review(ctx)
        self.assertEqual((dispatched.ok, waited.ok), (True, False))  # spawned but never waited
        test_index = ctx.first_index(lambda n, d: n == "Bash" and "unittest" in d["command"])
        edit_index = ctx.first_index(lambda n, d: n == "Edit")
        self.assertLess(test_index, edit_index)

        # wait_agent timed out in rollout output must NOT pass
        timed_out_t = transcript + "\n" + item(
            type="collab_tool_call", tool="wait_agent", arguments={},
            output_present=True, output_error=True, output_preview='{"timed_out": true}',
        )
        ctx_to = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[timed_out_t])
        _, waited_to = lib.independent_review(ctx_to)
        self.assertFalse(waited_to.ok)

    def test_codex_collab_calls_reads_function_call_output(self):
        fake_home = Path(self.tmp.name) / "codex_home"
        sess = fake_home / "sessions"
        sess.mkdir(parents=True)
        rollout = sess / "rollout-2026-t99.jsonl"
        rollout.write_text("\n".join([
            json.dumps({"payload": {"type": "function_call", "call_id": "c1", "name": "spawn_agent",
                                    "arguments": json.dumps({"task_name": "df_review", "fork_turns": "none", "message": "secret"})}}),
            json.dumps({"payload": {"type": "function_call_output", "call_id": "c1", "output": '{"agent_id":"a1"}'}}),
            json.dumps({"payload": {"type": "function_call", "call_id": "c2", "name": "wait_agent", "arguments": "{}"}}),
            json.dumps({"payload": {"type": "function_call_output", "call_id": "c2",
                                    "output": '{"status":{"a1":{"completed":"结论：可以合入"}},"timed_out":false}'}}),
        ]), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(fake_home)}):
            calls = self.run_module.codex_collab_calls("t99")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("message", calls[0]["item"]["arguments"])
        self.assertTrue(calls[1]["item"]["output_present"])
        self.assertFalse(calls[1]["item"]["output_error"])
        self.assertEqual(calls[1]["item"]["completed_agents"], ["a1"])
        self.assertTrue(calls[0]["item"]["reviewer"])
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "empty", base="",
                      transcripts=["\n".join(json.dumps(call) for call in calls)])
        self.assertEqual(required_failures(lib.independent_review(ctx)), [])

        # not_found with timed_out=false must be treated as NOT completed (output_error=True)
        rollout_nf = sess / "rollout-2026-t100.jsonl"
        rollout_nf.write_text("\n".join([
            json.dumps({"payload": {"type": "function_call", "call_id": "c1", "name": "spawn_agent",
                                    "arguments": json.dumps({"task_name": "df_review", "fork_turns": "none"})}}),
            json.dumps({"payload": {"type": "function_call_output", "call_id": "c1", "output": '{"agent_id":"a1"}'}}),
            json.dumps({"payload": {"type": "function_call", "call_id": "c2", "name": "wait_agent", "arguments": "{}"}}),
            json.dumps({"payload": {"type": "function_call_output", "call_id": "c2",
                                    "output": '{"status":{"a1":"not_found"},"timed_out":false}'}}),
        ]), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(fake_home)}):
            calls_nf = self.run_module.codex_collab_calls("t100")
        self.assertTrue(calls_nf[1]["item"]["output_error"])
        transcript_nf = "\n".join(json.dumps(c, ensure_ascii=False) for c in calls_nf)
        ctx_nf = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "empty", base="", transcripts=[transcript_nf])
        _, waited_nf = lib.independent_review(ctx_nf)
        self.assertFalse(waited_nf.ok)

    def test_codex_home_safety_guard(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            with self.assertRaises(SystemExit):
                self.run_module.ensure_isolated_codex_home(allow_default=False)
            # Explicit override allowed
            self.run_module.ensure_isolated_codex_home(allow_default=True)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(self.tmp.name) / "isolated")}):
            self.run_module.ensure_isolated_codex_home(allow_default=False)

    def test_evidence_calls_through_alias(self):
        ws, base = self.workspace("ledger")
        record_evidence(ws, "unit", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        transcript = fake_transcript(
            ("Bash", {"command": 'EV="python /p/df-verify/scripts/evidence.py" && $EV run -l test -- python -m unittest'}),
            ("Bash", {"command": "python /p/df-verify/scripts/evidence.py report"}),
            ("Bash", {"command": "$EV run -l x -- true"}),
        )
        ctx = self.context("ledger", ws, base, transcript, plugin=True)
        runs, reports = lib.evidence_recorded(ctx)
        self.assertTrue(runs.ok, runs.detail)
        self.assertTrue(reports.ok, reports.detail)

    def test_claude_command_stats(self):
        def event(kind, item):
            return json.dumps({"type": kind, "message": {"content": [item]}})
        transcript = "\n".join([
            event("assistant", {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ok"}}),
            event("user", {"type": "tool_result", "tool_use_id": "a", "content": "fine"}),
            event("assistant", {"type": "tool_use", "id": "b", "name": "PowerShell", "input": {"command": "bad"}}),
            event("user", {"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": "exit 1"}),
            event("assistant", {"type": "tool_use", "id": "c", "name": "Read", "input": {}}),
            event("user", {"type": "tool_result", "tool_use_id": "c", "is_error": True, "content": "missing"}),
        ])
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[transcript])
        self.assertEqual(ctx.command_stats(), (2, 1))  # only shell tools count

    def test_feature_requires_the_business_json_entrypoint(self):
        ws, base = self.workspace("ledger")
        self._apply_feature_solution(ws)
        for command in ("python evidence.py report --json",
                        "echo 'python -m ledger list --json'",
                        "python --version --json"):
            with self.subTest(command=command):
                transcript = fake_transcript(
                    ("Bash", {"command": "python -m unittest discover -s tests"}),
                    ("Edit", {"file_path": "ledger/cli.py"}),
                    ("Bash", {"command": command}),
                )
                checks = load_grader("feature")(self.context("ledger", ws, base, transcript))
                self.assertIn("真实调用了 --json", required_failures(checks))

    def test_project_rejects_a_library_and_a_vacuous_test(self):
        ws, base = self.workspace("empty")
        first = self.run_module.snapshot(ws)
        (ws / "storage.py").write_text("def unused(): return 1\n", encoding="utf-8")
        (ws / "test_storage.py").write_text(
            "import unittest\nclass TestStorage(unittest.TestCase):\n"
            "    def test_smoke(self): self.assertTrue(True)\n", encoding="utf-8",
        )
        transcript = fake_transcript(
            ("Bash", {"command": "python -m unittest discover -s ."}),
            ("Bash", {"command": "python storage.py"}),
        )
        ctx = self.context("empty", ws, base, fake_transcript(), transcript,
                           snapshots=[first, self.run_module.snapshot(ws)])
        self.assertTrue(required_failures(load_grader("project")(ctx)))

    def test_project_rejects_stateless_recording_with_passing_tests(self):
        ws, base = self.workspace("empty")
        first = self.run_module.snapshot(ws)
        self._apply_project_solution(ws)
        (ws / "ledger/store.py").write_text(
            "entries = []\ndef load(): return entries\n"
            "def append(entry): entries.append(entry); return entry\n", encoding="utf-8",
        )
        (ws / "tests/test_cli.py").write_text(
            "import unittest\nclass TestCLI(unittest.TestCase):\n"
            "    def test_smoke(self): self.assertTrue(True)\n", encoding="utf-8",
        )
        transcript = fake_transcript(
            ("Bash", {"command": "python -m unittest discover -s tests"}),
            ("Bash", {"command": "python -m ledger list"}),
        )
        ctx = self.context("empty", ws, base, fake_transcript(), transcript,
                           snapshots=[first, self.run_module.snapshot(ws)])
        checks = load_grader("project")(ctx)
        self.assertIn("录入、列出及跨进程持久化正确", required_failures(checks))
        self.assertTrue(next(check.ok for check in checks if check.name == "项目测试实际运行并通过"))

    def test_project_reference_with_evidence_and_review_passes(self):
        ws, base = self.workspace("empty")
        first = self.run_module.snapshot(ws)
        self._apply_project_solution(ws)
        env = {"LEDGER_FILE": str(Path(self.tmp.name) / "project-ledger.json")}
        record_evidence(ws, "unit", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        record_evidence(ws, "add", sys.executable, "-m", "ledger", "add", "12.5", "午饭", env=env)
        record_evidence(ws, "list", sys.executable, "-m", "ledger", "list", env=env)
        transcript = fake_transcript(
            ("Bash", {"command": "python evidence.py run -l unit -- python -m unittest discover -s tests"}),
            ("Bash", {"command": "python evidence.py run -l add -- python -m ledger add 12.5 午饭"}),
            ("Bash", {"command": "python evidence.py run -l list -- python -m ledger list"}),
            ("Bash", {"command": "python evidence.py report"}),
            ("Agent", {"subagent_type": "dev-flow:df-reviewer", "prompt": "审查当前候选版本"}),
        )
        ctx = self.context("empty", ws, base, fake_transcript(), transcript, plugin=True,
                           snapshots=[first, self.run_module.snapshot(ws)])
        self.assertEqual(required_failures(load_grader("project")(ctx)), [])

    def test_test_commands_need_a_completed_result(self):
        use = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "test-call", "name": "Bash",
             "input": {"command": "python -m unittest discover -s tests"}}]}}
        transcript = "\n".join(map(json.dumps, [use, {"type": "result", "result": "完成"}]))
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[transcript])
        checked = lib.ran_tests(ctx)
        self.assertFalse(checked.ok)
        self.assertEqual(checked.status, "unknown")
        background = fake_transcript(
            ("Bash", {"command": "python -m unittest discover -s tests", "run_in_background": True}),
        )
        ctx.transcripts = [background]
        self.assertFalse(lib.ran_tests(ctx).ok)

    def test_successful_static_search_does_not_hide_a_failed_test_run(self):
        transcript = fake_transcript(
            ("Bash", {"command": "python -m unittest discover -s tests"}, True),
            ("Bash", {"command": "rg unittest tests"}),
        )
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[transcript])
        self.assertFalse(lib.ran_tests(ctx).ok)

    def test_only_version_evidence_does_not_verify_a_feature(self):
        ws, base = self.workspace("ledger")
        record_evidence(ws, "env", sys.executable, "--version")
        transcript = fake_transcript(
            ("Bash", {"command": "python evidence.py run -l env -- python --version"}),
            ("Bash", {"command": "python evidence.py report"}),
        )
        checks = lib.evidence_recorded(self.context("ledger", ws, base, transcript, plugin=True))
        self.assertTrue(all(not check.ok for check in checks))
        record_evidence(ws, "unit", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        checks = lib.evidence_recorded(self.context("ledger", ws, base, transcript, plugin=True))
        self.assertTrue(all(not check.ok for check in checks))

    def test_baseline_uses_the_initial_source_snapshot(self):
        ws, base = self.workspace("ledger")
        record_evidence(ws, "baseline", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        self._apply_feature_solution(ws)
        state_path = (ws / ".dev-flow/.gitignore").as_posix()
        transcript = fake_transcript(
            ("PowerShell", {"command": f"Write-Output '*' > '{state_path}'"}),
            ("Bash", {"command": "python evidence.py run -l baseline -- python -m unittest discover -s tests"}),
            ("Edit", {"file_path": "ledger/cli.py"}),
        )
        checked = lib.baseline_before_edit(self.context("ledger", ws, base, transcript, plugin=True))
        self.assertTrue(checked.ok, checked.detail)

        other, other_base = self.workspace("ledger")
        self._apply_feature_solution(other)
        record_evidence(other, "baseline", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        early_edit = fake_transcript(
            ("PowerShell", {"command": "Set-Content ledger/cli.py '# early edit'"}),
            ("Bash", {"command": "python evidence.py run -l baseline -- python -m unittest discover -s tests"}),
            ("Edit", {"file_path": "ledger/cli.py"}),
        )
        checked = lib.baseline_before_edit(self.context("ledger", other, other_base, early_edit, plugin=True))
        self.assertFalse(checked.ok)

    def test_claude_review_tool_errors_are_not_independent_reviews(self):
        transcript = fake_transcript(
            ("Agent", {"subagent_type": "dev-flow:df-reviewer", "prompt": "review"}, True),
        )
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "empty", base="", transcripts=[transcript])
        self.assertTrue(required_failures(lib.independent_review(ctx)))

    def test_codex_review_needs_the_matching_reviewer_result(self):
        def event(tool, arguments, **output):
            return json.dumps({"type": "item.completed", "item": {
                "type": "collab_tool_call", "tool": tool, "arguments": arguments, **output}})
        spawned = [
            event("spawn_agent", {"task_name": "implementation", "fork_turns": "none"},
                  agent_id="worker", output_present=True, output_error=False),
            event("spawn_agent", {"task_name": "df_review", "fork_turns": "none"},
                  agent_id="reviewer", output_present=True, output_error=False),
        ]
        missing = event("wait_agent", {"ids": ["reviewer"]})
        wrong = event("wait_agent", {"ids": ["worker"]}, output_present=True, output_error=False,
                      completed_agents=["worker"])
        overridden = event("wait_agent", {"ids": ["reviewer"], "output_present": True,
                                          "output_error": False, "completed_agents": ["reviewer"]},
                           output_present=True, output_error=True, completed_agents=[])
        for waited in (missing, wrong, overridden):
            with self.subTest(waited=waited):
                ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "empty", base="",
                              transcripts=["\n".join([*spawned, waited])])
                self.assertTrue(required_failures(lib.independent_review(ctx)))
        completed = event("wait_agent", {"ids": ["reviewer"]}, output_present=True, output_error=False,
                          completed_agents=["reviewer"], output_preview="审查完成")
        ctx.transcripts = ["\n".join([*spawned, completed])]
        self.assertEqual(required_failures(lib.independent_review(ctx)), [])

    def test_summary_formats_missing_usage_as_dash_and_cached_tokens(self):
        out_dir = Path(self.tmp.name) / "summary_out"
        out_dir.mkdir()
        results = [
            {
                "case": "quick", "plugin": True, "passed": True,
                "dimensions": {
                    "execution": {"passed": True, "score": "1/1"},
                    "functional": {"passed": True, "score": "4/4"},
                    "compliance": {"passed": True, "score": "0/0"},
                },
                "required": "5/5", "soft": "2/2",
                "stats": {"seconds": 12.0, "commands": 2, "failed_commands": 0,
                          "cost_usd": None, "tokens": None, "cached_input_tokens": None},
                "checks": [],
            },
            {
                "case": "feature", "plugin": False, "passed": True,
                "dimensions": {
                    "execution": {"passed": True, "score": "1/1"},
                    "functional": {"passed": True, "score": "7/7"},
                    "compliance": {"passed": None, "score": "0/3"},
                },
                "required": "8/8", "soft": "1/4",
                "stats": {"seconds": 40.0, "commands": 5, "failed_commands": 1,
                          "cost_usd": None, "tokens": 5000, "cached_input_tokens": 3200},
                "checks": [],
            },
        ]
        with mock.patch("builtins.print"):
            self.run_module.summarize(results, out_dir)
        md = (out_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("| - | - |", md)
        self.assertIn("| 5000(3200) |", md)
        self.assertIn("➖ 0/3", md)

    def test_business_entrypoint_matching_across_shells(self):
        for command in (
            "python3.14 -X utf8 -m ledger list --json",
            '"C:/Program Files/Python/python.exe" -m ledger list --json',
            '"C:/Program Files/PowerShell/7/pwsh.exe" -Command "python -m ledger list --json"',
            'bash -c "python3 -m ledger list --json"',
            "python evidence.py run -l json -- python -m ledger list --json\n",
        ):
            with self.subTest(command=command):
                self.assertTrue(lib.invokes_python_module(command, "ledger", "list", "--json"))
        for command in ("echo 'python -m ledger list --json'",
                        "python -m ledger list --json || true",
                        "python -m ledger list --json; true"):
            with self.subTest(command=command):
                self.assertFalse(lib.invokes_python_module(command, "ledger", "list", "--json"))

    def test_tool_results_are_paired_within_their_turn(self):
        first = fake_transcript(("Bash", {"command": "python -m unittest discover -s tests"}, True))
        second = fake_transcript(("Bash", {"command": "python --version"}))
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[first, second])
        self.assertFalse(lib.ran_tests(ctx).ok)

    def test_evidence_requires_each_validation_at_the_current_snapshot(self):
        ws, base = self.workspace("ledger")
        self._apply_feature_solution(ws)
        transcript = fake_transcript(
            ("Bash", {"command": "python evidence.py run -l unit -- python -m unittest discover -s tests"}),
            ("Bash", {"command": "python evidence.py run -l json -- python -m ledger list --json"}),
            ("Bash", {"command": "python evidence.py report"}),
        )
        ctx = self.context("ledger", ws, base, transcript, plugin=True)
        validations = {"测试": lib.is_test_command,
                       "JSON": lambda command: lib.invokes_python_module(command, "ledger", "list", "--json")}
        record_evidence(ws, "unit", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        self.assertFalse(lib.evidence_recorded(ctx, validations=validations)[1].ok)
        record_evidence(ws, "json", sys.executable, "-m", "ledger", "list", "--json")
        self.assertTrue(lib.evidence_recorded(ctx, validations=validations)[1].ok)
        source = ws / "ledger/cli.py"
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertFalse(lib.evidence_recorded(ctx, validations=validations)[1].ok)
        record_evidence(ws, "unit", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        self.assertFalse(lib.evidence_recorded(ctx, validations=validations)[1].ok)
        record_evidence(ws, "json", sys.executable, "-m", "ledger", "list", "--json")
        self.assertTrue(lib.evidence_recorded(ctx, validations=validations)[1].ok)

    def test_claude_background_review_requires_the_matching_completed_task(self):
        spawned = ("Agent", {"subagent_type": "dev-flow:df-reviewer", "run_in_background": True},
                   False, json.dumps({"task_id": "review-1"}))
        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "empty", base="", transcripts=[fake_transcript(spawned)])
        self.assertFalse(lib.independent_review(ctx)[1].ok)
        for task_id, state, expected in (("other", "completed", False),
                                        ("review-1", "running", False),
                                        ("review-1", "completed", True)):
            with self.subTest(task_id=task_id, state=state):
                polled = ("TaskOutput", {"task_id": task_id}, False, json.dumps({"status": state}))
                ctx.transcripts = [fake_transcript(spawned, polled)]
                self.assertEqual(lib.independent_review(ctx)[1].ok, expected)

    def test_delivery_comparison_is_independent_of_plugin_compliance(self):
        case_dir = Path(self.tmp.name) / "feature-comparison"
        case_dir.mkdir()
        ws = case_dir / "workspace"
        base = self.run_module.prepare("ledger", ws)
        self._apply_feature_solution(ws)
        transcript = fake_transcript(
            ("Bash", {"command": "python -m unittest discover -s tests"}),
            ("Bash", {"command": "python -m ledger list --json"}),
        )
        (case_dir / "turn1.jsonl").write_text(transcript, encoding="utf-8")
        meta = {"case": "feature", "fixture": "ledger", "base": base,
                "snapshots": [self.run_module.snapshot(ws)], "expected_turns": 1,
                "stats": {"seconds": 1, "cost_usd": None, "tokens": None}, "plugin": True}
        (case_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        plugin = self.run_module.grade_case(case_dir)
        self.assertTrue(plugin["delivery_passed"])
        self.assertFalse(plugin["passed"])
        meta["plugin"] = False
        (case_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        baseline = self.run_module.grade_case(case_dir)
        self.assertTrue(baseline["delivery_passed"])
        self.assertTrue(baseline["passed"])

    def test_cross_call_ev_alias_and_multiline_evidence_scripts(self):
        ws, base = self.workspace("ledger")
        record_evidence(ws, "baseline", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        self._apply_feature_solution(ws)
        record_evidence(ws, "unit", sys.executable, "-m", "unittest", "discover", "-s", "tests")
        record_evidence(ws, "json", sys.executable, "-m", "ledger", "list", "--json")
        snap12 = json.loads(
            subprocess.run([sys.executable, str(EVIDENCE_CLI), "report", "--json"],
                           cwd=ws, capture_output=True, text=True, check=True).stdout
        )["short_snapshot"]

        # Claude persistent Bash session: define EV once, reuse $EV across later tool calls
        persistent_transcript = fake_transcript(
            ("Bash", {"command": 'EV="python /p/df-verify/scripts/evidence.py"'}),
            ("Bash", {"command": "$EV run -l baseline -- python -m unittest discover -s tests"}),
            ("Edit", {"file_path": "ledger/cli.py"}),
            ("Bash", {"command": "$EV run -l unit -- python -m unittest discover -s tests"}),
            ("Bash", {"command": "$EV run -l json -- python -m ledger list --json"}),
            ("Bash", {"command": "$EV report"}),
        )
        checks = load_grader("feature")(self.context("ledger", ws, base, persistent_transcript, plugin=True))
        self.assertEqual(required_failures(checks), [])

        validations = {
            "测试": lib.is_test_command,
            "JSON 入口": lambda command: lib.invokes_python_module(command, "ledger", "list", "--json"),
        }

        # Multiline evidence block under `set -e` (each line is a single simple command) passes
        set_e_multiline = fake_transcript(
            ("Bash", {"command": 'set -e\n'
                                 'EV="python3 /p/evidence.py"\n'
                                 '$EV run -l unit -- python3 -m unittest discover -s tests\n'
                                 '$EV run -l json -- python3 -m ledger list --json\n'
                                 '$EV report'}),
        )
        ev_set_e = lib.evidence_recorded(self.context("ledger", ws, base, set_e_multiline, plugin=True),
                                         validations=validations)
        self.assertTrue(all(c.ok for c in ev_set_e), [c.detail for c in ev_set_e])

        # Multiline block WITHOUT `set -e` passes only when stdout carries evidence.py's own exit=0 snapshot proof
        multiline_cmd = ('EV="python3 /p/evidence.py"\n'
                         '$EV run -l unit -- python3 -m unittest discover -s tests\n'
                         '$EV run -l json -- python3 -m ledger list --json\n'
                         '$EV report')
        multiline_with_proof = fake_transcript(
            ("Bash", {"command": multiline_cmd}, False,
             f"[evidence] exit=0 snapshot={snap12} 已记录到 .dev-flow/evidence.jsonl\n"),
        )
        ev_checks = lib.evidence_recorded(self.context("ledger", ws, base, multiline_with_proof, plugin=True),
                                          validations=validations)
        self.assertTrue(all(c.ok for c in ev_checks), [c.detail for c in ev_checks])

        # Same multiline block WITHOUT output confirmation must NOT pass (status="unknown")
        multiline_unconfirmed = fake_transcript(("Bash", {"command": multiline_cmd}, False, "ok"))
        run_unk, rep_unk = lib.evidence_recorded(self.context("ledger", ws, base, multiline_unconfirmed, plugin=True),
                                                 validations=validations)
        self.assertFalse(run_unk.ok)
        self.assertEqual(run_unk.status, "unknown")
        self.assertFalse(rep_unk.ok)

        # Short-circuited / masked report & run commands MUST NOT pass
        for bad_report_cmd in ("python /p/evidence.py report || true",
                               "false && python /p/evidence.py report; true"):
            with self.subTest(bad_report_cmd=bad_report_cmd):
                masked_report = fake_transcript(
                    ("Bash", {"command": "python /p/evidence.py run -l unit -- python -m unittest discover -s tests"}),
                    ("Bash", {"command": bad_report_cmd}),
                )
                _, report_chk = lib.evidence_recorded(self.context("ledger", ws, base, masked_report, plugin=True))
                self.assertFalse(report_chk.ok)
                self.assertEqual(report_chk.status, "unknown")

        # Unrelated `evidence.py run -- python --version && python -m unittest` must NOT count as evidence for tests
        split_ev = fake_transcript(
            ("Bash", {"command": "python /p/evidence.py run -l env -- python --version && python -m unittest discover -s tests"}),
            ("Bash", {"command": "python /p/evidence.py report"}),
        )
        split_run, split_rep = lib.evidence_recorded(self.context("ledger", ws, base, split_ev, plugin=True))
        self.assertFalse(split_run.ok)
        self.assertFalse(split_rep.ok)

    def test_shell_parsing_env_spaces_set_e_powershell_and_escaped_quotes(self):
        # Env assignment with spaces in quoted value
        spaced_env = 'LEDGER_FILE="/tmp/my ledger.json" python -m ledger list --json'
        self.assertTrue(lib.invokes_python_module(spaced_env, "ledger", "list", "--json"))

        # set -e preserves single simple commands across newlines, while conditional `&&` / `||` / `if` / `; true` do not
        set_e_script = ("set -e\n"
                        "LEDGER_FILE=/tmp/a.json python3 -m ledger add 12.5 lunch\n"
                        "LEDGER_FILE=/tmp/a.json python3 -m ledger list")
        self.assertTrue(lib.invokes_python_module(set_e_script, "ledger", "add"))
        self.assertTrue(lib.invokes_python_module(set_e_script, "ledger", "list"))
        for unguaranteed in (
            "set -e\npython -m ledger list --json || true",
            "set -e; false && python -m unittest discover -s tests; true",
            "set -e; python -m unittest discover -s tests && true; true",
            "set -e\nif false; then\npython -m unittest discover -s tests\nfi\ntrue",
            "set -e; exit 0; python -m unittest discover -s tests",
        ):
            with self.subTest(unguaranteed=unguaranteed):
                self.assertFalse(lib.is_test_command(unguaranteed))
                self.assertFalse(lib.invokes_python_module(unguaranteed, "ledger", "list", "--json"))

        # ran_tests marks conditional/masked test runs as unknown (never pass), even after an earlier passing run
        for masked_cmd in (
            "set -e; false && python -m unittest discover -s tests; true",
            "set -e; python -m unittest discover -s tests && true; true",
            "python -m unittest discover -s tests; true",
        ):
            with self.subTest(masked_cmd=masked_cmd):
                t = fake_transcript(("Bash", {"command": masked_cmd}))
                chk = lib.ran_tests(Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger", base="", transcripts=[t]))
                self.assertFalse(chk.ok)
                self.assertEqual(chk.status, "unknown")

        masked_after_pass = fake_transcript(
            ("Bash", {"command": "python -m unittest discover -s tests"}),
            ("Bash", {"command": "python -m unittest discover -s tests; true"}),
        )
        chk_after = lib.ran_tests(Context(ws=Path(self.tmp.name), fixture=FIXTURES / "ledger",
                                          base="", transcripts=[masked_after_pass]))
        self.assertFalse(chk_after.ok)
        self.assertEqual(chk_after.status, "unknown")

        # PowerShell `$EV = "..." ; & $EV ...`
        ps_cmd = '$EV = "python /p/evidence.py"; & $EV run -l unit -- python -m unittest discover -s tests'
        self.assertTrue(lib.is_evidence_call(ps_cmd, "run"))
        self.assertTrue(lib.is_test_command(ps_cmd))

        # bash -lc with escaped inner double quotes around nested bash -c
        nested = (r'/bin/bash -lc "python3 /p/evidence.py run -l json -- bash -c '
                  r'\"LEDGER_FILE=/tmp/a.json python3 -m ledger list --json\""')
        self.assertTrue(lib.is_evidence_call(nested, "run"))
        self.assertTrue(lib.invokes_python_module(nested, "ledger", "list", "--json"))

    def test_claude_background_review_task_notification_and_codex_dict_completed(self):
        spawned = ("Agent", {"subagent_type": "dev-flow:df-reviewer", "run_in_background": True},
                   False, "Async agent launched successfully. agentId: rev-42")
        base_transcript = fake_transcript(spawned, include_end=False)

        notif_ok = json.dumps({
            "type": "user",
            "message": {"content": "<task-notification><task-id>rev-42</task-id>"
                                   "<status>completed</status><result>LGTM: if a < b && c > 0</result></task-notification>"},
        })
        notif_fail = json.dumps({
            "type": "user",
            "message": {"content": "<task-notification><task-id>rev-42</task-id>"
                                   "<status>failed</status></task-notification>"},
        })
        notif_fail_with_body_example = json.dumps({
            "type": "user",
            "message": {"content": "<task-notification><task-id>rev-42</task-id><status>failed</status>"
                                   "<result>Example: <status>completed</status></result></task-notification>"},
        })
        notif_fail_nonxml_body = json.dumps({
            "type": "user",
            "message": {"content": "<task-notification><task-id>rev-42</task-id><status>failed</status>"
                                   "<result>a < b && <status>completed</status></result></task-notification>"},
        })
        notif_other = json.dumps({
            "type": "user",
            "message": {"content": "<task-notification><task-id>other-9</task-id>"
                                   "<status>completed</status></task-notification>"},
        })
        end = json.dumps({"type": "result", "result": "done"})

        ctx = Context(ws=Path(self.tmp.name), fixture=FIXTURES / "empty", base="",
                      transcripts=["\n".join([base_transcript, notif_ok, end])])
        self.assertEqual(required_failures(lib.independent_review(ctx)), [])

        for bad_notif in (notif_fail, notif_fail_with_body_example, notif_fail_nonxml_body, notif_other):
            with self.subTest(bad_notif=bad_notif):
                ctx.transcripts = ["\n".join([base_transcript, bad_notif, end])]
                self.assertFalse(lib.independent_review(ctx)[1].ok)

        # TaskOutput XML must also inspect top-level status rather than body text
        self.assertFalse(lib.task_completed("<status>failed</status><output><status>completed</status></output>"))
        self.assertTrue(lib.task_completed("<status>completed</status><output>diff: a < b</output>"))

        # Codex wait_agent with dict payload under `completed`
        present, is_err, completed = self.run_module._analyze_collab_output(
            "wait_agent",
            json.dumps({"status": {"a1": {"completed": {"final_message": "LGTM"}}}, "timed_out": False}),
        )
        self.assertEqual((present, is_err, completed), (True, False, ["a1"]))
        _, err_dict, done_err = self.run_module._analyze_collab_output(
            "wait_agent",
            json.dumps({"status": {"a1": {"completed": {"error": "boom"}}}, "timed_out": False}),
        )
        self.assertTrue(err_dict)
        self.assertEqual(done_err, [])

    def test_project_cli_timeout_and_unknown_cli_exercised(self):
        ws, base = self.workspace("empty")
        first = self.run_module.snapshot(ws)
        self._apply_project_solution(ws)
        (ws / "ledger/cli.py").write_text("import time; time.sleep(5)\n", encoding="utf-8")
        project_grade = importlib.util.spec_from_file_location("grade_project_mod", EVALS / "cases/project/grade.py")
        mod = importlib.util.module_from_spec(project_grade)
        project_grade.loader.exec_module(mod)

        # Timeout inside CLI must produce a failed functional check rather than an uncaught exception
        ctx = self.context("empty", ws, base, fake_transcript(), fake_transcript(),
                           snapshots=[first, self.run_module.snapshot(ws)])
        with mock.patch.object(ctx, "python", side_effect=subprocess.TimeoutExpired(["python", "-m", "ledger"], 20)):
            chk = mod.project_cli_behavior(ctx)
        self.assertFalse(chk.ok)
        self.assertEqual(chk.category, "functional")
        self.assertIn("超时", chk.detail)

        # Unfinished CLI tool call in transcript must be marked status="unknown"
        use = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "cli-call", "name": "Bash",
             "input": {"command": "python -m ledger list"}}]}}
        unfinished = "\n".join(map(json.dumps, [use, {"type": "result", "result": "done"}]))
        ctx2 = self.context("empty", ws, base, fake_transcript(), unfinished,
                            snapshots=[first, self.run_module.snapshot(ws)])
        exercised = mod.real_cli_exercised(ctx2, turn=1)
        self.assertFalse(exercised.ok)
        self.assertEqual(exercised.status, "unknown")


class RunTurnTests(unittest.TestCase):
    """The runner must not hang on CLIs whose children outlive the turn."""

    # parent prints a finished turn, leaves a grandchild holding stdout, then lingers
    LINGER = ("import subprocess, sys, time; "
              f"print(sys.stdin.read().strip()); print({json.dumps({'type': 'turn.completed'})!r}, flush=True); "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")

    def setUp(self):
        self.run_module = load_run_module()

    def test_killed_after_turn_completed(self):
        start = time.time()
        out, err, info = self.run_module.run_turn_detailed(
            [sys.executable, "-c", self.LINGER], ".", "hi", timeout=50, grace=1)
        self.assertLess(time.time() - start, 30)
        self.assertIn("turn.completed", out)
        self.assertTrue(out.startswith("hi"))
        self.assertIn("killed", err)
        self.assertTrue(info["has_end_event"])
        self.assertTrue(info["killed_after_done"])
        self.assertFalse(info["timed_out"])

    def test_timeout_kills_tree(self):
        start = time.time()
        _, err, info = self.run_module.run_turn_detailed(
            [sys.executable, "-c", "import time; time.sleep(60)"], ".", "", timeout=2)
        self.assertLess(time.time() - start, 30)
        self.assertIn("timeout after 2s", err)
        self.assertTrue(info["timed_out"])
        self.assertFalse(info["has_end_event"])


if __name__ == "__main__":
    unittest.main()
