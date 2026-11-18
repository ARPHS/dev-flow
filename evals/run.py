"""Run dev-flow behavior evals against a real agent CLI.

Each case copies a fixture into a fresh git repo, sends the case turns to
`claude -p` (follow-up turns use `--continue`), then grades the workspace and
transcript with `cases/<name>/grade.py`.

Examples:
    python -X utf8 evals/run.py                     # all cases, with plugin
    python -X utf8 evals/run.py --case feature debug
    python -X utf8 evals/run.py --no-plugin         # baseline for A/B comparison
    python -X utf8 evals/run.py --grade-only <run-dir>

Warning: agents run with --dangerously-skip-permissions inside temporary
workspaces. Run on a machine/account where that is acceptable.
"""

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

EVALS = Path(__file__).resolve().parent
ROOT = EVALS.parent
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


def agent_command(args, turn):
    exe = shutil.which("claude")
    if not exe:
        sys.exit("claude CLI not found on PATH")
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


def run_case(name, case, out, args):
    case_dir = out / name
    ws = case_dir / "workspace"
    case_dir.mkdir(parents=True)
    base = prepare(case["fixture"], ws)
    transcripts, snapshots, stats = [], [], {"cost_usd": 0.0, "agent_turns": 0, "seconds": 0.0}
    for index, prompt in enumerate(case["turns"]):
        start = time.time()
        try:
            proc = subprocess.run(agent_command(args, index), cwd=ws, input=prompt, capture_output=True,
                                  text=True, encoding="utf-8", timeout=args.timeout)
            text, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = f"timeout after {args.timeout}s"
        stats["seconds"] += time.time() - start
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
        transcripts.append(text)
        snapshots.append(snapshot(ws))
    meta = {"case": name, "fixture": case["fixture"], "base": base, "snapshots": snapshots,
            "stats": stats, "plugin": not args.no_plugin, "model": args.model}
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
    lines = ["| 用例 | 结果 | 必需 | 参考 | 耗时(s) | 成本($) |", "|---|---|---|---|---|---|"]
    for r in results:
        s = r["stats"]
        lines.append(f"| {r['case']} | {'✅' if r['passed'] else '❌'} | {r['required']} | {r['soft']} "
                     f"| {s['seconds']:.0f} | {s['cost_usd']:.2f} |")
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

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-noplugin" if args.no_plugin else "")
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
