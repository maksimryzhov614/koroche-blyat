"""Offline repository validation — Task 13.

Everything here is offline and deterministic: the validator calls module APIs
directly and never shells out to pytest, the network, or a live model. Output
carries no timestamp so two runs on the same tree are byte-identical.

Runtime target is Python 3.9, so annotations use typing.Optional/Tuple/List
rather than the 3.10 union syntax.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

CHECKS = (
    "encoding",
    "skill-frontmatter",
    "links",
    "placeholders",
    "generated-parity",
    "eval-fixtures",
    "package",
    "provenance",
    "docs-claims",
)

SKILL_DIR = "skills/koroche-blyat"
GENERATED_DIR = "adapters/generated"
ALLOWLIST = "release/PACKAGE_FILES.txt"

# Authored sources only. Raw captured evidence under evals/baselines and
# evals/snapshots is model output, not something this repository authors, so
# byte-level style rules do not apply to it.
TEXT_ROOTS = (
    "skills", "adapters", "scripts", "tests", "release",
    "evals/cases", "evals/goldens", "evals/schemas",
)
TEXT_FILES = (
    "VERSION", "LICENSE", "NOTICE.md", "UPSTREAMS.yml", "install.sh",
    "pyproject.toml", "evals/arms.yaml", "evals/schema.py", "evals/grade.py",
    "evals/run_control.py", "evals/__init__.py",
)
TEXT_SUFFIXES = (".md", ".py", ".yaml", ".yml", ".json", ".jsonl", ".txt", ".sh", ".ts", ".mjs", ".toml")

# Public documents only. docs/superpowers holds internal planning material and
# is deliberately out of scope: it discusses claims rather than making them.
PUBLIC_DOCS = (
    "README.md", "CHANGELOG.md", "docs/INSTALL.md",
    "docs/COMPATIBILITY.md", "docs/UPDATING.md", "docs/RELEASE-CHECKLIST.md",
)

PLACEHOLDER_PATTERNS = (
    (r"\bTODO\b", "TODO"),
    (r"\bFIXME\b", "FIXME"),
    (r"\bTBD\b", "TBD"),
    (r"\bXXX\b", "XXX"),
    (r"<placeholder>", "<placeholder>"),
    (r"(?i)lorem ipsum", "lorem ipsum"),
)
PLACEHOLDER_ROOTS = ("skills", "adapters", "docs")

CLAIM_PATTERNS = (
    (r"(?i)\d{1,3}\s*%\s*(?:меньше|экономи|reduction|fewer|less)", "unsupported token-reduction claim"),
    (r"(?i)(?:экономи\w*|сокраща\w*|saves?|reduces?)\D{0,24}\d{1,3}\s*%", "unsupported token-reduction claim"),
    (r"(?i)\bperfect\b", "unsupported perfection claim"),
    (r"(?i)\bguaranteed\b", "unsupported guarantee claim"),
    (r"(?i)100\s*%\s*accurate", "unsupported accuracy claim"),
)
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


@dataclass(frozen=True)
class Violation:
    check: str
    path: str
    line: int
    message: str


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_text(path: Path) -> Optional[str]:
    """Decode a file, or return None if it is not valid UTF-8.

    A file that fails the encoding check must not crash the checks that run
    after it: the encoding check already reports the problem, and the
    validator's contract is to report violations rather than raise.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _iter_text_files(root: Path) -> Iterable[Path]:
    seen = set()
    for name in TEXT_FILES:
        candidate = root / name
        if candidate.is_file():
            seen.add(candidate)
    for folder in TEXT_ROOTS:
        base = root / folder
        if not base.is_dir():
            continue
        for candidate in base.rglob("*"):
            if not candidate.is_file():
                continue
            if any(part in {"__pycache__", ".pytest_cache"} for part in candidate.parts):
                continue
            # tests/fixtures holds deliberately malformed inputs — CRLF, a
            # missing final newline — that the patch primitives must preserve
            # byte-for-byte. Normalizing them would destroy the tests.
            if candidate.match("tests/fixtures/*") or "fixtures" in candidate.relative_to(root).parts:
                continue
            if candidate.suffix in TEXT_SUFFIXES:
                seen.add(candidate)
    return sorted(seen)


def check_encoding(root: Path) -> List[Violation]:
    violations = []
    for path in _iter_text_files(root):
        relative = _relative(path, root)
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            violations.append(Violation("encoding", relative, 1, "file starts with a UTF-8 BOM"))
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            violations.append(
                Violation("encoding", relative, 1, "file is not valid UTF-8: %s" % error.reason)
            )
            continue
        if b"\x00" in raw:
            violations.append(Violation("encoding", relative, 1, "file contains a NUL byte"))
        if b"\r\n" in raw:
            violations.append(Violation("encoding", relative, 1, "file uses CRLF line endings"))
        if raw and not raw.endswith(b"\n"):
            violations.append(Violation("encoding", relative, 1, "file has no final newline"))
    return violations


def _frontmatter(text: str) -> Optional[str]:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    return text[4:end + 1]


def check_skill_frontmatter(root: Path) -> List[Violation]:
    violations = []
    skills = root / "skills"
    if not skills.is_dir():
        return violations
    for directory in sorted(p for p in skills.iterdir() if p.is_dir()):
        path = directory / "SKILL.md"
        relative = _relative(path, root)
        if not path.is_file():
            violations.append(Violation("skill-frontmatter", _relative(directory, root), 1, "SKILL.md is missing"))
            continue
        text = _read_text(path)
        if text is None:
            continue
        block = _frontmatter(text)
        if block is None:
            violations.append(Violation("skill-frontmatter", relative, 1, "YAML frontmatter is missing"))
            continue
        if len(block) >= 1024:
            violations.append(
                Violation("skill-frontmatter", relative, 1,
                          "frontmatter is %d characters; the limit is 1024" % len(block))
            )
        fields = {}
        for line in block.splitlines():
            if line[:1] not in {" ", "-", ""} and ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        for required in ("name", "description", "license", "compatibility"):
            if required not in fields:
                violations.append(
                    Violation("skill-frontmatter", relative, 1, "frontmatter is missing %s" % required)
                )
        name = fields.get("name")
        if name is not None and name != directory.name:
            violations.append(
                Violation("skill-frontmatter", relative, 1,
                          "frontmatter name %r does not equal directory name %r" % (name, directory.name))
            )
    return violations


def check_links(root: Path) -> List[Violation]:
    violations = []
    tree = root / SKILL_DIR
    if not tree.is_dir():
        return violations
    resolved_tree = tree.resolve()
    for path in sorted(tree.rglob("*.md")):
        relative = _relative(path, root)
        text = _read_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for target in MARKDOWN_LINK.findall(line):
                target = target.split("#", 1)[0].strip()
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                candidate = (path.parent / target).resolve()
                try:
                    candidate.relative_to(resolved_tree)
                except ValueError:
                    violations.append(
                        Violation("links", relative, number,
                                  "link %r resolves outside the skill tree" % target)
                    )
                    continue
                if not candidate.exists():
                    violations.append(
                        Violation("links", relative, number, "link %r does not resolve" % target)
                    )
    return violations


def check_placeholders(root: Path) -> List[Violation]:
    violations = []
    for folder in PLACEHOLDER_ROOTS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".txt", ".ts", ".sh"}:
                continue
            if "superpowers" in path.parts:
                continue
            relative = _relative(path, root)
            text = _read_text(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for pattern, label in PLACEHOLDER_PATTERNS:
                    if re.search(pattern, line):
                        violations.append(
                            Violation("placeholders", relative, number, "placeholder token %s" % label)
                        )
    return violations


def check_generated_parity(root: Path) -> List[Violation]:
    from scripts.generate_adapters import generate_from_bytes

    source = root / SKILL_DIR / "SKILL.md"
    if not source.is_file():
        return [Violation("generated-parity", SKILL_DIR + "/SKILL.md", 1, "canonical source is missing")]
    try:
        generated = generate_from_bytes(source.read_bytes())
    except Exception as error:  # noqa: BLE001 - reported, never raised
        return [Violation("generated-parity", SKILL_DIR + "/SKILL.md", 1, "cannot generate: %s" % error)]
    violations = []
    for relative, expected in sorted(generated.files.items()):
        # generate_from_bytes already keys its output by repository-relative
        # path, so prefixing GENERATED_DIR here would double it.
        path = root / relative
        if not path.is_file():
            violations.append(Violation("generated-parity", relative, 1, "generated file is missing"))
            continue
        if path.read_bytes() != expected:
            violations.append(
                Violation("generated-parity", relative, 1,
                          "generated file does not match the canonical source")
            )
    return violations


def check_eval_fixtures(root: Path) -> List[Violation]:
    from evals.schema import SchemaError, load_cases, load_goldens, validate_fixture_matrix

    cases_dir = root / "evals/cases"
    goldens_dir = root / "evals/goldens"
    if not cases_dir.is_dir() or not goldens_dir.is_dir():
        return []
    case_files = tuple(sorted(cases_dir.glob("*.yaml")))
    golden_files = tuple(
        sorted(path for path in goldens_dir.glob("*.yaml") if path.name != "lexicon.yaml")
    )
    try:
        cases = load_cases(case_files)
        goldens = load_goldens(golden_files)
        validate_fixture_matrix(cases, goldens)
    except SchemaError as error:
        return [Violation("eval-fixtures", "evals", 1, str(error))]
    return []


def check_package(root: Path) -> List[Violation]:
    path = root / ALLOWLIST
    if not path.is_file():
        return [Violation("package", ALLOWLIST, 1, "package allowlist is missing")]
    violations = []
    entries = []
    listing = _read_text(path)
    if listing is None:
        return [Violation("package", ALLOWLIST, 1, "package allowlist is not valid UTF-8")]
    for number, line in enumerate(listing.splitlines(), start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        entries.append(entry)
        if any(character in entry for character in "*?[]"):
            violations.append(Violation("package", ALLOWLIST, number, "entry %r uses a glob" % entry))
            continue
        if entry.startswith("/") or ".." in Path(entry).parts:
            violations.append(Violation("package", ALLOWLIST, number, "entry %r is not a safe relative path" % entry))
            continue
        if not (root / entry).is_file():
            violations.append(Violation("package", ALLOWLIST, number, "entry %r does not exist" % entry))
    if entries != sorted(entries):
        violations.append(Violation("package", ALLOWLIST, 1, "entries are not sorted"))
    if len(entries) != len(set(entries)):
        violations.append(Violation("package", ALLOWLIST, 1, "entries contain duplicates"))
    return violations


def check_provenance(root: Path) -> List[Violation]:
    from scripts.check_upstreams import check_offline, load_manifest

    path = root / "UPSTREAMS.yml"
    if not path.is_file():
        return [Violation("provenance", "UPSTREAMS.yml", 1, "provenance manifest is missing")]
    try:
        manifest = load_manifest(path)
        problems = check_offline(root, manifest)
    except Exception as error:  # noqa: BLE001 - reported, never raised
        return [Violation("provenance", "UPSTREAMS.yml", 1, str(error))]
    return [Violation("provenance", "UPSTREAMS.yml", 1, problem) for problem in problems]


def check_docs_claims(root: Path) -> List[Violation]:
    # An absent public document is not a violation: Task 13 runs this check
    # before Task 14 writes README.md and CHANGELOG.md.
    violations = []
    for name in PUBLIC_DOCS:
        path = root / name
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, message in CLAIM_PATTERNS:
                if re.search(pattern, line):
                    violations.append(Violation("docs-claims", name, number, message))
    return violations


_IMPLEMENTATIONS: Dict[str, Callable[[Path], List[Violation]]] = {
    "encoding": check_encoding,
    "skill-frontmatter": check_skill_frontmatter,
    "links": check_links,
    "placeholders": check_placeholders,
    "generated-parity": check_generated_parity,
    "eval-fixtures": check_eval_fixtures,
    "package": check_package,
    "provenance": check_provenance,
    "docs-claims": check_docs_claims,
}


def validate_repo(root: Path, selected: Optional[Sequence[str]] = None) -> Tuple[Violation, ...]:
    names = tuple(CHECKS) if selected is None else tuple(selected)
    unknown = [name for name in names if name not in _IMPLEMENTATIONS]
    if unknown:
        raise ValueError("unknown check: %s" % ", ".join(sorted(unknown)))
    root = Path(root)
    violations: List[Violation] = []
    for name in names:
        violations.extend(_IMPLEMENTATIONS[name](root))
    return tuple(sorted(violations, key=lambda item: (item.check, item.path, item.line, item.message)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.validate", description="Validate the repository offline.")
    parser.add_argument("--check", action="append", dest="checks", default=None)
    parser.add_argument("--root", default=None)
    try:
        options = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit:
        return 2
    root = Path(options.root) if options.root else Path(__file__).resolve().parents[1]
    try:
        violations = validate_repo(root, options.checks)
    except ValueError as error:
        sys.stderr.write("error: %s\n" % error)
        return 2
    for violation in violations:
        sys.stdout.write(
            "%s\t%s:%d\t%s\n" % (violation.check, violation.path, violation.line, violation.message)
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
