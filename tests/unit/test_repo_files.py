"""The repository's GitHub files agree with each other (docs/decisions.md D75).

If a workflow names an environment nobody defined, its run fails. If it uses an action the allow-list lacks,
the run is refused. These tests catch both before a push does.
"""

from __future__ import annotations

import fnmatch
import json
import re
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests.paths import REPO

# The scripts aren't a package; the tests load them by path, as test_run_evals.py does.
includes = cast(Callable[[Any, Any], bool], runpy.run_path(str(REPO / "scripts" / "github_settings.py"))["includes"])

GITHUB = REPO / ".github"
WORKFLOWS = sorted((GITHUB / "workflows").glob("*.yml"))
SETTINGS = GITHUB / "settings"


def workflow(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def uses(path: Path) -> set[str]:
    return set(re.findall(r"^\s*-?\s*uses:\s*(\S+)", path.read_text(encoding="utf-8"), re.MULTILINE))


def test_every_environment_a_workflow_names_is_defined() -> None:
    defined = set(json.loads((SETTINGS / "environments.json").read_text(encoding="utf-8")))
    jobs = [job for path in WORKFLOWS for job in workflow(path)["jobs"].values()]
    named = {job["environment"] for job in jobs if "environment" in job}
    assert named == defined  # and none is defined that nothing uses


def test_every_action_is_on_the_allow_list() -> None:
    allowed = json.loads((SETTINGS / "actions.json").read_text(encoding="utf-8"))["selected_actions"]
    outside = {action for path in WORKFLOWS for action in uses(path) if not action.startswith("actions/")}
    assert outside  # the workflows do use some
    for action in outside:
        assert any(fnmatch.fnmatch(action, pattern) for pattern in allowed["patterns_allowed"]), action


def test_secrets_are_used_only_in_gated_jobs() -> None:
    """Signing and publishing secrets live in environments, so only jobs in one may read them."""
    for path in WORKFLOWS:
        for name, job in workflow(path)["jobs"].items():
            text = yaml.safe_dump(job)
            if "secrets." in text:
                assert "environment" in job, f"{path.name}: job {name} reads secrets outside an environment"


def test_the_main_ruleset_needs_the_code_owner() -> None:
    ruleset = json.loads((SETTINGS / "ruleset-main.json").read_text(encoding="utf-8"))
    rules = {rule["type"]: rule for rule in ruleset["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request"} <= set(rules)
    review = rules["pull_request"]["parameters"]
    assert review["require_code_owner_review"]
    assert review["require_last_push_approval"]
    assert review["required_approving_review_count"] >= 1
    assert [actor["bypass_mode"] for actor in ruleset["bypass_actors"]] == ["pull_request"]  # no direct pushes
    owners = [line.split() for line in (GITHUB / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")]  # fmt: skip
    assert ["*", "@ALIRAZA47"] in owners


def test_release_tags_are_the_admins_alone() -> None:
    ruleset = json.loads((SETTINGS / "ruleset-release-tags.json").read_text(encoding="utf-8"))
    assert ruleset["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]
    assert {rule["type"] for rule in ruleset["rules"]} == {"creation", "update", "deletion"}
    release = workflow(GITHUB / "workflows" / "release.yml")
    triggers: dict[Any, Any] = release
    assert triggers[True]["push"]["tags"] == ["v*"]  # YAML 1.1 reads the key "on" as True


@pytest.mark.parametrize("path", sorted((GITHUB / "ISSUE_TEMPLATE").glob("*.yml")), ids=lambda p: p.name)
def test_issue_forms_parse(path: Path) -> None:
    form = yaml.safe_load(path.read_text(encoding="utf-8"))
    if path.name == "config.yml":
        assert form["blank_issues_enabled"] is False
        assert any("security/advisories/new" in link["url"] for link in form["contact_links"])
        return
    assert form["name"]
    assert form["description"]
    ids = [item["id"] for item in form["body"] if "id" in item]
    assert len(ids) == len(set(ids))


def test_the_community_files_exist() -> None:
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", ".github/PULL_REQUEST_TEMPLATE.md"):
        assert (REPO / name).is_file(), name


@pytest.mark.parametrize(
    ("actual", "wanted", "same"),
    [
        ({"a": 1, "b": 2}, {"a": 1}, True),  # GitHub adds defaults the files don't name
        ({"a": 1}, {"a": 2}, False),
        ({"rules": [{"type": "x", "p": 1}, {"type": "y"}]}, {"rules": [{"type": "y"}]}, True),
        ({"rules": [{"type": "x"}]}, {"rules": [{"type": "y"}]}, False),
        ({"tags": ["b", "a"]}, {"tags": ["a", "b"]}, True),
        ({"tags": ["a"]}, {"tags": ["a", "b"]}, False),
        ({"exclude": []}, {"exclude": []}, True),
        (None, {"a": 1}, False),
    ],
)
def test_the_settings_check_compares_what_the_files_name(actual: Any, wanted: Any, same: bool) -> None:
    assert includes(actual, wanted) is same
