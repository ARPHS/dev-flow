"""dev-flow evidence CLI: run verification commands and bind results to git snapshots.

Subcommands:
    run [-l LABEL] [--full-output] -- PROGRAM [ARGS...]
                         run PROGRAM directly (no shell), print its output (summarizing huge
                         outputs into .dev-flow/logs/), append a record to
                         .dev-flow/evidence.jsonl, and exit with its exit code. The caller's own
                         shell splits the arguments, so bash, PowerShell and cmd behave alike;
                         wrap explicitly (`-- bash -c "..."`) when shell features are needed.
    report [--json]      list the latest record per (label, cwd, cmd) and what changed since each
    fingerprint          print the snapshot id (git tree hash) of the current working tree
    diff BEFORE AFTER   read-only diff between two recorded snapshots (including untracked files)
    doctor [--json]      read-only environment and .dev-flow/ diagnostics (never changes config)

A snapshot is a git tree of the working tree (tracked changes + untracked, non-ignored
files, excluding .dev-flow/ and tool caches such as __pycache__) built in a throwaway index.
Everything is written inside .dev-flow/ (objects, logs, temporary index), never into the
project's .git or %TEMP%, so it also works in sandboxes that only allow writing to the
workspace. The tool only reports facts; whether a change invalidates a record is decided by
the df-verify skill. Standard library only; requires git >= 2.31.
"""

import argparse
import json
import locale
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

STATE = ".dev-flow"
CACHES = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".vitest", ".eslintcache")
CACHE_FILES = (".coverage", ".eslintcache", "*.tsbuildinfo")
# snapshot scope: the work tree minus .dev-flow/ and tool caches that running checks rewrite
SCOPE = [
    ".",
    f":(exclude){STATE}",
    *(f":(exclude,glob)**/{name}/**" for name in CACHES),
    *(f":(exclude,glob)**/{pattern}" for pattern in CACHE_FILES),
]
LARGE_FILE = 10 * 1024 * 1024     # untracked files above this are left out of snapshots
OBJECTS_CAP = 200 * 1024 * 1024   # .dev-flow/objects is cleared when it grows past this
TAIL_LINES = 15
TAIL_CHARS = 2000
MAX_STDOUT_CHARS = 12000          # outputs larger than this are saved to .dev-flow/logs/ and summarized
SHORT = 12
MIN_GIT = (2, 31)
MIN_PYTHON = (3, 8)


class GitError(RuntimeError):
    pass


SAFE_DIRECTORY_HINT = ("仓库所有者与当前用户不同（例如 Codex elevated 沙箱的沙箱用户），git 拒绝访问。这是环境阻碍："
                       "设置环境变量 GIT_CONFIG_COUNT=1、GIT_CONFIG_KEY_0=safe.directory、GIT_CONFIG_VALUE_0=* 后重跑"
                       "（Codex 可写入 shell_environment_policy.set），不要修改项目的 git 配置")


def git_hint(exc):
    return SAFE_DIRECTORY_HINT if "dubious ownership" in str(exc) else None


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), "replace")


def git(args, cwd, env=None, stdin=None):
    proc = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        raise GitError(decode(proc.stderr).strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def git_path(root, name):
    return Path(decode(git(["rev-parse", "--path-format=absolute", "--git-path", name], root)).strip())


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
    return Path(decode(git(["rev-parse", "--show-toplevel"], cwd)).strip()).resolve()


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
        tree = decode(git(["write-tree"], root, env)).strip()
    finally:
        for leftover in (index, index.with_name(index.name + ".lock")):
            leftover.unlink(missing_ok=True)
    return tree, sorted(n.decode("utf-8", "replace") for n in large)


def changes_between(root, objects, old, new):
    """Changed paths between two snapshots, or None when the old snapshot is gone."""
    try:
        out = git(["diff", "--name-status", "--no-renames", "-z", old, new], root, object_env(root, objects))
    except GitError:
        return None
    parts = [p for p in out.split(b"\0") if p]
    return [decode(parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


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


def tail(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-TAIL_LINES:])[-TAIL_CHARS:]


def display(argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def is_executable_target(target, cwd="."):
    """Check whether a single argument containing spaces refers to an actual executable or file."""
    path = Path(target)
    if not path.is_absolute():
        path = Path(cwd) / path
    if path.is_file():
        return True
    return shutil.which(target, path=os.environ.get("PATH")) is not None


def relative_cwd(cwd, root):
    resolved_cwd = Path(cwd).resolve()
    resolved_root = Path(root).resolve()
    try:
        return Path(os.path.relpath(resolved_cwd, resolved_root)).as_posix()
    except ValueError:
        return resolved_cwd.as_posix()


def summarize_output(output, exit_code, log_rel=None):
    """Condense huge outputs while preserving head, tail, and full log pointer."""
    lines = output.splitlines()
    head_n = 20 if exit_code == 0 else 30
    tail_n = 40 if exit_code == 0 else 120
    if len(lines) <= head_n + tail_n:
        # Very long single/few lines: truncate middle by chars
        keep_head = 3000 if exit_code == 0 else 4000
        keep_tail = 5000 if exit_code == 0 else 7000
        omitted = len(output) - keep_head - keep_tail
        pointer = f"（完整日志：{log_rel}）" if log_rel else ""
        return (f"{output[:keep_head]}\n"
                f"... [已省略约 {omitted} 字符{pointer}] ...\n"
                f"{output[-keep_tail:]}\n")
    omitted_lines = len(lines) - head_n - tail_n
    pointer = f"；完整日志：{log_rel}" if log_rel else ""
    parts = [
        "\n".join(lines[:head_n]),
        f"... [已省略中间 {omitted_lines} 行 / 共 {len(output)} 字符{pointer}] ...",
        "\n".join(lines[-tail_n:]),
    ]
    return "\n".join(parts) + "\n"


def save_log(root, label, output):
    logs_dir = root / STATE / "logs"
    logs_dir.mkdir(exist_ok=True)
    safe_label = re.sub(r"[^a-zA-Z0-9._-]+", "_", label or "run").strip("_") or "run"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = logs_dir / f"{stamp}-{safe_label}-{secrets.token_hex(3)}.log"
    log_path.write_text(output, encoding="utf-8")
    return log_path.relative_to(root).as_posix()


def run(argv, label=None, cwd="."):
    """Run `argv` without a shell. Returns (exit_code, output, record_or_None, notes)."""
    program = shutil.which(argv[0], path=os.environ.get("PATH")) or argv[0]  # PATHEXT: npm -> npm.cmd
    started = time.monotonic()
    try:
        proc = subprocess.run([program, *argv[1:]], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        return 127, f"无法启动 {argv[0]}：{exc}\n", None, ["命令未运行，未记录"]
    duration = round(time.monotonic() - started, 2)
    output = decode(proc.stdout)
    command = display(argv)
    try:
        root = repo_root(cwd)
    except GitError as exc:
        hint = git_hint(exc)
        return proc.returncode, output, None, [f"{hint}，未记录"] if hint else []
    except FileNotFoundError:
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
    log_rel = None
    if len(output) > MAX_STDOUT_CHARS:
        try:
            log_rel = save_log(root, label, output)
        except OSError as exc:
            notes.append(f"完整日志保存失败：{exc}")
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "label": label or "",
        "cmd": command,
        "argv": list(argv),
        "cwd": relative_cwd(cwd, root),
        "exit": proc.returncode,
        "duration_s": duration,
        "snapshot": tree,
        "tail": tail(output),
    }
    if log_rel:
        record["log"] = log_rel
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


def status_code(changes):
    if changes is None:
        return "uncomparable"
    return "valid" if not changes else "changed"


def report_data(cwd="."):
    root = repo_root(cwd)
    records = latest_records(load_records(root))
    store = object_store(root)
    now, large = snapshot(root, store)
    entries = []
    for index, r in enumerate(records, start=1):
        old = r.get("snapshot", "")
        changes = [] if old == now else changes_between(root, store, old, now) if old else None
        last = (r.get("tail") or "").splitlines()[-1:] or [""]
        entries.append({
            "index": index,
            "time": r.get("time", ""),
            "label": r.get("label") or "",
            "cmd": r.get("cmd", ""),
            "argv": r.get("argv"),
            "cwd": r.get("cwd") or ".",
            "exit": r.get("exit"),
            "duration_s": r.get("duration_s"),
            "snapshot": old,
            "status": status_code(changes),
            "status_text": describe(changes),
            "changes": changes,
            "tail": r.get("tail", ""),
            "summary": last[0][:80],
            "log": r.get("log"),
        })
    return {
        "snapshot": now,
        "short_snapshot": now[:SHORT],
        "large_files": large,
        "records": entries,
    }


def report(cwd="."):
    data = report_data(cwd)
    lines = [f"## 验证证据（快照：{data['short_snapshot']}）"]
    if not data["records"]:
        lines.append("没有验证记录。用 `evidence.py run` 运行验证。")
        return "\n".join(lines), False
    lines += ["| # | 检查 | 命令 | 目录 | 退出码 | 关键结果 | 状态 |", "|---|---|---|---|---|---|---|"]
    for r in data["records"]:
        cells = [str(r["index"]), r["label"] or "-", f"`{r['cmd']}`", r["cwd"],
                 str(r["exit"]), r["summary"], r["status_text"]]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    if data["large_files"]:
        lines.append("\n未纳入对比的大文件（建议加入 .gitignore）：" + ", ".join(data["large_files"][:5]))
    return "\n".join(lines), True


def fingerprint(cwd="."):
    root = repo_root(cwd)
    return snapshot(root, object_store(root))[0][:SHORT]


def snapshot_diff(before, after, cwd="."):
    root = repo_root(cwd)
    objects = root / STATE / "objects"
    if not objects.is_dir():
        raise StateMissing("没有可读取的快照对象目录")
    return decode(git(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                       before, after, "--"], root, object_env(root, objects)))


# ---- doctor ----

def parse_git_version(text):
    match = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def doctor(cwd="."):
    """Read-only environment & state check. Never mutates user config or workspace."""
    checks = []
    py_ver = sys.version_info[:3]
    py_ok = py_ver[:2] >= MIN_PYTHON
    checks.append({
        "name": "python",
        "ok": py_ok,
        "detail": f"{py_ver[0]}.{py_ver[1]}.{py_ver[2]} ({sys.executable})",
    })

    git_exe = shutil.which("git")
    git_ok = False
    git_detail = "未找到 git"
    if git_exe:
        try:
            ver_out = decode(git(["--version"], cwd)).strip()
            parsed = parse_git_version(ver_out)
            git_ok = parsed is not None and parsed >= MIN_GIT
            git_detail = ver_out if git_ok else f"{ver_out}（需要 >= {MIN_GIT[0]}.{MIN_GIT[1]}）"
        except (GitError, OSError) as exc:
            git_detail = str(exc)
    checks.append({"name": "git", "ok": git_ok, "detail": git_detail})

    root = None
    repo_ok = False
    repo_detail = "当前目录不在 Git 仓库中"
    dirty_files = []
    if git_ok:
        try:
            root = repo_root(cwd)
            raw_status = git(["status", "--porcelain", "-z", "--untracked-files=all"], root)
            entries = [e for e in raw_status.split(b"\0") if e]
            i = 0
            while i < len(entries):
                entry = decode(entries[i])
                if len(entry) >= 3:
                    code, path_str = entry[:2], entry[3:]
                    dirty_files.append(path_str)
                    if "R" in code or "C" in code:
                        i += 1
                i += 1
            repo_ok = True
            repo_detail = f"{root.as_posix()}（当前未提交文件：{len(dirty_files)}）"
        except GitError as exc:
            repo_ok = False
            repo_detail = git_hint(exc) or str(exc)
    checks.append({"name": "git_repo", "ok": repo_ok, "detail": repo_detail})

    state_info = {"present": False, "gitignore_ok": False, "project_md": False, "evidence_count": 0}
    if root is not None:
        state_dir = root / STATE
        if state_dir.is_dir():
            state_info["present"] = True
            gi = state_dir / ".gitignore"
            state_info["gitignore_ok"] = gi.is_file() and "*" in gi.read_text(encoding="utf-8", errors="replace").splitlines()
            state_info["project_md"] = (state_dir / "project.md").is_file()
            state_info["evidence_count"] = len(load_records(root))
            state_detail = (f"已存在（.gitignore={'OK' if state_info['gitignore_ok'] else '缺失'}, "
                            f"project.md={'有' if state_info['project_md'] else '无'}, "
                            f"证据记录={state_info['evidence_count']}）")
        else:
            state_detail = "未创建（quick 任务无需创建；feature/project 开工前创建）"
        checks.append({"name": "state_dir", "ok": True, "detail": state_detail})

    tips = []
    if os.name == "nt" and os.environ.get("PYTHONUTF8") != "1":
        tips.append("Windows 环境建议设置 PYTHONUTF8=1，避免中文输出或文件读写出现 GBK 编码问题")
    if os.name != "nt":
        tips.append("Linux/WSL 环境使用 Bash 与 Linux 路径；无 python 命令时改用 python3")

    return {
        "ok": py_ok and git_ok and repo_ok,
        "platform": sys.platform,
        "checks": checks,
        "state": state_info,
        "dirty_count": len(dirty_files),
        "tips": tips,
    }


def format_doctor(info):
    lines = ["## dev-flow 环境诊断（只读）"]
    for c in info["checks"]:
        icon = "✅" if c["ok"] else "❌"
        lines.append(f"- {icon} {c['name']}：{c['detail']}")
    if info["tips"]:
        lines.append("### 提示")
        for tip in info["tips"]:
            lines.append(f"- {tip}")
    return "\n".join(lines)


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
    run_parser.add_argument("--full-output", action="store_true", help="即使输出很长也完整打印到 stdout")
    run_parser.add_argument("cmd", nargs=argparse.REMAINDER,
                            help="-- 之后的程序和参数，直接执行、不经过 shell")
    report_parser = sub.add_parser("report", help="列出验证记录及之后的改动")
    report_parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结构化报告")
    sub.add_parser("fingerprint", help="打印当前工作区的快照 id")
    diff_parser = sub.add_parser("diff", help="只读查看两个快照之间的完整改动")
    diff_parser.add_argument("before", help="开工快照 id")
    diff_parser.add_argument("after", help="候选版本快照 id")
    doctor_parser = sub.add_parser("doctor", help="只读检查 Python、Git、仓库与 .dev-flow/ 状态")
    doctor_parser.add_argument("--json", action="store_true", help="以 JSON 格式输出诊断结果")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        info = doctor()
        print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else format_doctor(info))
        return 0 if info["ok"] else 1

    try:
        if args.command == "diff":
            if not all(re.fullmatch(r"[0-9a-fA-F]{12,64}", value) for value in (args.before, args.after)):
                parser.error("快照 id 必须是 12 到 64 位十六进制字符")
            sys.stdout.write(snapshot_diff(args.before, args.after))
            return 0
        if args.command == "fingerprint":
            print(fingerprint())
            return 0
        if args.command == "report":
            if args.json:
                data = report_data()
                print(json.dumps(data, ensure_ascii=False, indent=2))
                return 0 if data["records"] else 1
            text, ok = report()
            print(text)
            return 0 if ok else 1
    except StateMissing as exc:
        print(f"evidence.py: {exc}", file=sys.stderr)
        return 1
    except (GitError, FileNotFoundError) as exc:
        message = git_hint(exc) or (f"无法读取快照差异：{exc}" if args.command == "diff" else
                                    f"需要在 Git 仓库中运行，且 git >= 2.31（{exc}）")
        print(f"evidence.py: {message}", file=sys.stderr)
        return 1

    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("用法：run [-l LABEL] -- <程序> [参数...]")
    if len(cmd) == 1 and " " in cmd[0].strip() and not is_executable_target(cmd[0]):
        parser.error("命令被整体当成了一个参数。请去掉外层引号：run -- python -m pytest -q；"
                     "需要 shell 特性时显式写：run -- bash -c \"...\"")
    code, output, record, notes = run(cmd, label=args.label)
    shown = output
    log_rel = record.get("log") if record else None
    if not args.full_output and log_rel and len(output) > MAX_STDOUT_CHARS:
        shown = summarize_output(output, code, log_rel=log_rel)
    sys.stdout.write(shown if shown.endswith("\n") or not shown else shown + "\n")
    for note in notes:
        print(f"[evidence] {note}")
    if record:
        print(f"[evidence] exit={code} snapshot={record['snapshot'][:SHORT]} 已记录到 {STATE}/evidence.jsonl")
    elif not notes:
        print(f"[evidence] exit={code} 未记录（不在 Git 仓库或没有 {STATE}/）")
    return code


if __name__ == "__main__":
    sys.exit(main())
