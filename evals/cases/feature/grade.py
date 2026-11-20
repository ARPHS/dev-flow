import json
import tempfile
from pathlib import Path

from lib import (Check, baseline_before_edit, evidence_recorded, execution_succeeded, invokes_python_module,
                 is_test_command, no_commits, original_asserts_intact, original_tests_pass, ran_tests,
                 skill_used, tests_added, unit_tests_pass)


def json_output(ctx):
    with tempfile.TemporaryDirectory() as tmp:
        env = {"LEDGER_FILE": str(Path(tmp) / "ledger.json")}
        ctx.python("-m", "ledger", "add", "12.5", "午饭", env=env)
        ctx.python("-m", "ledger", "add", "3", "咖啡", env=env)
        proc = ctx.python("-m", "ledger", "list", "--json", env=env)
        empty = ctx.python("-m", "ledger", "list", "--json", env={"LEDGER_FILE": str(Path(tmp) / "none.json")})
    try:
        data = json.loads(proc.stdout)
        ok = (proc.returncode == 0 and isinstance(data, list) and len(data) == 2
              and data[0]["amount"] == 12.5 and data[0]["note"] == "午饭")
        detail = proc.stdout.strip()[:120]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        ok, detail = False, f"exit={proc.returncode} {exc}: {(proc.stdout + proc.stderr).strip()[:120]}"
    try:
        empty_ok = empty.returncode == 0 and json.loads(empty.stdout) == []
    except ValueError:
        empty_ok = False
    return [
        Check("list --json 输出正确", ok, detail, category="functional"),
        Check("无记录时输出 []", empty_ok, empty.stdout.strip()[:60], required=False, category="advisory"),
    ]


def cli_json_invoked(ctx):
    hits = [execution for execution in ctx.shell_executions()
            if invokes_python_module(execution["command"], "ledger", "list", "--json")]
    passing = [execution for execution in hits if execution_succeeded(execution)]
    unknown = bool(hits) and not passing and any(execution["exit_code"] is None for execution in hits)
    return Check("真实调用了 --json", bool(passing),
                 passing[0]["command"][:120] if passing else "未发现成功完成的 ledger list --json 调用",
                 category="functional", status="unknown" if unknown else "")


def grade(ctx):
    return [
        *json_output(ctx),
        unit_tests_pass(ctx),
        original_tests_pass(ctx),
        original_asserts_intact(ctx, required=False),
        tests_added(ctx),
        ran_tests(ctx),
        cli_json_invoked(ctx),
        no_commits(ctx),
        baseline_before_edit(ctx),
        skill_used(ctx, "df-flow"),
        *evidence_recorded(ctx, validations={
            "测试": is_test_command,
            "JSON 入口": lambda command: invokes_python_module(command, "ledger", "list", "--json"),
        }),
        Check("创建了 .dev-flow/progress.md", (ctx.ws / ".dev-flow" / "progress.md").exists(),
              required=False, category="compliance"),
        Check("报告包含退出码", any(k in ctx.final_text() for k in ("退出码", "exit")),
              required=False, category="advisory"),
    ]
