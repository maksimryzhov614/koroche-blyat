"""Offline repository validator — Task 13 Steps 1-2.

Each check gets a planted-violation test against a throwaway copy of the
tracked tree, plus the real repository as the positive control. A validator
that reported everything as broken would still satisfy the negative tests
alone, so `test_real_repository_is_clean` is the one that keeps them honest.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate import CHECKS, Violation, validate_repo

ROOT = Path(__file__).parents[1]

EXPECTED_ORDER = (
    "encoding",
    "skill-frontmatter",
    "links",
    "placeholders",
    "generated-parity",
    "eval-fixtures",
    "package",
    "provenance",
    "docs-claims",
)


def _tracked_copy(tmp_path: Path) -> Path:
    """Copy the tracked tree so a test can plant a violation in it."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8")
    target = tmp_path / "repo"
    for name in listing.split("\0"):
        if not name:
            continue
        source = ROOT / name
        if not source.is_file():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    return target


def _checks(violations, name):
    return [violation for violation in violations if violation.check == name]


# --- contract ----------------------------------------------------------------

def test_checks_are_declared_in_the_documented_order():
    assert CHECKS == EXPECTED_ORDER


def test_real_repository_is_clean():
    assert validate_repo(ROOT) == ()


def test_violations_sort_by_check_path_line_message(tmp_path):
    root = _tracked_copy(tmp_path)
    (root / "skills/koroche-blyat/references/slovar.md").write_bytes(b"\xff\xfe not utf-8\n")
    (root / "skills/koroche-blyat/references/sceny.md").write_bytes(b"no final newline")
    violations = validate_repo(root)
    keys = [(v.check, v.path, v.line, v.message) for v in violations]
    assert keys == sorted(keys)
    assert all(not Path(v.path).is_absolute() for v in violations)
    assert all("\\" not in v.path for v in violations)


def test_selected_check_runs_only_that_check(tmp_path):
    root = _tracked_copy(tmp_path)
    (root / "skills/koroche-blyat/SKILL.md").write_bytes(b"---\nname: wrong\n---\n")
    only = validate_repo(root, ["encoding"])
    assert {violation.check for violation in only} <= {"encoding"}


# --- encoding ----------------------------------------------------------------

@pytest.mark.parametrize(
    "payload, fragment",
    [
        (b"\xef\xbb\xbf# title\n", "BOM"),
        (b"line\r\nsecond\n", "CRLF"),
        (b"no final newline", "final newline"),
        (b"nul\x00byte\n", "NUL"),
        (b"\xff\xfe\n", "UTF-8"),
    ],
)
def test_encoding_rejects_byte_level_defects(tmp_path, payload, fragment):
    root = _tracked_copy(tmp_path)
    (root / "skills/koroche-blyat/references/slovar.md").write_bytes(payload)
    violations = _checks(validate_repo(root, ["encoding"]), "encoding")
    assert violations, fragment
    assert any(fragment.casefold() in v.message.casefold() for v in violations)


# --- skill frontmatter -------------------------------------------------------

def test_skill_frontmatter_directory_name_must_equal_declared_name(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "skills/koroche-blyat/SKILL.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("name: koroche-blyat", "name: something-else", 1), encoding="utf-8")
    assert _checks(validate_repo(root, ["skill-frontmatter"]), "skill-frontmatter")


def test_skill_frontmatter_must_stay_below_the_size_limit(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "skills/koroche-blyat/SKILL.md"
    text = path.read_text(encoding="utf-8")
    padded = text.replace(
        "metadata:", "description_padding: %s\nmetadata:" % ("x" * 1100), 1
    )
    path.write_text(padded, encoding="utf-8")
    violations = _checks(validate_repo(root, ["skill-frontmatter"]), "skill-frontmatter")
    assert any("1024" in v.message for v in violations)


# --- links -------------------------------------------------------------------

def test_links_must_resolve_inside_the_skill_tree(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "skills/koroche-blyat/SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[gone](references/does-not-exist.md)\n",
        encoding="utf-8",
    )
    assert _checks(validate_repo(root, ["links"]), "links")


def test_links_must_not_escape_the_skill_tree(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "skills/koroche-blyat/SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[out](../../VERSION)\n", encoding="utf-8"
    )
    violations = _checks(validate_repo(root, ["links"]), "links")
    assert any("outside" in v.message.casefold() for v in violations)


# --- placeholders ------------------------------------------------------------

@pytest.mark.parametrize("token", ["TODO", "FIXME", "TBD", "XXX", "<placeholder>", "lorem ipsum"])
def test_placeholders_are_rejected_in_shipped_sources(tmp_path, token):
    root = _tracked_copy(tmp_path)
    path = root / "skills/koroche-blyat/references/ontologia.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n%s\n" % token, encoding="utf-8"
    )
    assert _checks(validate_repo(root, ["placeholders"]), "placeholders")


# --- generated parity --------------------------------------------------------

def test_generated_parity_detects_a_hand_edited_adapter(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "adapters/generated/reminder.txt"
    path.write_bytes(path.read_bytes() + b"tampered\n")
    assert _checks(validate_repo(root, ["generated-parity"]), "generated-parity")


def test_generated_parity_detects_a_changed_canonical_source(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "skills/koroche-blyat/SKILL.md"
    path.write_bytes(path.read_bytes() + "\nдописано после генерации\n".encode("utf-8"))
    assert _checks(validate_repo(root, ["generated-parity"]), "generated-parity")


# --- eval fixtures -----------------------------------------------------------

def test_eval_fixtures_detects_an_unknown_golden_reference(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "evals/cases/simple-safe.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "golden_id: g-simple-dns-cache", "golden_id: g-does-not-exist", 1
        ),
        encoding="utf-8",
    )
    assert _checks(validate_repo(root, ["eval-fixtures"]), "eval-fixtures")


def test_eval_fixtures_detects_a_duplicate_id_across_suites(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "evals/goldens/severity.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("g-release-severity-", "g-severity-"),
        encoding="utf-8",
    )
    assert _checks(validate_repo(root, ["eval-fixtures"]), "eval-fixtures")


# --- package allowlist -------------------------------------------------------

def test_package_detects_a_missing_allowlisted_path(tmp_path):
    root = _tracked_copy(tmp_path)
    (root / "adapters/generated/reminder.txt").unlink()
    assert _checks(validate_repo(root, ["package"]), "package")


def test_package_allowlist_must_be_sorted_and_globless(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "release/PACKAGE_FILES.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("skills/koroche-blyat/*.md\nLICENSE\n", encoding="utf-8")
    violations = _checks(validate_repo(root, ["package"]), "package")
    assert any("glob" in v.message.casefold() for v in violations)
    assert any("sort" in v.message.casefold() for v in violations)


# --- provenance --------------------------------------------------------------

def test_provenance_detects_redistributed_license_drift(tmp_path):
    root = _tracked_copy(tmp_path)
    path = root / "skills/koroche-blyat/licenses/caveman-MIT.txt"
    path.write_bytes(path.read_bytes() + b"extra\n")
    assert _checks(validate_repo(root, ["provenance"]), "provenance")


# --- docs claims -------------------------------------------------------------

def test_docs_claims_is_silent_while_task_14_documents_do_not_exist(tmp_path):
    """Task 13 must pass before Task 14 writes README.md.

    The plan runs `scripts.validate` at the end of Task 13 and expects PASS,
    but README.md and CHANGELOG.md are only created in Task 14. An absent
    document is therefore not a violation; an unsupported claim inside a
    present one is.
    """
    root = _tracked_copy(tmp_path)
    assert not (root / "README.md").exists()
    assert _checks(validate_repo(root, ["docs-claims"]), "docs-claims") == []


@pytest.mark.parametrize(
    "claim",
    [
        "Экономит 40% токенов.",
        "Perfect accuracy on every host.",
        "Guaranteed always-on behaviour.",
        "100% accurate compression.",
    ],
)
def test_docs_claims_rejects_unsupported_marketing_claims(tmp_path, claim):
    root = _tracked_copy(tmp_path)
    (root / "README.md").write_text("# koroche-blyat\n\n%s\n" % claim, encoding="utf-8")
    assert _checks(validate_repo(root, ["docs-claims"]), "docs-claims")


def test_docs_claims_accepts_the_explicit_no_percentage_statement(tmp_path):
    root = _tracked_copy(tmp_path)
    (root / "README.md").write_text(
        "# koroche-blyat\n\nNo fixed token-saving percentage is claimed for 1.0.0.\n",
        encoding="utf-8",
    )
    assert _checks(validate_repo(root, ["docs-claims"]), "docs-claims") == []


# --- CLI ---------------------------------------------------------------------

def test_cli_exit_zero_on_the_clean_repository():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.validate"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_exit_one_on_violations(tmp_path):
    root = _tracked_copy(tmp_path)
    (root / "skills/koroche-blyat/references/slovar.md").write_bytes(b"no newline")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.validate", "--root", str(root)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "encoding" in result.stdout


def test_cli_exit_two_on_unknown_check():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.validate", "--check", "no-such-check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 2


def test_cli_output_has_no_timestamp_and_is_stable(tmp_path):
    root = _tracked_copy(tmp_path)
    (root / "skills/koroche-blyat/references/slovar.md").write_bytes(b"no newline")
    runs = [
        subprocess.run(
            [sys.executable, "-m", "scripts.validate", "--root", str(root)],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout
        for _ in range(2)
    ]
    assert runs[0] == runs[1]
    assert "202" not in runs[0].replace("1024", "")


def test_violation_is_frozen():
    violation = Violation("encoding", "a.md", 1, "message")
    with pytest.raises(Exception):
        violation.line = 2  # type: ignore[misc]
