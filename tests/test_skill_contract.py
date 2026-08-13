from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / "skills" / "koroche-blyat"
SKILL = SKILL_DIR / "SKILL.md"
BEGIN = "<!-- ALWAYS_ON_CORE:BEGIN -->"
REMINDER_BEGIN = "<!-- ALWAYS_ON_REMINDER:BEGIN -->"
REMINDER_END = "<!-- ALWAYS_ON_REMINDER:END -->"
END = "<!-- ALWAYS_ON_CORE:END -->"
REMINDER = "Контракт koroche-blyat остаётся активен: соблюдай приоритеты, защищённые фрагменты, Auto-Clarity, чистые артефакты и краткий естественный русский инженерный тон."
REQUIRED_REFERENCES = ("compression.md", "slovar.md", "sceny.md", "ontologia.md")
EXPECTED_FRONTMATTER = {
    "name": "koroche-blyat",
    "description": "Use when producing any response after installation, especially concise Russian technical chat, debugging, review, operations, and incident work where idiomatic engineering humor is appropriate.",
    "license": "MIT; see LICENSE.txt and NOTICE.md",
    "compatibility": "Always-on adapters target Prime Agent 0.7.1+, Codex CLI 0.147.0+, and Claude Code 2.1.197+.",
    "metadata": {"version": "1.0.0"},
}
_LINK_RE = re.compile(r"\[[^]]+\]\(([^)]+)\)")
_PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|TBD|FIXME)\b|"
    r"\[(?:complete self-contained|insert|placeholder)[^]]*\]|"
    r"\{\{[^}]+\}\}",
    re.IGNORECASE,
)


def _source() -> bytes:
    assert SKILL.is_file(), "canonical skill is missing: %s" % SKILL
    return SKILL.read_bytes()


def _sources() -> tuple[Path, ...]:
    return (SKILL,) + tuple(SKILL_DIR / "references" / name for name in REQUIRED_REFERENCES)


def _frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    assert end >= 0
    complete = text[: end + len("\n---")]
    assert len(complete) < 1024
    raw = text[4:end]
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed, text[end + len("\n---\n"):]


def _relative_link(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith("#"):
        return None
    path_text = unquote(parsed.path)
    assert path_text, (source, raw_target)
    assert not Path(path_text).is_absolute(), (source, raw_target)
    resolved = (source.parent / path_text).resolve()
    assert resolved.is_relative_to(SKILL_DIR.resolve()), (source, raw_target)
    return resolved


def test_frontmatter_matches_agentskills_contract():
    text = _source().decode("utf-8")
    frontmatter, _ = _frontmatter(text)
    assert frontmatter == EXPECTED_FRONTMATTER
    assert SKILL_DIR.name == frontmatter["name"]


def test_skill_sources_are_utf8_lf_without_bom():
    for path in _sources():
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), path
        data.decode("utf-8")
        assert b"\r" not in data, path


def test_always_on_core_and_reminder_markers_are_unique_and_ordered():
    text = _source().decode("utf-8")
    markers = (BEGIN, REMINDER_BEGIN, REMINDER_END, END)
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions)
    for marker in markers:
        assert text.count(marker) == 1
        assert ("\n" + marker + "\n") in text
    reminder_payload = text.split(REMINDER_BEGIN + "\n", 1)[1].split("\n" + REMINDER_END, 1)[0]
    assert reminder_payload == REMINDER
    assert "[complete self-contained core policy" not in text


def test_required_progressive_disclosure_references_exist():
    references = SKILL_DIR / "references"
    assert references.is_dir()
    for name in REQUIRED_REFERENCES:
        path = references / name
        assert path.is_file(), path
        assert path.stat().st_size > 0


def test_all_relative_links_resolve_inside_skill_tree():
    linked_from_skill = set()
    for source in _sources():
        text = source.read_text(encoding="utf-8")
        for raw_target in _LINK_RE.findall(text):
            resolved = _relative_link(source, raw_target)
            if resolved is None:
                continue
            assert resolved.is_file(), (source, raw_target)
            if source == SKILL:
                linked_from_skill.add(resolved)
    expected = {SKILL_DIR / "references" / name for name in REQUIRED_REFERENCES}
    assert expected <= linked_from_skill


def test_skill_sources_have_no_placeholders():
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        assert not _PLACEHOLDER_RE.search(text), path


def test_references_do_not_define_always_on_markers():
    references = SKILL_DIR / "references"
    assert references.is_dir()
    for name in REQUIRED_REFERENCES:
        text = (references / name).read_text(encoding="utf-8")
        assert "ALWAYS_ON_CORE" not in text
        assert "ALWAYS_ON_REMINDER" not in text
