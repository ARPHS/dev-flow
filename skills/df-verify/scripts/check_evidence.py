"""Optional completion gate for dev-flow.

Exit 0 (allow) unless `.flow/progress.md` says `状态：done` while its evidence is
missing or bound to a different working-tree fingerprint. In that case print the
reason to stderr and exit 2, which a Claude Code Stop hook treats as "keep working".
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fingerprint import fingerprint  # noqa: E402

FIELD = r"^\s*{}\s*[:：]\s*(.+?)\s*$"


def field(text, name):
    match = re.search(FIELD.format(name), text, re.MULTILINE)
    return match.group(1) if match else None


def has_evidence_row(text):
    section = text.split("## 验证证据", 1)
    if len(section) < 2:
        return False
    body = re.split(r"^## ", section[1], maxsplit=1, flags=re.MULTILINE)[0]
    rows = [line for line in body.splitlines() if line.strip().startswith("|")]
    data = [r for r in rows if not re.match(r"^\|[\s\-:|]+\|$", r.strip())]
    return len(data) >= 2  # header + at least one row


def check(cwd="."):
    progress = Path(cwd) / ".flow" / "progress.md"
    if not progress.is_file():
        return None
    text = progress.read_text(encoding="utf-8")
    if (field(text, "状态") or "").lower() != "done":
        return None
    if not has_evidence_row(text):
        return "progress.md 标记为 done，但「验证证据」表为空。请运行 df-verify 并记录证据。"
    recorded = field(text, "证据版本")
    try:
        current = fingerprint(cwd)
    except Exception:  # not a git repo or git missing: cannot judge
        return None
    if recorded != current:
        return (
            f"progress.md 的证据版本 {recorded} 与当前代码 {current} 不一致，"
            "代码在验证后发生了变化。请重新运行 df-verify 并更新证据版本。"
        )
    return None


def main():
    cwd = "."
    if not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            if payload.get("stop_hook_active"):
                return 0  # avoid infinite loops: block at most once per stop
            cwd = payload.get("cwd") or cwd
        except json.JSONDecodeError:
            pass
    reason = check(cwd)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
