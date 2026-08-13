from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Dict, Tuple

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOST_VERSIONS = {"prime-agent": "0.7.2", "codex": "0.147.0", "claude": "2.1.197"}


def _binary(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nprintf '%s\\n' '" + version + "'\n", encoding="utf-8")
    path.chmod(0o755)


def _env(tmp_path: Path, *, hosts=tuple(HOST_VERSIONS)) -> Tuple[Path, Dict[str, str]]:
    home = tmp_path / "Repo Home With Spaces"
    home.mkdir(parents=True)
    binary_dir = tmp_path / "Host Binaries With Spaces"
    for name in hosts:
        _binary(binary_dir / name, HOST_VERSIONS[name])
    env = {
        "HOME": str(home), "PATH": str(binary_dir) + os.pathsep + "/usr/bin:/bin",
        "PYTHON": sys.executable, "PYTHONUTF8": "1",
        "PRIME_AGENT_CODING_AGENT_DIR": str(home / "Prime Config With Spaces"),
        "CODEX_HOME": str(home / "Codex Config With Spaces"),
        "CLAUDE_CONFIG_DIR": str(home / "Claude Config With Spaces"),
        "XDG_STATE_HOME": str(home / "Private State With Spaces"),
    }
    return home, env


def _run(env: Dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ROOT / "install.sh"), *args], env=env,
        capture_output=True, text=True, check=False,
    )


def _tree(root: Path) -> Tuple[tuple, ...]:
    result = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            result.append((relative, "symlink", os.readlink(path), None))
        elif stat.S_ISDIR(mode):
            result.append((relative, "directory", None, stat.S_IMODE(mode)))
        else:
            result.append((
                relative, "file", hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.S_IMODE(mode),
            ))
    return tuple(result)


def test_dry_run_writes_no_file_or_directory(tmp_path: Path) -> None:
    home, env = _env(tmp_path, hosts=("prime-agent",))
    before = _tree(home)
    result = _run(env, "--dry-run", "--prime")
    assert result.returncode == 0, result.stderr
    assert _tree(home) == before


def test_double_install_is_byte_and_metadata_noop(tmp_path: Path) -> None:
    home, env = _env(tmp_path)
    first = _run(env, "--all")
    assert first.returncode == 0, first.stderr
    before = _tree(home)
    second = _run(env, "--all")
    assert second.returncode == 0, second.stderr
    assert _tree(home) == before


def test_full_install_uninstall_restores_exact_tree(tmp_path: Path) -> None:
    home, env = _env(tmp_path)
    initial = _tree(home)
    installed = _run(env, "--all")
    assert installed.returncode == 0, installed.stderr
    removed = _run(env, "--uninstall", "--all")
    assert removed.returncode == 0, removed.stderr
    assert _tree(home) == initial


@pytest.mark.parametrize(
    "first,second,removed,kept_path",
    [
        ("--prime", "--codex", "--prime", ".agents/skills/koroche-blyat/SKILL.md"),
        ("--codex", "--prime", "--codex", ".agents/skills/koroche-blyat/SKILL.md"),
    ],
)
def test_partial_shared_owner_uninstall_keeps_shared_skill(
    tmp_path: Path, first: str, second: str, removed: str, kept_path: str
) -> None:
    home, env = _env(tmp_path)
    assert _run(env, first).returncode == 0
    assert _run(env, second).returncode == 0
    result = _run(env, "--uninstall", removed)
    assert result.returncode == 0, result.stderr
    assert (home / kept_path).is_file()


def test_previous_claude_output_style_restores_exactly(tmp_path: Path) -> None:
    home, env = _env(tmp_path)
    settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.parent.mkdir(parents=True)
    original = b'{\r\n  "outputStyle" : "\\u006f\\u006c\\u0064",\r\n  "keep": 1\r\n}'
    settings.write_bytes(original)
    settings.chmod(0o640)
    assert _run(env, "--claude").returncode == 0
    removed = _run(env, "--uninstall", "--claude")
    assert removed.returncode == 0, removed.stderr
    assert settings.read_bytes() == original
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640


def test_user_changed_output_style_conflicts_and_force_preserves_value(tmp_path: Path) -> None:
    home, env = _env(tmp_path)
    settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    assert _run(env, "--claude").returncode == 0
    document = json.loads(settings.read_text(encoding="utf-8"))
    document["outputStyle"] = {"user": "choice"}
    settings.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before = settings.read_bytes()
    conflict = _run(env, "--uninstall", "--claude")
    assert conflict.returncode == 2
    assert settings.read_bytes() == before
    forced = _run(env, "--uninstall", "--force", "--claude")
    assert forced.returncode == 0, forced.stderr
    assert json.loads(settings.read_text(encoding="utf-8"))["outputStyle"] == {"user": "choice"}


def test_edited_owned_file_block_and_hook_require_force(tmp_path: Path) -> None:
    home, env = _env(tmp_path)
    assert _run(env, "--all").returncode == 0
    owned = home / ".agents/skills/koroche-blyat/SKILL.md"
    owned.write_bytes(owned.read_bytes() + b"user edit\n")
    conflict = _run(env, "--uninstall", "--all")
    assert conflict.returncode == 2
    assert owned.is_file()
    forced = _run(env, "--uninstall", "--force", "--all")
    assert forced.returncode == 0, forced.stderr
    assert not owned.exists()


def test_unrelated_post_install_edits_and_unknown_managed_child_survive(tmp_path: Path) -> None:
    home, env = _env(tmp_path)
    codex = Path(env["CODEX_HOME"])
    codex.mkdir(parents=True)
    hooks = codex / "hooks.json"
    hooks.write_bytes(b'{"before":"kept"}\n')
    assert _run(env, "--codex").returncode == 0
    document = json.loads(hooks.read_text(encoding="utf-8"))
    document["after"] = "user edit"
    hooks.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    unknown = home / ".agents/skills/koroche-blyat/user.md"
    unknown.write_bytes(b"survive byte for byte\n")
    result = _run(env, "--uninstall", "--codex")
    assert result.returncode == 0, result.stderr
    remaining = json.loads(hooks.read_text(encoding="utf-8"))
    assert remaining == {"before": "kept", "after": "user edit"}
    assert unknown.read_bytes() == b"survive byte for byte\n"


def test_uninstall_works_after_host_binary_is_removed(tmp_path: Path) -> None:
    home, env = _env(tmp_path, hosts=("prime-agent",))
    assert _run(env, "--prime").returncode == 0
    Path(env["PATH"].split(os.pathsep)[0], "prime-agent").unlink()
    result = _run(env, "--uninstall", "--prime")
    assert result.returncode == 0, result.stderr


def test_missing_manifest_uninstall_is_noop(tmp_path: Path) -> None:
    home, env = _env(tmp_path, hosts=())
    before = _tree(home)
    result = _run(env, "--uninstall", "--all")
    assert result.returncode == 0, result.stderr
    assert _tree(home) == before



def test_two_processes_share_one_home_lock(tmp_path: Path) -> None:
    home, env = _env(tmp_path, hosts=("prime-agent",))
    holder_code = (
        "from pathlib import Path\n"
        "from scripts.installer.transaction import REAL_FS\n"
        "import sys\n"
        "fd=REAL_FS.open_lock(Path(sys.argv[1])); print('locked', flush=True)\n"
        "sys.stdin.readline(); REAL_FS.close_lock(fd)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(home)], cwd=str(ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        contender = subprocess.Popen(
            [str(ROOT / "install.sh"), "--prime"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        import time
        time.sleep(0.15)
        assert contender.poll() is None
        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        output, error = contender.communicate(timeout=20)
        assert contender.returncode == 0, error + output
    finally:
        if holder.stdin is not None and not holder.stdin.closed:
            holder.stdin.close()
        holder.wait(timeout=10)



def test_real_codex_install_reports_required_manual_trust_action(tmp_path: Path) -> None:
    home, env = _env(tmp_path, hosts=("codex",))
    result = _run(env, "--codex")
    assert result.returncode == 0
    assert "Run /hooks and trust the koroche-blyat UserPromptSubmit hook" in result.stderr
