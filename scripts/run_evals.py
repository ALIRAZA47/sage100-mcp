"""Run the model eval set (SPEC.md §15.6): Claude Code answers each question in evals/tasks.yaml through
sage100-mcp, and the answers are scored against the golden ones.

    # a local server over stdio (the server command, as Claude Code would start it)
    uv run python scripts/run_evals.py --golden testbed -- uv run sage100-mcp stdio --config test.yaml
    # a remote server over HTTPS: the M4 gate from a Mac (token in SAGE100_MCP_TOKEN)
    uv run python scripts/run_evals.py --golden abc --url https://sage-mcp.corp.example/mcp

Each task runs `claude -p` with no built-in tools, only this server's tools allowed, and no saved session,
in an empty working directory and an environment without the calling Claude Code session's variables.
Claude Code needs credentials of its own: ANTHROPIC_API_KEY, or a login (`claude` /login).

A task passes when the last `ANSWER:` line of the reply matches the golden answer exactly (money to the
cent; lists in any order). The report counts tool calls, tool errors, WRITE_BLOCKED caused by the model,
tokens and cost per task, and checks §15.6's release bar: at least 90% exact, a median of 4 or fewer tool
calls, and no WRITE_BLOCKED. Tasks without a golden answer for the chosen set are skipped. Exit status 0
means the bar was met.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "evals" / "tasks.yaml"
SERVER = "sage100"
TOKEN_ENV = "SAGE100_MCP_TOKEN"
SERVER_ENV = re.compile(r"SAGE100_MCP_(HOME|CONFIG|CRED_\w+|_\w+)")  # what a stdio server reads
BAR_EXACT, BAR_MEDIAN_CALLS = 0.90, 4
# The calling session's own variables: a nested Claude Code must not join, or report into, that session.
SESSION_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE", "CCR_", "CLAUDE_AFTER_", "CLAUDE_AUTO", "CLAUDE_ENABLE_",
                    "CLAUDE_SESSION_", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_ADDITIONAL_")  # fmt: skip
PROMPT = """Answer this question about the company's Sage 100 data. Use the {server} tools to find the answer; \
don't guess.

Question: {question}

End your reply with one line of exactly this form:
ANSWER: <answer>
Give money as a plain number with two decimals and no currency sign (such as 1234.50), counts and quantities \
as plain numbers, lists of codes separated by commas, months as YYYY-MM, and yes or no as yes or no. If what \
is asked can't be done with these tools, the line is ANSWER: REFUSED"""


@dataclass
class Task:
    id: str
    question: str
    answer: str
    golden: dict[str, str | None]


@dataclass
class Outcome:
    id: str
    passed: bool
    expected: str
    got: str | None
    tool_calls: int
    other_tool_calls: int
    errors: list[str] = field(default_factory=list)  # the codes of the tool errors the model met
    write_blocked: bool = False
    tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    note: str = ""


def load_tasks(path: Path = TASKS) -> list[Task]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        Task(t["id"], t["question"], t["answer"], {k: None if v is None else str(v) for k, v in t["golden"].items()})
        for t in data["tasks"]
    ]


# --- scoring ----------------------------------------------------------------------------------------------


def normal(kind: str, text: str) -> str | None:
    """An answer in the form its golden is written in; None when it can't be read as that kind."""
    value = text.strip().strip(".").strip()
    if kind in ("money", "number", "count"):
        cleaned = value.replace(",", "").replace("$", "").strip()
        if re.fullmatch(r"\(\s*[\d.]+\s*\)", cleaned):  # accounting negative
            cleaned = "-" + cleaned.strip("() ")
        try:
            amount = Decimal(cleaned)
        except InvalidOperation:
            return None
        if kind == "money":
            return str(amount.quantize(Decimal("0.01")))
        if kind == "count":
            return str(int(amount)) if amount == amount.to_integral_value() else None
        return format(amount.normalize(), "f")
    if kind == "codes":
        parts = [part.strip().upper() for part in re.split(r"[,;\n]| and ", value) if part.strip()]
        return ", ".join(sorted(set(parts)))
    if kind == "code":
        return value.upper()
    if kind == "month":
        found = re.fullmatch(r"(\d{4})-(\d{1,2})", value)
        return f"{found.group(1)}-{int(found.group(2)):02d}" if found else None
    if kind == "yesno":
        found = re.match(r"(yes|no)\b", value.lower())
        return found.group(1) if found else None
    if kind == "refused":
        return "REFUSED" if value.upper().startswith("REFUSED") else value.upper()
    raise ValueError(f"unknown answer kind {kind!r}")


def final_answer(text: str) -> str | None:
    found = re.findall(r"^\s*\**ANSWER:?\**\s*:?\s*(.+?)\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    return found[-1].strip("*` ") if found else None


def score(task: Task, golden: str, events: Iterable[dict[str, Any]], seconds: float) -> Outcome:
    """One task's outcome from Claude Code's stream-json events."""
    calls = other = tokens = 0
    errors: list[str] = []
    cost = 0.0
    final = ""
    note = ""
    for event in events:
        kind = event.get("type")
        content = (event.get("message") or {}).get("content") or []
        if kind == "assistant":
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use":
                    if str(block.get("name", "")).startswith(f"mcp__{SERVER}__"):
                        calls += 1
                    else:
                        other += 1
        elif kind == "user":
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    errors.append(error_code(block.get("content")))
        elif kind == "result":
            final = str(event.get("result") or "")
            cost = float(event.get("total_cost_usd") or 0)
            usage = event.get("usage") or {}
            tokens = sum(int(usage.get(key) or 0) for key in ("input_tokens", "output_tokens",
                         "cache_read_input_tokens", "cache_creation_input_tokens"))  # fmt: skip
            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                note = f"run ended: {event.get('subtype')}"
    raw = final_answer(final)
    got = normal(task.answer, raw) if raw is not None else None
    return Outcome(
        id=task.id,
        passed=got is not None and got == normal(task.answer, golden),
        expected=golden,
        got=raw,
        tool_calls=calls,
        other_tool_calls=other,
        errors=errors,
        write_blocked="WRITE_BLOCKED" in errors,
        tokens=tokens,
        cost_usd=round(cost, 4),
        seconds=round(seconds, 1),
        note=note or ("" if raw is not None else "no ANSWER line"),
    )


def error_code(content: Any) -> str:
    text = content if isinstance(content, str) else " ".join(
        str(part.get("text", "")) for part in content or [] if isinstance(part, dict)
    )  # fmt: skip
    found = re.match(r"\s*([A-Z][A-Z_]{2,}):", text)
    return found.group(1) if found else "TOOL_ERROR"


# --- running Claude Code ----------------------------------------------------------------------------------


def clean_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(SESSION_PREFIXES)}
    return env | (extra or {})


def mcp_config(url: str | None, token: str | None, command: list[str]) -> dict[str, Any]:
    """Claude Code's MCP config: the remote endpoint and token, or the stdio command with only the variables
    the server reads (its home, config file, overrides and logins), never test settings or tokens."""
    if url:
        return {"mcpServers": {SERVER: {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}
    passed = {k: v for k, v in os.environ.items() if SERVER_ENV.fullmatch(k)}
    return {"mcpServers": {SERVER: {"type": "stdio", "command": command[0], "args": command[1:], "env": passed}}}


def run_task(task: Task, golden: str, *, claude: list[str], config: Path, model: str | None, max_turns: int,
             timeout_s: int) -> Outcome:  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="sage100-eval-") as workdir:
        command = [
            *claude, "-p", PROMPT.format(server=SERVER, question=task.question), "--output-format", "stream-json",
            "--verbose", "--mcp-config", str(config), "--strict-mcp-config", "--tools", "",
            "--allowedTools", f"mcp__{SERVER}", "--no-session-persistence", "--max-turns", str(max_turns),
        ]  # fmt: skip
        if model:
            command += ["--model", model]
        started = time.monotonic()
        try:
            done = subprocess.run(
                command, cwd=workdir, env=clean_environment(), capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired:
            return Outcome(task.id, False, golden, None, 0, 0, note=f"timed out after {timeout_s} s")
        events = []
        for line in done.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        outcome = score(task, golden, events, time.monotonic() - started)
        if not events:
            outcome.note = f"claude exited {done.returncode}: {done.stderr.strip()[:200]}"
        return outcome


def summary(outcomes: list[Outcome], skipped: list[str]) -> dict[str, Any]:
    scored = len(outcomes)
    exact = sum(o.passed for o in outcomes) / scored if scored else 0.0
    median_calls = statistics.median(o.tool_calls for o in outcomes) if outcomes else 0
    blocked = sum(o.write_blocked for o in outcomes)
    bar = {
        f"exact >= {BAR_EXACT:.0%}": exact >= BAR_EXACT,
        f"median tool calls <= {BAR_MEDIAN_CALLS}": median_calls <= BAR_MEDIAN_CALLS,
        "no WRITE_BLOCKED from the model": blocked == 0,
    }
    return {
        "tasks": scored,
        "skipped": skipped,
        "exact": round(exact, 3),
        "median_tool_calls": median_calls,
        "errors_per_task": round(sum(len(o.errors) for o in outcomes) / scored, 2) if scored else 0,
        "write_blocked": blocked,
        "tokens": sum(o.tokens for o in outcomes),
        "median_tokens": statistics.median(o.tokens for o in outcomes) if outcomes else 0,
        "cost_usd": round(sum(o.cost_usd for o in outcomes), 2),
        "bar": bar,
        "passed": scored > 0 and all(bar.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--golden", default="testbed", help="which golden answers to score against: testbed or abc")
    parser.add_argument("--url", help="a remote server's MCP endpoint; the token comes from " + TOKEN_ENV)
    parser.add_argument("--tasks", type=Path, default=TASKS)
    parser.add_argument("--only", help="comma-separated task ids")
    parser.add_argument("--claude", default="claude", help="the Claude Code command")
    parser.add_argument("--model", help="the model to answer with (Claude Code's default otherwise)")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--jobs", type=int, default=4, help="tasks run at once")
    parser.add_argument("--timeout-s", type=int, default=600)
    parser.add_argument("--report", type=Path, help="write the full report as JSON")
    parser.add_argument("server", nargs="*", help="after --: the stdio server command, for a local server")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "").strip() or None
    if args.url and not token:
        print(f"set {TOKEN_ENV} to a token for {args.url}", file=sys.stderr)
        return 2
    if not args.url and not args.server:
        print("give --url, or the server command after --", file=sys.stderr)
        return 2
    tasks = load_tasks(args.tasks)
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [task for task in tasks if task.id in wanted]
    runnable = [(task, task.golden.get(args.golden)) for task in tasks]
    skipped = [task.id for task, golden in runnable if golden is None]
    with tempfile.TemporaryDirectory(prefix="sage100-eval-config-") as directory:
        config = Path(directory) / "mcp.json"
        config.write_text(json.dumps(mcp_config(args.url, token, args.server)), encoding="utf-8")
        config.chmod(0o600)
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = [
                pool.submit(
                    run_task,
                    task,
                    golden,
                    claude=shlex.split(args.claude, posix=os.name != "nt"),
                    config=config,
                    model=args.model,
                    max_turns=args.max_turns,
                    timeout_s=args.timeout_s,
                )
                for task, golden in runnable
                if golden is not None
            ]
            outcomes = [future.result() for future in futures]
    report = {"golden": args.golden, "outcomes": [asdict(o) for o in outcomes]} | summary(outcomes, skipped)
    for outcome in outcomes:
        mark = "PASS" if outcome.passed else "FAIL"
        errors = f", errors {','.join(outcome.errors)}" if outcome.errors else ""
        print(f"[{mark}] {outcome.id}: expected {outcome.expected}, got {outcome.got}; {outcome.tool_calls} calls"
              f"{errors}; {outcome.tokens} tokens{'; ' + outcome.note if outcome.note else ''}")  # fmt: skip
    print(f"{report['exact']:.0%} exact over {report['tasks']} tasks ({len(skipped)} skipped); median tool calls "
          f"{report['median_tool_calls']}; WRITE_BLOCKED {report['write_blocked']}; "
          f"{report['tokens']} tokens, ${report['cost_usd']}")  # fmt: skip
    print("release bar met" if report["passed"] else "release bar NOT met: " + ", ".join(
        name for name, met in report["bar"].items() if not met))  # fmt: skip
    if args.report:
        args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
