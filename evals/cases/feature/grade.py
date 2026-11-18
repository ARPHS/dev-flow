import json
import re
import tempfile
from pathlib import Path

from lib import (Check, baseline_before_edit, evidence_recorded, no_commits, original_asserts_intact, ran_tests,
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


def cli_json_invoked(ctx):
    """--json on a command line, or inside a script the agent wrote and then ran."""
    direct = ctx.ran(r"--json")
    if direct:
        return Check("真实调用了 --json", True, direct[0][:120])
    for command in ctx.bash_commands():
        for name in re.findall(r"[\w./\\-]+\.py", command):
            script = ctx.ws / name
            if script.is_file() and "--json" in script.read_text(encoding="utf-8", errors="replace"):
                return Check("真实调用了 --json", True, f"通过 {name}")
    return Check("真实调用了 --json", False, "命令和被执行的脚本中都没有 --json")


def grade(ctx):
    return [
        *json_output(ctx),
        unit_tests_pass(ctx),
        original_asserts_intact(ctx),
        tests_added(ctx),
        ran_tests(ctx),
        cli_json_invoked(ctx),
        no_commits(ctx),
        baseline_before_edit(ctx),
        skill_used(ctx, "df-flow"),
        *evidence_recorded(ctx),
        Check("创建了 .dev-flow/progress.md", (ctx.ws / ".dev-flow" / "progress.md").exists(), required=False),
        Check("报告包含退出码", any(k in ctx.final_text() for k in ("退出码", "exit")), required=False),
    ]
