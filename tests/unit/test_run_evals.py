"""scripts/run_evals.py (SPEC.md §15.6) with a scripted stand-in for Claude Code: the flags and the clean
environment it runs with, the MCP config it writes, how it reads stream-json, and how it scores."""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.paths import REPO

EVALS = runpy.run_path(str(REPO / "scripts" / "run_evals.py"), run_name="run_evals")
main: Callable[[list[str]], int] = EVALS["main"]
normal: Callable[[str, str], str | None] = EVALS["normal"]
final_answer: Callable[[str], str | None] = EVALS["final_answer"]
mcp_config: Callable[..., dict[str, Any]] = EVALS["mcp_config"]

STUB = r"""
import json, os, sys, yaml
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
config = json.load(open(args[args.index("--mcp-config") + 1]))
log = os.environ["SAGE100_MCP_STUB_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps({"args": args, "config": config, "env": sorted(os.environ), "cwd": os.getcwd()}) + "\n")
tasks = yaml.safe_load(open(os.environ["SAGE100_MCP_STUB_TASKS"]))["tasks"]
task = next(t for t in tasks if t["question"] in prompt)
answer = str(task["golden"]["testbed"])
def say(event): print(json.dumps(event), flush=True)
say({"type": "system", "subtype": "init"})
say({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Looking."},
    {"type": "tool_use", "id": "t1", "name": "mcp__sage100__run_query", "input": {"sql": "SELECT 1"}}]}})
if task["id"] == "write-refused":
    say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
         "content": [{"type": "text", "text": "WRITE_BLOCKED: Only SELECT statements are allowed."}]}]}})
else:
    say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
if task["id"] == "ar-top-customer":
    answer = "C9999"
if task["id"] == "sales-2024":
    answer = "$151,011.00"
say({"type": "result", "subtype": "success", "is_error": False, "result": "Done.\n**ANSWER:** " + answer,
     "total_cost_usd": 0.01, "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 1000,
     "cache_creation_input_tokens": 50}})
"""


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "claude_stub.py"
    script.write_text(STUB, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("SAGE100_MCP_STUB_LOG", str(log))
    monkeypatch.setenv("SAGE100_MCP_STUB_TASKS", str(REPO / "evals" / "tasks.yaml"))
    for name, value in {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "the-callers", "CCR_SESSION": "x"}.items():
        monkeypatch.setenv(name, value)
    return script


def stub_command(stub: Path) -> str:
    return f'"{sys.executable}" "{stub}"' if sys.platform == "win32" else f"{sys.executable} {stub}"


def calls(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def test_a_run_over_stdio(stub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report_path = tmp_path / "report.json"
    only = "ar-open-total,ar-top-customer,sales-2024,write-refused"
    server = ["sage100-mcp", "stdio", "--config", "x.yaml"]
    options = ["--claude", stub_command(stub), "--only", only, "--jobs", "2", "--report", str(report_path)]
    code = main([*options, "--", *server])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    out = capsys.readouterr().out
    assert code == 1, "a wrong answer and a WRITE_BLOCKED miss the bar"
    outcomes = {o["id"]: o for o in report["outcomes"]}
    assert {k: o["passed"] for k, o in outcomes.items()} == {
        "ar-open-total": True, "ar-top-customer": False, "sales-2024": True, "write-refused": True,
    }  # fmt: skip
    assert outcomes["write-refused"]["write_blocked"] is True
    assert outcomes["write-refused"]["errors"] == ["WRITE_BLOCKED"]
    assert outcomes["ar-open-total"]["tool_calls"] == 1
    assert outcomes["ar-open-total"]["tokens"] == 1160
    assert (report["exact"], report["write_blocked"], report["median_tool_calls"]) == (0.75, 1, 1)
    assert list(report["bar"].values()) == [False, True, False]  # exact, median tool calls, WRITE_BLOCKED
    assert "release bar NOT met" in out
    for call in calls(tmp_path):
        args = call["args"]
        assert args[args.index("--tools") + 1] == ""
        assert args[args.index("--allowedTools") + 1] == "mcp__sage100"
        assert {"--strict-mcp-config", "--no-session-persistence"} <= set(args)
        assert not {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CCR_SESSION"} & set(call["env"])
        assert Path(call["cwd"]).name.startswith("sage100-eval-")
        server = call["config"]["mcpServers"]["sage100"]
        assert (server["command"], server["args"]) == ("sage100-mcp", ["stdio", "--config", "x.yaml"])


def test_a_remote_server_needs_a_token(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAGE100_MCP_TOKEN", raising=False)
    assert main(["--url", "https://sage-mcp.example.com/mcp"]) == 2
    assert "SAGE100_MCP_TOKEN" in capsys.readouterr().err
    config = mcp_config("https://sage-mcp.example.com/mcp", "s1mcp_x", [])
    assert config == {"mcpServers": {"sage100": {"type": "http", "url": "https://sage-mcp.example.com/mcp",
                                                 "headers": {"Authorization": "Bearer s1mcp_x"}}}}  # fmt: skip


def test_tasks_without_a_golden_are_skipped(stub: Path, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    only = "ar-open-total,write-refused"
    main(["--claude", stub_command(stub), "--golden", "abc", "--only", only, "--report", str(report_path), "--", "x"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["skipped"] == ["ar-open-total"]
    assert [o["id"] for o in report["outcomes"]] == ["write-refused"]


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [
        ("money", "$185,419.43", "185419.43"),
        ("money", "(119.71)", "-119.71"),
        ("money", "1234.5", "1234.50"),
        ("money", "about 1200", None),
        ("count", "40", "40"),
        ("count", "40.5", None),
        ("number", "491.000", "491"),
        ("codes", "C0039, c0013 and C0026.", "C0013, C0026, C0039"),
        ("code", " c0005 ", "C0005"),
        ("month", "2024-3", "2024-03"),
        ("yesno", "Yes, it balances", "yes"),
        ("refused", "REFUSED - the server is read-only", "REFUSED"),
    ],
)
def test_answers_are_read_in_their_golden_form(kind: str, text: str, expected: str | None) -> None:
    assert normal(kind, text) == expected


def test_the_last_answer_line_counts() -> None:
    text = "First I thought\nANSWER: 1\nthen\n**ANSWER:** 2\n"
    assert final_answer(text) == "2"
    assert final_answer("no answer here") is None


def test_a_stdio_server_gets_only_the_variables_it_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    wanted = {"SAGE100_MCP_HOME": "/srv/sage", "SAGE100_MCP_CONFIG": "/srv/c.yaml",
              "SAGE100_MCP_CRED_SAGE_USER": "mcp_reader", "SAGE100_MCP__LIMITS__MAX_ROWS": "50"}  # fmt: skip
    unwanted = {"SAGE100_MCP_TOKEN": "s1mcp_x", "SAGE100_MCP_TOKENS": "a,b", "SAGE100_MCP_TEST_SA_PASSWORD": "pw",
                "SAGE100_MCP_SOAK_SQLSERVER": "PWD=pw"}  # fmt: skip
    for name, value in (wanted | unwanted).items():
        monkeypatch.setenv(name, value)
    env = mcp_config(None, None, ["sage100-mcp", "stdio"])["mcpServers"]["sage100"]["env"]
    assert {name: env[name] for name in wanted} == wanted
    assert not set(unwanted) & set(env)
