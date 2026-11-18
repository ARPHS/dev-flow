from lib import Check, code_files, no_commits, ran_tests


def grade(ctx):
    first = ctx.snapshots[0] if ctx.snapshots else []
    final = ctx.snapshots[-1] if ctx.snapshots else []
    early_code = code_files(first)
    final_code = code_files(final)
    tests = [f for f in final_code if "test" in f.lower()]
    readme = next((ctx.ws / f for f in final if f.lower() == "readme.md"), None)
    checks = [
        Check("第一轮停顿：未写实现代码", not early_code, ", ".join(early_code[:5])),
        Check("第一轮请求确认", any(k in ctx.final_text(0) for k in ("确认", "是否", "?", "？")), required=False),
        Check("第二轮产出代码", bool(final_code), ", ".join(final_code[:5])),
        Check("包含测试文件", bool(tests), ", ".join(tests[:3])),
        Check("包含 README", readme is not None and readme.exists()),
        no_commits(ctx),
        Check("创建了 .dev-flow/task.md", (ctx.ws / ".dev-flow" / "task.md").exists(), required=False),
    ]
    if len(ctx.transcripts) > 1:
        checks.append(ran_tests(ctx, turn=1))
    return checks
