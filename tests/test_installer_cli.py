from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOSTS = ("prime", "codex", "claude")


def _fake_binary(directory: Path, name: str, version: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text("#!/bin/sh\nprintf '%s\n' " + version + "\n", encoding="utf-8")
    binary.chmod(0o755)


def _parse(*args: str):
    from scripts.install import parse_args

    return parse_args(list(args))


def test_no_host_flag_uses_ordered_defaults() -> None:
    options = _parse()
    assert options.requested_hosts == HOSTS
    assert options.action == "install"


def test_all_plus_an_individual_host_is_invalid() -> None:
    with pytest.raises(SystemExit) as exc:
        _parse("--all", "--prime")
    assert exc.value.code == 2


def test_force_is_valid_only_with_uninstall() -> None:
    with pytest.raises(SystemExit) as exc:
        _parse("--force", "--prime")
    assert exc.value.code == 2
    assert _parse("--uninstall", "--force", "--prime").force is True


def test_dry_run_uninstall_prime_is_valid() -> None:
    options = _parse("--dry-run", "--uninstall", "--prime")
    assert options.action == "uninstall"
    assert options.requested_hosts == ("prime",)
    assert options.dry_run is True


@pytest.mark.parametrize(
    "argument", ["--wat", "--uninst", "--dry", "--pri", "--code", "--clau", "--a"]
)
def test_unknown_or_abbreviated_arguments_fail_with_exit_2(argument: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(argument)
    assert exc.value.code == 2


def test_uninstall_does_not_require_installed_host_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    options = _parse("--dry-run", "--uninstall", "--all")
    assert options.requested_hosts == HOSTS


def test_dry_run_json_is_deterministic_redacted_and_writes_no_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "Home With Spaces"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    _fake_binary(fake_bin, "prime-agent", "0.7.2")
    env = {
        "HOME": str(home),
        "PATH": str(fake_bin) + os.pathsep + "/usr/bin:/bin",
        "PYTHONUTF8": "1",
        "XDG_STATE_HOME": str(home / "state must not exist"),
        "SECRET_SHOULD_NOT_LEAK": "top-secret",
    }
    command = [sys.executable, str(ROOT / "scripts" / "install.py"), "--dry-run", "--prime"]
    first = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    second = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    document = json.loads(first.stdout)
    assert tuple(document) == (
        "action", "requested_hosts", "effective_hosts", "release", "operations", "manual_actions"
    )
    assert document["action"] == "install"
    assert document["requested_hosts"] == document["effective_hosts"] == ["prime"]
    assert document["release"] == "1.0.0"
    assert document["operations"]
    assert document["manual_actions"] == []
    assert all(set(item) == {"id", "kind", "path", "change"} for item in document["operations"])
    assert "top-secret" not in first.stdout
    assert not (home / "state must not exist").exists()


def test_install_sh_works_when_checkout_path_contains_spaces(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout path with spaces"
    (checkout / "scripts" / "installer").mkdir(parents=True)
    for relative in (
        "install.sh",
        "VERSION",
        "scripts/install.py",
        "scripts/installer/__init__.py",
        "scripts/installer/model.py",
        "scripts/installer/patch_text.py",
        "scripts/installer/patch_json.py",
        "scripts/installer/hosts.py",
        "scripts/installer/manifest.py",
        "scripts/installer/plan.py",
        "scripts/installer/sources.py",
        "scripts/installer/journal.py",
        "scripts/installer/transaction.py",
    ):
        source = ROOT / relative
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copytree(ROOT / "skills", checkout / "skills")
    shutil.copytree(ROOT / "adapters", checkout / "adapters")
    (checkout / "install.sh").chmod(0o755)
    fake_bin = tmp_path / "space-fake-bin"
    _fake_binary(fake_bin, "claude", "2.1.197")
    result = subprocess.run(
        [str(checkout / "install.sh"), "--dry-run", "--claude"],
        env={
            "HOME": str(tmp_path),
            "PATH": str(fake_bin) + os.pathsep + "/usr/bin:/bin",
            "PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["requested_hosts"] == ["claude"]


def test_launcher_content_is_exact_and_executable() -> None:
    expected = br"""#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PYTHON=${PYTHON:-python3}
exec "$PYTHON" -E -B "$ROOT/scripts/install.py" "$@"
"""
    launcher = ROOT / "install.sh"
    assert launcher.read_bytes() == expected
    assert launcher.stat().st_mode & 0o100


def test_public_model_layer_exposes_frozen_dataclasses() -> None:
    from dataclasses import FrozenInstanceError, is_dataclass
    from scripts.installer import (
        Action, FileMutation, Host, InstallPlan, LogicalChange, Options,
        OwnedResource, OwnershipManifest, ResourceKind, Snapshot,
    )

    for model in (
        Action, FileMutation, Host, InstallPlan, LogicalChange, Options,
        OwnedResource, OwnershipManifest, ResourceKind, Snapshot,
    ):
        assert is_dataclass(model)
        assert model.__dataclass_params__.frozen is True
    options = _parse("--prime")
    with pytest.raises(FrozenInstanceError):
        options.action = "uninstall"


def test_launcher_dry_run_does_not_write_any_home_entry(tmp_path: Path) -> None:
    home = tmp_path / "Empty Home"
    home.mkdir()
    fake_bin = tmp_path / "zero-write-fake-bin"
    _fake_binary(fake_bin, "prime-agent", "0.7.2")
    before = list(home.rglob("*"))
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--dry-run", "--prime"],
        env={
            "HOME": str(home), "PATH": str(fake_bin) + os.pathsep + "/usr/bin:/bin", "PYTHON": sys.executable,
            "XDG_STATE_HOME": str(home / "state"), "PYTHONUTF8": "1",
            "PYTHONWARNINGS": "WARN_SECRET_SHOULD_NOT_LEAK",
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert "WARN_SECRET_SHOULD_NOT_LEAK" not in result.stdout + result.stderr
    assert list(home.rglob("*")) == before == []


def test_unknown_argument_diagnostic_never_reflects_secret() -> None:
    secret = "--credential=ARGV_SECRET_SHOULD_NOT_LEAK"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/install.py"), "--dry-run", secret],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "ARGV_SECRET_SHOULD_NOT_LEAK" not in result.stdout + result.stderr


def test_dry_run_never_emits_existing_config_or_policy_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "AGENTS.md").write_text("OLD_CONFIG_SECRET", encoding="utf-8")
    (codex / "hooks.json").write_text(
        '{"unrelated":"OLD_CONFIG_SECRET"}\n', encoding="utf-8"
    )
    fake_bin = tmp_path / "fake-bin-redaction"
    _fake_binary(fake_bin, "codex", "0.147.0")
    before = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*") if path.is_file()
    }
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--dry-run", "--codex"],
        env={
            "HOME": str(home),
            "PATH": str(fake_bin) + os.pathsep + "/usr/bin:/bin",
            "PYTHONUTF8": "1",
            "PYTHON": sys.executable,
        }, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OLD_CONFIG_SECRET" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["operations"]
    after = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*") if path.is_file()
    }
    assert after == before


def test_preflight_error_redacts_home_path(tmp_path: Path) -> None:
    home = tmp_path / "SECRET_HOME_COMPONENT"
    target = home / ".prime/agent/extensions/koroche-blyat/index.ts"
    target.parent.mkdir(parents=True)
    target.symlink_to(home / "elsewhere")
    fake_bin = tmp_path / "fake-bin-error"
    _fake_binary(fake_bin, "prime-agent", "0.7.2")
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--dry-run", "--prime"],
        env={
            "HOME": str(home), "PYTHON": sys.executable,
            "PATH": str(fake_bin) + os.pathsep + "/usr/bin:/bin",
        }, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert str(home) not in result.stderr
    assert "$HOME" in result.stderr



def test_runtime_transaction_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.install as installer
    from scripts.installer.transaction import RollbackFailure, TransactionFailure

    monkeypatch.setattr(installer, "_build", lambda options: object())
    monkeypatch.setattr(
        installer, "execute_transaction",
        lambda plan, **kwargs: (_ for _ in ()).throw(TransactionFailure("runtime")),
    )
    assert installer.main(["--prime"]) == 1
    monkeypatch.setattr(
        installer, "execute_transaction",
        lambda plan, **kwargs: (_ for _ in ()).throw(TransactionFailure("toctou", preflight=True)),
    )
    assert installer.main(["--prime"]) == 2
    monkeypatch.setattr(
        installer, "execute_transaction",
        lambda plan, **kwargs: (_ for _ in ()).throw(RollbackFailure(
            "incomplete", journal_path="/private/journal", backup_path="/private/backup"
        )),
    )
    assert installer.main(["--prime"]) == 3



def test_exit_three_reports_redacted_durable_evidence_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import scripts.install as installer
    from scripts.installer.transaction import RollbackFailure
    monkeypatch.setattr(installer, "_build", lambda options: object())
    monkeypatch.setattr(installer, "recover_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        installer, "execute_transaction",
        lambda plan, **kwargs: (_ for _ in ()).throw(RollbackFailure(
            "incomplete", journal_path=str(Path(os.environ["HOME"]) / ".state/tx/journal.json"),
            backup_path=str(Path(os.environ["HOME"]) / ".state/backups/tx"),
        )),
    )
    assert installer.main(["--prime"]) == 3
    diagnostic = capsys.readouterr().err
    assert "$HOME/.state/tx/journal.json" in diagnostic
    assert "$HOME/.state/backups/tx" in diagnostic



def test_main_releases_lock_when_build_fails_after_acquisition(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.install as installer
    events = []
    class FakeLockFS:
        def open_lock(self, home):
            events.append("open")
            return 7
        def close_lock(self, descriptor):
            events.append(("close", descriptor))
    monkeypatch.setattr(installer, "REAL_FS", FakeLockFS())
    monkeypatch.setattr(installer, "recover_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        installer, "_build", lambda options: (_ for _ in ()).throw(ValueError("bad plan"))
    )
    assert installer.main(["--prime"]) == 2
    assert events == ["open", ("close", 7)]
