"""Strict versioned fixture schemas for deterministic behavior evaluation."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

import yaml
from jsonschema import Draft202012Validator
from yaml.tokens import AliasToken, AnchorToken, TagToken

_SCHEMA_DIR = Path(__file__).with_name("schemas")
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REQUIRED_CHECKPOINTS = (1, 10, 50, 100)


class SchemaError(ValueError):
    """Raised when an eval fixture violates the versioned contract."""


@dataclass(frozen=True)
class Turn:
    index: int
    prompt: str
    golden_id: Optional[str]
    checkpoint: bool


@dataclass(frozen=True)
class Case:
    id: str
    suite: str
    kind: str
    tags: Tuple[str, ...]
    hosts: Tuple[str, ...]
    repetitions: int
    turns: Tuple[Turn, ...]


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    regex: Optional[str]
    critical: bool


@dataclass(frozen=True)
class ProtectedSpan:
    text: Optional[str]
    utf8_hex: Optional[str]
    occurrences: int

    def expected_bytes(self) -> bytes:
        if self.text is not None:
            return self.text.encode("utf-8")
        if self.utf8_hex is None:  # Defensive; schema rejects this state.
            raise SchemaError("protected span has no representation")
        try:
            return bytes.fromhex(self.utf8_hex)
        except ValueError as exc:
            raise SchemaError("protected span utf8_hex is invalid") from exc


@dataclass(frozen=True)
class OrderRule:
    fact_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ShapeRule:
    min_units: int
    max_units: int


@dataclass(frozen=True)
class LanguageRule:
    min_cyrillic_ratio: float


@dataclass(frozen=True)
class StyleRule:
    banned_patterns: Tuple[str, ...]


@dataclass(frozen=True)
class BoundaryRule:
    public_profanity: bool
    targeted_abuse: bool
    destructive_warning_joke: bool


@dataclass(frozen=True)
class Golden:
    id: str
    facts: Tuple[Fact, ...]
    protected_spans: Tuple[ProtectedSpan, ...]
    orders: Tuple[OrderRule, ...]
    shape: Optional[ShapeRule]
    language: Optional[LanguageRule]
    style: Optional[StyleRule]
    boundary: BoundaryRule


@dataclass(frozen=True)
class SnapshotRecord:
    host: str
    arm: str
    case_id: str
    repetition: int
    turn: int
    text: str
    golden_id: Optional[str]
    infrastructure_error: bool = False
    host_version: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    policy_sha256: Optional[str] = None
    seed: Optional[int] = None
    session_id: Optional[str] = None
    session_length: Optional[int] = None
    prompt: Optional[str] = None
    prompt_sha256: Optional[str] = None
    response_sha256: Optional[str] = None
    input_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    runner_git_sha: Optional[str] = None
    schema_version: int = 1
    grader_version: int = 1


@dataclass(frozen=True)
class AssertionResult:
    kind: str
    name: str
    passed: bool
    required: bool
    detail: str


@dataclass(frozen=True)
class RecordGrade:
    host: str
    arm: str
    case_id: str
    repetition: int
    turn: int
    golden_id: Optional[str]
    assertions: Tuple[AssertionResult, ...]
    passed: bool
    blocked: bool
    block_reasons: Tuple[str, ...]
    fact_passed: int
    fact_total: int
    protected_passed: int
    protected_total: int
    order_passed: int
    order_total: int
    shape_passed: Optional[bool]
    language_passed: Optional[bool]
    style_passed: Optional[bool]
    boundary_passed: bool
    infrastructure_error: bool


@dataclass(frozen=True)
class GradeReport:
    expected: int
    graded: int
    passed: int
    failed: int
    blocked: int
    infrastructure_errors: int
    fact_passed: int
    fact_total: int
    simple_shape_passed: int
    simple_shape_total: int
    grades: Tuple[RecordGrade, ...]
    release_ok: bool


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError("cannot read fixture %s: %s" % (path, exc)) from exc
    try:
        tokens = yaml.scan(text)
        for token in tokens:
            if isinstance(token, (AliasToken, AnchorToken)):
                raise SchemaError("YAML aliases and anchors are forbidden: %s" % path)
            if isinstance(token, TagToken):
                raise SchemaError("YAML custom tags are forbidden: %s" % path)
    except SchemaError:
        raise
    except yaml.YAMLError as exc:
        raise SchemaError("invalid YAML token stream in %s: %s" % (path, exc)) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SchemaError("invalid YAML in %s: %s" % (path, exc)) from exc


def _schema(name: str) -> Mapping[str, Any]:
    try:
        return json.loads((_SCHEMA_DIR / (name + ".schema.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SchemaError("cannot load %s JSON Schema: %s" % (name, exc)) from exc


def _validate(document: Any, name: str, path: Path) -> None:
    errors = sorted(Draft202012Validator(_schema(name)).iter_errors(document), key=lambda error: tuple(str(part) for part in error.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise SchemaError("%s at %s in %s" % (error.message, location, path))


def _check_id(value: str, kind: str) -> None:
    if not _ID_RE.fullmatch(value):
        raise SchemaError("invalid %s id: %s" % (kind, value))


def _unique(values: Iterable[str], kind: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise SchemaError("duplicate %s id: %s" % (kind, value))
        seen.add(value)


def _as_paths(paths: Sequence[Path]) -> Tuple[Path, ...]:
    return tuple(Path(path) for path in paths)


def load_cases(paths: Sequence[Path]) -> Tuple[Case, ...]:
    cases = []
    for path in _as_paths(paths):
        document = _read_yaml(path)
        _validate(document, "case", path)
        if type(document.get("schema_version")) is not int or document.get("schema_version") != 1:
            raise SchemaError("schema_version must be integer 1 in %s" % path)
        for raw in document["cases"]:
            _check_id(raw["id"], "case")
            turns = tuple(
                Turn(
                    index=turn["index"],
                    prompt=turn["prompt"],
                    golden_id=turn["golden_id"],
                    checkpoint=turn["checkpoint"],
                )
                for turn in raw["turns"]
            )
            indexes = tuple(turn.index for turn in turns)
            if len(set(indexes)) != len(indexes):
                raise SchemaError("duplicate turn index in case %s" % raw["id"])
            if tuple(sorted(indexes)) != indexes:
                raise SchemaError("turn indexes must be increasing in case %s" % raw["id"])
            checkpoints = tuple(turn.index for turn in turns if turn.checkpoint)
            is_persistence = raw["kind"] == "persistence" or raw["suite"] == "persistence"
            if (checkpoints or is_persistence) and checkpoints != _REQUIRED_CHECKPOINTS:
                raise SchemaError(
                    "persistence checkpoints for %s must equal %r, got %r"
                    % (raw["id"], _REQUIRED_CHECKPOINTS, checkpoints)
                )
            cases.append(
                Case(
                    id=raw["id"],
                    suite=raw["suite"],
                    kind=raw["kind"],
                    tags=tuple(raw["tags"]),
                    hosts=tuple(raw["hosts"]),
                    repetitions=raw["repetitions"],
                    turns=turns,
                )
            )
    _unique((case.id for case in cases), "case")
    return tuple(cases)


def _compile(pattern: str, description: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise SchemaError("invalid %s regex %r: %s" % (description, pattern, exc)) from exc


def load_goldens(paths: Sequence[Path]) -> Tuple[Golden, ...]:
    goldens = []
    for path in _as_paths(paths):
        document = _read_yaml(path)
        _validate(document, "golden", path)
        if type(document.get("schema_version")) is not int or document.get("schema_version") != 1:
            raise SchemaError("schema_version must be integer 1 in %s" % path)
        for raw in document["goldens"]:
            _check_id(raw["id"], "golden")
            facts = []
            for item in raw["facts"]:
                _check_id(item["id"], "fact")
                if item["regex"] is not None:
                    _compile(item["regex"], "fact")
                facts.append(Fact(item["id"], item["text"], item["regex"], item["critical"]))
            _unique((fact.id for fact in facts), "fact")
            spans = tuple(
                ProtectedSpan(item.get("text"), item.get("utf8_hex"), item["occurrences"])
                for item in raw["protected_spans"]
            )
            for span in spans:
                expected = span.expected_bytes()
                try:
                    expected.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SchemaError("protected span is not valid UTF-8") from exc
            orders = tuple(OrderRule(tuple(item["fact_ids"])) for item in raw["orders"])
            fact_ids = {fact.id for fact in facts}
            for order in orders:
                unknown = set(order.fact_ids) - fact_ids
                if unknown:
                    raise SchemaError("order rule references unknown facts: %s" % sorted(unknown))
            shape = None if raw["shape"] is None else ShapeRule(raw["shape"]["min_units"], raw["shape"]["max_units"])
            if shape is not None and shape.min_units > shape.max_units:
                raise SchemaError("shape min_units exceeds max_units")
            language = None if raw["language"] is None else LanguageRule(raw["language"]["min_cyrillic_ratio"])
            style = None if raw["style"] is None else StyleRule(tuple(raw["style"]["banned_patterns"]))
            if style is not None:
                for pattern in style.banned_patterns:
                    _compile(pattern, "style")
            boundary = BoundaryRule(
                raw["boundary"]["public_profanity"],
                raw["boundary"]["targeted_abuse"],
                raw["boundary"]["destructive_warning_joke"],
            )
            goldens.append(
                Golden(
                    id=raw["id"],
                    facts=tuple(facts),
                    protected_spans=spans,
                    orders=orders,
                    shape=shape,
                    language=language,
                    style=style,
                    boundary=boundary,
                )
            )
    _unique((golden.id for golden in goldens), "golden")
    return tuple(goldens)


def validate_fixture_matrix(cases: Sequence[Case], goldens: Sequence[Golden]) -> None:
    available = {golden.id for golden in goldens}
    for case in cases:
        for turn in case.turns:
            if turn.golden_id is not None and turn.golden_id not in available:
                raise SchemaError(
                    "case %s turn %d references unknown golden %s"
                    % (case.id, turn.index, turn.golden_id)
                )
