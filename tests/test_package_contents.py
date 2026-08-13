"""Fail-closed release packaging — Task 13 Steps 5-6.

The allowlist is explicit and globless, so a new shipped file is a deliberate
edit rather than something a wildcard sweeps in silently. Reproducibility is
asserted by building twice under one SOURCE_DATE_EPOCH and comparing bytes.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.package_release import (
    EXECUTABLE_FILES,
    build_release,
    load_allowlist,
)

ROOT = Path(__file__).parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
PREFIX = "koroche-blyat-%s" % VERSION
EPOCH = "1786500000"
FORBIDDEN_SEGMENTS = {
    "engine", "proxy", "mcp", "shrink", "browse", "mem", "cacheengine", "platform",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    output = tmp_path_factory.mktemp("dist")
    return build_release(ROOT, VERSION, output, epoch=int(EPOCH))


# --- allowlist ---------------------------------------------------------------

def test_allowlist_is_sorted_explicit_and_globless():
    entries = load_allowlist(ROOT)
    assert entries == tuple(sorted(entries))
    assert len(entries) == len(set(entries))
    for entry in entries:
        assert not any(character in entry for character in "*?[]"), entry
        assert not entry.startswith("/"), entry
        assert ".." not in Path(entry).parts, entry


def test_every_allowlisted_path_exists_and_is_a_regular_file():
    for entry in load_allowlist(ROOT):
        path = ROOT / entry
        assert path.is_file(), entry
        assert not path.is_symlink(), entry


def test_allowlist_has_no_casefold_collisions():
    entries = load_allowlist(ROOT)
    folded = [entry.casefold() for entry in entries]
    assert len(folded) == len(set(folded))


def test_allowlist_excludes_caches_media_and_developer_only_paths():
    for entry in load_allowlist(ROOT):
        parts = Path(entry).parts
        assert not set(parts) & FORBIDDEN_SEGMENTS, entry
        assert ".DS_Store" not in parts, entry
        assert "__pycache__" not in parts, entry
        assert parts[0] not in {"tests", "evals", "docs", ".github"}, entry
        assert Path(entry).suffix not in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}, entry


def test_shipped_sources_are_utf8_without_crlf():
    for entry in load_allowlist(ROOT):
        raw = (ROOT / entry).read_bytes()
        raw.decode("utf-8")
        assert b"\r\n" not in raw, entry


def test_missing_allowlist_entry_fails_the_build(tmp_path):
    listing = ROOT / "release/PACKAGE_FILES.txt"
    staging = tmp_path / "repo"
    staging.mkdir()
    (staging / "release").mkdir()
    (staging / "release/PACKAGE_FILES.txt").write_text(
        listing.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(FileNotFoundError):
        build_release(staging, VERSION, tmp_path / "dist", epoch=int(EPOCH))


# --- archive contents --------------------------------------------------------

def test_both_archives_contain_exactly_the_allowlist(built):
    expected = {"%s/%s" % (PREFIX, entry) for entry in load_allowlist(ROOT)}
    with tarfile.open(built["tar"], "r:gz") as archive:
        tar_names = {member.name for member in archive.getmembers() if member.isfile()}
    with zipfile.ZipFile(built["zip"]) as archive:
        zip_names = set(archive.namelist())
    assert tar_names == expected
    assert zip_names == expected


def test_archive_members_carry_zero_uid_gid_and_expected_modes(built):
    with tarfile.open(built["tar"], "r:gz") as archive:
        for member in archive.getmembers():
            assert member.uid == 0 and member.gid == 0, member.name
            assert member.uname == "" and member.gname == "", member.name
            assert not member.issym() and not member.isdev(), member.name
            relative = member.name[len(PREFIX) + 1:]
            expected = 0o755 if relative in EXECUTABLE_FILES else 0o644
            assert stat.S_IMODE(member.mode) == expected, (member.name, oct(member.mode))


def test_unpacked_bytes_equal_the_source_bytes(built, tmp_path):
    target = tmp_path / "unpacked"
    with tarfile.open(built["tar"], "r:gz") as archive:
        archive.extractall(target)
    for entry in load_allowlist(ROOT):
        assert (target / PREFIX / entry).read_bytes() == (ROOT / entry).read_bytes(), entry


def test_zip_and_tar_agree_byte_for_byte_on_every_member(built):
    with zipfile.ZipFile(built["zip"]) as archive:
        for entry in load_allowlist(ROOT):
            assert archive.read("%s/%s" % (PREFIX, entry)) == (ROOT / entry).read_bytes(), entry


# --- reproducibility ---------------------------------------------------------

def test_two_builds_under_one_epoch_are_byte_identical(tmp_path):
    first = build_release(ROOT, VERSION, tmp_path / "one", epoch=int(EPOCH))
    second = build_release(ROOT, VERSION, tmp_path / "two", epoch=int(EPOCH))
    for key in ("tar", "zip", "sums"):
        assert first[key].read_bytes() == second[key].read_bytes(), key


def test_sha256sums_is_sorted_lowercase_and_matches_the_archives(built):
    lines = built["sums"].read_text(encoding="utf-8").splitlines()
    names = [line.split("  ", 1)[1] for line in lines]
    assert names == sorted(names)
    for line in lines:
        digest, name = line.split("  ", 1)
        assert digest == digest.lower()
        assert len(digest) == 64
        actual = hashlib.sha256((built["tar"].parent / name).read_bytes()).hexdigest()
        assert digest == actual, name


# --- CLI ---------------------------------------------------------------------

def test_cli_builds_into_the_requested_directory(tmp_path):
    environment = dict(os.environ, SOURCE_DATE_EPOCH=EPOCH)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.package_release",
         "--version", VERSION, "--output-dir", str(tmp_path / "dist")],
        cwd=ROOT, capture_output=True, text=True, env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "dist" / ("%s.tar.gz" % PREFIX)).is_file()
    assert (tmp_path / "dist" / ("%s.zip" % PREFIX)).is_file()
    assert (tmp_path / "dist" / "SHA256SUMS").is_file()


def test_cli_rejects_a_version_that_disagrees_with_the_version_file(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.package_release",
         "--version", "9.9.9", "--output-dir", str(tmp_path / "dist")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 2
