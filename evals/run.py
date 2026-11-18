"""Run dev-flow behavior evals against a real agent CLI.

Each case copies a fixture into a fresh git repo, sends the case turns to the
agent CLI, then grades the workspace and transcript with `cases/<name>/grade.py`.

- claude: `claude -p --plugin-dir <plugin>`; follow-up turns use `--continue`.
- codex:  `codex exec --json` in the default workspace-write sandbox; follow-up
  turns resume the thread. Uses the CODEX_HOME from the environment (set it to a
  dedicated eval home holding only model/auth settings). The plugin is reinstalled
  from this working tree before the run, because Codex loads it from its cache.

Examples:
    python -X utf8 evals/run.py                     # all cases, with plugin
    python -X utf8 evals/run.py --case feature debug
    python -X utf8 evals/run.py --no-plugin         # baseline for A/B comparison
    python -X utf8 evals/run.py --agent codex --case feature
    python -X utf8 evals/run.py --grade-only <run-dir>

Warning: claude runs with --dangerously-skip-permissions inside temporary
workspaces. Run on a machine/account where that is acceptable.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

EVALS = Path(__file__).resolve().parent
ROOT = EVALS.parent / "plugins/dev-flow"
sys.path.insert(0, str(EVALS))
from lib import FIXTURES, Context  # noqa: E402


def load_cases(names=None):
    cases = {}
    for path in sorted((EVALS / "cases").iterdir()):
        if (path / "case.json").exists() and (not names or path.name in names):
            cases[path.name] = json.loads((path / "case.json").read_text(encoding="utf-8"))
    missing = set(names or []) - set(cases)
    if missing:
        sys.exit(f"unknown case: {', '.join(sorted(missing))}")
    return cases


def load_grader(name):
    spec = importlib.util.spec_from_file_location(f"grade_{name}", EVALS / "cases" / name / "grade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.grade


def git(ws, *args):
    return subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@local", *args],
                          cwd=ws, capture_output=True, text=True, check=True).stdout.strip()


def prepare(fixture, ws):
    shutil.copytree(FIXTURES / fixture, ws, ignore=shutil.ignore_patterns("__pycache__"))
    git(ws, "init", "-q")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "fixture")
    return git(ws, "rev-parse", "HEAD")


def snapshot(ws):
    return sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(ws).parts
                  and "__pycache__" not in p.parts)


def find_exe(name):
    exe = shutil.which(name)
    if not exe:
        sys.exit(f"{name} CLI not found on PATH")
    return exe


CODEX_PLUGIN = "dev-flow@dev-flow"


def install_codex_plugin(enabled=True):
    """Refresh Codex's cached copy of the plugin from this working tree, or uninstall it for a baseline.

    A `-c plugins."dev-flow@dev-flow".enabled=false` override is not honoured, so baselines uninstall.
    """
    exe = find_exe("codex")
    quiet = {"capture_output": True, "text": True, "encoding": "utf-8"}
    subprocess.run([exe, "plugin", "remove", CODEX_PLUGIN], **quiet)
    if not enabled:
        return
    subprocess.run([exe, "plugin", "marketplace", "add", str(EVALS.parent)], **quiet)
    proc = subprocess.run([exe, "plugin", "add", CODEX_PLUGIN], **quiet)
    if proc.returncode != 0:
        sys.exit(f"codex plugin add failed: {proc.stderr.strip()[-300:]}")


def codex_command(args, thread):
    cmd = [find_exe("codex"), "exec"]
    if thread:
        cmd += ["resume", thread]
    cmd += ["--json", "--skip-git-repo-check", "-c", 'sandbox_mode="workspace-write"']
    if args.model:
        cmd += ["-m", args.model]
    return cmd + ["-"]


def thread_id(text):
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            return event.get("thread_id")
    return None


COLLAB_TOOLS = {"spawn_agent", "followup_task", "wait_agent"}


def codex_collab_calls(thread):
    """`codex exec --json` does not emit collaboration tool calls; read them from the session rollout."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    calls = []
    for path in sorted((home / "sessions").rglob(f"rollout-*{thread}.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            if payload.get("type") == "function_call" and payload.get("name") in COLLAB_TOOLS:
                try:
                    arguments = json.loads(payload.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                arguments.pop("message", None)  # stored encrypted; not useful for grading
                calls.append({"type": "item.completed", "source": "rollout", "item": {
                    "type": "collab_tool_call", "tool": payload["name"], "arguments": arguments}})
    return calls


def agent_command(args, turn):
    exe = find_exe("claude")
    cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    if not args.no_plugin:
        cmd += ["--plugin-dir", str(ROOT)]
    if args.model:
        cmd += ["--model", args.model]
    if args.max_turns:
        cmd += ["--max-turns", str(args.max_turns)]
    if turn > 0:
        cmd.append("--continue")
    return cmd


TURN_DONE = ('"type":"turn.completed"', '"type":"result"')  # codex / claude end-of-turn events
GRACE = 30  # seconds a CLI may linger after its turn ended (e.g. a background subagent)


def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        import signal
        os.killpg(proc.pid, signal.SIGKILL)


def run_turn(command, cwd, prompt, timeout, grace=GRACE):
    """Run one agent turn; return (stdout, stderr-or-note).

    The process tree is killed on timeout, or shortly after the end-of-turn event if the
    CLI keeps running: a plain subprocess timeout only kills the direct child and then
    blocks on pipes still held by grandchildren.
    """
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **group)
    out, err, done = [], [], threading.Event()

    def read_out():
        for line in proc.stdout:
            out.append(line)
            if any(marker in line.replace(" ", "") for marker in TURN_DONE):
                done.set()

    readers = [threading.Thread(target=read_out, daemon=True),
               threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)]
    for reader in readers:
        reader.start()
    proc.stdin.write(prompt)
    proc.stdin.close()

    deadline, note = time.time() + timeout, ""
    while proc.poll() is None:
        if time.time() > deadline:
            note = f"timeout after {timeout}s"
        elif done.wait(1) and proc.poll() is None:
            try:
                proc.wait(grace)
            except subprocess.TimeoutExpired:
                note = f"still running {grace}s after the turn ended; killed"
        if note:
            kill_tree(proc)
            break
    for reader in readers:
        reader.join(10)
    for pipe in (proc.stdout, proc.stderr):
        pipe.close()
    proc.wait(10)
    return "".join(out), "\n".join(filter(None, ["".join(err).strip(), note]))


def run_case(name, case, out, args):
    case_dir = out / name
    ws = case_dir / "workspace"
    case_dir.mkdir(parents=True)
    base = prepare(case["fixture"], ws)
    transcripts, snapshots = [], []
    stats = {"cost_usd": 0.0, "agent_turns": 0, "seconds": 0.0, "tokens": 0}
    thread, collab_seen = None, 0
    for index, prompt in enumerate(case["turns"]):
        start = time.time()
        if args.agent == "codex":
            if index > 0 and not thread:
                break  # first turn never started a thread; nothing to resume
            command = codex_command(args, thread if index > 0 else None)
        else:
            command = agent_command(args, index)
        try:
            text, err = run_turn(command, ws, prompt, args.timeout)
        except OSError as exc:
            text, err = "", f"failed to start: {exc}"
        stats["seconds"] += time.time() - start
        thread = thread or (thread_id(text) if args.agent == "codex" else None)
        if thread:
            calls = codex_collab_calls(thread)
            text += "".join("\n" + json.dumps(c, ensure_ascii=False) for c in calls[collab_seen:])
            collab_seen = len(calls)
        (case_dir / f"turn{index + 1}.jsonl").write_text(text, encoding="utf-8")
        if err:
            (case_dir / f"turn{index + 1}.stderr.txt").write_text(err, encoding="utf-8")
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                stats["cost_usd"] += event.get("total_cost_usd") or 0
                stats["agent_turns"] += event.get("num_turns") or 0
            elif isinstance(event, dict) and event.get("type") == "turn.completed":  # codex
                usage = event.get("usage") or {}
                stats["tokens"] += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                stats["agent_turns"] += 1
        transcripts.append(text)
        snapshots.append(snapshot(ws))
    meta = {"case": name, "fixture": case["fixture"], "base": base, "snapshots": snapshots,
            "stats": stats, "plugin": not args.no_plugin, "model": args.model, "agent": args.agent}
    (case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return grade_case(case_dir)


def grade_case(case_dir):
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    turns = sorted(case_dir.glob("turn*.jsonl"), key=lambda p: int(p.stem[4:]))
    ctx = Context(ws=case_dir / "workspace", fixture=FIXTURES / meta["fixture"], base=meta["base"],
                  transcripts=[p.read_text(encoding="utf-8") for p in turns], snapshots=meta["snapshots"])
    try:
        checks = [c.to_dict() for c in load_grader(meta["case"])(ctx)]
    except Exception as exc:  # grader crash is a failure, not a runner crash
        checks = [{"name": "grader 运行", "ok": False, "detail": repr(exc), "required": True}]
    required = [c for c in checks if c["required"]]
    soft = [c for c in checks if not c["required"]]
    result = {
        "case": meta["case"], "passed": all(c["ok"] for c in required),
        "required": f"{sum(c['ok'] for c in required)}/{len(required)}",
        "soft": f"{sum(c['ok'] for c in soft)}/{len(soft)}",
        "stats": meta["stats"], "checks": checks,
    }
    (case_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def summarize(results, out):
    lines = ["| 用例 | 结果 | 必需 | 参考 | 耗时(s) | 成本($) | tokens |", "|---|---|---|---|---|---|---|"]
    for r in results:
        s = r["stats"]
        lines.append(f"| {r['case']} | {'✅' if r['passed'] else '❌'} | {r['required']} | {r['soft']} "
                     f"| {s['seconds']:.0f} | {s['cost_usd']:.2f} | {s.get('tokens', 0)} |")
    lines.append("")
    for r in results:
        for c in r["checks"]:
            if not c["ok"]:
                tag = "必需" if c["required"] else "参考"
                lines.append(f"- {r['case']} [{tag}] {c['name']}：{c['detail']}")
    text = "\n".join(lines)
    (out / "summary.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\n结果目录：{out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    parser.add_argument("--case", nargs="*", help="只运行指定用例")
    parser.add_argument("--no-plugin", action="store_true", help="不加载插件，用于对照")
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int, default=1800, help="每轮超时秒数")
    parser.add_argument("--out", type=Path, help="结果目录，默认在系统临时目录")
    parser.add_argument("--grade-only", type=Path, help="对已有结果目录重新评分")
    args = parser.parse_args()

    if args.grade_only:
        dirs = sorted(d for d in args.grade_only.iterdir() if (d / "meta.json").exists())
        summarize([grade_case(d) for d in dirs], args.grade_only)
        return

    if args.agent == "codex":
        install_codex_plugin(enabled=not args.no_plugin)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{args.agent}" + ("-noplugin" if args.no_plugin else "")
    out = args.out or Path(tempfile.gettempdir()) / "dev-flow-evals" / stamp
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for name, case in load_cases(args.case).items():
        print(f"▶ {name}: {case['description']}", flush=True)
        results.append(run_case(name, case, out, args))
    summarize(results, out)
    sys.exit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()
