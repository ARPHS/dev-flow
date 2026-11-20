from lib import Check, no_flow, skill_used


def grade(ctx):
    changed = ctx.changed_files()
    answer = ctx.final_text()
    return [
        Check("没有修改文件", not changed, ", ".join(changed), category="functional"),
        no_flow(ctx),
        skill_used(ctx, "df-flow", required=True, expect=False),
        Check("回答提到并发覆盖风险", any(k in answer for k in ("并发", "覆盖", "竞争", "race")),
              required=False, category="advisory"),
    ]
