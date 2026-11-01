"""The §15.8 log scan: no password, token or masked value in logs, audit files or error details.

Run the suite with a log file at DEBUG and a known temporary directory, then scan both:

    uv run pytest -m "not windows" --log-file=build/pytest.log --log-file-level=DEBUG --basetemp=build/tmp
    uv run python scripts/log_scan.py build/pytest.log build/tmp --testbed

Every text file under the paths is read: the log (where the server writes each tool error's message, hint
and detail at DEBUG), audit .jsonl files, and the logs of servers the tests started. A finding is:

- `PWD=` or `PASSWORD=` with a value other than `***`;
- a bearer token (`s1mcp_...`);
- a secret given with --secret, or with --testbed one of the passwords the tests use;
- with --testbed, a value the synthetic test data holds in a column the policy masks.

Findings are printed by file and line with the secret itself hidden. Exit status 0 means none.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

CONNECTION_SECRET = re.compile(r"(?i)\b(PWD|PASSWORD)\s*=\s*(?!\*\*\*)([^\s;'\"}]+)")
TOKEN = re.compile(r"s1mcp_[A-Za-z0-9_-]{20,}")
TEXT_SUFFIXES = {".log", ".jsonl", ".json", ".txt", ".yaml", ".yml", ".out", ".err", "", ".gz"}
SKIP_DIRS = {"__pycache__", ".git", ".venv"}
# A directory holding this file is a planted test of the scan itself: skipped when scanned from above.
FIXTURE_MARKER = ".log-scan-fixture"
MIN_VALUE_LENGTH = 6  # shorter masked values ("N", "4111") would match unrelated text
# Passwords the tests use (tests/premium_testbed.py, tests/emulator, tests/unit/test_credentials.py,
# tests/emulator/test_executor.py). None of them may ever be written anywhere.
TEST_PASSWORDS = ("reader-pass", "from-keyring", "s3cret", "pa;ss}word", "Wr0ng;pass")
SA_PASSWORD_ENV = "SAGE100_MCP_TEST_SA_PASSWORD"


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str

    def text(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def files(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file():
            yield path
        elif path.is_dir():
            planted = {marker.parent for marker in path.rglob(FIXTURE_MARKER) if marker.parent != path}
            for found in sorted(path.rglob("*")):
                if planted & set(found.parents) or SKIP_DIRS & set(found.parts):
                    continue
                if found.is_file() and found.suffix.lower() in TEXT_SUFFIXES:
                    yield found


def testbed_values() -> tuple[set[str], set[str]]:
    """(the test passwords, every value the synthetic data keeps in a masked column)."""
    from sage100_mcp.policy import load_policy

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the repository, for tests/
    from tests import premium_testbed as testbed

    policy = load_policy(None)
    passwords = {testbed.READER_PASSWORD, *TEST_PASSWORDS}
    if os.environ.get(SA_PASSWORD_ENV):
        passwords.add(os.environ[SA_PASSWORD_ENV])
    masked: set[str] = set()
    for rows in (testbed.emp_rows(), testbed.pcc_rows(), testbed.small_rows("ABC")):
        for table, records in rows.items():
            for record in records:
                for column, value in record.items():
                    text = "" if value is None else str(value).strip()
                    if len(text) >= MIN_VALUE_LENGTH and policy.is_masked(table, column):
                        masked.add(text)
    return passwords, masked


def read_lines(path: Path) -> list[str]:
    if path.suffix == ".gz":  # earlier days of the audit log
        with gzip.open(path, "rt", encoding="utf-8") as packed:
            return list(packed)
    with path.open(encoding="utf-8") as handle:
        return list(handle)


def scan(paths: Iterable[Path], secrets: set[str], masked: set[str]) -> tuple[list[Finding], int]:
    """Every finding, and how many files were read."""
    bounded = [re.compile(rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])") for value in sorted(masked)]
    findings: list[Finding] = []
    count = 0
    for path in files(paths):
        try:
            lines = read_lines(path)
        except (UnicodeDecodeError, OSError, EOFError):
            continue  # binary: DuckDB spill files, databases; or a gzip that isn't one
        count += 1
        for number, line in enumerate(lines, 1):
            if CONNECTION_SECRET.search(line):
                findings.append(Finding(path, number, "a connection-string password"))
            if TOKEN.search(line):
                findings.append(Finding(path, number, "a bearer token"))
            if any(secret in line for secret in secrets):
                findings.append(Finding(path, number, "a known secret"))
            if any(pattern.search(line) for pattern in bounded):
                findings.append(Finding(path, number, "a value from a masked column"))
    return findings, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="log files and directories to read")
    parser.add_argument("--secret", action="append", default=[], help="a value that must appear nowhere")
    parser.add_argument("--testbed", action="store_true", help="also the test passwords and masked test data")
    args = parser.parse_args(argv)
    missing = [str(path) for path in args.paths if not path.exists()]
    if missing:
        print(f"not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    secrets = {secret for secret in args.secret if secret}
    masked: set[str] = set()
    if args.testbed:
        passwords, masked = testbed_values()
        secrets |= passwords
    findings, count = scan(args.paths, secrets, masked)
    for finding in findings[:200]:
        print(finding.text())
    print(
        f"{count} files read; {len(secrets)} secrets and {len(masked)} masked values looked for; "
        f"{len(findings)} finding(s)"
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
