from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    "codex": ROOT / "adapters" / "codex" / "user-prompt-reminder.sh",
    "claude": ROOT / "adapters" / "claude" / "user-prompt-reminder.sh",
}
GENERATED_REMINDER = ROOT / "adapters" / "generated" / "reminder.txt"
SECRET = b'SECRET_SHOULD_NOT_LEAK "quoted"\nsecond-line\x00tail'


def _payload(raw: bytes) -> bytes:
    metadata, separator, body = raw.partition(b"\n\n")
    assert separator == b"\n\n"
    assert metadata.startswith(b"canonical-sha256: ")
    assert body.endswith(b"\n")
    assert b"\n" not in body[:-1]
    return body


def _run(script: Path, stdin: bytes = SECRET) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["/bin/sh", str(script)],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, "CDPATH": "/definitely/not/used"},
        timeout=10,
    )


def _installed_copy(tmp_path: Path, adapter: str) -> Path:
    directory = tmp_path / "installed path with spaces" / adapter
    directory.mkdir(parents=True)
    target = directory / "user-prompt-reminder.sh"
    shutil.copyfile(ADAPTERS[adapter], target)
    target.chmod(0o755)
    return target


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_hook_never_reflects_prompt_or_secret(adapter: str) -> None:
    result = _run(ADAPTERS[adapter])
    expected = _payload(GENERATED_REMINDER.read_bytes())
    assert result.returncode == 0
    assert result.stdout == expected
    assert result.stderr == b""
    assert b"SECRET_SHOULD_NOT_LEAK" not in result.stdout + result.stderr
    assert b"hook_event_name" not in result.stdout + result.stderr


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_repo_and_installed_paths_emit_exact_reminder(adapter: str, tmp_path: Path) -> None:
    expected = _payload(GENERATED_REMINDER.read_bytes())
    repo_result = _run(ADAPTERS[adapter], b'{"hook_event_name":"UserPromptSubmit"}')
    assert (repo_result.returncode, repo_result.stdout, repo_result.stderr) == (0, expected, b"")

    installed = _installed_copy(tmp_path, adapter)
    sibling = installed.parent / "reminder.txt"
    sibling.write_bytes(b"canonical-sha256: " + b"a" * 64 + b"\n\nInstalled reminder only.\n")
    installed_result = _run(installed)
    assert (installed_result.returncode, installed_result.stdout, installed_result.stderr) == (
        0,
        b"Installed reminder only.\n",
        b"",
    )


def test_hooks_are_byte_identical_and_executable() -> None:
    codex = ADAPTERS["codex"]
    claude = ADAPTERS["claude"]
    assert codex.read_bytes() == claude.read_bytes()
    for script in (codex, claude):
        raw = script.read_bytes()
        assert raw.startswith(b"#!/bin/sh\n")
        assert b"\r" not in raw
        assert stat.S_IMODE(script.stat().st_mode) == 0o755
        direct = subprocess.run(
            [str(script)], input=b"{}", capture_output=True, check=False, timeout=10
        )
        assert (direct.returncode, direct.stdout, direct.stderr) == (
            0, _payload(GENERATED_REMINDER.read_bytes()), b""
        )


@pytest.mark.parametrize("adapter", ["codex", "claude"])
@pytest.mark.parametrize(
    "kind",
    ["missing", "metadata-only", "multiline", "whitespace", "bad-header", "short-hash", "no-delimiter"],
)
def test_missing_or_corrupt_reminder_is_nonblocking_and_diagnostic(
    adapter: str, kind: str, tmp_path: Path
) -> None:
    script = _installed_copy(tmp_path, adapter)
    reminder = script.parent / "reminder.txt"
    if kind == "metadata-only":
        reminder.write_bytes(b"canonical-sha256: " + b"b" * 64 + b"\n\n")
    elif kind == "multiline":
        reminder.write_bytes(b"canonical-sha256: " + b"b" * 64 + b"\n\none\ntwo\n")
    elif kind == "whitespace":
        reminder.write_bytes(b"canonical-sha256: " + b"b" * 64 + b"\n\n padded \n")
    elif kind == "bad-header":
        reminder.write_bytes(b"sha256: " + b"b" * 64 + b"\n\npayload\n")
    elif kind == "short-hash":
        reminder.write_bytes(b"canonical-sha256: abc\n\npayload\n")
    elif kind == "no-delimiter":
        reminder.write_bytes(b"canonical-sha256: " + b"b" * 64 + b"\npayload\n")

    result = _run(script)
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr
    assert b"SECRET_SHOULD_NOT_LEAK" not in result.stderr
    assert str(tmp_path).encode() not in result.stderr


def test_source_scripts_do_not_embed_policy_or_reminder_literals() -> None:
    reminder = _payload(GENERATED_REMINDER.read_bytes()).rstrip(b"\n")
    for script in ADAPTERS.values():
        raw = script.read_bytes()
        assert reminder not in raw
        assert b"ALWAYS_ON_CORE" not in raw
        assert b"KOROCHE_BLYAT_UNATTENDED" not in raw
        assert b"SECRET_SHOULD_NOT_LEAK" not in raw
        assert b"jq" not in raw
        assert b"python" not in raw.lower()
        assert b"matcher" not in raw
        assert b"statusMessage" not in raw
        assert raw.count(b"cat >/dev/null || :") == 1
        assert raw.index(b"cat >/dev/null || :") < raw.index(b"dirname --")
        for forbidden in (b"grep ", b"awk ", b"curl ", b"wget ", b"tee ", b"mktemp "):
            assert forbidden not in raw


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_hook_uses_trusted_tools_and_never_persists_stdin(
    adapter: str, tmp_path: Path
) -> None:
    hostile = tmp_path / "hostile-bin"
    hostile.mkdir()
    captured = tmp_path / "persisted-secret"
    fake_cat = hostile / "cat"
    fake_cat.write_text(
        "#!/bin/sh\n/bin/cat >" + shlex.quote(str(captured)) + "\n",
        encoding="utf-8",
    )
    fake_cat.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", str(ADAPTERS[adapter])],
        input=SECRET,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={
            **os.environ,
            "PATH": str(hostile),
            "BASH_FUNC_cat%%": "() { /bin/cat > " + shlex.quote(str(captured)) + "; }",
            "BASH_FUNC_printf%%": "() { /bin/echo FUNCTION_HIJACK; }",
            "BASH_FUNC_[%%": "() { return 1; }",
            "BASH_FUNC_test%%": "() { return 1; }",
        },
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == _payload(GENERATED_REMINDER.read_bytes())
    assert result.stderr == b""
    assert not captured.exists()


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_sibling_wins_when_fallback_is_also_present(adapter: str, tmp_path: Path) -> None:
    script_dir = tmp_path / adapter
    generated_dir = tmp_path / "generated"
    script_dir.mkdir()
    generated_dir.mkdir()
    script = script_dir / "user-prompt-reminder.sh"
    shutil.copyfile(ADAPTERS[adapter], script)
    script.chmod(0o755)
    (script_dir / "reminder.txt").write_bytes(
        b"canonical-sha256: " + b"a" * 64 + b"\n\nSibling wins.\n"
    )
    (generated_dir / "reminder.txt").write_bytes(
        b"canonical-sha256: " + b"b" * 64 + b"\n\nFallback loses.\n"
    )
    result = _run(script)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"Sibling wins.\n", b"")
