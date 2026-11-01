# Model evals

SPEC.md §15.6: Claude Code answers 40 plain-English questions through sage100-mcp, and the answers are
scored against golden ones. The release bar is at least 90% exact, a median of 4 or fewer tool calls per
question, and no `WRITE_BLOCKED` error caused by the model. [decisions.md](decisions.md) D62 has the
details.

## The question set

[`evals/tasks.yaml`](../evals/tasks.yaml) holds the questions: AR and AP balances and aging, open sales
and purchase orders, inventory, the trial balance, sales history, and one request to change data, which
must be refused. Each question names its company and, where it matters, its date, so it has one right
answer.

Each task has two golden answers:

- **`testbed`**: the answer on the synthetic test bed that the emulator and SQL Server tests use.
  `tests/emulator/test_eval_goldens.py` works each one out from the test rows in plain Python, and checks
  that the server's own tools give the same answer on the emulator. `tests/premium/test_eval_goldens.py`
  checks the same on SQL Server. After changing the test data, rerun the first with
  `SAGE100_MCP_UPDATE_GOLDENS=1` to rewrite these answers.
- **`abc`**: the answer for Sage's ABC sample company, written in from ABC's own reports on a machine with
  Sage installed. Until a task has one, an ABC run skips it.

## Running it

The runner starts `claude -p` for each question:

- with no built-in tools and only this server's tools allowed;
- in an empty working directory, with no saved session;
- with the environment of any Claude Code session that launched it removed.

Claude Code needs credentials of its own (`ANTHROPIC_API_KEY`, or a `/login`). Each question costs a few
cents.

Against the test bed, through a local server over stdio:

```bash
SAGE100_MCP_CRED_PREMIUM_USER=... SAGE100_MCP_CRED_PREMIUM_PASSWORD=... \
  uv run python scripts/run_evals.py --golden testbed --report evals.json -- uv run sage100-mcp stdio --config test.yaml
```

Against ABC on a remote server over HTTPS, which is the M4 gate run from a Mac:

```bash
export SAGE100_MCP_TOKEN='s1mcp_...'
uv run python scripts/run_evals.py --golden abc --url https://sage-mcp.corp.example/mcp --report evals.json
```

Useful options:

- `--only ar-over-60,gl-period-6-balances` runs some tasks only;
- `--model` picks the model;
- `--jobs` sets how many questions run at once (default 4);
- `--claude` names the Claude Code command.

For each task the runner prints whether it passed, the answer it got, the tool calls it made, any tool
errors and the tokens used. It ends with whether the release bar was met. With `--report`, the same goes
to a JSON file.

## How answers are scored

The prompt asks the model to end with `ANSWER: <answer>` in a set form:

- money as a plain number with two decimals;
- counts and quantities as plain numbers;
- lists of codes separated by commas;
- months as `YYYY-MM`;
- `yes` or `no`;
- `REFUSED` for what the tools can't do.

The last `ANSWER:` line is compared with the golden answer in its own form. `$185,419.43` and `185419.43`
are equal, lists match in any order, and money must match to the cent. A reply without an `ANSWER:` line
fails.

## Filling in the ABC answers

On the Windows runner with the ABC sample company:

1. Run the matching Sage report or inquiry for each task, with the dates the question gives. For example,
   AR Aged Invoice Report as of 12/31/2024, and Trial Balance for period 6.
2. Write each figure into the task's `golden.abc`, in the form described under "How answers are scored".
3. Commit the change, then run the eval with `--golden abc`.
