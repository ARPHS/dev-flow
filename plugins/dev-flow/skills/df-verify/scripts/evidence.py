"""dev-flow evidence CLI: run verification commands and bind results to git snapshots.

Subcommands:
    run [-l LABEL] -- PROGRAM [ARGS...]
                         run PROGRAM directly (no shell), print its output, append a record to
                         .dev-flow/evidence.jsonl, and exit with its exit code. The caller's own
                         shell splits the arguments, so bash, PowerShell and cmd behave alike;
                         wrap explicitly (`-- bash -c "..."`) when shell features are needed.
    report               list the latest record per (label, command) and what changed since each
    fingerprint          print the snapshot id (git tree hash) of the current working tree

A snapshot is a git tree of the working tree (tracked changes + untracked, non-ignored
files, excluding .dev-flow/ and caches such as __pycache__) built in a throwaway index. Everything is written inside
.dev-flow/ (objects, temporary index), never into the project's .git or %TEMP%, so it
also works in sandboxes that only allow writing to the workspace. The tool only reports
facts; whether a change invalidates a record is decided by the df-verify skill.
Standard library only; requires git >= 2.31.
"""

import argparse
import json
import locale
import os
import shlex
import secrets
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STATE = ".dev-flow"
CACHES = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
# snapshot scope: the work tree minus .dev-flow/ and tool caches that running checks rewrite
SCOPE = [".", f":(exclude){STATE}", *(f":(exclude,glob)**/{name}/**" for name in CACHES)]
LARGE_FILE = 10 * 1024 * 1024    # untracked files above this are left out of snapshots
OBJECTS_CAP = 200 * 1024 * 1024  # .dev-flow/objects is cleared when it grows past this
TAIL_LINES = 15
TAIL_CHARS = 2000
SHORT = 12


class GitError(RuntimeError):
    pass


def git(args, cwd, env=None, stdin=None):
    proc = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        raise GitError(decode(proc.stderr).strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def git_path(root, name):
    return Path(git(["rev-parse", "--path-format=absolute", "--git-path", name], root).decode().strip())


def remove_tree(path):
    """Delete a directory, including git's read-only object files on Windows."""
    def retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def repo_root(cwd="."):
    return Path(git(["rev-parse", "--show-toplevel"], cwd).decode().strip())


# ---- snapshots ----

def object_env(root, objects):
    return {"GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_path(root, "objects"))}


class StateMissing(RuntimeError):
    pass


def object_store(root):
    """Object directory for snapshots. Everything evidence.py writes stays inside .dev-flow/:
    sandboxes (e.g. Codex on Windows) may forbid writing to %TEMP% or to the project's .git."""
    state = root / STATE
    if not state.is_dir():
        raise StateMissing(f"没有 {STATE}/ 目录。快照只在 {STATE}/ 任务中使用")
    objects = state / "objects"
    objects.mkdir(exist_ok=True)
    return objects


def snapshot(root, objects):
    """Return (tree_hash, large_files_left_out) for the current working tree."""
    env = object_env(root, objects)
    index = objects.parent / f"index-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        real = git_path(root, "index")
        if real.is_file():
            # copy2 keeps the index mtime: git's racy-clean check compares entries against it,
            # and a newer mtime would make same-size edits within one second look unchanged.
            shutil.copy2(real, index)
        env["GIT_INDEX_FILE"] = str(index)
        # git 2.4x rejects `add -u <pathspec>` when no tracked file matches (e.g. no commits yet)
        if git(["ls-files", "-z", "--", *SCOPE], root, env):
            git(["add", "-u", "--", *SCOPE], root, env)
        listed = git(["ls-files", "--others", "--exclude-standard", "-z", "--", *SCOPE], root, env)
        small, large = [], []
        for name in (n for n in listed.split(b"\0") if n):
            path = root / name.decode("utf-8", "surrogateescape")
            (large if path.is_file() and path.stat().st_size > LARGE_FILE else small).append(name)
        if small:
            git(["--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul"],
                root, env, stdin=b"\0".join(small))
        tree = git(["write-tree"], root, env).decode().strip()
    finally:
        for leftover in (index, index.with_name(index.name + ".lock")):
            leftover.unlink(missing_ok=True)
    return tree, sorted(n.decode("utf-8", "replace") for n in large)


def changes_between(root, objects, old, new):
    """Changed paths between two snapshots, or None when the old snapshot is gone."""
    try:
        out = git(["diff", "--name-status", "--no-renames", old, new], root, object_env(root, objects))
    except GitError:
        return None
    return [line.split("\t", 1)[1] for line in decode(out).splitlines() if "\t" in line]


def dir_size(path):
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


# ---- records ----

def evidence_file(root):
    return root / STATE / "evidence.jsonl"


def load_records(root):
    path = evidence_file(root)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def latest_records(records):
    """Latest record per (label, cwd, cmd): a baseline run is kept apart from a later AC run."""
    latest = {}
    for record in records:
        key = (record.get("label"), record.get("cwd"), record.get("cmd"))
        latest.pop(key, None)
        latest[key] = record
    return list(latest.values())


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), "replace")


def tail(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-TAIL_LINES:])[-TAIL_CHARS:]


def display(argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def run(argv, label=None, cwd="."):
    """Run `argv` without a shell. Returns (exit_code, output, record_or_None, notes)."""
    program = shutil.which(argv[0], path=os.environ.get("PATH")) or argv[0]  # PATHEXT: npm -> npm.cmd
    try:
        proc = subprocess.run([program, *argv[1:]], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        return 127, f"无法启动 {argv[0]}：{exc}\n", None, ["命令未运行，未记录"]
    output = decode(proc.stdout)
    command = display(argv)
    try:
        root = repo_root(cwd)
    except (GitError, FileNotFoundError):
        return proc.returncode, output, None, []
    if not (root / STATE).is_dir():
        return proc.returncode, output, None, []
    notes = []
    objects = root / STATE / "objects"
    if objects.is_dir() and dir_size(objects) > OBJECTS_CAP:
        try:
            remove_tree(objects)
            notes.append("快照目录超过上限已清空，更早的记录将显示为\"无法对比\"")
        except OSError as exc:
            notes.append(f"快照目录超过上限，但清空失败：{exc}")
    try:
        tree, large = snapshot(root, object_store(root))  # after the run: covers files it changed
    except GitError as exc:
        return proc.returncode, output, None, [f"快照失败，未记录：{exc}"]
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "label": label or "",
        "cmd": command,
        "cwd": Path(os.path.relpath(Path(cwd).resolve(), root)).as_posix(),
        "exit": proc.returncode,
        "snapshot": tree,
        "tail": tail(output),
    }
    with evidence_file(root).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if large:
        notes.append("未纳入快照的大文件：" + ", ".join(large[:5]))
    return proc.returncode, output, record, notes


def describe(changes):
    if changes is None:
        return "无法对比（快照已清理），需重跑"
    if not changes:
        return "当前有效"
    shown = ", ".join(changes[:5])
    more = f" 等 {len(changes)} 个" if len(changes) > 5 else ""
    return f"之后改动：{shown}{more}"


def report(cwd="."):
    root = repo_root(cwd)
    records = latest_records(load_records(root))
    store = object_store(root)
    now, large = snapshot(root, store)
    rows = []
    for r in records:
        old = r.get("snapshot", "")
        changes = [] if old == now else changes_between(root, store, old, now) if old else None
        rows.append((r, describe(changes)))
    lines = [f"## 验证证据（快照：{now[:SHORT]}）"]
    if not rows:
        lines.append("没有验证记录。用 `evidence.py run` 运行验证。")
        return "\n".join(lines), False
    lines += ["| # | 检查 | 命令 | 目录 | 退出码 | 关键结果 | 状态 |", "|---|---|---|---|---|---|---|"]
    for index, (r, state) in enumerate(rows, start=1):
        last = (r.get("tail") or "").splitlines()[-1:] or [""]
        cells = [str(index), r.get("label") or "-", f"`{r['cmd']}`", r.get("cwd") or ".",
                 str(r.get("exit")), last[0][:80], state]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    if large:
        lines.append("\n未纳入对比的大文件（建议加入 .gitignore）：" + ", ".join(large[:5]))
    return "\n".join(lines), True


def fingerprint(cwd="."):
    root = repo_root(cwd)
    return snapshot(root, object_store(root))[0][:SHORT]


# ---- CLI ----

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # stable for agents reading pipes
    parser = argparse.ArgumentParser(prog="evidence.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="运行命令并记录证据")
    run_parser.add_argument("-l", "--label", help="对应的验收条件或检查名，例如 AC1、baseline、lint")
    run_parser.add_argument("cmd", nargs=argparse.REMAINDER,
                            help="-- 之后的程序和参数，直接执行、不经过 shell")
    sub.add_parser("report", help="列出验证记录及之后的改动")
    sub.add_parser("fingerprint", help="打印当前工作区的快照 id")
    args = parser.parse_args(argv)

    try:
        if args.command == "fingerprint":
            print(fingerprint())
            return 0
        if args.command == "report":
            text, ok = report()
            print(text)
            return 0 if ok else 1
    except StateMissing as exc:
        print(f"evidence.py: {exc}", file=sys.stderr)
        return 1
    except (GitError, FileNotFoundError) as exc:
        print(f"evidence.py: 需要在 Git 仓库中运行，且 git >= 2.31（{exc}）", file=sys.stderr)
        return 1

    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("用法：run [-l LABEL] -- <程序> [参数...]")
    if len(cmd) == 1 and " " in cmd[0].strip():
        parser.error("命令被整体当成了一个参数。请去掉外层引号：run -- python -m pytest -q；"
                     "需要 shell 特性时显式写：run -- bash -c \"...\"")
    code, output, record, notes = run(cmd, label=args.label)
    sys.stdout.write(output if output.endswith("\n") or not output else output + "\n")
    for note in notes:
        print(f"[evidence] {note}")
    if record:
        print(f"[evidence] exit={code} snapshot={record['snapshot'][:SHORT]} 已记录到 {STATE}/evidence.jsonl")
    elif not notes:
        print(f"[evidence] exit={code} 未记录（不在 Git 仓库或没有 {STATE}/）")
    return code


if __name__ == "__main__":
    sys.exit(main())
