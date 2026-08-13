"""Workflow policy — Task 15 Steps 1-2.

Workflows are parsed as data, not grepped as text. The rules here are the ones
that keep CI honest: third-party actions pinned to a commit, no paid inference
or upstream network on pull requests, a real cross-OS and cross-Python matrix,
and a release path that a human still has to publish.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
VALIDATE = WORKFLOWS / "validate.yml"
UPSTREAMS = WORKFLOWS / "upstreams.yml"
RELEASE = WORKFLOWS / "release.yml"
ALL_WORKFLOWS = (VALIDATE, UPSTREAMS, RELEASE)

SHA_PIN = re.compile(r"^[0-9a-f]{40}$")
FIRST_PARTY_PREFIX = "actions/"


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document):
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    if "on" in document:
        return document["on"]
    return document[True]


def _steps(document):
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            yield job, step


def _run_text(document) -> str:
    return "\n".join(step.get("run", "") for _, step in _steps(document))


# --- pinning -----------------------------------------------------------------

@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path):
    document = _load(path)
    for _, step in _steps(document):
        uses = step.get("uses")
        if uses is None:
            continue
        assert "@" in uses, uses
        _, _, reference = uses.partition("@")
        assert SHA_PIN.match(reference), "%s is not pinned to a 40-hex commit" % uses


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda path: path.name)
def test_pinned_actions_record_the_human_readable_version(path):
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "uses:" in line and "@" in line:
            assert "#" in line, "pin without a version comment: %s" % line.strip()


# --- pull-request validation -------------------------------------------------

def test_validation_runs_on_pull_requests_with_read_only_permissions():
    document = _load(VALIDATE)
    triggers = _triggers(document)
    assert "pull_request" in triggers
    assert document["permissions"] == {"contents": "read"}


def test_validation_cancels_superseded_runs():
    document = _load(VALIDATE)
    concurrency = document["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    assert "github." in str(concurrency["group"])


def test_validation_matrix_covers_both_operating_systems_and_python_floors():
    document = _load(VALIDATE)
    job = document["jobs"]["validate"]
    matrix = job["strategy"]["matrix"]
    assert any(str(item).startswith("ubuntu") for item in matrix["os"])
    assert any(str(item).startswith("macos") for item in matrix["os"])
    assert "3.9" in [str(item) for item in matrix["python"]]
    assert len(matrix["python"]) >= 2


def test_validation_pins_encoding_and_locale():
    document = _load(VALIDATE)
    environment = document["env"]
    assert environment["PYTHONUTF8"] in (1, "1")
    assert "LC_ALL" in environment and "LANG" in environment


@pytest.mark.parametrize(
    "fragment",
    [
        "scripts.generate_adapters --check",
        "scripts.validate",
        "pytest -q",
        "scripts.package_release",
        "cmp ",
        "tar -xzf",
        "install.sh --dry-run",
    ],
)
def test_validation_runs_every_offline_gate(fragment):
    assert fragment in _run_text(_load(VALIDATE)), fragment


def test_validation_syntax_checks_each_shell_script_separately():
    """`sh -n a b c` only parses the first file; the rest become $1, $2.

    The plan's literal one-liner would therefore check install.sh alone and
    silently ignore both hooks, so each script gets its own invocation.
    """
    text = _run_text(_load(VALIDATE))
    for script in (
        "install.sh",
        "adapters/codex/user-prompt-reminder.sh",
        "adapters/claude/user-prompt-reminder.sh",
    ):
        assert re.search(r"/bin/sh -n %s\b" % re.escape(script), text), script
    assert not re.search(r"/bin/sh -n \S+ \S+", text), "multiple files in one sh -n call"


@pytest.mark.parametrize("path", [VALIDATE, RELEASE], ids=lambda path: path.name)
def test_build_epoch_is_pinned_workflow_wide_not_per_command(path):
    # Pinning SOURCE_DATE_EPOCH in `env` covers every step, so a later build
    # step cannot silently pick up the ambient clock.
    assert str(_load(path)["env"]["SOURCE_DATE_EPOCH"]).isdigit()


def test_validation_builds_twice_and_compares_bytes():
    text = _run_text(_load(VALIDATE))
    assert text.count("scripts.package_release") >= 2
    assert "cmp " in text


def test_pull_request_validation_never_runs_paid_or_networked_work():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "--confirm-live" not in text
    assert "check_upstreams --online" not in text
    assert "run_live" not in text
    assert "secrets." not in text


# --- upstream monitoring -----------------------------------------------------

def test_upstream_workflow_is_scheduled_or_manual_only():
    triggers = _triggers(_load(UPSTREAMS))
    assert set(triggers) <= {"schedule", "workflow_dispatch"}
    assert "schedule" in triggers


def test_upstream_workflow_verifies_pins_online_without_editing_them():
    document = _load(UPSTREAMS)
    text = _run_text(document)
    assert "--online" in text
    assert document["permissions"] == {"contents": "read"}
    for forbidden in ("git commit", "git push", "sed -i"):
        assert forbidden not in text, forbidden


# --- release -----------------------------------------------------------------

def test_release_triggers_only_on_version_tags():
    triggers = _triggers(_load(RELEASE))
    assert set(triggers) <= {"push", "workflow_dispatch"}
    assert triggers["push"]["tags"] == ["v*"]
    assert "pull_request" not in triggers


def test_release_checks_the_tag_against_the_version_file():
    text = _run_text(_load(RELEASE))
    assert "VERSION" in text
    assert "GITHUB_REF_NAME" in text or "github.ref_name" in text


def test_release_uses_a_protected_environment_and_attestation_permissions():
    document = _load(RELEASE)
    job = document["jobs"]["release"]
    assert job["environment"] == "release"
    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["attestations"] == "write"
    assert job["permissions"]["contents"] == "write"


def test_release_reverifies_downloaded_assets_before_a_human_publishes():
    text = _run_text(_load(RELEASE))
    assert "--draft" in text
    assert "gh release download" in text
    assert "SHA256SUMS" in text
    assert "shasum -a 256 -c" in text or "sha256sum -c" in text


def test_release_does_not_publish_automatically():
    text = _run_text(_load(RELEASE))
    assert "gh release edit" not in text or "--draft=false" not in text
    assert "--latest" not in text
