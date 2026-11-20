"""Shared helpers for eval graders.

A grader is `cases/<name>/grade.py` exposing `grade(ctx) -> list[Check]`.
Checks are grouped into dimensions:
- execution:  runner/turn health (no timeout, startup failure, or missing end event)
- functional: user-visible task correctness and compatibility (shared across plugin & --no-plugin)
- compliance: dev-flow protocol adherence (required when plugin=True, observed when --no-plugin)
- advisory:   soft trend/style signals (never gate pass/fail)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures"
EVIDENCE_CLI = EVALS.parent / "plugins" / "dev-flow" / "skills" / "df-verify" / "scripts" / "evidence.py"
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".rb", ".kt", ".swift", ".c", ".cpp"}

PYTHON_EXE = re.compile(r"(?:python(?:3(?:\.\d+)*)?|py)(?:\.exe)?$", re.I)
REVIEW_ROLE = re.compile(r"(?:^|[:/_-])(?:df[-_]reviewer|df[-_]review|reviewer|review)(?:$|[/_-])", re.I)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True
    category: str = "functional"
    status: str = ""

    def __post_init__(self):
        if self.status == "unknown":
            self.ok = False
        elif not self.status:
            self.status = "pass" if self.ok else "fail"

    def to_dict(self):
        return asdict(self)


SKILL_READ = re.compile(r"(df-[a-z]+)[/\\]+SKILL\.md")


def output_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def program_name(program):
    return program.replace("\\", "/").rsplit("/", 1)[-1].lower()


def python_args(argv):
    if not argv or not PYTHON_EXE.fullmatch(program_name(argv[0])):
        return None
    args = argv[1:]
    while args:
        if args[0] in ("-X", "-W") and len(args) > 1:
            args = args[2:]
        elif args[0] in ("-u", "-B", "-E", "-I", "-s", "-S", "-O", "-OO", "-q") or re.fullmatch(r"-3(?:\.\d+)?", args[0]):
            args = args[1:]
        else:
            break
    return args


def _tokenize_shell(text):
    """Tokenize a shell command into (token, is_operator) pairs while preserving quotes, escapes and Windows paths."""
    tokens = []
    buf = []
    has_token = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r":
            if has_token:
                tokens.append(("".join(buf), False))
                buf, has_token = [], False
            i += 1
        elif ch == "\n":
            if has_token:
                tokens.append(("".join(buf), False))
                buf, has_token = [], False
            tokens.append(("\n", True))
            i += 1
        elif ch == "\\" and i + 1 < n and text[i + 1] == "\n":
            i += 2
        elif ch in ";&|<>":
            if has_token:
                tokens.append(("".join(buf), False))
                buf, has_token = [], False
            if text[i:i + 3] == "<<-":
                tokens.append(("<<-", True))
                i += 3
            elif text[i:i + 2] in ("&&", "||", "<<", ">>", ">&", "<&"):
                tokens.append((text[i:i + 2], True))
                i += 2
            else:
                tokens.append((ch, True))
                i += 1
        elif ch == "'":
            has_token = True
            i += 1
            while i < n and text[i] != "'":
                buf.append(text[i])
                i += 1
            if i >= n:
                return None
            i += 1
        elif ch == '"':
            has_token = True
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in ('"', "\\"):
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                return None
            i += 1
        else:
            has_token = True
            buf.append(ch)
            i += 1
    if has_token:
        tokens.append(("".join(buf), False))
    return tokens


def _is_env_assignment(token):
    return bool(re.match(r"^[A-Za-z_]\w*=", token))


def _is_set_errexit(argv):
    if not argv or argv[0] != "set":
        return None
    result = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-o" and i + 1 < len(argv):
            if argv[i + 1] == "errexit":
                result = True
            i += 2
            continue
        if arg == "+o" and i + 1 < len(argv):
            if argv[i + 1] == "errexit":
                result = False
            i += 2
            continue
        if len(arg) > 1 and arg[0] in ("-", "+") and arg[1:].isalpha():
            if "e" in arg[1:]:
                result = arg[0] == "-"
        i += 1
    return result


def _record_assignments(argv, active_aliases, out_aliases=None):
    """Record shell variable assignments (Bash `VAR=...` / `export VAR=...`, PowerShell `$VAR = ...`)."""
    if not argv:
        return False
    # PowerShell: $VAR = "value"
    if len(argv) >= 3 and re.fullmatch(r"\$([A-Za-z_]\w*)", argv[0]) and argv[1] == "=":
        name = argv[0][1:]
        value = " ".join(argv[2:])
        active_aliases[name] = value
        if out_aliases is not None:
            out_aliases[name] = value
        return True
    # Bash / PowerShell inline: VAR=value, $VAR=value, export VAR=value
    items = argv[1:] if argv[0] == "export" and len(argv) > 1 else argv
    if items and all(re.match(r"^\$?[A-Za-z_]\w*=", item) for item in items):
        for item in items:
            name, value = item.lstrip("$").split("=", 1)
            active_aliases[name] = value
            if out_aliases is not None:
                out_aliases[name] = value
        return True
    return False


def command_argvs(command, aliases=None, strict_chain=True, errexit=False, out_aliases=None):
    """Recognize direct commands, shell wrappers and evidence wrappers; retain only a guaranteed-exit chain."""
    active_aliases = dict(aliases or {})
    if isinstance(command, list):
        groups = [(command, dict(active_aliases))]
    elif isinstance(command, str):
        tokens = _tokenize_shell(command.strip())
        if tokens is None:
            return []
        if any(is_op and tok in ("<<", "<<-") for tok, is_op in tokens):
            return []
        groups, current = [], []
        keep_on_seq = errexit or not strict_chain
        for tok, is_op in tokens:
            if is_op and tok in ("&&", ";", "||", "|", "\n", "&"):
                if tok == "&" and not current:
                    continue  # PowerShell call operator `& $EV ...`
                if current:
                    if not _record_assignments(current, active_aliases, out_aliases=out_aliases):
                        toggled = _is_set_errexit(current)
                        if toggled is not None:
                            errexit = toggled
                            keep_on_seq = errexit or not strict_chain
                        groups.append((current, dict(active_aliases)))
                if tok in ("||", "|", "&") or (tok in (";", "\n") and not keep_on_seq):
                    groups = []
                current = []
            else:
                current.append(tok)
        if current:
            if not _record_assignments(current, active_aliases, out_aliases=out_aliases):
                groups.append((current, dict(active_aliases)))
    else:
        return []

    result = []
    for argv, cmd_aliases in groups:
        while argv:
            if _is_env_assignment(argv[0]):
                argv = argv[1:]
            elif argv[0] == "env":
                argv = argv[1:]
                while argv and argv[0].startswith("-"):
                    if argv[0] in ("-u", "--unset", "-C", "--chdir") and len(argv) > 1:
                        argv = argv[2:]
                    elif argv[0] == "--":
                        argv = argv[1:]
                        break
                    else:
                        argv = argv[1:]
            else:
                break
        if not argv:
            continue
        alias_match = re.fullmatch(r"\$\{?([A-Za-z_]\w*)\}?", argv[0])
        if alias_match and alias_match.group(1) in cmd_aliases:
            alias_name = alias_match.group(1)
            nested_aliases = {k: v for k, v in cmd_aliases.items() if k != alias_name}
            prefixes = command_argvs(cmd_aliases[alias_name], aliases=nested_aliases, strict_chain=strict_chain)
            if len(prefixes) == 1:
                argv = [*prefixes[0], *argv[1:]]
        name = program_name(argv[0])
        if name in ("bash", "sh"):
            flag = next((index for index, arg in enumerate(argv[1:], start=1)
                         if re.fullmatch(r"-[a-zA-Z]*c", arg)), None)
            if flag is not None and flag + 1 < len(argv):
                sub_errexit = any(re.fullmatch(r"-[a-zA-Z]*e[a-zA-Z]*", arg) for arg in argv[1:flag + 1])
                result.extend(command_argvs(argv[flag + 1], aliases=cmd_aliases,
                                            strict_chain=strict_chain, errexit=sub_errexit))
            continue
        if name in ("pwsh", "pwsh.exe", "powershell", "powershell.exe", "cmd", "cmd.exe"):
            flag = next((index for index, arg in enumerate(argv[1:], start=1)
                         if arg.lower() in ("-c", "-command", "/c")), None)
            if flag is not None and flag + 1 < len(argv):
                result.extend(command_argvs(" ".join(argv[flag + 1:]), aliases=cmd_aliases,
                                            strict_chain=strict_chain))
            continue
        result.append(argv)
        args = python_args(argv)
        if args and program_name(args[0]) == "evidence.py" and args[1:2] == ["run"] and "--" in args:
            result.extend(command_argvs(args[args.index("--") + 1:], aliases=cmd_aliases,
                                        strict_chain=strict_chain))
    return result


def is_test_command(command):
    for argv in command_argvs(command):
        args = python_args(argv)
        test_args = args[2:] if args and args[:2] in (["-m", "unittest"], ["-m", "pytest"]) else None
        if program_name(argv[0]) in ("pytest", "pytest.exe", "py.test"):
            test_args = argv[1:]
        if test_args is not None and not any(arg in test_args for arg in
                                              ("--help", "-h", "--version", "--collect-only", "--co")):
            return True
    return False


def invokes_python_module(command, module, *arguments):
    return any(args and args[:2] == ["-m", module] and args[2:2 + len(arguments)] == list(arguments)
               for args in (python_args(argv) for argv in command_argvs(command)))


def is_evidence_call(command, subcommand):
    return any(args and program_name(args[0]) == "evidence.py" and args[1:2] == [subcommand]
               for args in (python_args(argv) for argv in command_argvs(command)))


def execution_succeeded(execution):
    return execution.get("output_present") is True and execution.get("exit_code") == 0 and execution.get("is_error") is False


def is_review_request(data):
    if data.get("reviewer") is True:
        return True
    return any(REVIEW_ROLE.search(str(data.get(key) or "")) for key in ("subagent_type", "agent_type", "task_name"))


def background_agent_id(output):
    try:
        parsed = json.loads(output)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed.get("agent_id") or parsed.get("task_id") or parsed.get("agentId")
    match = re.search(r"(?:agentId|agent_id|task_id)\s*[:=]\s*([\w-]+)", output)
    if match:
        return match.group(1)
    xml_match = re.search(r"<task-id>\s*([^<\s]+)\s*</task-id>", output)
    return xml_match.group(1) if xml_match else None


def task_completed(output):
    try:
        parsed = json.loads(output)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed.get("status") == "completed" and not parsed.get("error")
    return "<status>completed</status>" in output


def codex_tool_uses(item):
    """Translate one completed Codex item. Codex has no Skill tool: the model reads SKILL.md itself."""
    kind = item.get("type")
    if kind == "command_execution":
        command = item.get("command") or ""
        for skill in dict.fromkeys(SKILL_READ.findall(command)):
            yield "Skill", {"skill": f"dev-flow:{skill}"}
        code = item.get("exit_code")
        yield "Bash", {"command": command, "exit_code": code, "output_present": code is not None,
                       "output_error": None if code is None else code != 0,
                       "output": item.get("aggregated_output") or ""}
    elif kind == "file_change":
        for change in item.get("changes") or []:
            yield "Edit", {"file_path": change.get("path", ""), "kind": change.get("kind")}
    elif item.get("tool"):  # mcp / collaboration tools such as spawn_agent
        data = item.get("arguments")
        payload = dict(data) if isinstance(data, dict) else {k: v for k, v in item.items() if k != "result"}
        for extra in ("call_id", "output_present", "output_error", "output_preview", "completed_agents", "agent_id", "reviewer"):
            payload.pop(extra, None)
            if extra in item:
                payload[extra] = item[extra]
        yield item["tool"], payload


@dataclass
class Context:
    ws: Path
    fixture: Path
    base: str
    transcripts: list = field(default_factory=list)  # raw stream-json text per turn
    snapshots: list = field(default_factory=list)    # sorted file lists after each turn
    plugin: bool = True
    turns_meta: list = field(default_factory=list)   # per-turn execution metadata from run.py
    stderrs: list = field(default_factory=list)      # per-turn stderr/notes text
    expected_turns: int = 0

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
        """(name, input) pairs in Claude Code's vocabulary; Codex events are translated."""
        for index in range(len(self.transcripts)) if turn is None else [turn]:
            events = list(self.events(index))
            results = {item.get("tool_use_id"): item for event in events if event.get("type") == "user"
                       for item in event.get("message", {}).get("content", []) or []
                       if isinstance(item, dict) and item.get("type") == "tool_result"}
            for event in events:
                if event.get("type") == "assistant":
                    for item in event.get("message", {}).get("content", []) or []:
                        if not isinstance(item, dict) or item.get("type") != "tool_use":
                            continue
                        data = dict(item.get("input") or {})
                        result = results.get(item.get("id"))
                        present = result is not None
                        data["output_present"] = present
                        data["output_error"] = bool(result.get("is_error")) if present else None
                        data["output"] = output_text(result.get("content")) if present else ""
                        data["output_preview"] = data["output"][:200]
                        data["call_id"] = item.get("id")
                        if item.get("name") in ("Bash", "PowerShell"):
                            code = 1 if data["output_error"] else 0
                            background = data.get("run_in_background") or data["output"].lstrip().startswith("Command running in background")
                            data["exit_code"] = code if present and not background else None
                        yield item.get("name", ""), data
                elif event.get("type") == "item.completed":
                    yield from codex_tool_uses(event.get("item") or {})

    def shell_executions(self, turn=None):
        """Shell commands paired with outcome (`exit_code` / `is_error`), parsed argvs, and tool index."""
        turns = range(len(self.transcripts)) if turn is None else [turn]
        executions = []
        tool_index = 0
        for turn_idx in turns:
            turn_aliases = {}
            for name, data in self.tool_uses(turn_idx):
                if name in ("Bash", "PowerShell"):
                    command = data.get("command", "")
                    prior_aliases = dict(turn_aliases)
                    argvs = command_argvs(command, aliases=prior_aliases, out_aliases=turn_aliases)
                    all_argvs = command_argvs(command, aliases=prior_aliases, strict_chain=False)
                    executions.append({
                        "command": command,
                        "argvs": argvs,
                        "all_argvs": all_argvs,
                        "exit_code": data.get("exit_code"),
                        "is_error": data.get("output_error"),
                        "output_present": data.get("output_present", False),
                        "output": data.get("output", ""),
                        "tool_index": tool_index,
                    })
                tool_index += 1
        return executions

    def completed_task_notifications(self, turn=None):
        """Task IDs reported completed via Claude Code <task-notification> user messages."""
        completed = set()
        for event in self.events(turn):
            if event.get("type") != "user":
                continue
            text = output_text((event.get("message") or {}).get("content"))
            for block in re.findall(r"<task-notification>(.*?)</task-notification>", text, re.DOTALL):
                match = re.search(r"<task-id>\s*([^<\s]+)\s*</task-id>", block)
                if match and "<status>completed</status>" in block:
                    completed.add(match.group(1))
        return completed

    def bash_commands(self, turn=None):
        return [i.get("command", "") for n, i in self.tool_uses(turn) if n in ("Bash", "PowerShell")]

    def command_stats(self, turn=None):
        """(shell commands run, commands that failed): a cheap proxy for wasted round trips."""
        shell_ids, total, failed = set(), 0, 0
        for event in self.events(turn):
            kind = event.get("type")
            if kind in ("assistant", "user"):  # claude: tool_use, then tool_result with is_error
                for item in event.get("message", {}).get("content", []) or []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use" and item.get("name") in ("Bash", "PowerShell"):
                        total += 1
                        shell_ids.add(item.get("id"))
                    elif item.get("type") == "tool_result" and item.get("is_error") and item.get("tool_use_id") in shell_ids:
                        failed += 1
            elif kind == "item.completed" and (event.get("item") or {}).get("type") == "command_execution":
                total += 1
                failed += event["item"].get("exit_code") not in (0, None)
        return total, failed

    def skills_invoked(self, turn=None):
        names = []
        for name, data in self.tool_uses(turn):
            if name == "Skill":
                names.append(str(data.get("skill") or data.get("command") or data.get("name") or ""))
        return names

    def subagents(self, turn=None):
        """Descriptions of dispatched subagents (Claude Agent/Task tool, Codex spawn_agent)."""
        found = []
        for name, data in self.tool_uses(turn):
            if name in ("Agent", "Task", "spawn_agent"):
                found.append(" ".join(str(data.get(k) or "") for k in
                                      ("subagent_type", "agent_type", "task_name", "fork_turns", "description", "message", "prompt")).strip()[:300])
        return found

    def final_text(self, turn=-1):
        if not self.transcripts:
            return ""
        result = ""
        for event in self.events(turn if turn >= 0 else len(self.transcripts) - 1):
            if event.get("type") == "result":
                result = event.get("result") or ""
            elif event.get("type") == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                result = event["item"].get("text") or ""  # codex: last agent message of the turn
        return result

    def has_end_event(self, turn):
        for event in self.events(turn):
            if event.get("type") in ("turn.completed", "result"):
                return True
        return False

    def turn_end_error(self, turn):
        """Return an error reason when a turn ended with an error event (e.g. Claude error_max_turns)."""
        if turn >= len(self.transcripts):
            return None
        last_error = None
        completed_ok = False
        for event in self.events(turn):
            etype = event.get("type")
            if etype == "result":
                subtype = str(event.get("subtype") or "")
                if event.get("is_error") is True or subtype.startswith("error"):
                    last_error = subtype or str(event.get("result") or "result.is_error")[:80]
                    completed_ok = False
                else:
                    completed_ok = True
                    last_error = None
            elif etype == "turn.failed":
                err = event.get("error")
                if isinstance(err, dict):
                    last_error = str(err.get("message") or err.get("code") or "turn.failed")[:80]
                else:
                    last_error = str(err or "turn.failed")[:80]
                completed_ok = False
            elif etype == "turn.completed":
                completed_ok = True
                last_error = None
        return None if completed_ok else last_error

    def ran(self, pattern, turn=None):
        regex = re.compile(pattern)
        return [c for c in self.bash_commands(turn) if regex.search(c)]

    def ran_executions(self, pattern, turn=None):
        regex = re.compile(pattern)
        return [e for e in self.shell_executions(turn) if regex.search(e["command"])]

    def first_index(self, predicate, turn=None):
        for index, (name, data) in enumerate(self.tool_uses(turn)):
            if predicate(name, data):
                return index
        return None

    # ---- workspace ----
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.ws, capture_output=True, text=True, encoding="utf-8")

    def git_bytes(self, *args):
        return subprocess.run(["git", *args], cwd=self.ws, capture_output=True)

    def changed_files(self):
        if not self.base or not (self.ws / ".git").exists():
            return []
        committed_raw = self.git_bytes("diff", "--name-only", "-z", self.base).stdout
        committed = [p.decode("utf-8", "replace") for p in committed_raw.split(b"\0") if p]
        status_raw = self.git_bytes("status", "--porcelain", "-z", "--untracked-files=all").stdout
        entries = [e for e in status_raw.split(b"\0") if e]
        current = []
        i = 0
        while i < len(entries):
            entry = entries[i].decode("utf-8", "replace")
            if len(entry) >= 3:
                code, path_str = entry[:2], entry[3:]
                current.append(path_str)
                if "R" in code or "C" in code:
                    i += 1
            i += 1
        return sorted(f for f in set(committed) | set(current) if "__pycache__/" not in f)

    def new_commits(self):
        out = self.git("rev-list", f"{self.base}..HEAD").stdout.split()
        return len(out)

    def run(self, args, env=None, input=None, timeout=120):
        merged = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
        return subprocess.run(args, cwd=self.ws, capture_output=True, text=True, encoding="utf-8",
                              env=merged, input=input, timeout=timeout)

    def python(self, *args, **kwargs):
        return self.run([sys.executable, *args], **kwargs)

    def evidence_report(self):
        """Run evidence.py report --json against this workspace when .dev-flow/ exists."""
        if not (self.ws / ".dev-flow").is_dir() or not EVIDENCE_CLI.is_file():
            return None
        proc = self.python(str(EVIDENCE_CLI), "report", "--json")
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return None

    def evidence_records(self):
        path = self.ws / ".dev-flow" / "evidence.jsonl"
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records


# ---- reusable checks ----

def execution_checks(ctx):
    """Verify runner-level execution integrity (no timeout, startup failure, or missing turn end)."""
    expected = ctx.expected_turns or len(ctx.transcripts) or 1
    turns_meta = list(ctx.turns_meta)
    issues = []

    if len(ctx.transcripts) < expected:
        issues.append(f"预期 {expected} 轮，仅产出 {len(ctx.transcripts)} 轮")

    for idx in range(expected):
        meta = turns_meta[idx] if idx < len(turns_meta) else {}
        stderr = ctx.stderrs[idx] if idx < len(ctx.stderrs) else ""
        if meta.get("skipped"):
            issues.append(f"第 {idx + 1} 轮被跳过（{meta.get('error') or '前序失败'}）")
            continue
        if meta.get("started") is False or "failed to start:" in stderr:
            issues.append(f"第 {idx + 1} 轮启动失败（{meta.get('error') or stderr.splitlines()[-1]}）")
            continue
        if meta.get("timed_out") or "timeout after" in stderr:
            issues.append(f"第 {idx + 1} 轮超时")
            continue
        has_end = meta.get("has_end_event")
        if has_end is None:
            has_end = ctx.has_end_event(idx) if idx < len(ctx.transcripts) else False
        end_error = ctx.turn_end_error(idx)
        if end_error:
            issues.append(f"第 {idx + 1} 轮异常结束（{end_error}）")
            continue
        if not has_end:
            issues.append(f"第 {idx + 1} 轮缺少结束事件（turn.completed/result）")
            continue
        exit_code = meta.get("exit_code")
        if exit_code not in (0, None) and not meta.get("killed_after_done"):
            issues.append(f"第 {idx + 1} 轮异常退出（exit={exit_code}）")

    return [
        Check("执行完整（无超时、启动失败或缺失结束事件）", not issues,
              "; ".join(issues) if issues else f"全部 {expected} 轮正常结束",
              required=True, category="execution"),
    ]


def no_commits(ctx):
    count = ctx.new_commits()
    return Check("未擅自提交", count == 0, f"新提交数 {count}", category="functional")


def unit_tests_pass(ctx):
    proc = ctx.python("-m", "unittest", "discover", "-s", "tests")
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return Check("项目测试通过", proc.returncode == 0, f"exit={proc.returncode} {' | '.join(tail)}",
                 category="functional")


def original_tests_pass(ctx, rel_dir="tests"):
    """Run an immutable copy of the original fixture tests against the agent's modified code."""
    src_tests = ctx.fixture / rel_dir
    if not src_tests.is_dir():
        return Check("原始夹具测试兼容通过", True, "无原始测试目录", category="functional")
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / rel_dir
        shutil.copytree(src_tests, copied, ignore=shutil.ignore_patterns("__pycache__"))
        env = {"PYTHONPATH": str(ctx.ws)}
        proc = ctx.python("-m", "unittest", "discover", "-s", str(copied), env=env)
    output = (proc.stdout + proc.stderr).strip()
    ran_match = re.search(r"Ran (\d+) test", output)
    ran_count = int(ran_match.group(1)) if ran_match else 0
    skip_match = re.search(r"skipped=(\d+)", output)
    skipped_count = int(skip_match.group(1)) if skip_match else 0
    executed_count = ran_count - skipped_count
    ok = proc.returncode == 0 and executed_count > 0
    tail = output.splitlines()[-3:]
    return Check("原始夹具测试兼容通过", ok,
                 f"exit={proc.returncode} ran={ran_count} skipped={skipped_count} {' | '.join(tail)}",
                 required=True, category="functional")


def original_asserts_intact(ctx, rel_dir="tests", required=False):
    missing = []
    for original in (ctx.fixture / rel_dir).rglob("*.py"):
        target = ctx.ws / original.relative_to(ctx.fixture)
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        current_lines = {line.strip() for line in current.splitlines()}
        for line in original.read_text(encoding="utf-8").splitlines():
            if "assert" in line and line.strip() not in current_lines:
                missing.append(f"{original.name}: {line.strip()}")
    return Check("原有断言行未改动", not missing, "; ".join(missing[:3]),
                 required=required, category="functional" if required else "advisory")


def count_tests(root, rel_dir="tests"):
    return sum(len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8"), re.MULTILINE))
               for p in (Path(root) / rel_dir).rglob("*.py"))


def tests_added(ctx, rel_dir="tests"):
    before, after = count_tests(ctx.fixture, rel_dir), count_tests(ctx.ws, rel_dir)
    return Check("新增了测试", after > before, f"{before} → {after}", category="functional")


def ran_tests(ctx, turn=None):
    hits = [execution for execution in ctx.shell_executions(turn)
            if any(is_test_command(argv) for argv in execution["argvs"])]
    if not hits:
        return Check("实际运行了测试命令且最终通过", False, "未发现测试命令", category="functional")
    last = hits[-1]
    if last["exit_code"] is None or not last["output_present"]:
        return Check("实际运行了测试命令且最终通过", False, "测试命令缺少完成结果或退出码",
                     category="functional", status="unknown")
    if not execution_succeeded(last):
        return Check("实际运行了测试命令且最终通过", False,
                     f"最后一次测试退出码 {last['exit_code']}：{last['command'][:100]}", category="functional")
    return Check("实际运行了测试命令且最终通过", True, last["command"][:120], category="functional")


def baseline_before_edit(ctx, required=None):
    req = ctx.plugin if required is None else required
    records = [record for record in ctx.evidence_records() if record.get("label") == "baseline"
               and is_test_command(record.get("argv") or record.get("cmd", ""))
               and isinstance(record.get("exit"), int)]
    if not records or not ctx.base:
        return Check("修改前先跑基线", False, "缺少绑定初始源码快照的测试基线证据",
                     required=req, category="compliance", status="unknown")
    initial = ctx.git("rev-parse", f"{ctx.base}^{{tree}}")
    if initial.returncode != 0:
        return Check("修改前先跑基线", False, "无法读取夹具初始源码快照",
                     required=req, category="compliance", status="unknown")
    tree = initial.stdout.strip()
    ok = any(record.get("snapshot") == tree for record in records)
    detail = f"初始源码 {tree[:12]}，基线快照 " + ", ".join(str(record.get("snapshot", ""))[:12] for record in records)
    return Check("修改前先跑基线", ok, detail, required=req, category="compliance")


def evidence_recorded(ctx, turn=None, required=None, validations=None):
    req = ctx.plugin if required is None else required
    validations = validations or {"测试": is_test_command}
    executions = ctx.shell_executions(turn)
    runs = [execution for execution in executions
            if any(is_evidence_call(argv, "run") for argv in execution["all_argvs"])]
    reports = [execution for execution in executions
               if any(is_evidence_call(argv, "report") for argv in execution["all_argvs"])]
    data = ctx.evidence_report()
    records = data.get("records", []) if data else []
    passing = [record for record in records if record.get("exit") == 0 and record.get("label") != "baseline"]
    current = [record for record in passing if record.get("status") == "valid"]
    missing_runs = [name for name, matches in validations.items()
                    if not any(matches(record.get("argv") or record.get("cmd", "")) for record in passing)]
    missing_current = [name for name, matches in validations.items()
                       if not any(matches(record.get("argv") or record.get("cmd", "")) for record in current)]
    missing_calls = [name for name, matches in validations.items()
                     if not any(execution_succeeded(execution)
                                and any(matches(argv) for argv in execution["all_argvs"]) for execution in runs)]
    run_ok = not missing_calls and not missing_runs
    report_ok = any(execution_succeeded(execution) for execution in reports) and not missing_calls and not missing_current
    run_detail = f"{len(runs)} 次，落盘 {len(records)} 条"
    report_detail = f"{len(reports)} 次，当前有效且通过 {len(current)} 条"
    if missing_runs:
        run_detail += "，缺少通过证据：" + "、".join(missing_runs)
    if missing_current:
        report_detail += "，缺少当前有效证据：" + "、".join(missing_current)
    if missing_calls:
        detail = "，缺少成功返回的验证调用：" + "、".join(missing_calls)
        run_detail += detail
        report_detail += detail

    return [
        Check("用 evidence.py run 记录验证", run_ok, run_detail, required=req, category="compliance"),
        Check("用 evidence.py report 生成当前有效证据表", report_ok, report_detail, required=req, category="compliance"),
    ]


def independent_review(ctx, turn=None, required=None):
    req = ctx.plugin if required is None else required
    uses = list(ctx.tool_uses(turn))
    reviewers = [(name, data) for name, data in uses if name in ("Agent", "Task", "spawn_agent") and is_review_request(data)]
    started = [(name, data) for name, data in reviewers
               if data.get("output_present") is True and data.get("output_error") is False
               and (name != "spawn_agent" or (data.get("agent_id") and
                    (data.get("fork_turns") == "none" or data.get("fork_context") is False)))]
    unknown = any(not data.get("output_present") or (name == "spawn_agent" and
                  ("fork_turns" not in data and "fork_context" not in data)) for name, data in reviewers)
    dispatched = Check("派发了独立审查子代理", bool(started),
                       f"可识别审查请求 {len(reviewers)} 次，成功派发 {len(started)} 次",
                       required=req, category="compliance", status="unknown" if unknown and not started else "")
    review_ids = {str(data["agent_id"]) for name, data in started if name == "spawn_agent"}
    waits = [data for name, data in uses if name == "wait_agent"]
    completed = any(data.get("output_present") is True and data.get("output_error") is False
                    and isinstance(data.get("completed_agents"), list)
                    and bool(review_ids.intersection(map(str, data["completed_agents"]))) for data in waits)
    completed = completed or any(name in ("Agent", "Task") and not data.get("run_in_background")
                                 and bool(data.get("output", "").strip()) for name, data in started)
    background_ids = {str(agent_id) for name, data in started if name in ("Agent", "Task") and data.get("run_in_background")
                      for agent_id in [background_agent_id(data.get("output", ""))] if agent_id}
    polled = [data for name, data in uses if name == "TaskOutput" and str(data.get("task_id")) in background_ids]
    notified = background_ids.intersection(ctx.completed_task_notifications(turn))
    completed = completed or bool(notified) or any(
        data.get("output_present") is True and data.get("output_error") is False
        and task_completed(data.get("output", "")) for data in polled)
    wait_unknown = (unknown or bool(background_ids and not polled and not notified)
                    or any(not data.get("output_present") or not isinstance(data.get("completed_agents"), list) for data in waits))
    returned = Check("等待了审查结果并返回", completed, "对应审查代理已完成" if completed else "缺少对应审查代理的成功完成结果",
                     required=req, category="compliance", status="unknown" if wait_unknown and not completed else "")
    return [dispatched, returned]


def no_flow(ctx):
    return Check("未创建 .dev-flow/", not (ctx.ws / ".dev-flow").exists(), category="functional")


def skill_used(ctx, name, required=False, expect=True):
    used = any(name in s for s in ctx.skills_invoked())
    label = f"{'调用' if expect else '未调用'} {name}"
    return Check(label, used == expect, ", ".join(ctx.skills_invoked()) or "无技能调用",
                 required=required, category="compliance" if required else "advisory")


def code_files(files):
    return [f for f in files if Path(f).suffix in CODE_SUFFIXES and not f.startswith(".dev-flow/")]
