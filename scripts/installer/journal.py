from __future__ import annotations

from pathlib import Path
import stat
import os
from typing import Any, List


def _private_directory(path: Path, fs: Any) -> bool:
    try:
        metadata = fs.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and getattr(metadata, "st_uid", os.geteuid()) == os.geteuid()
    )


def pending_journals(state_dir: Path, fs: Any) -> List[Path]:
    state_dir = Path(state_dir)
    if not fs.lexists(state_dir):
        return []
    transactions = state_dir / "transactions"
    if not fs.lexists(transactions):
        return []
    if not _private_directory(transactions, fs):
        raise ValueError("transactions directory is not private")
    result = []
    for name in sorted(fs.listdir(transactions)):
        if name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError("unsafe transaction directory entry")
        directory = transactions / name
        if not _private_directory(directory, fs):
            raise ValueError("transaction entry is not a private directory")
        journal = directory / "journal.json"
        if not fs.lexists(journal):
            if (
                not 16 <= len(name) <= 64
                or any(character not in "0123456789abcdef" for character in name)
            ):
                raise ValueError("transaction journal is missing")
            backup = state_dir / "backups" / name
            if fs.lexists(backup) and not _private_directory(backup, fs):
                raise ValueError("orphan backup is not private")
            from .transaction import _remove_tree
            if fs.lexists(backup):
                _remove_tree(backup, fs)
            _remove_tree(directory, fs)
            continue
        result.append(journal)
    return result


def recover_pending(state_dir: Path, fs=None, home: Path = None) -> None:
    if fs is None:
        from .transaction import REAL_FS
        fs = REAL_FS
    from .transaction import recover_journal
    for journal in pending_journals(Path(state_dir), fs):
        recover_journal(journal, fs=fs, home=home)
