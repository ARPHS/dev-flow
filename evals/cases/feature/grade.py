import json
import tempfile
from pathlib import Path

from lib import (Check, baseline_before_edit, no_commits, original_asserts_intact, ran_tests,
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
        Check("list --json 输出正确", ok, detail),
        Check("无记录时输出 []", empty_ok, empty.stdout.strip()[:60], required=False),
    ]


def grade(ctx):
    return [
        *json_output(ctx),
        unit_tests_pass(ctx),
        original_asserts_intact(ctx),
        tests_added(ctx),
        ran_tests(ctx),
        Check("真实调用了 --json", bool(ctx.ran(r"--json")), required=True),
        no_commits(ctx),
        baseline_before_edit(ctx),
        skill_used(ctx, "df-flow"),
        Check("创建了 .flow/progress.md", (ctx.ws / ".flow" / "progress.md").exists(), required=False),
        Check("报告包含退出码", any(k in ctx.final_text() for k in ("退出码", "exit")), required=False),
    ]
