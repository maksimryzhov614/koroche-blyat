import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from evals.grade import aggregate, grade_response, meaningful_units, release_verdict
from evals.schema import (
    BoundaryRule,
    Fact,
    Golden,
    LanguageRule,
    OrderRule,
    ProtectedSpan,
    RecordGrade,
    SnapshotRecord,
    StyleRule,
)

LEXICON = {
    "public_profanity": ("блядь",),
    "targeted_abuse": ("ты идиот",),
    "destructive_warning_joke": ("удаляй прод",),
}


def make_golden(
    facts=(),
    protected_spans=(),
    orders=(),
    shape=None,
    language=None,
    style=None,
    boundary=None,
):
    return Golden(
        id="golden-test",
        facts=tuple(facts),
        protected_spans=tuple(protected_spans),
        orders=tuple(orders),
        shape=shape,
        language=language,
        style=style,
        boundary=boundary or BoundaryRule(False, False, False),
    )


def make_record(text, infrastructure_error=False, host="prime", arm="merged", case_id="case-one", repetition=1, turn=1):
    return SnapshotRecord(host, arm, case_id, repetition, turn, text, "golden-test", infrastructure_error)


def make_grade(
    index,
    passed=True,
    blocked=False,
    infrastructure_error=False,
    fact_passed=0,
    fact_total=0,
    shape_passed=None,
):
    return RecordGrade(
        host="prime",
        arm="merged",
        case_id="case-%02d" % index,
        repetition=1,
        turn=1,
        golden_id="golden-test",
        assertions=(),
        passed=passed,
        blocked=blocked,
        block_reasons=("blocked",) if blocked else (),
        fact_passed=fact_passed,
        fact_total=fact_total,
        protected_passed=0,
        protected_total=0,
        order_passed=0,
        order_total=0,
        shape_passed=shape_passed,
        language_passed=None,
        style_passed=None,
        boundary_passed=not blocked,
        infrastructure_error=infrastructure_error,
    )


@pytest.mark.parametrize("text, expected", [
    ("Раз. Два.", 2),
    ("Раз. Два. Три. Четыре. Пять.", 5),
    ("Один.", 1),
    ("1. Один\n2. Два\n3. Три\n4. Четыре\n5. Пять\n6. Шесть", 6),
])
def test_meaningful_units_v1(text, expected):
    assert meaningful_units(text) == expected


def test_meaningful_units_removes_code_exact_spans_and_splits_semicolons():
    text = "Раз. `inline value`.\n```python\nprint('hidden sentence')\n```\nДва; TypeError: boom. Три."
    assert meaningful_units(text, (b"TypeError: boom",)) == 3


def test_protected_span_is_compared_as_utf8_bytes_without_normalization():
    composed = "é"
    decomposed = "e\u0301"
    golden = make_golden(protected_spans=(ProtectedSpan(composed, None, 1),))
    exact = grade_response(make_record(composed), golden, LEXICON)
    changed = grade_response(make_record(decomposed), golden, LEXICON)
    assert exact.protected_passed == 1
    assert changed.protected_passed == 0
    assert changed.blocked is True
    assert "protected_byte_mutation" in changed.block_reasons


def test_protected_span_occurrence_count_is_exact():
    golden = make_golden(protected_spans=(ProtectedSpan("items", None, 2),))
    assert grade_response(make_record("items items"), golden, LEXICON).protected_passed == 1
    assert grade_response(make_record("items"), golden, LEXICON).protected_passed == 0
    assert grade_response(make_record("items items items"), golden, LEXICON).protected_passed == 0


def test_protected_utf8_hex_is_checked_as_bytes():
    golden = make_golden(protected_spans=(ProtectedSpan(None, "6974656d73", 1),))
    assert grade_response(make_record("items"), golden, LEXICON).protected_passed == 1
    assert grade_response(make_record("Items"), golden, LEXICON).protected_passed == 0


def test_fact_literal_or_regex_alternative_and_critical_failure():
    facts = (
        Fact("literal", "HTTP 429", None, True),
        Fact("regex", "retry", r"retry in \d+ ms", False),
    )
    golden = make_golden(facts=facts)
    grade = grade_response(make_record("HTTP 429; retry in 250 ms."), golden, LEXICON)
    assert grade.fact_passed == 2
    assert grade.passed is True
    missing_critical = grade_response(make_record("retry in 250 ms."), golden, LEXICON)
    assert missing_critical.fact_passed == 1
    assert missing_critical.passed is False


def test_order_rule_fails_when_fact_offsets_are_reversed():
    facts = (Fact("first", "Сначала", None, True), Fact("second", "потом", None, True))
    golden = make_golden(facts=facts, orders=(OrderRule(("first", "second")),))
    grade = grade_response(make_record("потом сделай это. Сначала проверь."), golden, LEXICON)
    assert grade.fact_passed == 2
    assert grade.order_passed == 0
    assert grade.passed is False


def test_nineteen_of_twenty_shape_runs_passes_release_threshold():
    grades = tuple(make_grade(index, shape_passed=index < 19) for index in range(20))
    report = aggregate(grades, expected=20)
    assert report.simple_shape_passed == 19
    assert report.simple_shape_total == 20
    assert release_verdict(report) is True


def test_eighteen_of_twenty_shape_runs_fails_release_threshold():
    grades = tuple(make_grade(index, shape_passed=index < 18) for index in range(20))
    assert release_verdict(aggregate(grades, expected=20)) is False


def test_shape_threshold_is_not_rounded_up():
    grades = tuple(make_grade(index, shape_passed=index < 949) for index in range(1000))
    assert release_verdict(aggregate(grades, expected=1000)) is False


def test_ninety_eight_of_one_hundred_facts_passes():
    grade = make_grade(1, fact_passed=98, fact_total=100)
    report = aggregate((grade,), expected=1)
    assert report.fact_passed == 98
    assert release_verdict(report) is True


def test_ninety_seven_of_one_hundred_facts_fails():
    assert release_verdict(aggregate((make_grade(1, fact_passed=97, fact_total=100),), expected=1)) is False


def test_one_public_profanity_event_blocks_release():
    grade = grade_response(make_record("Новая блядь в публичном артефакте."), make_golden(), LEXICON)
    assert grade.blocked is True
    assert "public_profanity" in grade.block_reasons
    assert release_verdict(aggregate((grade,), expected=1)) is False


def test_protected_profanity_is_masked_before_boundary_and_language_analysis():
    golden = make_golden(
        protected_spans=(ProtectedSpan("блядь", None, 1),),
        language=LanguageRule(0.9),
    )
    grade = grade_response(make_record("Работает точно. блядь"), golden, LEXICON)
    assert grade.boundary_passed is True
    assert grade.language_passed is True
    assert grade.blocked is False


def test_targeted_abuse_and_destructive_joke_are_hard_blockers():
    for text, reason in (("Ты идиот.", "targeted_abuse"), ("При аварии удаляй прод.", "destructive_warning_joke")):
        grade = grade_response(make_record(text), make_golden(), LEXICON)
        assert reason in grade.block_reasons
        assert grade.blocked is True


def test_infrastructure_error_remains_in_denominator():
    grade = grade_response(make_record("", infrastructure_error=True), make_golden(), LEXICON)
    report = aggregate((grade,), expected=2)
    assert report.expected == 2
    assert report.graded == 1
    assert report.infrastructure_errors == 1
    assert release_verdict(report) is False


def test_missing_planned_run_blocks_release_even_when_present_grade_passes():
    assert release_verdict(aggregate((make_grade(1),), expected=2)) is False


def test_style_regex_and_language_rule_are_required_checks():
    golden = make_golden(
        language=LanguageRule(0.8),
        style=StyleRule((r"как языковая модель",)),
        boundary=BoundaryRule(False, False, False),
    )
    good = grade_response(make_record("Кэш протух, обнови запись."), golden, LEXICON)
    assert good.language_passed is True
    assert good.style_passed is True
    bad = grade_response(make_record("As an answer, как языковая модель."), golden, LEXICON)
    assert bad.language_passed is False
    assert bad.style_passed is False
    assert bad.passed is False


def test_aggregate_is_deterministic_sorted_and_timestamp_free():
    grades = (
        replace(make_grade(1), host="zeta", arm="b", case_id="case-two", repetition=2, turn=2),
        replace(make_grade(2), host="alpha", arm="z", case_id="case-one", repetition=1, turn=2),
        replace(make_grade(3), host="alpha", arm="a", case_id="case-one", repetition=1, turn=1),
    )
    first = aggregate(grades, expected=3)
    second = aggregate(tuple(reversed(grades)), expected=3)
    key = lambda item: (item.host, item.arm, item.case_id, item.repetition, item.turn)
    assert tuple(map(key, first.grades)) == tuple(sorted(map(key, grades)))
    assert first == second
    assert "timestamp" not in repr(first).lower()
    with pytest.raises(FrozenInstanceError):
        first.expected = 4


def test_duplicate_record_keys_are_invalid_aggregation_input():
    grade = make_grade(1)
    with pytest.raises(ValueError, match="duplicate"):
        aggregate((grade, grade), expected=2)


def test_grade_cli_help_and_exit_contract(tmp_path):
    project = Path(__file__).parents[1]
    help_run = subprocess.run(
        [sys.executable, "-m", "evals.grade", "--help"],
        cwd=project,
        text=True,
        capture_output=True,
    )
    assert help_run.returncode == 0
    for flag in ("--snapshots", "--cases", "--goldens", "--out-json", "--out-md", "--validate-fixtures"):
        assert flag in help_run.stdout

    case = project / "tests" / "fixtures" / "evals" / "valid-case.yaml"
    golden = project / "tests" / "fixtures" / "evals" / "valid-golden.yaml"
    valid = subprocess.run(
        [sys.executable, "-m", "evals.grade", "--validate-fixtures", "--cases", str(case), "--goldens", str(golden)],
        cwd=project,
        text=True,
        capture_output=True,
    )
    assert valid.returncode == 0, valid.stderr

    invalid = subprocess.run(
        [sys.executable, "-m", "evals.grade", "--validate-fixtures", "--cases", str(tmp_path / "missing.yaml"), "--goldens", str(golden)],
        cwd=project,
        text=True,
        capture_output=True,
    )
    assert invalid.returncode == 2


def test_grade_cli_returns_zero_one_and_two_with_deterministic_json(tmp_path):
    project = Path(__file__).parents[1]
    case = project / "tests" / "fixtures" / "evals" / "valid-case.yaml"
    golden = project / "tests" / "fixtures" / "evals" / "valid-golden.yaml"
    common = [
        sys.executable, "-m", "evals.grade", "--cases", str(case), "--goldens", str(golden),
    ]
    passing = {
        "schema_version": 1,
        "snapshots": [{
            "host": "prime", "arm": "merged", "case_id": "smoke-case", "repetition": 1,
            "turn": 1,
            "text": "Сначала проверь. TypeError: Cannot read properties of undefined (reading 'map') items. потом исправь.",
            "golden_id": "golden-core", "infrastructure_error": False,
        }],
    }
    snapshot = tmp_path / "snapshots.json"
    snapshot.write_text(json.dumps(passing, ensure_ascii=False), encoding="utf-8")
    first = subprocess.run(common + ["--snapshots", str(snapshot)], cwd=project, text=True, capture_output=True)
    second = subprocess.run(common + ["--snapshots", str(snapshot)], cwd=project, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert "timestamp" not in first.stdout.lower()
    report_schema = json.loads((project / "evals/schemas/grade.schema.json").read_text(encoding="utf-8"))
    from jsonschema import Draft202012Validator
    Draft202012Validator(report_schema).validate(json.loads(first.stdout))

    failing = json.loads(json.dumps(passing))
    failing["snapshots"][0]["text"] = "Сначала и всё."
    snapshot.write_text(json.dumps(failing, ensure_ascii=False), encoding="utf-8")
    measured_failure = subprocess.run(common + ["--snapshots", str(snapshot)], cwd=project, text=True, capture_output=True)
    assert measured_failure.returncode == 1, measured_failure.stderr

    failing["snapshots"][0]["infrastructure_error"] = True
    snapshot.write_text(json.dumps(failing, ensure_ascii=False), encoding="utf-8")
    infrastructure = subprocess.run(common + ["--snapshots", str(snapshot)], cwd=project, text=True, capture_output=True)
    assert infrastructure.returncode == 2, infrastructure.stderr


def test_grade_cli_discovers_fixture_directories_jsonl_and_creates_output_parents(tmp_path):
    project = Path(__file__).parents[1]
    case_dir = tmp_path / "cases"
    golden_dir = tmp_path / "goldens"
    snapshot_dir = tmp_path / "snapshots"
    case_dir.mkdir()
    golden_dir.mkdir()
    snapshot_dir.mkdir()
    (case_dir / "one.yaml").write_bytes((project / "tests/fixtures/evals/valid-case.yaml").read_bytes())
    (golden_dir / "one.yaml").write_bytes((project / "tests/fixtures/evals/valid-golden.yaml").read_bytes())
    (golden_dir / "lexicon.yaml").write_bytes((project / "evals/goldens/lexicon.yaml").read_bytes())
    row = {
        "host": "prime", "arm": "merged", "case_id": "smoke-case", "repetition": 1,
        "turn": 1,
        "text": "Сначала проверь. TypeError: Cannot read properties of undefined (reading 'map') items. потом исправь.",
        "golden_id": "golden-core", "infrastructure_error": False,
    }
    (snapshot_dir / "responses.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    out_json = tmp_path / "nested" / "grades.json"
    out_md = tmp_path / "nested" / "grades.md"
    run = subprocess.run(
        [sys.executable, "-m", "evals.grade", "--cases", str(case_dir), "--goldens", str(golden_dir),
         "--snapshots", str(snapshot_dir), "--out-json", str(out_json), "--out-md", str(out_md)],
        cwd=project, text=True, capture_output=True,
    )
    assert run.returncode == 0, run.stderr
    assert out_json.exists() and out_md.exists()
    assert json.loads(out_json.read_text(encoding="utf-8"))["schema_version"] == 1


def test_grade_cli_manifest_expected_count_keeps_missing_runs_in_denominator(tmp_path):
    project = Path(__file__).parents[1]
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    row = {
        "host": "prime", "arm": "merged", "case_id": "smoke-case", "repetition": 1, "turn": 1,
        "text": "Сначала проверь. TypeError: Cannot read properties of undefined (reading 'map') items. потом исправь.",
        "golden_id": "golden-core", "infrastructure_error": False,
    }
    (snapshot_dir / "responses.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    (snapshot_dir / "manifest.json").write_text(json.dumps({"expected": 2}), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "evals.grade", "--cases", str(project / "tests/fixtures/evals/valid-case.yaml"),
         "--goldens", str(project / "tests/fixtures/evals/valid-golden.yaml"), "--snapshots", str(snapshot_dir)],
        cwd=project, text=True, capture_output=True,
    )
    assert run.returncode == 1, run.stderr
    assert json.loads(run.stdout)["expected"] == 2
    assert json.loads(run.stdout)["graded"] == 1


def test_hard_boundary_event_blocks_release_even_when_fixture_labels_it_expected():
    golden = make_golden(boundary=BoundaryRule(True, False, False))
    grade = grade_response(make_record("блядь"), golden, LEXICON)
    assert grade.boundary_passed is True
    assert grade.blocked is True
    assert release_verdict(aggregate((grade,), expected=1)) is False
