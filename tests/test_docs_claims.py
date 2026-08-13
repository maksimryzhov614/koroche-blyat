"""Public documentation claims — Task 14 Steps 1-2.

The documents may only say what the repository can back up. Versions are
compared against the installer's own floors and the capability fixture rather
than a constant repeated in prose, so a bumped floor cannot silently leave the
README stale.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
INSTALL = ROOT / "docs/INSTALL.md"
COMPATIBILITY = ROOT / "docs/COMPATIBILITY.md"
UPDATING = ROOT / "docs/UPDATING.md"
DOCUMENTS = (README, CHANGELOG, INSTALL, COMPATIBILITY, UPDATING)

VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
FIXTURE = json.loads((ROOT / "tests/fixtures/host-capabilities-v1.json").read_text(encoding="utf-8"))
FLOORS = {item["id"]: item["minimum_version"] for item in FIXTURE["hosts"]}
RELEASE_ASSET = (
    "https://github.com/maksimryzhov614/koroche-blyat/releases/download/"
    "v1.0.0/koroche-blyat-1.0.0.tar.gz"
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.name)
def test_document_exists_and_is_utf8_with_final_newline(path):
    raw = path.read_bytes()
    raw.decode("utf-8")
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw


# --- README structure --------------------------------------------------------

def test_adult_language_notice_appears_before_anything_else():
    lines = [line for line in _text(README).splitlines() if line.strip()]
    head = "\n".join(lines[:4]).casefold()
    assert "мат" in head or "adult language" in head or "ненормативн" in head


def test_readme_states_the_outcome_and_the_clean_artifact_boundary():
    text = _text(README).casefold()
    assert "коммит" in text or "commit" in text
    assert "артефакт" in text or "artifact" in text
    assert "чист" in text or "clean" in text


def test_support_table_lists_only_the_three_verified_hosts():
    text = _text(README)
    assert "Prime Agent" in text
    assert "Codex CLI" in text
    assert "Claude Code" in text
    for stranger in ("Cursor", "Windsurf", "Copilot", "Aider", "Continue"):
        assert stranger not in text, stranger


@pytest.mark.parametrize("host", sorted(FLOORS))
def test_documented_floor_matches_the_capability_fixture(host):
    from scripts.installer.plan import _FLOORS

    assert _FLOORS[host] == FLOORS[host], "installer and fixture disagree for %s" % host
    assert FLOORS[host] in _text(README), "README does not document the %s floor" % host


def test_release_version_is_taken_from_the_version_file():
    assert VERSION == "1.0.0"
    assert VERSION in _text(README)


# --- installation guidance ---------------------------------------------------

def test_install_documents_the_immutable_asset_and_checksum_verification():
    text = _text(INSTALL)
    assert RELEASE_ASSET in text
    assert "SHA256SUMS" in text
    assert "shasum" in text or "sha256sum" in text


def test_install_documents_inspection_dry_run_install_update_and_uninstall():
    text = _text(INSTALL)
    assert "--dry-run" in text
    assert "--uninstall" in text
    assert "tar -tzf" in text or "unzip -l" in text
    assert "./install.sh" in text


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.name)
def test_no_document_pipes_a_remote_script_into_a_shell(path):
    text = _text(path)
    assert not re.search(r"curl[^\n]*\|\s*(?:sudo\s+)?(?:sh|bash)", text)
    assert not re.search(r"wget[^\n]*\|\s*(?:sudo\s+)?(?:sh|bash)", text)
    assert "/main | sh" not in text


def test_docs_state_that_npx_skills_update_does_not_update_adapters():
    text = " ".join(_text(path) for path in (README, UPDATING))
    assert "npx skills update" in text
    window = text[text.index("npx skills update"):][:400].casefold()
    assert "не обнов" in window or "does not update" in window


# --- compatibility -----------------------------------------------------------

def test_compatibility_defines_every_state_and_the_manual_codex_action():
    text = _text(COMPATIBILITY)
    for state in ("Supported", "Verified", "Degraded", "Unsupported"):
        assert state in text, state
    assert "/hooks" in text
    for bypass in ("--safe-mode", "--bare", "--no-extensions", "--no-context-files"):
        assert bypass in text, bypass


def test_compatibility_admits_managed_policy_can_outrank_always_on():
    text = _text(COMPATIBILITY).casefold()
    assert "managed" in text or "управляем" in text


# --- claims ------------------------------------------------------------------

@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.name)
def test_no_unsupported_marketing_claim_appears(path):
    text = _text(path)
    assert not re.search(r"(?i)(?:экономи\w*|сокраща\w*|saves?|reduces?)\D{0,24}\d{1,3}\s*%", text)
    assert not re.search(r"(?i)\d{1,3}\s*%\s*(?:меньше|экономи|reduction|fewer|less)", text)
    assert not re.search(r"(?i)\bperfect\b", text)
    assert not re.search(r"(?i)\bguaranteed\b", text)
    assert not re.search(r"(?i)100\s*%\s*accurate", text)


def test_readme_declines_the_token_claim_explicitly():
    assert "No fixed token-saving percentage is claimed for 1.0.0." in _text(README)


def test_the_offline_validator_agrees_with_these_documents():
    from scripts.validate import validate_repo

    assert validate_repo(ROOT, ["docs-claims"]) == ()


# --- provenance and attribution ----------------------------------------------

def test_readme_names_both_upstream_authors_and_denies_affiliation():
    text = _text(README)
    assert "Julius Brussee" in text
    assert "Serge Shima" in text
    assert "не аффилирован" in text.casefold() or "not affiliated" in text.casefold()


def test_readme_does_not_use_caveman_as_the_product_brand():
    text = _text(README)
    assert "Caveman" in text, "attribution must still name the upstream"
    assert not re.search(r"(?i)koroche[- ]blyat\s+by\s+caveman", text)
    heading = text.splitlines()[0]
    assert "Caveman" not in heading


def test_readme_states_that_no_network_call_happens_at_runtime():
    text = _text(README).casefold()
    assert "сет" in text or "network" in text


# --- changelog ---------------------------------------------------------------

def test_changelog_keeps_work_unreleased_until_the_release_commit():
    text = _text(CHANGELOG)
    assert "Keep a Changelog" in text
    assert "[Unreleased]" in text
    assert not re.search(r"## \[1\.0\.0\] - \d{4}-\d{2}-\d{2}", text), (
        "the dated 1.0.0 heading belongs to Task 16, not here"
    )
