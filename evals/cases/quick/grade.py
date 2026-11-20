from lib import Check, no_commits, no_flow, skill_used


def grade(ctx):
    readme = (ctx.ws / "README.md").read_text(encoding="utf-8")
    changed = ctx.changed_files()
    return [
        Check("拼写已修正", "recieve" not in readme and "receive" in readme, category="functional"),
        Check("只修改 README.md", changed == ["README.md"], ", ".join(changed) or "无改动", category="functional"),
        no_flow(ctx),
        no_commits(ctx),
        skill_used(ctx, "df-flow", expect=False),
        skill_used(ctx, "df-verify", expect=False),
    ]
