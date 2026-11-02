"""Apply the repository's GitHub settings from .github/settings (docs/decisions.md D75).

    uv run python scripts/github_settings.py                    # every part, as a repository admin
    uv run python scripts/github_settings.py --parts rulesets   # some parts: rulesets, actions, environments, security
    uv run python scripts/github_settings.py --check            # report what differs, change nothing

The settings live in the repository, so a change to them is reviewed like code. There are four parts:
- **rulesets.**
  - `main` changes only through a pull request whose latest push the code owner approved.
  - Nobody force-pushes to `main` or deletes it.
  - Only a repository admin creates, moves or deletes `v*` tags, which start a release.
- **actions.**
  - Workflows may use GitHub's own actions and the two other actions they name, nothing else.
  - Tokens are read-only unless a workflow asks for more, and no workflow can approve a pull request.
  - A pull request from a fork runs no workflow until a maintainer approves the run.
- **environments.** `release`, `pypi` and `registry` need the repository owner's approval, and only the
  listed branches and tags may use them. The signing and publishing secrets belong in these environments,
  not in the repository's secrets, so no workflow run gets them without that approval.
- **security.** Private vulnerability reporting is on, as SECURITY.md promises.

The script calls the GitHub API through `gh api`, so it needs the GitHub CLI logged in as a repository admin.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SETTINGS = REPO / ".github" / "settings"
PARTS = ("rulesets", "actions", "environments", "security")
ACTIONS = {  # the order matters: selected actions apply only once allowed_actions is "selected"
    "permissions": "actions/permissions",
    "selected_actions": "actions/permissions/selected-actions",
    "workflow": "actions/permissions/workflow",
    "fork_pr_contributor_approval": "actions/permissions/fork-pr-contributor-approval",
}


class GitHubError(Exception):
    pass


def gh(method: str, path: str, body: Any = None) -> Any:
    command = ["gh", "api", "--method", method, path, "-H", "Accept: application/vnd.github+json"]
    if body is not None:
        command += ["--input", "-"]
    done = subprocess.run(command, input=None if body is None else json.dumps(body), capture_output=True, text=True)
    if done.returncode:
        raise GitHubError(f"{method} {path}: {(done.stdout + done.stderr).strip()}")
    return json.loads(done.stdout) if done.stdout.strip() else None


def includes(actual: Any, wanted: Any) -> bool:
    """Whether ``actual`` has everything ``wanted`` says: GitHub adds defaults that the files don't name."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(includes(actual.get(key), value) for key, value in wanted.items())
    if isinstance(wanted, list) and wanted and isinstance(wanted[0], dict):
        return isinstance(actual, list) and all(any(includes(item, want) for item in actual) for want in wanted)
    if isinstance(wanted, list):
        return isinstance(actual, list) and sorted(map(str, actual)) == sorted(map(str, wanted))
    return bool(actual == wanted)


def load(name: str) -> Any:
    return json.loads((SETTINGS / name).read_text(encoding="utf-8"))


def rulesets(repo: str, check: bool) -> list[str]:
    existing = {ruleset["name"]: ruleset["id"] for ruleset in gh("GET", f"repos/{repo}/rulesets") or []}
    report = []
    for path in sorted(SETTINGS.glob("ruleset-*.json")):
        wanted = json.loads(path.read_text(encoding="utf-8"))
        name, number = wanted["name"], existing.get(wanted["name"])
        if check:
            actual = gh("GET", f"repos/{repo}/rulesets/{number}") if number else None
            report.append(f"ruleset {name}: {'ok' if actual and includes(actual, wanted) else 'differs'}")
        elif number:
            gh("PUT", f"repos/{repo}/rulesets/{number}", wanted)
            report.append(f"ruleset {name}: updated")
        else:
            gh("POST", f"repos/{repo}/rulesets", wanted)
            report.append(f"ruleset {name}: created")
    return report


def actions(repo: str, check: bool) -> list[str]:
    settings, report = load("actions.json"), []
    for key, endpoint in ACTIONS.items():
        if check:
            same = includes(gh("GET", f"repos/{repo}/{endpoint}"), settings[key])
            report.append(f"actions {key}: {'ok' if same else 'differs'}")
        else:
            gh("PUT", f"repos/{repo}/{endpoint}", settings[key])
            report.append(f"actions {key}: set")
    return report


def environments(repo: str, check: bool) -> list[str]:
    owner = gh("GET", f"repos/{repo}")["owner"]
    if owner["type"] != "User":
        raise GitHubError("the environments' reviewer is the repository owner, so the owner must be a user")
    reviewers = [{"type": "User", "id": owner["id"]}]
    report = []
    for name, refs in load("environments.json").items():
        wanted_refs = {(pattern, "branch") for pattern in refs.get("branches", [])}
        wanted_refs |= {(pattern, "tag") for pattern in refs.get("tags", [])}
        body = {
            "wait_timer": 0,
            "prevent_self_review": False,
            "reviewers": reviewers,
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        }
        if check:
            report.append(f"environment {name}: {_environment_state(repo, name, owner['id'], wanted_refs)}")
            continue
        gh("PUT", f"repos/{repo}/environments/{name}", body)
        for pattern, kind in sorted(wanted_refs - _deploy_refs(repo, name)):
            gh("POST", f"repos/{repo}/environments/{name}/deployment-branch-policies", {"name": pattern, "type": kind})
        report.append(f"environment {name}: owner approves; {', '.join(f'{k} {p}' for p, k in sorted(wanted_refs))}")
    return report


def _deploy_refs(repo: str, name: str) -> set[tuple[str, str]]:
    policies = gh("GET", f"repos/{repo}/environments/{name}/deployment-branch-policies")["branch_policies"]
    return {(policy["name"], policy.get("type", "branch")) for policy in policies}


def _environment_state(repo: str, name: str, owner_id: int, wanted_refs: set[tuple[str, str]]) -> str:
    try:
        environment = gh("GET", f"repos/{repo}/environments/{name}")
    except GitHubError as exc:
        if "Not Found" in str(exc):
            return "missing"
        raise
    rules = {rule["type"]: rule for rule in environment.get("protection_rules", [])}
    reviewers = rules.get("required_reviewers", {}).get("reviewers", [])
    gated = any(reviewer["reviewer"].get("id") == owner_id for reviewer in reviewers)
    return "ok" if gated and _deploy_refs(repo, name) == wanted_refs else "differs"


def security(repo: str, check: bool) -> list[str]:
    if check:
        enabled = (gh("GET", f"repos/{repo}/private-vulnerability-reporting") or {}).get("enabled")
        return [f"private vulnerability reporting: {'ok' if enabled else 'off'}"]
    gh("PUT", f"repos/{repo}/private-vulnerability-reporting")
    return ["private vulnerability reporting: on"]


def origin_repo() -> str:
    url = subprocess.run(["git", "-C", str(REPO), "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    found = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not found:
        raise SystemExit(f"origin ({url}) is not a GitHub repository; pass --repo OWNER/NAME")
    return found.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", help="OWNER/NAME (default: the origin remote)")
    parser.add_argument("--parts", default=",".join(PARTS), help=f"comma-separated: {', '.join(PARTS)}")
    parser.add_argument("--check", action="store_true", help="report what differs and change nothing")
    args = parser.parse_args(argv)
    repo = args.repo or origin_repo()
    steps: dict[str, Callable[[str, bool], list[str]]] = {
        "rulesets": rulesets, "actions": actions, "environments": environments, "security": security,
    }  # fmt: skip
    failed = 0
    for part in [p.strip() for p in args.parts.split(",") if p.strip()]:
        if part not in steps:
            parser.error(f"unknown part {part!r}")
        try:
            for line in steps[part](repo, args.check):
                print(line)
        except GitHubError as exc:
            failed += 1
            print(f"{part}: FAILED: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
