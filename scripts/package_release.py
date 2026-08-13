"""Deterministic release archives — Task 13.

Two builds of the same tree under the same SOURCE_DATE_EPOCH produce
byte-identical archives. That requires removing every ambient input: member
order comes from the sorted allowlist, uid/gid are zero, owner names are empty,
mtime comes from the epoch, and the gzip header carries neither a filename nor
a timestamp of its own.

The allowlist is explicit and globless on purpose. Shipping a new file has to
be a deliberate edit; a wildcard would let caches, media, or developer-only
paths reach a release unnoticed.

Runtime target is Python 3.9 and the standard library only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

ALLOWLIST = "release/PACKAGE_FILES.txt"
EXECUTABLE_FILES = frozenset({
    "install.sh",
    "adapters/codex/user-prompt-reminder.sh",
    "adapters/claude/user-prompt-reminder.sh",
})
DEFAULT_EPOCH = 1786500000


def load_allowlist(root: Path) -> Tuple[str, ...]:
    path = Path(root) / ALLOWLIST
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            entries.append(entry)
    return tuple(entries)


def _mode_for(entry: str) -> int:
    return 0o755 if entry in EXECUTABLE_FILES else 0o644


def _resolve_epoch(epoch: Optional[int]) -> int:
    if epoch is not None:
        return int(epoch)
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    return int(raw) if raw else DEFAULT_EPOCH


def _collect(root: Path, entries: Sequence[str]) -> Dict[str, bytes]:
    payload = {}
    for entry in entries:
        path = root / entry
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("allowlisted path is missing or not a regular file: %s" % entry)
        payload[entry] = path.read_bytes()
    return payload


def _write_tar(target: Path, prefix: str, payload: Dict[str, bytes], epoch: int) -> None:
    import io

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in sorted(payload):
            data = payload[entry]
            info = tarfile.TarInfo("%s/%s" % (prefix, entry))
            info.size = len(data)
            info.mtime = epoch
            info.mode = _mode_for(entry)
            info.type = tarfile.REGTYPE
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    # mtime=0 and an empty filename keep the gzip header free of ambient state.
    with open(str(target), "wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as compressor:
            compressor.write(buffer.getvalue())


def _write_zip(target: Path, prefix: str, payload: Dict[str, bytes], epoch: int) -> None:
    stamp = time.gmtime(epoch)[:6]
    with zipfile.ZipFile(str(target), "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(payload):
            info = zipfile.ZipInfo("%s/%s" % (prefix, entry), date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so the mode below is meaningful
            info.external_attr = (_mode_for(entry) & 0xFFFF) << 16
            archive.writestr(info, payload[entry])


def build_release(
    root: Path, version: str, output_dir: Path, epoch: Optional[int] = None
) -> Dict[str, Path]:
    root = Path(root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved = _resolve_epoch(epoch)
    prefix = "koroche-blyat-%s" % version
    payload = _collect(root, load_allowlist(root))

    tar_path = output_dir / ("%s.tar.gz" % prefix)
    zip_path = output_dir / ("%s.zip" % prefix)
    sums_path = output_dir / "SHA256SUMS"
    _write_tar(tar_path, prefix, payload, resolved)
    _write_zip(zip_path, prefix, payload, resolved)

    lines = []
    for path in sorted((tar_path, zip_path), key=lambda item: item.name):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append("%s  %s" % (digest, path.name))
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"tar": tar_path, "zip": zip_path, "sums": sums_path}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.package_release")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--epoch", type=int, default=None)
    try:
        options = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit:
        return 2
    root = Path(__file__).resolve().parents[1]
    declared = (root / "VERSION").read_text(encoding="utf-8").strip()
    if options.version != declared:
        sys.stderr.write(
            "error: --version %s does not match VERSION %s\n" % (options.version, declared)
        )
        return 2
    try:
        built = build_release(root, options.version, Path(options.output_dir), options.epoch)
    except (OSError, ValueError) as error:
        sys.stderr.write("error: %s\n" % error)
        return 2
    for key in ("tar", "zip", "sums"):
        sys.stdout.write("%s\n" % built[key].name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
