from __future__ import annotations

import hashlib
import importlib
import subprocess
import sys
import time
from dataclasses import is_dataclass
from pathlib import Path
from typing import Mapping, get_type_hints

import pytest

ROOT = Path(__file__).parents[1]
BEGIN = b"<!-- ALWAYS_ON_CORE:BEGIN -->"
REMINDER_BEGIN = b"<!-- ALWAYS_ON_REMINDER:BEGIN -->"
REMINDER_END = b"<!-- ALWAYS_ON_REMINDER:END -->"
END = b"<!-- ALWAYS_ON_CORE:END -->"
MARKERS = (BEGIN, REMINDER_BEGIN, REMINDER_END, END)
REMINDER = "Контракт koroche-blyat остаётся активен: соблюдай приоритеты, защищённые фрагменты, Auto-Clarity, чистые артефакты и краткий естественный русский инженерный тон."
OUTPUT_KEYS = {
    "adapters/generated/always-on.md",
    "adapters/generated/reminder.txt",
    "adapters/generated/claude-output-style.md",
}
OUTPUT_NAMES = {Path(key).name for key in OUTPUT_KEYS}
CLAUDE_HEADER = (
    "---\n"
    "name: koroche-blyat\n"
    "description: Краткий русский инженерный стиль с точной технической передачей и чистыми артефактами\n"
    "keep-coding-instructions: true\n"
    "---\n"
).encode("utf-8")


def _api():
    module = importlib.import_module("scripts.generate_adapters")
    assert is_dataclass(module.CanonicalPolicy)
    assert is_dataclass(module.GeneratedAdapters)
    return module


def _source(
    before: bytes = "До café и A\u00a0B.\n".encode("utf-8"),
    reminder: bytes = REMINDER.encode("utf-8"),
    after: bytes = b"After --no-cache.\n",
) -> bytes:
    return (
        BEGIN + b"\n" + before
        + REMINDER_BEGIN + b"\n" + reminder + b"\n" + REMINDER_END + b"\n"
        + after + END + b"\n"
    )


def _expected_core(before: bytes, reminder: bytes, after: bytes) -> str:
    return (before + reminder + b"\n" + after).decode("utf-8")


def _parse(source: bytes):
    return _api().parse_canonical_source(source)


def _run_cli(source: Path, output: Path, check: bool = False) -> subprocess.CompletedProcess:
    command = [
        sys.executable, "-m", "scripts.generate_adapters",
        "--source", str(source), "--output-dir", str(output),
    ]
    if check:
        command.append("--check")
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True)


def test_canonical_interfaces_are_frozen_dataclasses():
    api = _api()
    assert api.CanonicalPolicy.__dataclass_params__.frozen
    assert api.GeneratedAdapters.__dataclass_params__.frozen
    assert tuple(api.CanonicalPolicy.__dataclass_fields__) == ("source_sha256", "core", "reminder")
    assert tuple(api.GeneratedAdapters.__dataclass_fields__) == ("source_sha256", "files")
    assert get_type_hints(api.CanonicalPolicy) == {
        "source_sha256": str, "core": str, "reminder": str,
    }
    assert get_type_hints(api.GeneratedAdapters) == {
        "source_sha256": str, "files": Mapping[str, bytes],
    }


@pytest.mark.parametrize("kind", ("bom", "crlf", "invalid-utf8"))
def test_parse_rejects_bom_crlf_and_invalid_utf8(kind):
    source = _source()
    if kind == "bom":
        source = b"\xef\xbb\xbf" + source
    elif kind == "crlf":
        source = source.replace(b"\n", b"\r\n")
    else:
        source = b"\xff" + source
    with pytest.raises((ValueError, UnicodeError)):
        _parse(source)


@pytest.mark.parametrize("marker", MARKERS)
def test_parse_rejects_each_missing_marker(marker):
    with pytest.raises(ValueError):
        _parse(_source().replace(marker, b"", 1))


@pytest.mark.parametrize("marker", MARKERS)
def test_parse_rejects_each_duplicate_marker(marker):
    with pytest.raises(ValueError):
        _parse(_source() + marker + b"\n")


@pytest.mark.parametrize("source", (
    END + b"\n" + _source().replace(END, b"", 1),
    REMINDER_END + b"\n" + _source().replace(REMINDER_END, b"", 1),
    REMINDER_BEGIN + b"\n" + REMINDER.encode("utf-8") + b"\n" + REMINDER_END + b"\n" + BEGIN + b"\nCORE\n" + END + b"\n",
))
def test_parse_rejects_reversed_or_non_nested_markers(source):
    with pytest.raises(ValueError):
        _parse(source)


def test_parse_rejects_multiline_reminder():
    with pytest.raises(ValueError):
        _parse(_source(reminder="первая строка\nвторая строка".encode("utf-8")))


@pytest.mark.parametrize("reminder", (b"", b" ", b"\t"))
def test_parse_rejects_empty_or_whitespace_only_reminder(reminder):
    with pytest.raises(ValueError):
        _parse(_source(reminder=reminder))


def test_parse_preserves_outer_core_and_reminder_without_normalization():
    before = "До café и A\u00a0B.\n\n".encode("utf-8")
    reminder = "напоминание dev\u200dops".encode("utf-8")
    after = "После cafe\u0301.\n".encode("utf-8")
    source = _source(before=before, reminder=reminder, after=after)
    policy = _parse(source)
    assert policy.source_sha256 == hashlib.sha256(source).hexdigest()
    assert policy.core == _expected_core(before, reminder, after)
    assert policy.reminder == reminder.decode("utf-8")


def test_generate_has_exact_outputs_hashes_and_byte_exact_payloads():
    api = _api()
    before = b"CORE before.\n"
    reminder = REMINDER.encode("utf-8")
    after = b"CORE after --no-cache.\n"
    source = _source(before=before, reminder=reminder, after=after)
    core = before + reminder + b"\n" + after
    generated = api.generate_from_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    assert generated.source_sha256 == digest
    assert set(generated.files) == OUTPUT_KEYS
    for data in generated.files.values():
        assert ("canonical-sha256: " + digest).encode("ascii") in data
        assert all(marker not in data for marker in MARKERS)
        assert b"timestamp" not in data.lower()
    always_on = generated.files["adapters/generated/always-on.md"]
    reminder_file = generated.files["adapters/generated/reminder.txt"]
    claude = generated.files["adapters/generated/claude-output-style.md"]
    metadata = ("canonical-sha256: %s\n\n" % digest).encode("ascii")
    assert always_on == metadata + core
    assert reminder_file == metadata + REMINDER.encode("utf-8") + b"\n"
    assert claude == CLAUDE_HEADER + metadata + core


def test_cli_check_is_read_only_for_missing_matching_and_stale_outputs(tmp_path):
    source = tmp_path / "SKILL.md"
    source.write_bytes(_source())
    output = tmp_path / "generated"

    missing = _run_cli(source, output, check=True)
    assert missing.returncode == 1
    assert not output.exists()

    generated = _run_cli(source, output)
    assert generated.returncode == 0, generated.stderr
    paths = sorted(path for path in output.iterdir() if path.is_file())
    assert {path.name for path in paths} == OUTPUT_NAMES
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    matching = _run_cli(source, output, check=True)
    assert matching.returncode == 0, matching.stderr
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    stale_path = output / "always-on.md"
    stale_path.write_bytes(b"stale\n")
    stale_before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    stale = _run_cli(source, output, check=True)
    assert stale.returncode == 1
    assert stale_before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def test_writer_is_idempotent_and_replaces_only_changed_files(tmp_path):
    source = tmp_path / "SKILL.md"
    source.write_bytes(_source())
    output = tmp_path / "generated"
    first = _run_cli(source, output)
    assert first.returncode == 0, first.stderr
    paths = sorted(path for path in output.iterdir() if path.is_file())
    assert {path.name for path in paths} == OUTPUT_NAMES
    initial = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    time.sleep(0.01)
    second = _run_cli(source, output)
    assert second.returncode == 0, second.stderr
    assert initial == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    changed = output / "reminder.txt"
    changed.write_bytes(b"changed\n")
    unchanged = {path: path.stat().st_mtime_ns for path in paths if path != changed}
    time.sleep(0.01)
    repaired = _run_cli(source, output)
    assert repaired.returncode == 0, repaired.stderr
    assert changed.read_bytes() == initial[changed][0]
    assert unchanged == {path: path.stat().st_mtime_ns for path in paths if path != changed}



def test_writer_replaces_stale_file_symlink_without_touching_its_target(tmp_path):
    source = tmp_path / "SKILL.md"
    source.write_bytes(_source())
    output = tmp_path / "generated"
    first = _run_cli(source, output)
    assert first.returncode == 0, first.stderr
    expected = (output / "reminder.txt").read_bytes()

    external = tmp_path / "external.txt"
    external.write_bytes(b"external sentinel\n")
    (output / "reminder.txt").unlink()
    (output / "reminder.txt").symlink_to(external)

    repaired = _run_cli(source, output)
    assert repaired.returncode == 0, repaired.stderr
    assert external.read_bytes() == b"external sentinel\n"
    assert not (output / "reminder.txt").is_symlink()
    assert (output / "reminder.txt").read_bytes() == expected
