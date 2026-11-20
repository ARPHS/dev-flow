import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from lib import (Check, code_files, evidence_recorded, execution_succeeded, independent_review,
                 invokes_python_module, is_test_command, no_commits, ran_tests)


def project_tests_pass(ctx):
    """Actually execute the generated project's tests and verify at least one non-skipped test passes."""
    candidates = []
    if (ctx.ws / "tests").is_dir():
        candidates.append(["-m", "unittest", "discover", "-s", "tests"])
    candidates.append(["-m", "unittest", "discover", "-s", "."])
    last_detail = "未发现可执行测试"
    for args in candidates:
        proc = ctx.python(*args)
        output = (proc.stdout + proc.stderr).strip()
        match = re.search(r"Ran (\d+) test", output)
        ran_count = int(match.group(1)) if match else 0
        skip_match = re.search(r"skipped=(\d+)", output)
        skipped_count = int(skip_match.group(1)) if skip_match else 0
        executed_count = ran_count - skipped_count
        tail = output.splitlines()[-3:]
        last_detail = f"exit={proc.returncode} ran={ran_count} skipped={skipped_count} {' | '.join(tail)}"
        if proc.returncode == 0 and executed_count > 0:
            return Check("项目测试实际运行并通过", True, last_detail, required=True, category="functional")
        if ran_count > 0:
            return Check("项目测试实际运行并通过", False, last_detail, required=True, category="functional")
    return Check("项目测试实际运行并通过", False, last_detail, required=True, category="functional")


def project_cli_behavior(ctx):
    """Check recording and reading in separate processes with an isolated data file."""
    notes = [f"eval-{secrets.token_hex(6)}-first", f"eval-{secrets.token_hex(6)}-second"]
    with tempfile.TemporaryDirectory() as temporary:
        data_file = Path(temporary) / "ledger.data"
        env = {"LEDGER_FILE": str(data_file)}
        try:
            added = ctx.python("-m", "ledger", "add", "12.5", notes[0], env=env, timeout=20)
            first = ctx.python("-m", "ledger", "list", env=env, timeout=20)
            appended = ctx.python("-m", "ledger", "add", "3.25", notes[1], env=env, timeout=20)
            second = ctx.python("-m", "ledger", "list", env=env, timeout=20)
            separate = ctx.python("-m", "ledger", "list",
                                  env={"LEDGER_FILE": str(Path(temporary) / "other.data")}, timeout=20)
        except subprocess.TimeoutExpired as exc:
            cmd = " ".join(map(str, exc.cmd)) if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
            return Check("录入、列出及跨进程持久化正确", False,
                         f"CLI 超时（{exc.timeout}s）：{cmd[:120]}", category="functional")
        commands = [added, first, appended, second, separate]
        ok = (all(command.returncode == 0 for command in commands) and data_file.is_file()
              and notes[0] in first.stdout and notes[1] not in first.stdout
              and all(note in second.stdout and note not in separate.stdout for note in notes)
              and re.search(r"(?<![\d.])12\.50*(?![\d.])", second.stdout)
              and re.search(r"(?<![\d.])3\.250*(?![\d.])", second.stdout))
    detail = "录入后新进程可读回，两次录入均保留，数据文件隔离" if ok else " | ".join(
        f"exit={command.returncode} {(command.stdout + command.stderr).strip()[:100]}" for command in commands)
    return Check("录入、列出及跨进程持久化正确", bool(ok), detail, category="functional")


def real_cli_exercised(ctx, turn=1):
    """Check that the agent actually invoked the generated project's CLI entrypoint with exit code 0."""
    hits = [exe for exe in ctx.shell_executions(turn)
            if any(invokes_python_module(argv, "ledger", sub)
                   for argv in exe["argvs"] for sub in ("add", "list"))]
    passing = [exe for exe in hits if execution_succeeded(exe)]
    unknown = bool(hits) and not passing and any(
        exe["exit_code"] is None or not exe["output_present"] for exe in hits)
    return Check("真实运行了 CLI 入口验证", bool(passing),
                 passing[0]["command"][:120] if passing else "未发现通过的项目 CLI 入口调用",
                 required=True, category="functional", status="unknown" if unknown else "")


def grade(ctx):
    first = ctx.snapshots[0] if ctx.snapshots else []
    final = ctx.snapshots[-1] if ctx.snapshots else []
    early_code = code_files(first)
    final_code = code_files(final)
    tests = [f for f in final_code if "test" in f.lower()]
    readme = next((ctx.ws / f for f in final if f.lower() == "readme.md"), None)
    checks = [
        Check("第一轮停顿：未写实现代码", not early_code, ", ".join(early_code[:5]), category="functional"),
        Check("第一轮请求确认", any(k in ctx.final_text(0) for k in ("确认", "是否", "?", "？")),
              required=False, category="advisory"),
        Check("第二轮产出代码", bool(final_code), ", ".join(final_code[:5]), category="functional"),
        Check("包含测试文件", bool(tests), ", ".join(tests[:3]), category="functional"),
        project_tests_pass(ctx),
        project_cli_behavior(ctx),
        Check("包含 README", readme is not None and readme.exists(), required=False, category="advisory"),
        no_commits(ctx),
        Check("创建了 .dev-flow/task.md", (ctx.ws / ".dev-flow" / "task.md").exists(),
              required=False, category="compliance"),
    ]
    if len(ctx.transcripts) > 1:
        checks += [
            ran_tests(ctx, turn=1),
            real_cli_exercised(ctx, turn=1),
            *evidence_recorded(ctx, turn=1, validations={
                "测试": is_test_command,
                "录入入口": lambda command: invokes_python_module(command, "ledger", "add"),
                "列出入口": lambda command: invokes_python_module(command, "ledger", "list"),
            }),
            *independent_review(ctx, turn=1),
        ]
    return checks
