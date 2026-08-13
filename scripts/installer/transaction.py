from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .model import DirectoryMutation, FileMutation, InstallPlan, Snapshot
from .patch_json import parse_json_document


class TransactionFailure(Exception):
    def __init__(
        self, message: str, *, journal_path: Optional[str] = None,
        backup_path: Optional[str] = None, rollback_failed: bool = False,
        preflight: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.journal_path = journal_path
        self.backup_path = backup_path
        self.rollback_failed = rollback_failed
        self.preflight = preflight


class RollbackFailure(TransactionFailure):
    def __init__(self, message: str, *, journal_path: str, backup_path: str) -> None:
        super().__init__(
            message, journal_path=journal_path, backup_path=backup_path,
            rollback_failed=True,
        )


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class RealFS:
    """Injectable filesystem facade using no-follow, fd-relative leaf operations."""

    @staticmethod
    def _flags_directory() -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return flags

    def _open_parent(self, path: Path) -> Tuple[int, str]:
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts or path.name in ("", ".", ".."):
            raise OSError(errno.EINVAL, "unsafe filesystem path")
        descriptor = os.open("/", self._flags_directory())
        try:
            for component in path.parts[1:-1]:
                next_descriptor = os.open(
                    component, self._flags_directory(), dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor, path.name
        except BaseException:
            os.close(descriptor)
            raise

    def lexists(self, path: Path) -> bool:
        try:
            descriptor, leaf = self._open_parent(Path(path))
        except FileNotFoundError:
            return False
        try:
            os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(descriptor)

    def lstat(self, path: Path):
        descriptor, leaf = self._open_parent(Path(path))
        try:
            return os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        finally:
            os.close(descriptor)

    def read(self, path: Path) -> bytes:
        descriptor, leaf = self._open_parent(Path(path))
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(leaf, flags, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        try:
            chunks = []
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(file_descriptor)

    def readlink(self, path: Path) -> str:
        descriptor, leaf = self._open_parent(Path(path))
        try:
            return os.readlink(leaf, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def mkdir(self, path: Path, mode: int) -> None:
        descriptor, leaf = self._open_parent(Path(path))
        try:
            os.mkdir(leaf, mode, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def chmod(self, path: Path, mode: int) -> None:
        descriptor, leaf = self._open_parent(Path(path))
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            leaf_descriptor = os.open(leaf, flags, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        try:
            os.fchmod(leaf_descriptor, mode)
        finally:
            os.close(leaf_descriptor)

    def write(self, path: Path, value: bytes) -> None:
        descriptor, leaf = self._open_parent(Path(path))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(leaf, flags, 0o600, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        try:
            view = memoryview(value)
            offset = 0
            while offset < len(view):
                written = os.write(file_descriptor, view[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short write")
                offset += written
        finally:
            os.close(file_descriptor)

    def replace(self, source: Path, target: Path) -> None:
        source_descriptor, source_leaf = self._open_parent(Path(source))
        try:
            target_descriptor, target_leaf = self._open_parent(Path(target))
            try:
                os.replace(
                    source_leaf, target_leaf,
                    src_dir_fd=source_descriptor, dst_dir_fd=target_descriptor,
                )
            finally:
                os.close(target_descriptor)
        finally:
            os.close(source_descriptor)

    def unlink(self, path: Path) -> None:
        descriptor, leaf = self._open_parent(Path(path))
        try:
            os.unlink(leaf, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def rmdir(self, path: Path) -> None:
        descriptor, leaf = self._open_parent(Path(path))
        try:
            os.rmdir(leaf, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def listdir(self, path: Path) -> List[str]:
        flags = self._flags_directory()
        descriptor = os.open(str(path), flags)
        try:
            return os.listdir(descriptor)
        finally:
            os.close(descriptor)

    def fsync(self, value: Any) -> None:
        if isinstance(value, int):
            os.fsync(value)
            return
        path = Path(value)
        descriptor, leaf = self._open_parent(path)
        flags = os.O_RDONLY
        try:
            metadata = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode) and hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            leaf_descriptor = os.open(leaf, flags, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        try:
            os.fsync(leaf_descriptor)
        finally:
            os.close(leaf_descriptor)

    def open_lock(self, home: Path) -> int:
        flags = self._flags_directory()
        descriptor = os.open(str(home), flags)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def close_lock(self, descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def validate(self, plan: InstallPlan) -> None:
        del plan


REAL_FS = RealFS()


def snapshot(path: Path, fs: Any = REAL_FS) -> Snapshot:
    path = Path(path)
    operation_path = path
    if fs is REAL_FS and path.is_absolute():
        try:
            operation_path = path.parent.resolve(strict=False) / path.name
        except OSError:
            operation_path = path
    if not fs.lexists(operation_path):
        return Snapshot(str(path), None, None, False, None)
    metadata = fs.lstat(operation_path)
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return Snapshot(
            str(path), None, None, True, None, "symlink", fs.readlink(operation_path)
        )
    if stat.S_ISDIR(metadata.st_mode):
        return Snapshot(str(path), None, mode, True, None, "directory")
    if not stat.S_ISREG(metadata.st_mode):
        return Snapshot(str(path), None, mode, True, None, "special")
    content = fs.read(operation_path)
    return Snapshot(str(path), _sha(content), mode, True, content, "file", None)


def _same_snapshot(left: Snapshot, right: Snapshot) -> bool:
    return (
        left.exists == right.exists
        and left.bytes_sha256 == right.bytes_sha256
        and left.mode == right.mode
        and left.content == right.content
        and left.file_type == right.file_type
        and left.symlink_target == right.symlink_target
    )


def _path_inside(path: Path, home: Path) -> Tuple[str, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise TransactionFailure("transaction path is not canonical", preflight=True)
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise TransactionFailure("transaction path is outside HOME", preflight=True) from error
    if not relative.parts:
        raise TransactionFailure("HOME cannot be a mutation target", preflight=True)
    return relative.parts


def _validate_ancestors(path: Path, home: Path, fs: Any) -> None:
    parts = _path_inside(path, home)
    current = home
    home_metadata = fs.lstat(home)
    if not stat.S_ISDIR(home_metadata.st_mode) or stat.S_ISLNK(home_metadata.st_mode):
        raise TransactionFailure("HOME is not a safe directory", preflight=True)
    for component in parts[:-1]:
        current = current / component
        if not fs.lexists(current):
            break
        metadata = fs.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise TransactionFailure("target ancestor is unsafe", preflight=True)


def _snapshot_document(value: Snapshot) -> Dict[str, Any]:
    return {
        "exists": value.exists,
        "file_type": value.file_type,
        "mode": value.mode,
        "sha256": value.bytes_sha256,
        "symlink_target": value.symlink_target,
    }


def _snapshot_from_document(path: Path, value: Mapping[str, Any], content: Optional[bytes]) -> Snapshot:
    return Snapshot(
        str(path), value.get("sha256"), value.get("mode"),
        bool(value.get("exists")), content,
        str(value.get("file_type", "file")), value.get("symlink_target"),
    )


def _ensure_dir(path: Path, mode: int, fs: Any, created: List[Path]) -> None:
    if fs.lexists(path):
        metadata = fs.lstat(path)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise TransactionFailure("private state path is unsafe", preflight=True)
        return
    fs.mkdir(path, mode)
    fs.chmod(path, mode)
    created.append(path)
    fs.fsync(path.parent)


def _ensure_state_infrastructure(plan: InstallPlan, fs: Any) -> List[Path]:
    if plan.state_dir is None:
        raise TransactionFailure("plan has no state directory", preflight=True)
    home = Path(plan.paths["home"])
    state = Path(plan.state_dir)
    _path_inside(state, home)
    created: List[Path] = []
    missing = []
    current = state
    while current != home and not fs.lexists(current):
        missing.append(current)
        current = current.parent
    _validate_ancestors(state, home, fs)
    for directory in reversed(missing):
        desired = 0o700 if directory == state or state in directory.parents else 0o755
        _ensure_dir(directory, desired, fs, created)
    if stat.S_IMODE(fs.lstat(state).st_mode) != 0o700:
        raise TransactionFailure("state directory mode must be 0700", preflight=True)
    for directory in (state / "transactions", state / "backups"):
        _ensure_dir(directory, 0o700, fs, created)
        if stat.S_IMODE(fs.lstat(directory).st_mode) != 0o700:
            raise TransactionFailure("transaction state mode must be 0700", preflight=True)
    return created


def _all_file_mutations(plan: InstallPlan) -> Tuple[FileMutation, ...]:
    result = list(plan.mutations)
    if plan.manifest_mutation is not None:
        result.append(plan.manifest_mutation)
    return tuple(result)


def _validate_plan(plan: InstallPlan, fs: Any) -> None:
    if "home" not in plan.paths or plan.state_dir is None or plan.manifest_path is None:
        raise TransactionFailure("transaction plan is incomplete", preflight=True)
    home = Path(plan.paths["home"])
    state = Path(plan.state_dir)
    manifest = Path(plan.manifest_path)
    _path_inside(state, home)
    _path_inside(manifest, home)
    if manifest != state / "manifest.json":
        raise TransactionFailure("manifest path does not match state directory", preflight=True)
    paths = []
    for mutation in _all_file_mutations(plan):
        path = Path(mutation.path)
        _validate_ancestors(path, home, fs)
        if mutation.old_snapshot.path != mutation.path:
            raise TransactionFailure("snapshot path does not match mutation", preflight=True)
        paths.append(path)
    for mutation in plan.directory_mutations:
        path = Path(mutation.path)
        _validate_ancestors(path, home, fs)
        paths.append(path)
    normalized = [tuple(part.casefold() for part in path.relative_to(home).parts) for path in paths]
    if len(set(normalized)) != len(normalized):
        raise TransactionFailure("transaction target collision", preflight=True)
    if plan.manifest_mutation is not None and Path(plan.manifest_mutation.path) != manifest:
        raise TransactionFailure("manifest mutation path mismatch", preflight=True)


def _validate_preimages(plan: InstallPlan, fs: Any) -> None:
    for mutation in _all_file_mutations(plan):
        actual = snapshot(Path(mutation.path), fs)
        if not _same_snapshot(actual, mutation.old_snapshot):
            raise TransactionFailure(
                "snapshot changed before transaction: %s" % mutation.path,
                preflight=True,
            )
        if actual.file_type not in ("file",) and actual.exists:
            raise TransactionFailure("managed file target has unsafe type", preflight=True)
    for mutation in plan.directory_mutations:
        path = Path(mutation.path)
        actual = snapshot(path, fs)
        if mutation.change == "create":
            if actual.exists != mutation.old_exists:
                raise TransactionFailure("directory snapshot changed", preflight=True)
            if actual.exists and (actual.file_type != "directory" or actual.mode != mutation.old_mode):
                raise TransactionFailure("directory snapshot changed", preflight=True)
        elif mutation.change == "remove_if_empty":
            if not actual.exists or actual.file_type != "directory":
                raise TransactionFailure("directory snapshot changed", preflight=True)
            if mutation.old_mode is not None and actual.mode != mutation.old_mode:
                raise TransactionFailure("directory snapshot changed", preflight=True)
        else:
            raise TransactionFailure("unsupported directory mutation", preflight=True)


def _relative(path: Path, home: Path) -> str:
    return PurePosixPath(*_path_inside(path, home)).as_posix()


def _write_private(path: Path, value: bytes, mode: int, fs: Any) -> None:
    if fs.lexists(path):
        fs.unlink(path)
    fs.write(path, value)
    fs.chmod(path, mode)
    fs.fsync(path)
    fs.fsync(path.parent)


def _journal_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def _store_journal(path: Path, document: Mapping[str, Any], fs: Any) -> None:
    temporary = path.parent / ("." + path.name + ".tmp")
    if fs.lexists(temporary):
        fs.unlink(temporary)
    fs.write(temporary, _journal_bytes(document))
    fs.chmod(temporary, 0o600)
    fs.fsync(temporary)
    fs.replace(temporary, path)
    fs.fsync(path.parent)


def _prepare_journal(
    plan: InstallPlan, txid: str, fs: Any,
    infrastructure_created: Sequence[Path] = (),
) -> Tuple[Path, Path, Dict[str, Any]]:
    home = Path(plan.paths["home"])
    state = Path(plan.state_dir)
    transaction_dir = state / "transactions" / txid
    backup_dir = state / "backups" / txid
    created: List[Path] = []
    _ensure_dir(transaction_dir, 0o700, fs, created)
    _ensure_dir(backup_dir, 0o700, fs, created)
    operations = []
    ordered_directories = list(plan.directory_mutations)
    ordered_files = list(plan.mutations)
    if plan.manifest_mutation is not None:
        ordered_files.append(plan.manifest_mutation)
    ordered: List[Tuple[str, Any]] = []
    if plan.action == "uninstall":
        ordered.extend(("file", item) for item in ordered_files)
        ordered.extend(("directory", item) for item in ordered_directories)
    else:
        ordered.extend(("directory", item) for item in ordered_directories)
        ordered.extend(("file", item) for item in ordered_files)
    for index, (kind, mutation) in enumerate(ordered):
        if kind == "directory":
            operations.append({
                "index": index, "kind": "directory", "path": _relative(Path(mutation.path), home),
                "change": mutation.change, "old_exists": mutation.old_exists,
                "old_mode": mutation.old_mode, "new_mode": mutation.new_mode,
                "applied": False, "manifest": False,
            })
            continue
        old = mutation.old_snapshot
        backup_name = None
        backup_sha = None
        if old.exists and old.file_type == "file":
            if old.content is None:
                raise TransactionFailure("snapshot content is missing", preflight=True)
            backup_name = "%06d.bin" % index
            backup = backup_dir / backup_name
            _write_private(backup, old.content, 0o600, fs)
            backup_sha = _sha(old.content)
        operations.append({
            "index": index, "kind": "file", "path": _relative(Path(mutation.path), home),
            "old": _snapshot_document(old), "backup": backup_name,
            "backup_sha256": backup_sha, "new_exists": mutation.new_content is not None,
            "new_sha256": _sha(mutation.new_content) if mutation.new_content is not None else None,
            "new_mode": mutation.new_mode, "applied": False,
            "manifest": mutation is plan.manifest_mutation,
        })
    document: Dict[str, Any] = {
        "schema_version": 1, "transaction_id": txid, "status": "prepared",
        "home": str(home), "state": _relative(state, home),
        "backup": _relative(backup_dir, home), "action": plan.action,
        "infrastructure_created": [
            _relative(path, home) for path in infrastructure_created
        ],
        "operations": operations,
    }
    journal = transaction_dir / "journal.json"
    _store_journal(journal, document, fs)
    fs.fsync(backup_dir)
    fs.fsync(transaction_dir)
    fs.fsync(backup_dir.parent)
    fs.fsync(transaction_dir.parent)
    return journal, backup_dir, document


def _mutation_map(plan: InstallPlan) -> Dict[str, Any]:
    home = Path(plan.paths["home"])
    result: Dict[str, Any] = {}
    for mutation in plan.directory_mutations:
        result[_relative(Path(mutation.path), home)] = mutation
    for mutation in plan.mutations:
        result[_relative(Path(mutation.path), home)] = mutation
    if plan.manifest_mutation is not None:
        result[_relative(Path(plan.manifest_mutation.path), home)] = plan.manifest_mutation
    return result


def _temp_for(path: Path, txid: str, index: int) -> Path:
    return path.parent / (".%s.koroche-blyat.%s.%06d" % (path.name, txid, index))


def _write_replacement(path: Path, content: bytes, mode: int, txid: str, index: int, fs: Any) -> None:
    temporary = _temp_for(path, txid, index)
    if fs.lexists(temporary):
        raise TransactionFailure("transaction temporary path collision")
    fs.write(temporary, content)
    fs.chmod(temporary, mode)
    fs.fsync(temporary)
    fs.replace(temporary, path)
    fs.fsync(path.parent)


def _verify_operation_preimage(
    operation: Mapping[str, Any], mutation: Any, txid: str, fs: Any
) -> None:
    path = Path(mutation.path)
    actual = snapshot(path, fs)
    if operation["kind"] == "file":
        if not _same_snapshot(actual, mutation.old_snapshot):
            raise TransactionFailure("snapshot changed during transaction")
        if actual.exists:
            metadata = fs.lstat(path)
            if getattr(metadata, "st_nlink", 1) != 1:
                raise TransactionFailure("managed hardlink target is unsafe")
        if mutation.new_content is not None:
            # The temporary path must be proven free BEFORE the operation is
            # journaled as applied. Rollback deletes `_temp_for(...)` to free
            # the name for the restore write, so an operation inside the
            # applied set implicitly authorizes that deletion. Detecting the
            # collision later — in `_write_replacement` — would journal the
            # authorization first and make rollback destroy a pre-existing
            # file this transaction never created.
            temporary = _temp_for(path, txid, int(operation["index"]))
            if fs.lexists(temporary):
                raise TransactionFailure("transaction temporary path collision")
        return
    if mutation.change == "create":
        if not mutation.old_exists and actual.exists:
            if actual.file_type == "directory" and actual.mode == mutation.new_mode:
                return
            raise TransactionFailure("directory changed during transaction")
        if actual.exists != mutation.old_exists:
            raise TransactionFailure("directory changed during transaction")
        if actual.exists and (
            actual.file_type != "directory" or actual.mode != mutation.old_mode
        ):
            raise TransactionFailure("directory changed during transaction")
    elif mutation.change == "remove_if_empty":
        if not actual.exists or actual.file_type != "directory":
            raise TransactionFailure("directory changed during transaction")
        if mutation.old_mode is not None and actual.mode != mutation.old_mode:
            raise TransactionFailure("directory changed during transaction")


def _apply_operation(
    operation: Dict[str, Any], mutation: Any, home: Path, txid: str, fs: Any
) -> None:
    path = home / PurePosixPath(operation["path"])
    if operation["kind"] == "directory":
        if mutation.change == "create":
            if not fs.lexists(path):
                fs.mkdir(path, int(mutation.new_mode))
                fs.chmod(path, int(mutation.new_mode))
                fs.fsync(path.parent)
        elif mutation.change == "remove_if_empty":
            try:
                fs.rmdir(path)
                fs.fsync(path.parent)
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
        return
    if mutation.new_content is None:
        if fs.lexists(path):
            fs.unlink(path)
            fs.fsync(path.parent)
        return
    if mutation.new_mode is None:
        raise TransactionFailure("file mutation mode is missing")
    _write_replacement(
        path, mutation.new_content, mutation.new_mode,
        txid, int(operation["index"]), fs,
    )


def _expected_after(operation: Mapping[str, Any], path: Path, fs: Any) -> bool:
    actual = snapshot(path, fs)
    if operation["kind"] == "directory":
        if operation["change"] == "create":
            return actual.exists and actual.file_type == "directory" and actual.mode == operation["new_mode"]
        return (
            not actual.exists
            or (
                actual.file_type == "directory"
                and actual.mode == operation["old_mode"]
            )
        )
    if not operation["new_exists"]:
        return not actual.exists
    return (
        actual.exists and actual.file_type == "file"
        and actual.bytes_sha256 == operation["new_sha256"]
        and actual.mode == operation["new_mode"]
    )


def _load_backup(operation: Mapping[str, Any], backup_dir: Path, fs: Any) -> Optional[bytes]:
    name = operation.get("backup")
    if name is None:
        return None
    if not isinstance(name, str) or "/" in name or "\\" in name:
        raise ValueError("invalid backup name")
    path = backup_dir / name
    metadata = fs.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or getattr(metadata, "st_nlink", 1) != 1
        or getattr(metadata, "st_uid", os.geteuid()) != os.geteuid()
    ):
        raise ValueError("invalid backup file")
    value = fs.read(path)
    if _sha(value) != operation.get("backup_sha256"):
        raise ValueError("backup hash mismatch")
    return value


def _restore_operation(
    operation: Mapping[str, Any], home: Path, backup_dir: Path,
    txid: str, fs: Any,
) -> None:
    path = home / PurePosixPath(str(operation["path"]))
    actual = snapshot(path, fs)
    if operation["kind"] == "file":
        temporary = _temp_for(path, txid, int(operation["index"]))
        if fs.lexists(temporary):
            temporary_snapshot = snapshot(temporary, fs)
            temporary_metadata = fs.lstat(temporary)
            if (
                temporary_snapshot.file_type != "file"
                or getattr(temporary_metadata, "st_nlink", 1) != 1
                or getattr(temporary_metadata, "st_uid", os.geteuid()) != os.geteuid()
            ):
                raise ValueError("rollback temporary file changed externally")
            fs.unlink(temporary)
            fs.fsync(temporary.parent)
        old = operation["old"]
        old_matches = (
            actual.exists == bool(old["exists"])
            and actual.file_type == old["file_type"]
            and actual.mode == old["mode"]
            and actual.bytes_sha256 == old["sha256"]
            and actual.symlink_target == old["symlink_target"]
        )
        if old_matches:
            return
    if operation["kind"] == "directory":
        if operation["change"] == "create" and not operation["old_exists"] and not actual.exists:
            return
        if operation["change"] == "remove_if_empty" and operation["old_exists"] and actual.exists:
            if actual.file_type == "directory" and actual.mode == operation["old_mode"]:
                return
    if not _expected_after(operation, path, fs):
        raise ValueError("rollback target changed externally: %s" % operation["path"])
    if operation["kind"] == "directory":
        if operation["change"] == "create" and not operation["old_exists"]:
            try:
                fs.rmdir(path)
                fs.fsync(path.parent)
            except OSError as error:
                if error.errno not in (errno.ENOENT,):
                    raise
        elif operation["change"] == "remove_if_empty" and operation["old_exists"]:
            if not fs.lexists(path):
                fs.mkdir(path, int(operation["old_mode"]))
                fs.chmod(path, int(operation["old_mode"]))
                fs.fsync(path.parent)
        return
    old = operation["old"]
    old_exists = bool(old["exists"])
    if not old_exists:
        if fs.lexists(path):
            fs.unlink(path)
            fs.fsync(path.parent)
        return
    if old["file_type"] != "file":
        raise ValueError("unsupported rollback file type")
    content = _load_backup(operation, backup_dir, fs)
    if content is None:
        raise ValueError("rollback backup is missing")
    _write_replacement(
        path, content, int(old["mode"]), txid + ".rollback",
        int(operation["index"]), fs,
    )


def _remove_tree(path: Path, fs: Any) -> None:
    if not fs.lexists(path):
        return
    metadata = fs.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fs.unlink(path)
        fs.fsync(path.parent)
        return
    for name in sorted(fs.listdir(path)):
        if name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError("unsafe directory entry")
        _remove_tree(path / name, fs)
    fs.rmdir(path)
    fs.fsync(path.parent)


def _cleanup_transaction(journal_path: Path, backup_dir: Path, fs: Any) -> None:
    transaction_dir = journal_path.parent
    # The journal is the recovery anchor and is removed last.
    _remove_tree(backup_dir, fs)
    _remove_tree(transaction_dir, fs)


def rollback(applied: Any, journal: Any = None, fs: Any = REAL_FS) -> None:
    """Roll back a journal. The two-argument form remains compatible with the plan API."""
    if journal is None:
        journal_path = Path(applied)
        document = _load_journal_document(journal_path, fs)
    elif isinstance(journal, Mapping):
        journal_path = Path(applied)
        document = dict(journal)
    else:
        journal_path = Path(journal)
        document = _load_journal_document(journal_path, fs)
    home = Path(document["home"])
    state = home / PurePosixPath(document["state"])
    backup_dir = home / PurePosixPath(document["backup"])
    fs.in_rollback = True
    try:
        document["status"] = "rolling_back"
        _store_journal(journal_path, document, fs)
        applied_directories = []
        for operation in reversed(document["operations"]):
            if not operation.get("applied"):
                continue
            if operation.get("kind") == "directory":
                applied_directories.append(operation)
                continue
            _restore_operation(
                operation, home, backup_dir, document["transaction_id"], fs
            )
            operation["applied"] = False
            _store_journal(journal_path, document, fs)
        deferred_directories = []
        for operation in applied_directories:
            target = home / PurePosixPath(operation["path"])
            if target == state or target in state.parents:
                deferred_directories.append(operation)
                continue
            _restore_operation(
                operation, home, backup_dir, document["transaction_id"], fs
            )
            operation["applied"] = False
            _store_journal(journal_path, document, fs)
        _cleanup_transaction(journal_path, backup_dir, fs)
        for operation in deferred_directories:
            # Transaction infrastructure occupied these paths. The caller's
            # bootstrap cleanup removes only directories it actually created.
            operation["applied"] = False
    finally:
        fs.in_rollback = False


def _load_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal key")
        result[key] = value
    return result


def _journal_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("%s journal path is invalid" % label)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("%s journal path is invalid" % label)
    return path.as_posix()


def _journal_hash(value: Any, label: str, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("%s journal hash is invalid" % label)
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("%s journal hash is invalid" % label) from error
    return value


def _validate_journal_operation(value: Any, expected_index: int) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("journal operation is invalid")
    common = {"index", "kind", "path", "applied", "manifest"}
    if type(value.get("index")) is not int or value["index"] != expected_index:
        raise ValueError("journal operation index is invalid")
    if type(value.get("applied")) is not bool or type(value.get("manifest")) is not bool:
        raise ValueError("journal operation flags are invalid")
    value["path"] = _journal_relative(value.get("path"), "operation")
    if value.get("kind") == "directory":
        required = common | {"change", "old_exists", "old_mode", "new_mode"}
        if set(value) != required:
            raise ValueError("directory journal operation schema is invalid")
        if value["change"] not in ("create", "remove_if_empty"):
            raise ValueError("directory journal change is invalid")
        if type(value["old_exists"]) is not bool:
            raise ValueError("directory old_exists is invalid")
        for key in ("old_mode", "new_mode"):
            mode = value[key]
            if mode is not None and (type(mode) is not int or not 0 <= mode <= 0o7777):
                raise ValueError("directory journal mode is invalid")
        if value["manifest"]:
            raise ValueError("directory cannot be the manifest operation")
        return value
    required = common | {
        "old", "backup", "backup_sha256", "new_exists", "new_sha256", "new_mode",
    }
    if value.get("kind") != "file" or set(value) != required:
        raise ValueError("file journal operation schema is invalid")
    if type(value["new_exists"]) is not bool:
        raise ValueError("file new_exists is invalid")
    if value["new_mode"] is not None and (
        type(value["new_mode"]) is not int or not 0 <= value["new_mode"] <= 0o7777
    ):
        raise ValueError("file new_mode is invalid")
    _journal_hash(value["new_sha256"], "new", optional=not value["new_exists"])
    if value["new_exists"] and value["new_sha256"] is None:
        raise ValueError("new journal hash is missing")
    old = value["old"]
    old_keys = {"exists", "file_type", "mode", "sha256", "symlink_target"}
    if not isinstance(old, dict) or set(old) != old_keys or type(old["exists"]) is not bool:
        raise ValueError("old journal snapshot is invalid")
    if old["file_type"] not in ("file", "directory", "symlink", "special"):
        raise ValueError("old journal type is invalid")
    if old["mode"] is not None and (type(old["mode"]) is not int or not 0 <= old["mode"] <= 0o7777):
        raise ValueError("old journal mode is invalid")
    _journal_hash(old["sha256"], "old", optional=not old["exists"] or old["file_type"] != "file")
    backup = value["backup"]
    if backup is not None and (
        not isinstance(backup, str) or not backup.endswith(".bin")
        or PurePosixPath(backup).name != backup
    ):
        raise ValueError("backup name is invalid")
    _journal_hash(value["backup_sha256"], "backup", optional=backup is None)
    if old["exists"] and old["file_type"] == "file":
        if backup is None or value["backup_sha256"] != old["sha256"]:
            raise ValueError("file backup descriptor is invalid")
    elif backup is not None:
        raise ValueError("unexpected backup descriptor")
    return value


def _load_journal_document(
    path: Path, fs: Any, trusted_home: Optional[Path] = None
) -> Dict[str, Any]:
    path = Path(path)
    metadata = fs.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or getattr(metadata, "st_nlink", 1) != 1
        or getattr(metadata, "st_uid", os.geteuid()) != os.geteuid()
    ):
        raise ValueError("journal is not a private regular file")
    raw = fs.read(path)
    if len(raw) > 1024 * 1024:
        raise ValueError("journal is too large")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=_load_pairs)
    if not isinstance(document, dict):
        raise ValueError("journal must be an object")
    required = {
        "schema_version", "transaction_id", "status", "home", "state",
        "backup", "action", "infrastructure_created", "operations",
    }
    if set(document) != required or document.get("schema_version") != 1:
        raise ValueError("journal schema is invalid")
    txid = document.get("transaction_id")
    if (
        not isinstance(txid, str) or not 16 <= len(txid) <= 64
        or any(character not in "0123456789abcdef" for character in txid)
        or path.parent.name != txid
    ):
        raise ValueError("transaction id is invalid")
    if document.get("status") not in {
        "prepared", "applying", "validating", "manifest_applied",
        "committed", "rolling_back", "rollback_failed",
    }:
        raise ValueError("journal status is invalid")
    if document.get("action") not in ("install", "uninstall"):
        raise ValueError("journal action is invalid")
    home_value = document.get("home")
    if not isinstance(home_value, str):
        raise ValueError("journal HOME is invalid")
    home = Path(home_value)
    if not home.is_absolute() or ".." in home.parts:
        raise ValueError("journal HOME is invalid")
    if trusted_home is not None and home != Path(trusted_home):
        raise ValueError("journal HOME does not match trusted HOME")
    state_relative = _journal_relative(document.get("state"), "state")
    state = path.parent.parent.parent
    if home / PurePosixPath(state_relative) != state:
        raise ValueError("journal state path does not match its location")
    backup_relative = _journal_relative(document.get("backup"), "backup")
    expected_backup = state / "backups" / txid
    if home / PurePosixPath(backup_relative) != expected_backup:
        raise ValueError("journal backup path is invalid")
    infrastructure = document.get("infrastructure_created")
    if not isinstance(infrastructure, list) or len(infrastructure) > 256:
        raise ValueError("journal infrastructure list is invalid")
    seen_infrastructure = set()
    for item in infrastructure:
        relative = _journal_relative(item, "infrastructure")
        target = home / PurePosixPath(relative)
        if target == home or state not in target.parents and target != state and target not in state.parents:
            raise ValueError("journal infrastructure path is invalid")
        key = tuple(
            __import__("unicodedata").normalize("NFC", part).casefold()
            for part in PurePosixPath(relative).parts
        )
        if key in seen_infrastructure:
            raise ValueError("journal infrastructure collision")
        seen_infrastructure.add(key)
    if not fs.lexists(expected_backup):
        raise ValueError("journal backup directory is missing")
    backup_metadata = fs.lstat(expected_backup)
    if not stat.S_ISDIR(backup_metadata.st_mode) or stat.S_IMODE(backup_metadata.st_mode) != 0o700:
        raise ValueError("journal backup directory is not private")
    operations = document.get("operations")
    if not isinstance(operations, list) or len(operations) > 4096:
        raise ValueError("journal operations are invalid")
    physical = set()
    manifest_count = 0
    for index, operation in enumerate(operations):
        validated = _validate_journal_operation(operation, index)
        key = tuple(
            __import__("unicodedata").normalize("NFC", part).casefold()
            for part in PurePosixPath(validated["path"]).parts
        )
        if key in physical:
            raise ValueError("journal target collision")
        physical.add(key)
        manifest_count += int(validated["manifest"])
    if manifest_count > 1:
        raise ValueError("journal has multiple manifest operations")
    if document["status"] == "committed" and any(
        not operation["applied"] for operation in operations
    ):
        raise ValueError("committed journal has unapplied operations")
    return document

def recover_journal(
    journal_path: Path, fs: Any = REAL_FS, home: Optional[Path] = None
) -> None:
    document = _load_journal_document(journal_path, fs, trusted_home=home)
    home = Path(document["home"])
    backup_dir = home / PurePosixPath(document["backup"])
    if document["status"] == "committed":
        # COMMITTED is durable and authoritative. Cleanup must never touch or
        # gate on targets, which may have legitimate later user edits.
        _cleanup_transaction(Path(journal_path), backup_dir, fs)
        for relative in reversed(document["infrastructure_created"]):
            target = home / PurePosixPath(relative)
            try:
                fs.rmdir(target)
                fs.fsync(target.parent)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    raise
        return
    rollback(journal_path, document, fs)


def _validate_mutation_result(mutation: FileMutation, label: str, fs: Any) -> None:
    actual = snapshot(Path(mutation.path), fs)
    if mutation.new_content is None:
        if actual.exists:
            raise ValueError("committed %s deletion validation failed" % label)
    elif (
        not actual.exists or actual.file_type != "file"
        or actual.bytes_sha256 != _sha(mutation.new_content)
        or actual.mode != mutation.new_mode
    ):
        raise ValueError("committed %s validation failed" % label)


def _validate_non_manifest(plan: InstallPlan, fs: Any) -> None:
    for mutation in plan.directory_mutations:
        actual = snapshot(Path(mutation.path), fs)
        if mutation.change == "create":
            if not actual.exists or actual.file_type != "directory" or actual.mode != mutation.new_mode:
                raise ValueError("committed directory validation failed")
        elif mutation.change == "remove_if_empty":
            # Empty-directory cleanup runs after the manifest commit.
            continue
    for mutation in plan.mutations:
        _validate_mutation_result(mutation, "file", fs)
        if mutation.new_content is None:
            continue
        path = Path(mutation.path)
        if path.suffix == ".json":
            parse_json_document(mutation.new_content, str(path))
        elif path.suffix == ".toml":
            raise ValueError("unexpected TOML transaction target")
        if path.name in ("AGENTS.md", "AGENTS.override.md"):
            begin = b"<!-- BEGIN KOROCHE-BLYAT MANAGED: codex-always-on v1 -->"
            end = b"<!-- END KOROCHE-BLYAT MANAGED: codex-always-on v1 -->"
            begin_count = mutation.new_content.count(begin)
            end_count = mutation.new_content.count(end)
            if begin_count != end_count or begin_count > 1:
                raise ValueError("committed marker validation failed")
            if begin_count == 1 and mutation.new_content.index(begin) >= mutation.new_content.index(end):
                raise ValueError("committed marker order validation failed")


def validate_committed(plan: InstallPlan, fs: Any = REAL_FS) -> None:
    try:
        _validate_non_manifest(plan, fs)
        if plan.manifest_mutation is not None:
            _validate_mutation_result(plan.manifest_mutation, "manifest", fs)
        for mutation in plan.directory_mutations:
            if mutation.change != "remove_if_empty":
                continue
            actual = snapshot(Path(mutation.path), fs)
            if actual.exists and actual.file_type != "directory":
                raise ValueError("committed directory validation failed")
        fs.validate(plan)
    except TransactionFailure:
        raise
    except (OSError, ValueError) as error:
        raise TransactionFailure("committed validation failed: %s" % error) from error

def _cleanup_bootstrap(created: Iterable[Path], fs: Any) -> None:
    for path in sorted(created, key=lambda item: len(item.parts), reverse=True):
        try:
            fs.rmdir(path)
            fs.fsync(path.parent)
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                raise




def _cleanup_unjournaled(
    journal_path: Optional[Path], backup_dir: Optional[Path], fs: Any
) -> None:
    if journal_path is not None:
        _remove_tree(journal_path.parent, fs)
    if backup_dir is not None:
        _remove_tree(backup_dir, fs)

def _retry_remove_if_empty(plan: InstallPlan, fs: Any) -> None:
    for mutation in plan.directory_mutations:
        if mutation.change != "remove_if_empty":
            continue
        path = Path(mutation.path)
        if not fs.lexists(path):
            continue
        try:
            fs.rmdir(path)
            fs.fsync(path.parent)
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                raise

def execute_transaction(
    plan: InstallPlan, fs: Any = REAL_FS, lock: Optional[int] = None
) -> None:
    home = Path(plan.paths.get("home", ""))
    owned_lock = lock is None
    bootstrap: List[Path] = []
    journal_path: Optional[Path] = None
    backup_dir: Optional[Path] = None
    document: Optional[Dict[str, Any]] = None
    preflight_complete = False
    if lock is None:
        lock = fs.open_lock(home)
    try:
        from .journal import recover_pending
        recover_pending(Path(plan.state_dir), fs=fs, home=home)
        _validate_plan(plan, fs)
        _validate_preimages(plan, fs)
        preflight_complete = True
        if (
            not plan.mutations and plan.manifest_mutation is None
            and not plan.directory_mutations
        ):
            return
        bootstrap = _ensure_state_infrastructure(plan, fs)
        txid = secrets.token_hex(16)
        state = Path(plan.state_dir)
        journal_path = state / "transactions" / txid / "journal.json"
        backup_dir = state / "backups" / txid
        journal_path, backup_dir, document = _prepare_journal(
            plan, txid, fs, infrastructure_created=bootstrap
        )
        document["status"] = "applying"
        _store_journal(journal_path, document, fs)
        mutation_map = _mutation_map(plan)
        manifest_operation = None
        after_manifest = []
        seen_manifest = False
        for operation in document["operations"]:
            if operation.get("manifest"):
                manifest_operation = operation
                seen_manifest = True
                continue
            if seen_manifest:
                after_manifest.append(operation)
                continue
            mutation = mutation_map[operation["path"]]
            _verify_operation_preimage(operation, mutation, txid, fs)
            operation["applied"] = True
            _store_journal(journal_path, document, fs)
            _apply_operation(operation, mutation, home, txid, fs)
        document["status"] = "validating"
        _store_journal(journal_path, document, fs)
        _validate_non_manifest(plan, fs)
        fs.validate(plan)
        if manifest_operation is not None:
            mutation = mutation_map[manifest_operation["path"]]
            _verify_operation_preimage(manifest_operation, mutation, txid, fs)
            manifest_operation["applied"] = True
            _store_journal(journal_path, document, fs)
            _apply_operation(manifest_operation, mutation, home, txid, fs)
            document["status"] = "manifest_applied"
            _store_journal(journal_path, document, fs)
            _validate_mutation_result(mutation, "manifest", fs)
        for operation in after_manifest:
            mutation = mutation_map[operation["path"]]
            _verify_operation_preimage(operation, mutation, txid, fs)
            operation["applied"] = True
            _store_journal(journal_path, document, fs)
            _apply_operation(operation, mutation, home, txid, fs)
        document["status"] = "committed"
        _store_journal(journal_path, document, fs)
        try:
            _cleanup_transaction(journal_path, backup_dir, fs)
            _cleanup_bootstrap(bootstrap, fs)
            _retry_remove_if_empty(plan, fs)
        except BaseException:
            # The durable committed marker is the linearization point. A
            # cleanup failure must never roll back the committed postimage.
            try:
                if not fs.lexists(journal_path):
                    _remove_tree(backup_dir, fs)
            except BaseException:
                pass
            return
    except TransactionFailure as error:
        if journal_path is None or backup_dir is None or document is None:
            try:
                _cleanup_unjournaled(journal_path, backup_dir, fs)
                _cleanup_bootstrap(bootstrap, fs)
            except BaseException:
                pass
            raise
        try:
            rollback(journal_path, document, fs)
            _cleanup_bootstrap(bootstrap, fs)
        except BaseException as rollback_error:
            try:
                document["status"] = "rollback_failed"
                _store_journal(journal_path, document, fs)
            except BaseException:
                pass
            raise RollbackFailure(
                "rollback failed: %s" % rollback_error,
                journal_path=str(journal_path), backup_path=str(backup_dir),
            ) from error
        raise TransactionFailure(
            str(error), journal_path=str(journal_path),
            backup_path=str(backup_dir), rollback_failed=False,
            preflight=error.preflight,
        ) from error
    except BaseException as error:
        if journal_path is None or backup_dir is None or document is None:
            try:
                _cleanup_unjournaled(journal_path, backup_dir, fs)
                _cleanup_bootstrap(bootstrap, fs)
            except BaseException:
                pass
            raise TransactionFailure(
                str(error), preflight=not preflight_complete
            ) from error
        try:
            rollback(journal_path, document, fs)
            _cleanup_bootstrap(bootstrap, fs)
        except BaseException as rollback_error:
            try:
                document["status"] = "rollback_failed"
                _store_journal(journal_path, document, fs)
            except BaseException:
                pass
            raise RollbackFailure(
                "rollback failed: %s" % rollback_error,
                journal_path=str(journal_path), backup_path=str(backup_dir),
            ) from error
        raise TransactionFailure(
            str(error), journal_path=str(journal_path),
            backup_path=str(backup_dir), rollback_failed=False,
        ) from error
    finally:
        if owned_lock and lock is not None:
            fs.close_lock(lock)
