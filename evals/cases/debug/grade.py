from lib import (Check, no_commits, original_asserts_intact, ran_tests, skill_used, tests_added,
                 unit_tests_pass)

HIDDEN = r"""
import sys
from orders.export import export_orders
from orders.source import OrderSource
bad = []
for n in (0, 1, 9, 10, 11, 25, 31):
    for size in (1, 3, 7, 10, 50):
        orders = [{"id": i, "status": "paid" if i % 2 else "new", "amount": i} for i in range(n * 2)]
        rows = export_orders(OrderSource(orders), status="paid", page_size=size)
        expected = [o["id"] for o in orders if o["status"] == "paid"]
        if [r["id"] for r in rows] != expected:
            bad.append(f"n={n} size={size} got={len(rows)} want={len(expected)}")
print("; ".join(bad[:3]) or "ok")
sys.exit(1 if bad else 0)
"""


def grade(ctx):
    hidden = ctx.python("-c", HIDDEN)
    return [
        Check("隐藏用例：各种数量和页大小都完整导出", hidden.returncode == 0,
              (hidden.stdout + hidden.stderr).strip()[:160]),
        unit_tests_pass(ctx),
        original_asserts_intact(ctx),
        tests_added(ctx),
        ran_tests(ctx),
        no_commits(ctx),
        skill_used(ctx, "df-debug"),
    ]
