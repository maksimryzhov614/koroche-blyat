from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


BEGIN = b"<!-- ALWAYS_ON_CORE:BEGIN -->"
REMINDER_BEGIN = b"<!-- ALWAYS_ON_REMINDER:BEGIN -->"
REMINDER_END = b"<!-- ALWAYS_ON_REMINDER:END -->"
END = b"<!-- ALWAYS_ON_CORE:END -->"
MARKERS = (BEGIN, REMINDER_BEGIN, REMINDER_END, END)


@dataclass(frozen=True)
class CanonicalPolicy:
    source_sha256: str
    core: str
    reminder: str


@dataclass(frozen=True)
class GeneratedAdapters:
    source_sha256: str
    files: Mapping[str, bytes]


def _marker_lines(source: bytes) -> list[bytes]:
    if source.startswith(b"\xef\xbb\xbf"):
        raise ValueError("UTF-8 BOM is not allowed")
    if b"\r" in source:
        raise ValueError("CRLF and carriage returns are not allowed")
    try:
        source.decode("utf-8")
    except UnicodeDecodeError:
        raise
    lines = source.splitlines(keepends=True)
    if b"".join(lines) != source or any(not line.endswith(b"\n") for line in lines):
        raise ValueError("source must use LF-terminated lines")
    contents = [line[:-1] for line in lines]
    positions: dict[bytes, list[int]] = {marker: [] for marker in MARKERS}
    for index, line in enumerate(contents):
        for marker in MARKERS:
            if line == marker:
                positions[marker].append(index)
            elif marker in line:
                raise ValueError("marker must occupy its complete line")
    if any(len(positions[marker]) != 1 for marker in MARKERS):
        raise ValueError("each marker must occur exactly once")
    begin, reminder_begin, reminder_end, end = (positions[m][0] for m in MARKERS)
    if not begin < reminder_begin < reminder_end < end:
        raise ValueError("markers are not correctly nested")
    return contents


def parse_canonical_source(source: bytes) -> CanonicalPolicy:
    if not isinstance(source, bytes):
        raise TypeError("source must be bytes")
    lines = _marker_lines(source)
    positions = {marker: next(i for i, line in enumerate(lines) if line == marker)
                 for marker in MARKERS}
    reminder_lines = lines[positions[REMINDER_BEGIN] + 1:positions[REMINDER_END]]
    if len(reminder_lines) != 1 or not reminder_lines[0].strip():
        raise ValueError("reminder must be exactly one non-empty line")
    # Removing marker lines from the complete outer block preserves every authored byte.
    raw_lines = source.splitlines(keepends=True)
    core_lines = [line for i, line in enumerate(raw_lines)
                  if positions[BEGIN] < i < positions[END]
                  and line[:-1] not in MARKERS]
    core = b"".join(core_lines).decode("utf-8")
    reminder = reminder_lines[0].decode("utf-8")
    return CanonicalPolicy(hashlib.sha256(source).hexdigest(), core, reminder)


def generate_from_bytes(source: bytes) -> GeneratedAdapters:
    policy = parse_canonical_source(source)
    digest = policy.source_sha256.encode("ascii")
    core = policy.core.encode("utf-8")
    metadata = b"canonical-sha256: " + digest + b"\n\n"
    files = {
        "adapters/generated/always-on.md": metadata + core,
        "adapters/generated/reminder.txt": metadata + policy.reminder.encode("utf-8") + b"\n",
        "adapters/generated/claude-output-style.md": (
            b"---\n"
            b"name: koroche-blyat\n"
            b"description: "+"Краткий русский инженерный стиль с точной технической передачей и чистыми артефактами".encode("utf-8")+b"\n"
            b"keep-coding-instructions: true\n"
            b"---\n" + metadata + core
        ),
    }
    return GeneratedAdapters(policy.source_sha256, files)


def _replace_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".%s." % path.name, suffix=".tmp",
            dir=str(path.parent), delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(data)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _write_or_check(generated: GeneratedAdapters, output_dir: Path, check: bool) -> int:
    stale = False
    for key, data in generated.files.items():
        path = output_dir / Path(key).name
        matches = path.is_file() and not path.is_symlink() and path.read_bytes() == data
        if not matches:
            stale = True
            if not check:
                _replace_file(path, data)
    return 1 if stale and check else 0


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source", type=Path, default=root / "skills/koroche-blyat/SKILL.md")
    parser.add_argument("--output-dir", type=Path, default=root / "adapters/generated")
    args = parser.parse_args(argv)
    try:
        generated = generate_from_bytes(args.source.read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        parser.error(str(exc))
    return _write_or_check(generated, args.output_dir, args.check)


if __name__ == "__main__":
    raise SystemExit(main())
