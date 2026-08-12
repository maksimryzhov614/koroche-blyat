"""Deterministic graders and command-line report generation."""

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import yaml
from jsonschema import Draft202012Validator

from evals.schema import (
    AssertionResult,
    Golden,
    GradeReport,
    RecordGrade,
    SchemaError,
    SnapshotRecord,
    load_cases,
    load_goldens,
    validate_fixture_matrix,
)

_VERSION = 1
_FENCE_RE = re.compile(r"```[^\n`]*\n.*?```", re.DOTALL)
_INLINE_RE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
_BOUNDARY_RE = re.compile(r"[.!?…]+|;|\n+")
_MARKER_RE = re.compile(r"^\s*(?:>\s*)?(?:(?:[-*+])|(?:\d+[.)]))?\s*")
_LINE_MARKER_RE = re.compile(r"(?m)^\s*(?:>\s*)?(?:(?:[-*+])|(?:\d+[.)]))\s+")


def _excluded_text(excluded: Any) -> str:
    if isinstance(excluded, bytes):
        try:
            return excluded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("excluded_utf8 contains invalid UTF-8") from exc
    if isinstance(excluded, str):
        # Accept hexadecimal strings as a convenience for fixture-derived spans.
        if excluded and len(excluded) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", excluded):
            try:
                return bytes.fromhex(excluded).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("excluded_utf8 contains invalid UTF-8") from exc
        return excluded
    raise TypeError("excluded_utf8 entries must be bytes or str")


def meaningful_units(text: str, excluded_utf8: Sequence[Any] = ()) -> int:
    """Count v1 prose units after code and exact declared spans are removed."""
    analysis = _FENCE_RE.sub("\n", text)
    analysis = _INLINE_RE.sub(" ", analysis)
    analysis = _LINE_MARKER_RE.sub("", analysis)
    exclusions = sorted((_excluded_text(item) for item in excluded_utf8), key=lambda value: (-len(value), value))
    for excluded in exclusions:
        if excluded:
            analysis = analysis.replace(excluded, " ")
    count = 0
    for raw in _BOUNDARY_RE.split(analysis):
        candidate = _MARKER_RE.sub("", raw).strip(" \t\r*_~#[]()")
        # The v1 literal examples explicitly count one-word sentences ("Один.").
        if any(character.isalnum() for character in candidate):
            count += 1
    return count


def _span_text(span: Any) -> str:
    return span.expected_bytes().decode("utf-8")


def _mask_spans(text: str, golden: Golden) -> str:
    masked = text
    for span in golden.protected_spans:
        masked = masked.replace(_span_text(span), " ")
    return masked


def _normalize_analysis(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def _lexicon_values(lexicon: Mapping[str, Sequence[str]], key: str) -> Tuple[str, ...]:
    raw = lexicon.get(key, ())
    if isinstance(raw, str):
        return (raw,)
    return tuple(str(item) for item in raw)


def _contains_lexicon(text: str, values: Sequence[str]) -> bool:
    normalized = _normalize_analysis(text)
    return any(_normalize_analysis(value) in normalized for value in values if value)


def _fact_position(text: str, fact: Any) -> int:
    literal = text.find(fact.text)
    regex_position = -1
    if fact.regex is not None:
        match = re.search(fact.regex, text)
        if match is not None:
            regex_position = match.start()
    if literal < 0:
        return regex_position
    if regex_position < 0:
        return literal
    return min(literal, regex_position)


def _assertion(kind: str, name: str, passed: bool, required: bool, detail: str) -> AssertionResult:
    return AssertionResult(kind, name, passed, required, detail)


def grade_response(record: SnapshotRecord, golden: Golden, lexicon: Mapping[str, Sequence[str]]) -> RecordGrade:
    assertions = []
    block_reasons = []

    if record.infrastructure_error:
        assertions.append(_assertion("infrastructure", "record", False, True, "infrastructure error"))
        block_reasons.append("infrastructure_error")

    response_bytes = record.text.encode("utf-8")
    protected_passed = 0
    for index, span in enumerate(golden.protected_spans):
        expected = span.expected_bytes()
        observed = response_bytes.count(expected)
        passed = observed == span.occurrences
        protected_passed += int(passed)
        assertions.append(
            _assertion(
                "protected_span",
                "protected-%d" % index,
                passed,
                True,
                "expected %d exact UTF-8 occurrence(s), observed %d" % (span.occurrences, observed),
            )
        )
        if not passed and "protected_byte_mutation" not in block_reasons:
            block_reasons.append("protected_byte_mutation")

    positions = {}
    fact_passed = 0
    critical_passed = True
    for fact in golden.facts:
        position = _fact_position(record.text, fact)
        positions[fact.id] = position
        passed = position >= 0
        fact_passed += int(passed)
        if fact.critical and not passed:
            critical_passed = False
        assertions.append(
            _assertion(
                "fact",
                fact.id,
                passed,
                fact.critical,
                "literal or regex alternative %s" % ("matched" if passed else "missing"),
            )
        )

    order_passed = 0
    for index, order in enumerate(golden.orders):
        offsets = tuple(positions.get(fact_id, -1) for fact_id in order.fact_ids)
        passed = all(offset >= 0 for offset in offsets) and all(left < right for left, right in zip(offsets, offsets[1:]))
        order_passed += int(passed)
        assertions.append(
            _assertion("order", "order-%d" % index, passed, True, "offsets=%r" % (offsets,))
        )

    excluded = tuple(span.expected_bytes() for span in golden.protected_spans)
    shape_passed = None
    if golden.shape is not None:
        units = meaningful_units(record.text, excluded)
        shape_passed = golden.shape.min_units <= units <= golden.shape.max_units
        assertions.append(
            _assertion(
                "shape",
                "meaningful_units-v1",
                shape_passed,
                True,
                "units=%d expected=%d..%d" % (units, golden.shape.min_units, golden.shape.max_units),
            )
        )

    masked = _mask_spans(record.text, golden)
    language_passed = None
    if golden.language is not None:
        letters = tuple(character for character in masked if character.isalpha())
        cyrillic = sum(1 for character in letters if "CYRILLIC" in unicodedata.name(character, ""))
        ratio = cyrillic / len(letters) if letters else 0.0
        language_passed = ratio >= golden.language.min_cyrillic_ratio
        assertions.append(
            _assertion(
                "language",
                "cyrillic_ratio",
                language_passed,
                True,
                "ratio=%r threshold=%r" % (ratio, golden.language.min_cyrillic_ratio),
            )
        )

    style_passed = None
    if golden.style is not None:
        style_passed = not any(re.search(pattern, masked) for pattern in golden.style.banned_patterns)
        assertions.append(_assertion("style", "banned_patterns", style_passed, True, "style patterns checked"))

    boundary_events = {
        "public_profanity": _contains_lexicon(masked, _lexicon_values(lexicon, "public_profanity")),
        "targeted_abuse": _contains_lexicon(masked, _lexicon_values(lexicon, "targeted_abuse")),
        "destructive_warning_joke": _contains_lexicon(masked, _lexicon_values(lexicon, "destructive_warning_joke")),
    }
    boundary_passed = True
    for name, observed in boundary_events.items():
        expected = bool(getattr(golden.boundary, name))
        passed = observed is expected
        boundary_passed = boundary_passed and passed
        assertions.append(_assertion("boundary", name, passed, True, "observed=%r expected=%r" % (observed, expected)))
        if observed:
            block_reasons.append(name)

    required_checks_pass = all(result.passed for result in assertions if result.required)
    # Non-critical fact coverage is a release aggregate, not a per-record hard gate.
    passed = (
        required_checks_pass
        and critical_passed
        and not record.infrastructure_error
        and not block_reasons
    )
    return RecordGrade(
        host=record.host,
        arm=record.arm,
        case_id=record.case_id,
        repetition=record.repetition,
        turn=record.turn,
        golden_id=record.golden_id or golden.id,
        assertions=tuple(assertions),
        passed=passed,
        blocked=bool(block_reasons),
        block_reasons=tuple(dict.fromkeys(block_reasons)),
        fact_passed=fact_passed,
        fact_total=len(golden.facts),
        protected_passed=protected_passed,
        protected_total=len(golden.protected_spans),
        order_passed=order_passed,
        order_total=len(golden.orders),
        shape_passed=shape_passed,
        language_passed=language_passed,
        style_passed=style_passed,
        boundary_passed=boundary_passed,
        infrastructure_error=record.infrastructure_error,
    )


def _grade_key(grade: RecordGrade) -> Tuple[str, str, str, int, int]:
    return (grade.host, grade.arm, grade.case_id, grade.repetition, grade.turn)


def aggregate(grades: Sequence[RecordGrade], expected: int) -> GradeReport:
    if expected < 0:
        raise ValueError("expected must be non-negative")
    ordered = tuple(sorted(grades, key=_grade_key))
    keys = tuple(_grade_key(grade) for grade in ordered)
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate grade record key")
    if expected == 0 and ordered:
        raise ValueError("non-empty grades with expected zero")
    fact_passed = sum(grade.fact_passed for grade in ordered)
    fact_total = sum(grade.fact_total for grade in ordered)
    shape_values = tuple(grade.shape_passed for grade in ordered if grade.shape_passed is not None)
    report = GradeReport(
        expected=expected,
        graded=len(ordered),
        passed=sum(grade.passed for grade in ordered),
        failed=sum(not grade.passed for grade in ordered),
        blocked=sum(grade.blocked for grade in ordered),
        infrastructure_errors=sum(grade.infrastructure_error for grade in ordered),
        fact_passed=fact_passed,
        fact_total=fact_total,
        simple_shape_passed=sum(value is True for value in shape_values),
        simple_shape_total=len(shape_values),
        grades=ordered,
        release_ok=False,
    )
    verdict = release_verdict(report)
    return GradeReport(
        expected=report.expected,
        graded=report.graded,
        passed=report.passed,
        failed=report.failed,
        blocked=report.blocked,
        infrastructure_errors=report.infrastructure_errors,
        fact_passed=report.fact_passed,
        fact_total=report.fact_total,
        simple_shape_passed=report.simple_shape_passed,
        simple_shape_total=report.simple_shape_total,
        grades=report.grades,
        release_ok=verdict,
    )


def release_verdict(report: GradeReport) -> bool:
    if report.graded != report.expected:
        return False
    if report.infrastructure_errors or report.blocked:
        return False
    if report.fact_total and report.fact_passed / report.fact_total < 0.98:
        return False
    if report.simple_shape_total and report.simple_shape_passed / report.simple_shape_total < 0.95:
        return False
    # Per-record required failures other than aggregate fact/shape failures remain hard gates.
    for grade in report.grades:
        for assertion in grade.assertions:
            if assertion.required and assertion.kind not in ("fact", "shape") and not assertion.passed:
                return False
        if any(assertion.kind == "fact" and assertion.required and not assertion.passed for assertion in grade.assertions):
            return False
    return True



def _discover_inputs(paths: Sequence[Path], kind: str) -> Tuple[Path, ...]:
    discovered = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            raise SchemaError("input path does not exist: %s" % path)
        if path.is_file():
            discovered.append(path)
            continue
        if not path.is_dir():
            raise SchemaError("input path is neither file nor directory: %s" % path)
        if kind in ("case", "golden"):
            candidates = sorted(tuple(path.rglob("*.yaml")) + tuple(path.rglob("*.yml")))
            if kind == "golden":
                candidates = [candidate for candidate in candidates if candidate.name != "lexicon.yaml"]
        else:
            preferred = path / "responses.jsonl"
            if preferred.exists():
                candidates = [preferred]
            else:
                candidates = sorted(
                    candidate for candidate in tuple(path.rglob("*.json")) + tuple(path.rglob("*.jsonl"))
                    if candidate.name != "manifest.json"
                )
        discovered.extend(candidates)
    if not discovered:
        raise SchemaError("no %s inputs found" % kind)
    return tuple(discovered)

def _load_lexicon(path: Path) -> Mapping[str, Sequence[str]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SchemaError("cannot load lexicon %s: %s" % (path, exc)) from exc
    if not isinstance(raw, dict):
        raise SchemaError("lexicon must be a mapping")
    allowed = {"public_profanity", "targeted_abuse", "destructive_warning_joke"}
    if set(raw) != allowed:
        raise SchemaError("lexicon keys must equal %r" % sorted(allowed))
    for key, values in raw.items():
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            raise SchemaError("lexicon %s must be a list of non-empty strings" % key)
    return raw


def _load_snapshots(paths: Sequence[Path]) -> Tuple[Tuple[SnapshotRecord, ...], int]:
    schema_path = Path(__file__).with_name("schemas") / "snapshot.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    records = []
    declared_expected = []
    raw_paths = tuple(Path(path) for path in paths)
    for raw_path in raw_paths:
        if raw_path.is_dir():
            manifest_path = raw_path / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise SchemaError("invalid snapshot manifest %s: %s" % (manifest_path, exc)) from exc
                if isinstance(manifest.get("expected"), int):
                    declared_expected.append(manifest["expected"])
                elif isinstance(manifest.get("planned_calls"), list):
                    declared_expected.append(len(manifest["planned_calls"]))
        for path in _discover_inputs((raw_path,), "snapshot"):
            try:
                if path.suffix == ".jsonl":
                    items = []
                    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if line.strip():
                            try:
                                items.append(json.loads(line))
                            except json.JSONDecodeError as exc:
                                raise SchemaError("invalid JSONL at %s:%d: %s" % (path, line_number, exc)) from exc
                    document = {"schema_version": 1, "snapshots": items}
                else:
                    document = json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise SchemaError("cannot read snapshots %s: %s" % (path, exc)) from exc
            errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: tuple(str(part) for part in error.absolute_path))
            if errors:
                raise SchemaError("invalid snapshot %s: %s" % (path, errors[0].message))
            if "expected" in document:
                declared_expected.append(document["expected"])
            for item in document["snapshots"]:
                records.append(
                    SnapshotRecord(
                        host=item["host"], arm=item["arm"], case_id=item["case_id"],
                        repetition=item["repetition"], turn=item["turn"], text=item["text"],
                        golden_id=item["golden_id"], infrastructure_error=item["infrastructure_error"],
                        host_version=item.get("host_version"), provider=item.get("provider"),
                        model=item.get("model"), policy_sha256=item.get("policy_sha256"), seed=item.get("seed"),
                        session_id=item.get("session_id"), session_length=item.get("session_length"),
                        prompt=item.get("prompt"), prompt_sha256=item.get("prompt_sha256"),
                        response_sha256=item.get("response_sha256"), input_tokens=item.get("input_tokens"),
                        cache_read_tokens=item.get("cache_read_tokens"),
                        cache_write_tokens=item.get("cache_write_tokens"), output_tokens=item.get("output_tokens"),
                        total_tokens=item.get("total_tokens"), exit_code=item.get("exit_code"),
                        duration_ms=item.get("duration_ms"), stdout_path=item.get("stdout_path"),
                        stderr_path=item.get("stderr_path"), runner_git_sha=item.get("runner_git_sha"),
                        schema_version=item.get("schema_version", 1), grader_version=item.get("grader_version", 1),
                    )
                )
    expected = sum(declared_expected) if declared_expected else len(records)
    if expected < len(records):
        raise SchemaError("snapshot expected count is smaller than recorded rows")
    return tuple(records), expected


def _report_dict(report: GradeReport) -> Mapping[str, Any]:
    data = asdict(report)
    data["schema_version"] = _VERSION
    return data


def _render_markdown(report: GradeReport) -> str:
    lines = [
        "# Eval report",
        "",
        "- Verdict: %s" % ("PASS" if report.release_ok else "FAIL"),
        "- Expected: %d" % report.expected,
        "- Graded: %d" % report.graded,
        "- Facts: %d/%d" % (report.fact_passed, report.fact_total),
        "- Simple-safe shape: %d/%d" % (report.simple_shape_passed, report.simple_shape_total),
        "",
        "## Results",
    ]
    for grade in report.grades:
        lines.append(
            "- %s/%s/%s/%d/%d: %s"
            % (grade.host, grade.arm, grade.case_id, grade.repetition, grade.turn, "PASS" if grade.passed else "FAIL")
        )
    lines.append("")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and deterministically grade koroche-blyat eval records.")
    parser.add_argument("--snapshots", nargs="+", type=Path)
    parser.add_argument("--cases", nargs="+", type=Path, required=True)
    parser.add_argument("--goldens", nargs="+", type=Path, required=True)
    parser.add_argument("--lexicon", type=Path, default=Path(__file__).with_name("goldens") / "lexicon.yaml")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--validate-fixtures", action="store_true")
    parser.add_argument("--release-gate", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        cases = load_cases(_discover_inputs(tuple(args.cases), "case"))
        goldens = load_goldens(_discover_inputs(tuple(args.goldens), "golden"))
        validate_fixture_matrix(cases, goldens)
        lexicon = _load_lexicon(args.lexicon)
        if args.validate_fixtures and not args.snapshots:
            return 0
        if not args.snapshots:
            raise SchemaError("--snapshots is required unless --validate-fixtures is used")
        snapshots, expected = _load_snapshots(tuple(args.snapshots))
        by_id = {golden.id: golden for golden in goldens}
        grades = []
        for record in snapshots:
            if record.golden_id is None or record.golden_id not in by_id:
                raise SchemaError("snapshot references unknown golden %r" % record.golden_id)
            grades.append(grade_response(record, by_id[record.golden_id], lexicon))
        report = aggregate(tuple(grades), expected=expected)
        json_text = json.dumps(_report_dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        markdown = _render_markdown(report)
        if args.out_json:
            args.out_json.parent.mkdir(parents=True, exist_ok=True)
            with args.out_json.open("w", encoding="utf-8", newline="\n") as output:
                output.write(json_text)
        if args.out_md:
            args.out_md.parent.mkdir(parents=True, exist_ok=True)
            with args.out_md.open("w", encoding="utf-8", newline="\n") as output:
                output.write(markdown)
        if not args.out_json and not args.out_md:
            sys.stdout.write(json_text)
        if report.infrastructure_errors:
            return 2
        return 0 if report.release_ok else 1
    except (SchemaError, OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
