from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Dict, Optional, Tuple

import pytest

from scripts.installer.manifest import empty_manifest
from scripts.installer.model import DirectoryMutation, FileMutation, InstallPlan, Snapshot


ROOT = Path(__file__).resolve().parents[1]
HOME_MODE = 0o755


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _snapshot(path: Path) -> Snapshot:
    if not path.exists() and not path.is_symlink():
        return Snapshot(str(path), None, None, False, None)
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return Snapshot(str(path), None, None, True, None, "symlink", os.readlink(path))
    if stat.S_ISDIR(mode):
        return Snapshot(str(path), None, stat.S_IMODE(mode), True, None, "directory")
    content = path.read_bytes()
    return Snapshot(str(path), _hash(content), stat.S_IMODE(mode), True, content)


def _tree(root: Path, *, ignore_state: bool = False) -> Tuple[tuple, ...]:
    rows = []
    if not root.exists():
        return ()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if ignore_state and (relative == ".state" or relative.startswith(".state/")):
            continue
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            rows.append((relative, "symlink", os.readlink(path), None))
        elif stat.S_ISDIR(mode):
            rows.append((relative, "directory", None, stat.S_IMODE(mode)))
        else:
            rows.append((relative, "file", _hash(path.read_bytes()), stat.S_IMODE(mode)))
    return tuple(rows)


def _plan(home: Path) -> InstallPlan:
    state = home / ".state" / "koroche-blyat"
    manifest = state / "manifest.json"
    existing = home / "config.json"
    deleted = home / "delete.txt"
    created = home / "nested" / "script.sh"
    existing.write_bytes(b'{"before":1}\n')
    existing.chmod(0o640)
    deleted.write_bytes(b"delete me\n")
    deleted.chmod(0o604)
    mutations = (
        FileMutation(str(existing), _snapshot(existing), b'{"after":2}\n', 0o640),
        FileMutation(str(deleted), _snapshot(deleted), None, None),
        FileMutation(str(created), _snapshot(created), b"#!/bin/sh\nexit 0\n", 0o755),
    )
    manifest_mutation = FileMutation(
        str(manifest), _snapshot(manifest),
        b'{"installed":true}\n', 0o600,
    )
    directories = (
        DirectoryMutation(str(home / ".state"), False, None, "create", 0o755),
        DirectoryMutation(str(state), False, None, "create", 0o700),
        DirectoryMutation(str(home / "nested"), False, None, "create", 0o755),
    )
    return InstallPlan(
        action="install", requested_hosts=("prime",), effective_hosts=("prime",),
        release="1.0.0", operations=(), mutations=mutations,
        paths={"home": home, "state": state}, state_dir=str(state),
        manifest_path=str(manifest), manifest_mutation=manifest_mutation,
        result_manifest=empty_manifest(), directory_mutations=directories,
    )


class FaultFS:
    """Operation-counting wrapper used to inject one deterministic failure."""

    def __init__(self, real, operation: Optional[str] = None, occurrence: int = 1,
                 *, rollback: bool = False, partial_write: bool = False) -> None:
        self.real = real
        self.operation = operation
        self.occurrence = occurrence
        self.rollback = rollback
        self.partial_write = partial_write
        self.rollback_failure = None
        self.counts: Dict[str, int] = {}
        self.in_rollback = False
        self.trace = []

    def _call(self, name: str, *args, **kwargs):
        self.trace.append((name, self.in_rollback))
        self.counts[name] = self.counts.get(name, 0) + 1
        rollback_failure = getattr(self, "rollback_failure", None)
        fail_primary = (
            name == self.operation
            and self.counts[name] == self.occurrence
            and self.in_rollback == self.rollback
        )
        fail_rollback = (
            self.in_rollback and rollback_failure is not None
            and name == rollback_failure[0]
            and sum(1 for event_name, in_rollback in self.trace if event_name == name and in_rollback)
                == rollback_failure[1]
        )
        if fail_primary or fail_rollback:
            if name == "write" and self.partial_write:
                value = args[1]
                if value:
                    self.real.write(args[0], value[:max(1, len(value) // 2)])
            raise OSError(28, "injected disk-full failure")
        return getattr(self.real, name)(*args, **kwargs)

    def __getattr__(self, name: str):
        if name == "in_rollback":
            raise AttributeError(name)
        return lambda *args, **kwargs: self._call(name, *args, **kwargs)


def test_transaction_api_is_importable() -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import (
        REAL_FS, RollbackFailure, TransactionFailure, execute_transaction,
        rollback, snapshot, validate_committed,
    )
    assert REAL_FS is not None
    assert issubclass(RollbackFailure, TransactionFailure)
    assert callable(recover_pending)
    assert all(callable(item) for item in (execute_transaction, rollback, snapshot, validate_committed))


def test_snapshot_captures_file_directory_symlink_and_missing(tmp_path: Path) -> None:
    from scripts.installer.transaction import snapshot
    regular = tmp_path / "regular"
    regular.write_bytes(b"payload")
    regular.chmod(0o640)
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o750)
    link = tmp_path / "link"
    link.symlink_to("regular")

    got = [snapshot(item) for item in (regular, directory, link, tmp_path / "missing")]
    assert got[0] == _snapshot(regular)
    assert got[1].file_type == "directory" and got[1].mode == 0o750
    assert got[2].file_type == "symlink" and got[2].symlink_target == "regular"
    assert got[3].exists is False


def test_success_commits_manifest_last_preserves_existing_mode_and_cleans_journal(
    tmp_path: Path,
) -> None:
    from scripts.installer.transaction import REAL_FS, execute_transaction, validate_committed
    home = tmp_path / "home"
    home.mkdir(mode=HOME_MODE)
    plan = _plan(home)
    fs = FaultFS(REAL_FS)
    execute_transaction(plan, fs=fs)
    validate_committed(plan, fs=fs)

    assert (home / "config.json").read_bytes() == b'{"after":2}\n'
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o640
    assert not (home / "delete.txt").exists()
    assert stat.S_IMODE((home / "nested/script.sh").stat().st_mode) == 0o755
    assert (home / ".state/koroche-blyat/manifest.json").read_bytes() == b'{"installed":true}\n'
    replace_targets = [item for item in fs.trace if item[0] == "replace" and not item[1]]
    assert replace_targets
    assert list((home / ".state/koroche-blyat/transactions").glob("*")) == []
    assert list((home / ".state/koroche-blyat/backups").glob("*")) == []


def _operation_counts(tmp_path: Path) -> Dict[str, int]:
    from scripts.installer.transaction import REAL_FS, execute_transaction
    home = tmp_path / "count-home"
    home.mkdir(parents=True)
    plan = _plan(home)
    fs = FaultFS(REAL_FS)
    execute_transaction(plan, fs=fs)
    counts: Dict[str, int] = {}
    for name, in_rollback in fs.trace:
        if in_rollback:
            continue
        counts[name] = counts.get(name, 0) + 1
        if name == "validate":
            break
    return counts


@pytest.mark.parametrize("operation", ["write", "fsync", "replace", "unlink", "validate"])
def test_every_injected_forward_failure_restores_exact_initial_tree(
    tmp_path: Path, operation: str
) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    counts = _operation_counts(tmp_path / (operation + "-count"))
    assert counts.get(operation, 0) > 0
    for occurrence in range(1, counts[operation] + 1):
        home = tmp_path / (operation + "-%03d" % occurrence)
        home.mkdir()
        plan = _plan(home)
        before = _tree(home)
        fs = FaultFS(REAL_FS, operation, occurrence)
        with pytest.raises(TransactionFailure) as failure:
            execute_transaction(plan, fs=fs)
        assert not failure.value.rollback_failed
        assert _tree(home, ignore_state=True) == tuple(
            row for row in before if not (row[0] == ".state" or row[0].startswith(".state/"))
        )
        manifest = home / ".state/koroche-blyat/manifest.json"
        assert not manifest.exists()


def test_partial_write_disk_full_rolls_back_exactly(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    before = _tree(home)
    fs = FaultFS(REAL_FS, "write", 1, partial_write=True)
    with pytest.raises(TransactionFailure):
        execute_transaction(plan, fs=fs)
    assert _tree(home, ignore_state=True) == before


def test_concurrent_edit_after_plan_before_lock_aborts_before_target_writes(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    (home / "config.json").write_bytes(b"concurrent edit\n")
    before = _tree(home)
    fs = FaultFS(REAL_FS)
    with pytest.raises(TransactionFailure, match="snapshot changed"):
        execute_transaction(plan, fs=fs)
    assert _tree(home, ignore_state=True) == before
    assert not any(name in ("replace", "unlink") for name, rolling_back in fs.trace if not rolling_back)


def test_symlink_target_is_rejected_before_first_target_write(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    target = home / "nested/script.sh"
    target.parent.mkdir()
    target.symlink_to(home / "outside")
    before = _tree(home)
    fs = FaultFS(REAL_FS)
    with pytest.raises(TransactionFailure):
        execute_transaction(plan, fs=fs)
    assert _tree(home, ignore_state=True) == before


def test_rollback_failure_reports_durable_private_paths(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, RollbackFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    # Trigger validation failure after commits, then fail the first rollback replace.
    fs = FaultFS(REAL_FS, "validate", 1)
    fs.rollback_failure = ("replace", 1)
    with pytest.raises(RollbackFailure) as failure:
        execute_transaction(plan, fs=fs)
    assert failure.value.rollback_failed
    journal = Path(failure.value.journal_path)
    backup = Path(failure.value.backup_path)
    assert journal.is_file() and stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert backup.is_dir() and stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert str(journal).startswith(str(home / ".state/koroche-blyat/transactions"))
    assert str(backup).startswith(str(home / ".state/koroche-blyat/backups"))
    document = json.loads(journal.read_text(encoding="utf-8"))
    assert document["status"] == "rollback_failed"


def test_pending_journal_recovers_before_new_transaction(tmp_path: Path) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    fs = FaultFS(REAL_FS, "validate", 1)
    fs.rollback_failure = ("replace", 1)
    with pytest.raises(TransactionFailure) as failure:
        execute_transaction(plan, fs=fs)
    recover_pending(Path(plan.state_dir), fs=REAL_FS, home=home)
    assert (home / "config.json").read_bytes() == b'{"before":1}\n'
    assert (home / "delete.txt").read_bytes() == b"delete me\n"
    assert not (home / "nested/script.sh").exists()
    assert not Path(failure.value.journal_path).exists()



def test_hardlinked_existing_target_is_rejected_without_changing_peer(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    peer = tmp_path / "outside-peer"
    os.link(home / "config.json", peer)
    before = peer.read_bytes(), stat.S_IMODE(peer.stat().st_mode)
    with pytest.raises(TransactionFailure, match="hardlink"):
        execute_transaction(plan, fs=REAL_FS)
    assert (peer.read_bytes(), stat.S_IMODE(peer.stat().st_mode)) == before
    assert not (home / ".state/koroche-blyat/manifest.json").exists()


def test_invalid_plan_rejects_manifest_collision_before_state_creation(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    collision = replace(
        plan,
        mutations=plan.mutations + (plan.manifest_mutation,),
    )
    with pytest.raises(TransactionFailure, match="collision"):
        execute_transaction(collision, fs=REAL_FS)
    assert not (home / ".state").exists()


def test_tampered_pending_journal_fails_closed_without_target_writes(tmp_path: Path) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    state = tmp_path / "home/.state/koroche-blyat"
    transaction = state / "transactions/deadbeef"
    backup = state / "backups/deadbeef"
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    transaction.parent.chmod(0o700)
    backup.parent.chmod(0o700)
    transaction.chmod(0o700)
    backup.chmod(0o700)
    journal = transaction / "journal.json"
    journal.write_bytes(b'{"schema_version":1,"schema_version":1}\n')
    journal.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_bytes(b"sentinel")
    with pytest.raises(ValueError, match="duplicate|schema"):
        recover_pending(state, fs=REAL_FS, home=state.parents[1])
    assert outside.read_bytes() == b"sentinel"


def test_preflight_failure_does_not_create_transaction_state(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    (home / "config.json").write_bytes(b"concurrent")
    with pytest.raises(TransactionFailure):
        execute_transaction(plan, fs=REAL_FS)
    assert not (home / ".state").exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda doc: doc.__setitem__("home", "/tmp/outside"),
        lambda doc: doc.__setitem__("state", "../escape"),
        lambda doc: doc.__setitem__("backup", ".state/koroche-blyat/backups/other"),
        lambda doc: doc["operations"][0].__setitem__("path", "../../outside"),
        lambda doc: doc["operations"][0].__setitem__("applied", "yes"),
    ],
)
def test_pending_journal_rejects_untrusted_paths_and_types_without_writes(
    tmp_path: Path, mutator
) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state = home / ".state/koroche-blyat"
    transaction = state / "transactions/deadbeefdeadbeef"
    backup = state / "backups/deadbeefdeadbeef"
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    target = home / "target"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"new")
    doc = {
        "schema_version": 1, "transaction_id": "deadbeefdeadbeef",
        "status": "applying", "home": str(home),
        "state": ".state/koroche-blyat",
        "backup": ".state/koroche-blyat/backups/deadbeefdeadbeef",
        "action": "install", "infrastructure_created": [],
        "operations": [{
            "index": 0, "kind": "file", "path": "target",
            "old": {"exists": False, "file_type": "file", "mode": None,
                    "sha256": None, "symlink_target": None},
            "backup": None, "backup_sha256": None,
            "new_exists": True, "new_sha256": _hash(b"new"),
            "new_mode": 0o644, "applied": True, "manifest": False,
        }],
    }
    mutator(doc)
    journal = transaction / "journal.json"
    journal.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_bytes(b"sentinel")
    before = _tree(tmp_path)
    with pytest.raises(ValueError):
        recover_pending(state, fs=REAL_FS, home=home)
    assert outside.read_bytes() == b"sentinel"
    assert _tree(tmp_path) == before



def test_durable_committed_journal_recovery_keeps_postimage_and_only_cleans_evidence(
    tmp_path: Path
) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state = home / ".state/koroche-blyat"
    txid = "deadbeefdeadbeef"
    transaction = state / "transactions" / txid
    backup = state / "backups" / txid
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    target = home / "target"
    target.write_bytes(b"installed")
    target.chmod(0o644)
    document = {
        "schema_version": 1, "transaction_id": txid, "status": "committed",
        "home": str(home), "state": ".state/koroche-blyat",
        "backup": ".state/koroche-blyat/backups/" + txid,
        "action": "install", "infrastructure_created": [], "operations": [{
            "index": 0, "kind": "file", "path": "target",
            "old": {"exists": False, "file_type": "file", "mode": None,
                    "sha256": None, "symlink_target": None},
            "backup": None, "backup_sha256": None, "new_exists": True,
            "new_sha256": _hash(b"installed"), "new_mode": 0o644,
            "applied": True, "manifest": False,
        }],
    }
    journal = transaction / "journal.json"
    journal.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    recover_pending(state, fs=REAL_FS, home=home)
    assert target.read_bytes() == b"installed"
    assert not transaction.exists()
    assert not backup.exists()



def test_validation_occurs_before_manifest_replace_and_manifest_is_last_file_commit(
    tmp_path: Path,
) -> None:
    from scripts.installer.transaction import REAL_FS, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    fs = FaultFS(REAL_FS)
    execute_transaction(plan, fs=fs)
    forward = [(name, rolling_back) for name, rolling_back in fs.trace if not rolling_back]
    validate_index = next(index for index, item in enumerate(forward) if item[0] == "validate")
    replace_indexes = [index for index, item in enumerate(forward) if item[0] == "replace"]
    assert any(index < validate_index for index in replace_indexes)
    assert any(index > validate_index for index in replace_indexes)



def test_rollback_never_overwrites_external_edit_after_forward_commit(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, RollbackFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)

    class ExternalEditFS(FaultFS):
        def _call(self, name: str, *args, **kwargs):
            if name == "validate" and not self.in_rollback:
                (home / "config.json").write_bytes(b"external edit\n")
                raise OSError(5, "validation failed after external edit")
            return super()._call(name, *args, **kwargs)

    with pytest.raises(RollbackFailure) as failure:
        execute_transaction(plan, fs=ExternalEditFS(REAL_FS))
    assert (home / "config.json").read_bytes() == b"external edit\n"
    assert Path(failure.value.journal_path).is_file()
    assert Path(failure.value.backup_path).is_dir()



def test_fd_relative_replace_never_follows_swapped_symlink_parent(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS
    safe = tmp_path / "safe"
    outside = tmp_path / "outside"
    safe.mkdir()
    outside.mkdir()
    source = safe / "temp"
    source.write_bytes(b"owned")
    canary = outside / "target"
    canary.write_bytes(b"outside")
    safe.rename(tmp_path / "safe-old")
    safe.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        REAL_FS.replace(source, safe / "target")
    assert canary.read_bytes() == b"outside"



def test_invalid_json_postimage_fails_validation_and_rolls_back(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    broken = replace(plan.mutations[0], new_content=b'{"duplicate":1,"duplicate":2}\n')
    plan = replace(plan, mutations=(broken,) + plan.mutations[1:])
    before = _tree(home)
    with pytest.raises(TransactionFailure) as failure:
        execute_transaction(plan, fs=REAL_FS)
    assert not failure.value.rollback_failed
    assert _tree(home, ignore_state=True) == before



def test_process_crash_after_target_replace_recovers_exact_initial_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    before = _tree(home)
    crash_script = r"""
import importlib.util, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('fixture', sys.argv[1])
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
from scripts.installer.transaction import REAL_FS, execute_transaction
home=Path(sys.argv[2]); plan=fixture._plan(home)
class CrashFS(fixture.FaultFS):
    def _call(self, name, *args, **kwargs):
        result=super()._call(name,*args,**kwargs)
        if name == 'replace' and Path(args[1]).name == 'config.json' and not self.in_rollback:
            os._exit(91)
        return result
execute_transaction(plan, fs=CrashFS(REAL_FS))
"""
    result = subprocess.run(
        [sys.executable, "-c", crash_script, str(Path(__file__)), str(home)],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    assert result.returncode == 91
    recovery_script = r"""
import sys
from pathlib import Path
from scripts.installer.journal import recover_pending
from scripts.installer.transaction import REAL_FS
recover_pending(Path(sys.argv[1]), fs=REAL_FS, home=Path(sys.argv[2]))
"""
    recovered = subprocess.run(
        [sys.executable, "-c", recovery_script, str(Path(plan.state_dir)), str(home)],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert _tree(home, ignore_state=True) == before


@pytest.mark.skipif(not Path("/var").is_symlink(), reason="platform has no /var symlink alias")
def test_public_snapshot_accepts_noncanonical_system_symlink_ancestor(tmp_path: Path) -> None:
    from scripts.installer.transaction import snapshot
    canonical = tmp_path / "alias-target"
    canonical.write_bytes(b"value")
    visible = Path(str(canonical).replace("/private/var/", "/var/", 1))
    got = snapshot(visible)
    assert got.exists and got.bytes_sha256 == _hash(b"value")
    assert got.path == str(visible)



def test_partial_target_temp_write_is_cleaned_and_rolls_back_exactly(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    before = _tree(home)

    class PartialTargetFS(FaultFS):
        def _call(self, name: str, *args, **kwargs):
            if (
                name == "write" and ".koroche-blyat." in Path(args[0]).name
                and not Path(args[0]).name.startswith(".journal")
            ):
                value = args[1]
                self.real.write(args[0], value[:max(1, len(value) // 2)])
                raise OSError(28, "partial target write")
            return super()._call(name, *args, **kwargs)

    with pytest.raises(TransactionFailure) as failure:
        execute_transaction(plan, fs=PartialTargetFS(REAL_FS))
    assert not failure.value.rollback_failed
    assert _tree(home, ignore_state=True) == before
    assert not list(home.rglob("*.koroche-blyat.*"))


def test_prepare_failure_removes_unjournaled_transaction_and_retry_succeeds(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    before = _tree(home)
    fs = FaultFS(REAL_FS, "write", 1, partial_write=True)
    with pytest.raises(TransactionFailure) as failure:
        execute_transaction(plan, fs=fs)
    assert not failure.value.rollback_failed
    assert _tree(home) == before
    execute_transaction(plan, fs=REAL_FS)
    assert (home / ".state/koroche-blyat/manifest.json").is_file()


def test_directory_rollback_conflict_keeps_recovery_evidence(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, RollbackFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)

    class DirectoryConflictFS(FaultFS):
        def _call(self, name: str, *args, **kwargs):
            if name == "validate" and not self.in_rollback:
                (home / "nested/external.txt").write_bytes(b"user")
                raise OSError(5, "validation failure")
            return super()._call(name, *args, **kwargs)

    with pytest.raises(RollbackFailure) as failure:
        execute_transaction(plan, fs=DirectoryConflictFS(REAL_FS))
    assert (home / "nested/external.txt").read_bytes() == b"user"
    assert Path(failure.value.journal_path).is_file()
    assert Path(failure.value.backup_path).is_dir()


def test_committed_cleanup_failure_never_rolls_back_committed_postimage(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)

    class CleanupFailureFS(FaultFS):
        def __init__(self, real) -> None:
            super().__init__(real)
            self.transaction_removed = False
        def _call(self, name: str, *args, **kwargs):
            if name == "rmdir" and Path(args[0]).parent.name == "transactions":
                result = super()._call(name, *args, **kwargs)
                self.transaction_removed = True
                return result
            if name == "fsync" and self.transaction_removed:
                self.transaction_removed = False
                raise OSError(5, "cleanup fsync failure")
            return super()._call(name, *args, **kwargs)

    execute_transaction(plan, fs=CleanupFailureFS(REAL_FS))
    assert (home / "config.json").read_bytes() == b'{"after":2}\n'
    assert (home / ".state/koroche-blyat/manifest.json").read_bytes() == b'{"installed":true}\n'


def test_preexisting_empty_state_tree_survives_validation_rollback(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    state = Path(plan.state_dir)
    (state / "transactions").mkdir(parents=True)
    (state / "backups").mkdir()
    for directory in (state, state / "transactions", state / "backups"):
        directory.chmod(0o700)
    plan = replace(
        plan,
        directory_mutations=tuple(
            item for item in plan.directory_mutations
            if Path(item.path) == home / "nested"
        ),
    )
    before = _tree(home)
    with pytest.raises(TransactionFailure):
        execute_transaction(plan, fs=FaultFS(REAL_FS, "validate", 1))
    assert _tree(home) == before



def test_committed_journal_with_unapplied_operation_is_rejected_without_cleanup(
    tmp_path: Path
) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state = home / ".state/koroche-blyat"
    txid = "feedfacefeedface"
    transaction = state / "transactions" / txid
    backup = state / "backups" / txid
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    document = {
        "schema_version": 1, "transaction_id": txid, "status": "committed",
        "home": str(home), "state": ".state/koroche-blyat",
        "backup": ".state/koroche-blyat/backups/" + txid,
        "action": "install", "infrastructure_created": [], "operations": [{
            "index": 0, "kind": "file", "path": "target",
            "old": {"exists": False, "file_type": "file", "mode": None,
                    "sha256": None, "symlink_target": None},
            "backup": None, "backup_sha256": None, "new_exists": True,
            "new_sha256": _hash(b"new"), "new_mode": 0o644,
            "applied": False, "manifest": False,
        }],
    }
    journal = transaction / "journal.json"
    journal.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    with pytest.raises(ValueError, match="committed"):
        recover_pending(state, fs=REAL_FS, home=home)
    assert journal.is_file() and backup.is_dir()



def test_committed_recovery_accepts_nonempty_remove_if_empty_directory(tmp_path: Path) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state = home / ".state/koroche-blyat"
    txid = "cafebabecafebabe"
    transaction = state / "transactions" / txid
    backup = state / "backups" / txid
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    managed = home / "managed"
    managed.mkdir(mode=0o755)
    (managed / "unknown").write_bytes(b"keep")
    document = {
        "schema_version": 1, "transaction_id": txid, "status": "committed",
        "home": str(home), "state": ".state/koroche-blyat",
        "backup": ".state/koroche-blyat/backups/" + txid,
        "action": "uninstall", "infrastructure_created": [], "operations": [{
            "index": 0, "kind": "directory", "path": "managed",
            "change": "remove_if_empty", "old_exists": True,
            "old_mode": 0o755, "new_mode": None,
            "applied": True, "manifest": False,
        }],
    }
    journal = transaction / "journal.json"
    journal.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    recover_pending(state, fs=REAL_FS, home=home)
    assert (managed / "unknown").read_bytes() == b"keep"
    assert not transaction.exists() and not backup.exists()



def test_validate_committed_wraps_validation_errors_as_transaction_failure(tmp_path: Path) -> None:
    from scripts.installer.transaction import TransactionFailure, validate_committed
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    with pytest.raises(TransactionFailure):
        validate_committed(plan)



def test_prepare_io_failure_is_runtime_failure_not_preflight(tmp_path: Path) -> None:
    from scripts.installer.transaction import REAL_FS, TransactionFailure, execute_transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    with pytest.raises(TransactionFailure) as failure:
        execute_transaction(plan, fs=FaultFS(REAL_FS, "write", 1))
    assert failure.value.preflight is False



def test_recovery_binds_journal_to_trusted_home(tmp_path: Path) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state = home / ".state/koroche-blyat"
    txid = "abad1deaabad1dea"
    transaction = state / "transactions" / txid
    backup = state / "backups" / txid
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    relative_state = state.relative_to(Path("/" )).as_posix()
    relative_backup = backup.relative_to(Path("/" )).as_posix()
    document = {
        "schema_version": 1, "transaction_id": txid, "status": "applying",
        "home": "/", "state": relative_state, "backup": relative_backup,
        "action": "install", "infrastructure_created": [], "operations": [],
    }
    journal = transaction / "journal.json"
    journal.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    with pytest.raises(ValueError, match="HOME"):
        recover_pending(state, fs=REAL_FS, home=home)
    assert journal.is_file()



def test_committed_recovery_cleanup_never_blocks_or_overwrites_later_user_edit(
    tmp_path: Path
) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state = home / ".state/koroche-blyat"
    txid = "decafbaddecafbad"
    transaction = state / "transactions" / txid
    backup = state / "backups" / txid
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    target = home / "target"
    target.write_bytes(b"later user edit")
    target.chmod(0o644)
    document = {
        "schema_version": 1, "transaction_id": txid, "status": "committed",
        "home": str(home), "state": ".state/koroche-blyat",
        "backup": ".state/koroche-blyat/backups/" + txid,
        "action": "install", "infrastructure_created": [], "operations": [{
            "index": 0, "kind": "file", "path": "target",
            "old": {"exists": False, "file_type": "file", "mode": None,
                    "sha256": None, "symlink_target": None},
            "backup": None, "backup_sha256": None, "new_exists": True,
            "new_sha256": _hash(b"installed"), "new_mode": 0o644,
            "applied": True, "manifest": False,
        }],
    }
    journal = transaction / "journal.json"
    journal.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    recover_pending(state, fs=REAL_FS, home=home)
    assert target.read_bytes() == b"later user edit"
    assert not transaction.exists() and not backup.exists()



def test_committed_recovery_removes_only_recorded_empty_infrastructure(tmp_path: Path) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state_parent = home / ".state"
    state = state_parent / "koroche-blyat"
    txid = "0123456789abcdef"
    transaction = state / "transactions" / txid
    backup = state / "backups" / txid
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    state_parent.chmod(0o755)
    document = {
        "schema_version": 1, "transaction_id": txid, "status": "committed",
        "home": str(home), "state": ".state/koroche-blyat",
        "backup": ".state/koroche-blyat/backups/" + txid,
        "action": "uninstall",
        "infrastructure_created": [
            ".state", ".state/koroche-blyat",
            ".state/koroche-blyat/transactions", ".state/koroche-blyat/backups",
        ],
        "operations": [],
    }
    journal = transaction / "journal.json"
    journal.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    recover_pending(state, fs=REAL_FS, home=home)
    assert not state_parent.exists()



def test_recovery_cleans_crash_before_first_durable_journal(tmp_path: Path) -> None:
    from scripts.installer.journal import recover_pending
    from scripts.installer.transaction import REAL_FS
    home = tmp_path / "home"
    state = home / ".state/koroche-blyat"
    txid = "1111222233334444"
    transaction = state / "transactions" / txid
    backup = state / "backups" / txid
    transaction.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (state, transaction.parent, backup.parent, transaction, backup):
        directory.chmod(0o700)
    (backup / "partial.bin").write_bytes(b"partial")
    (backup / "partial.bin").chmod(0o600)
    recover_pending(state, fs=REAL_FS, home=home)
    assert not transaction.exists() and not backup.exists()



def test_preexisting_transaction_temp_collision_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.installer.transaction as transaction
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home)
    txid = "a" * 32
    monkeypatch.setattr(transaction.secrets, "token_hex", lambda size: txid)
    collision = home / (".config.json.koroche-blyat.%s.000003" % txid)
    collision.write_bytes(b"external sentinel")
    with pytest.raises(transaction.TransactionFailure):
        transaction.execute_transaction(plan, fs=transaction.REAL_FS)
    assert collision.read_bytes() == b"external sentinel"
