"""Shared helpers for eval graders.

A grader is `cases/<name>/grade.py` exposing `grade(ctx) -> list[Check]`.
A case passes when every check with `required=True` passes; soft checks are
reported for trend tracking only.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures"
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".rb", ".kt", ".swift", ".c", ".cpp"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True

    def to_dict(self):
        return asdict(self)


@dataclass
class Context:
    ws: Path
    fixture: Path
    base: str
    transcripts: list = field(default_factory=list)  # raw stream-json text per turn
    snapshots: list = field(default_factory=list)    # sorted file lists after each turn

    # ---- transcript ----
    def events(self, turn=None):
        texts = self.transcripts if turn is None else [self.transcripts[turn]]
        for text in texts:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    def tool_uses(self, turn=None):
        for event in self.events(turn):
            if event.get("type") != "assistant":
                continue
            for item in event.get("message", {}).get("content", []) or []:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    yield item.get("name", ""), item.get("input", {}) or {}

    def bash_commands(self, turn=None):
        return [i.get("command", "") for n, i in self.tool_uses(turn) if n in ("Bash", "PowerShell")]

    def skills_invoked(self, turn=None):
        names = []
        for name, data in self.tool_uses(turn):
            if name == "Skill":
                names.append(str(data.get("skill") or data.get("command") or data.get("name") or ""))
        return names

    def final_text(self, turn=-1):
        if not self.transcripts:
            return ""
        result = ""
        for event in self.events(turn if turn >= 0 else len(self.transcripts) - 1):
            if event.get("type") == "result":
                result = event.get("result") or ""
        return result

    def ran(self, pattern, turn=None):
        regex = re.compile(pattern)
        return [c for c in self.bash_commands(turn) if regex.search(c)]

    def first_index(self, predicate, turn=None):
        for index, (name, data) in enumerate(self.tool_uses(turn)):
            if predicate(name, data):
                return index
        return None

    # ---- workspace ----
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.ws, capture_output=True, text=True, encoding="utf-8")

    def changed_files(self):
        committed = self.git("diff", "--name-only", self.base).stdout.split()
        status = self.git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
        current = [line[3:].strip().strip('"') for line in status if line.strip()]
        return sorted(set(committed) | set(current))

    def new_commits(self):
        out = self.git("rev-list", f"{self.base}..HEAD").stdout.split()
        return len(out)

    def run(self, args, env=None, input=None, timeout=120):
        merged = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
        return subprocess.run(args, cwd=self.ws, capture_output=True, text=True, encoding="utf-8",
                              env=merged, input=input, timeout=timeout)

    def python(self, *args, **kwargs):
        return self.run([sys.executable, *args], **kwargs)


# ---- reusable checks ----

def no_commits(ctx):
    count = ctx.new_commits()
    return Check("未擅自提交", count == 0, f"新提交数 {count}")


def unit_tests_pass(ctx):
    proc = ctx.python("-m", "unittest", "discover", "-s", "tests")
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return Check("项目测试通过", proc.returncode == 0, f"exit={proc.returncode} {' | '.join(tail)}")


def original_asserts_intact(ctx, rel_dir="tests"):
    missing = []
    for original in (ctx.fixture / rel_dir).rglob("*.py"):
        target = ctx.ws / original.relative_to(ctx.fixture)
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        current_lines = {line.strip() for line in current.splitlines()}
        for line in original.read_text(encoding="utf-8").splitlines():
            if "assert" in line and line.strip() not in current_lines:
                missing.append(f"{original.name}: {line.strip()}")
    return Check("原有断言未被删改", not missing, "; ".join(missing[:3]))


def count_tests(root, rel_dir="tests"):
    return sum(len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8"), re.MULTILINE))
               for p in (Path(root) / rel_dir).rglob("*.py"))


def tests_added(ctx, rel_dir="tests"):
    before, after = count_tests(ctx.fixture, rel_dir), count_tests(ctx.ws, rel_dir)
    return Check("新增了测试", after > before, f"{before} → {after}")


def ran_tests(ctx, turn=None):
    hits = ctx.ran(r"unittest|pytest", turn)
    return Check("实际运行了测试命令", bool(hits), hits[0][:120] if hits else "未发现测试命令")


def baseline_before_edit(ctx, required=False):
    first_edit = ctx.first_index(lambda n, d: n in ("Edit", "Write", "MultiEdit", "NotebookEdit"))
    first_test = ctx.first_index(lambda n, d: n in ("Bash", "PowerShell") and re.search(r"unittest|pytest", d.get("command", "")))
    ok = first_test is not None and (first_edit is None or first_test < first_edit)
    return Check("修改前先跑基线", ok, f"首次测试#{first_test} 首次编辑#{first_edit}", required)


def no_flow(ctx):
    return Check("未创建 .dev-flow/", not (ctx.ws / ".dev-flow").exists())


def skill_used(ctx, name, required=False, expect=True):
    used = any(name in s for s in ctx.skills_invoked())
    label = f"{'调用' if expect else '未调用'} {name}"
    return Check(label, used == expect, ", ".join(ctx.skills_invoked()) or "无技能调用", required)


def code_files(files):
    return [f for f in files if Path(f).suffix in CODE_SUFFIXES and not f.startswith(".dev-flow/")]
