"""Run dev-flow behavior evals against a real agent CLI.

Each case copies a fixture into a fresh git repo, sends the case turns to the
agent CLI, then grades the workspace and transcript with `cases/<name>/grade.py`.

- claude: `claude -p --plugin-dir <plugin>`; follow-up turns use `--continue`.
- codex:  `codex exec --json` in the default workspace-write sandbox; follow-up
  turns resume the thread. Uses a dedicated CODEX_HOME from the environment
  (holding only model/auth settings) so the user's default ~/.codex plugin setup
  is not modified. The plugin is reinstalled from this working tree before the
  run, because Codex loads it from its cache.

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
from lib import FIXTURES, Context, execution_checks, is_review_request  # noqa: E402


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


def ensure_isolated_codex_home(allow_default=False):
    """Refuse to reinstall/uninstall plugins in the user's default ~/.codex unless explicitly allowed."""
    if allow_default:
        return
    env_home = os.environ.get("CODEX_HOME", "").strip()
    default_home = (Path.home() / ".codex").resolve()
    if not env_home or Path(env_home).expanduser().resolve() == default_home:
        sys.exit(
            "安全保护：--agent codex 会卸载/重装 dev-flow@dev-flow 插件缓存。\n"
            "请先将 CODEX_HOME 指向专用评测目录（避免改动你的默认 ~/.codex 配置），\n"
            "例如：CODEX_HOME=/tmp/codex-eval python3 evals/run.py --agent codex\n"
            "如确认要在默认目录运行，请显式传入 --allow-default-codex-home。"
        )


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


def _extract_agent_id(output_text):
    try:
        data = json.loads(output_text)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        aid = data.get("agent_id") or data.get("id")
        return str(aid) if aid else None
    return None


def _is_completed_agent_state(state):
    if isinstance(state, str):
        return state.strip().lower() == "completed"
    if isinstance(state, dict):
        if isinstance(state.get("completed"), str) and state["completed"].strip():
            return True
        status_val = state.get("status") or state.get("state")
        if isinstance(status_val, str) and status_val.strip().lower() == "completed":
            return True
    return False


def _analyze_collab_output(tool_name, output_text):
    """Return (output_present, output_error, completed_agents) for a collaboration tool output."""
    text = (output_text or "").strip()
    if not text:
        return False, True, []
    if tool_name != "wait_agent":
        lowered = text.lower()
        is_err = any(m in lowered for m in ("error:", '"error"', "failed"))
        return True, is_err, []

    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None

    if isinstance(parsed, dict):
        if parsed.get("timed_out") is True or parsed.get("error") or parsed.get("errored"):
            return True, True, []
        status = parsed.get("status")
        if isinstance(status, dict):
            completed = [str(aid) for aid, st in status.items() if _is_completed_agent_state(st)]
            if not completed:
                return True, True, []
            return True, False, completed
        if isinstance(status, str):
            return True, status.strip().lower() != "completed", []
        state = parsed.get("state")
        if isinstance(state, str):
            return True, state.strip().lower() != "completed", []
        return True, True, []

    return True, True, []


def codex_collab_calls(thread):
    """`codex exec --json` does not emit collaboration tool calls; read them and their outputs from rollout."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    calls = []
    outputs = {}
    raw_items = []
    for path in sorted((home / "sessions").rglob(f"rollout-*{thread}.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            ptype = payload.get("type")
            if ptype == "function_call_output" and payload.get("call_id"):
                out_val = payload.get("output")
                outputs[payload["call_id"]] = out_val if isinstance(out_val, str) else json.dumps(out_val, ensure_ascii=False)
            elif ptype == "function_call" and payload.get("name") in COLLAB_TOOLS:
                raw_items.append(payload)

    for payload in raw_items:
        try:
            arguments = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        reviewer = is_review_request(arguments)
        arguments.pop("message", None)  # stored encrypted; not useful for grading
        call_id = payload.get("call_id")
        tool_name = payload["name"]
        item = {"type": "collab_tool_call", "tool": tool_name, "arguments": arguments, "reviewer": reviewer}
        if call_id:
            item["call_id"] = call_id
            if call_id in outputs:
                text = outputs[call_id]
                present, is_err, completed = _analyze_collab_output(tool_name, text)
                item["output_present"] = present
                item["output_error"] = is_err
                item["output_preview"] = text.strip()[:200]
                if tool_name == "wait_agent":
                    item["completed_agents"] = completed
                elif tool_name == "spawn_agent":
                    aid = _extract_agent_id(text)
                    if aid:
                        item["agent_id"] = aid
            else:
                item["output_present"] = False
                item["output_error"] = True
        calls.append({"type": "item.completed", "source": "rollout", "item": item})
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


def run_turn_detailed(command, cwd, prompt, timeout, grace=GRACE):
    """Run one agent turn; return (stdout, stderr-or-note, status_dict).

    The process tree is killed on timeout, or shortly after the end-of-turn event if the
    CLI keeps running: a plain subprocess timeout only kills the direct child and then
    blocks on pipes still held by grandchildren.
    """
    started = time.time()
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
    timed_out, killed_after_done = False, False
    while proc.poll() is None:
        if time.time() > deadline:
            note = f"timeout after {timeout}s"
            timed_out = True
        elif done.wait(1) and proc.poll() is None:
            try:
                proc.wait(grace)
            except subprocess.TimeoutExpired:
                note = f"still running {grace}s after the turn ended; killed"
                killed_after_done = True
        if note:
            kill_tree(proc)
            break
    for reader in readers:
        reader.join(10)
    for pipe in (proc.stdout, proc.stderr):
        pipe.close()
    proc.wait(10)
    info = {
        "started": True,
        "skipped": False,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "killed_after_done": killed_after_done,
        "has_end_event": done.is_set(),
        "seconds": round(time.time() - started, 2),
        "error": note,
    }
    return "".join(out), "\n".join(filter(None, ["".join(err).strip(), note])), info


def run_turn(command, cwd, prompt, timeout, grace=GRACE):
    """Backward-compatible wrapper returning (stdout, stderr-or-note)."""
    out, err, _ = run_turn_detailed(command, cwd, prompt, timeout, grace=grace)
    return out, err


def run_case(name, case, out, args):
    case_dir = out / name
    ws = case_dir / "workspace"
    case_dir.mkdir(parents=True)
    base = prepare(case["fixture"], ws)
    transcripts, snapshots, turns_meta = [], [], []
    stats = {
        "cost_usd": None,
        "agent_turns": 0,
        "seconds": 0.0,
        "tokens": None,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "usage_present": False,
    }
    thread, collab_seen = None, 0
    for index, prompt in enumerate(case["turns"]):
        start = time.time()
        if args.agent == "codex":
            if index > 0 and not thread:
                turns_meta.append({
                    "index": index + 1, "started": False, "skipped": True,
                    "exit_code": None, "timed_out": False, "killed_after_done": False,
                    "has_end_event": False, "seconds": 0.0,
                    "error": "previous turn did not start a thread",
                })
                continue
            command = codex_command(args, thread if index > 0 else None)
        else:
            command = agent_command(args, index)
        try:
            text, err, info = run_turn_detailed(command, ws, prompt, args.timeout)
        except OSError as exc:
            text, err = "", f"failed to start: {exc}"
            info = {
                "started": False, "skipped": False, "exit_code": None,
                "timed_out": False, "killed_after_done": False,
                "has_end_event": False, "seconds": round(time.time() - start, 2),
                "error": err,
            }
        info["index"] = index + 1
        turns_meta.append(info)
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
                cost = event.get("total_cost_usd")
                if cost is not None:
                    stats["cost_usd"] = (stats["cost_usd"] or 0.0) + cost
                    stats["usage_present"] = True
                stats["agent_turns"] += event.get("num_turns") or 0
            elif isinstance(event, dict) and event.get("type") == "turn.completed":  # codex
                usage = event.get("usage") or {}
                in_tok = usage.get("input_tokens")
                cached_tok = usage.get("cached_input_tokens")
                out_tok = usage.get("output_tokens")
                if in_tok is not None or out_tok is not None:
                    stats["usage_present"] = True
                    stats["input_tokens"] = (stats["input_tokens"] or 0) + (in_tok or 0)
                    stats["output_tokens"] = (stats["output_tokens"] or 0) + (out_tok or 0)
                    stats["tokens"] = (stats["tokens"] or 0) + (in_tok or 0) + (out_tok or 0)
                if cached_tok is not None:
                    stats["cached_input_tokens"] = (stats["cached_input_tokens"] or 0) + cached_tok
                stats["agent_turns"] += 1
        transcripts.append(text)
        snapshots.append(snapshot(ws))
    meta = {
        "case": name, "fixture": case["fixture"], "base": base, "snapshots": snapshots,
        "expected_turns": len(case["turns"]), "turns": turns_meta,
        "stats": stats, "plugin": not args.no_plugin, "model": args.model, "agent": args.agent,
    }
    (case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return grade_case(case_dir)


def _dimension_ratio(checks):
    return f"{sum(c['ok'] for c in checks)}/{len(checks)}"


def grade_case(case_dir):
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    turns = sorted(case_dir.glob("turn*.jsonl"), key=lambda p: int(p.stem[4:]))
    stderrs = []
    for idx in range(max(len(turns), meta.get("expected_turns") or 0)):
        stderr_file = case_dir / f"turn{idx + 1}.stderr.txt"
        stderrs.append(stderr_file.read_text(encoding="utf-8", errors="replace") if stderr_file.is_file() else "")
    case_spec_path = EVALS / "cases" / meta["case"] / "case.json"
    expected_turns = meta.get("expected_turns")
    if not expected_turns and case_spec_path.is_file():
        expected_turns = len(json.loads(case_spec_path.read_text(encoding="utf-8")).get("turns") or [])
    plugin = meta.get("plugin", True)
    ctx = Context(
        ws=case_dir / "workspace",
        fixture=FIXTURES / meta["fixture"],
        base=meta["base"],
        transcripts=[p.read_text(encoding="utf-8") for p in turns],
        snapshots=meta["snapshots"],
        plugin=plugin,
        turns_meta=meta.get("turns") or [],
        stderrs=stderrs,
        expected_turns=expected_turns or len(turns),
    )
    exec_checks = [c.to_dict() for c in execution_checks(ctx)]
    try:
        case_checks = [c.to_dict() for c in load_grader(meta["case"])(ctx)]
    except Exception as exc:  # grader crash is a failure, not a runner crash
        case_checks = [{"name": "grader 运行", "ok": False, "detail": repr(exc),
                        "required": True, "category": "execution", "status": "fail"}]
    checks = exec_checks + case_checks
    commands, failed = ctx.command_stats()

    execution = [c for c in checks if c.get("category") == "execution"]
    functional = [c for c in checks if c.get("category") == "functional" and c["required"]]
    compliance_all = [c for c in checks if c.get("category") == "compliance"]
    compliance_req = [c for c in compliance_all if c["required"]]
    required = [c for c in checks if c["required"]]
    soft = [c for c in checks if not c["required"]]

    execution_passed = all(c["ok"] for c in execution)
    functional_passed = all(c["ok"] for c in functional)
    compliance_passed = all(c["ok"] for c in compliance_req) if plugin else None
    overall_passed = execution_passed and functional_passed and (compliance_passed if plugin else True)

    result = {
        "case": meta["case"],
        "plugin": plugin,
        "passed": overall_passed,
        "delivery_passed": execution_passed and functional_passed,
        "dimensions": {
            "execution": {"passed": execution_passed, "score": _dimension_ratio(execution)},
            "functional": {"passed": functional_passed, "score": _dimension_ratio(functional)},
            "compliance": {
                "passed": compliance_passed,
                "score": _dimension_ratio(compliance_req if plugin else compliance_all),
            },
        },
        "required": _dimension_ratio(required),
        "soft": _dimension_ratio(soft),
        "stats": {**meta["stats"], "commands": commands, "failed_commands": failed},
        "checks": checks,
    }
    (case_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _fmt_num(val, fmt="{:.2f}"):
    return "-" if val is None else fmt.format(val)


def summarize(results, out):
    lines = [
        "| 用例 | 综合 | 交付 | 执行 | 功能 | 合规 | 参考 | 耗时(s) | 命令 | 失败命令 | 成本($) | tokens(缓存) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = r["stats"]
        dims = r["dimensions"]
        delivery = r.get("delivery_passed", dims["execution"]["passed"] and dims["functional"]["passed"])
        exec_cell = f"{'✅' if dims['execution']['passed'] else '❌'} {dims['execution']['score']}"
        func_cell = f"{'✅' if dims['functional']['passed'] else '❌'} {dims['functional']['score']}"
        if dims["compliance"]["passed"] is None:
            comp_cell = f"➖ {dims['compliance']['score']}"
        else:
            comp_cell = f"{'✅' if dims['compliance']['passed'] else '❌'} {dims['compliance']['score']}"
        tok = s.get("tokens")
        cached = s.get("cached_input_tokens")
        tok_cell = "-" if tok is None else (f"{tok}({cached})" if cached is not None else str(tok))
        lines.append(
            f"| {r['case']} | {'✅' if r['passed'] else '❌'} | {'✅' if delivery else '❌'} | {exec_cell} | {func_cell} | {comp_cell} "
            f"| {r['soft']} | {s['seconds']:.0f} | {s.get('commands', 0)} | {s.get('failed_commands', 0)} "
            f"| {_fmt_num(s.get('cost_usd'))} | {tok_cell} |"
        )
    lines.append("")
    for r in results:
        for c in r["checks"]:
            if not c["ok"]:
                if c.get("status") == "unknown":
                    tag = f"{c.get('category', 'check')}/无法确认"
                else:
                    tag = f"{c.get('category', 'check')}/{'必需' if c['required'] else '参考'}"
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
    parser.add_argument("--allow-default-codex-home", action="store_true",
                        help="允许直接修改默认 ~/.codex 插件缓存（不推荐，建议改设 CODEX_HOME）")
    args = parser.parse_args()

    if args.grade_only:
        dirs = sorted(d for d in args.grade_only.iterdir() if (d / "meta.json").exists())
        summarize([grade_case(d) for d in dirs], args.grade_only)
        return

    if args.agent == "codex":
        ensure_isolated_codex_home(allow_default=args.allow_default_codex_home)
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
