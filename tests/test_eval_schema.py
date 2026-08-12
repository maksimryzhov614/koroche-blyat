import ast
import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

import evals.schema as schema_mod

FIXTURES = Path(__file__).parent / "fixtures" / "evals"
SCHEMAS = Path(__file__).parents[1] / "evals" / "schemas"


def test_valid_fixtures_load_and_public_shapes_are_frozen():
    cases = schema_mod.load_cases((FIXTURES / "valid-case.yaml",))
    goldens = schema_mod.load_goldens((FIXTURES / "valid-golden.yaml",))
    schema_mod.validate_fixture_matrix(cases, goldens)

    assert cases[0].id == "smoke-case"
    assert tuple(turn.index for turn in cases[0].turns if turn.checkpoint) == (1, 10, 50, 100)
    assert goldens[0].id == "golden-core"
    assert [field.name for field in fields(schema_mod.Turn)] == [
        "index", "prompt", "golden_id", "checkpoint"
    ]
    assert [field.name for field in fields(schema_mod.Case)] == [
        "id", "suite", "kind", "tags", "hosts", "repetitions", "turns"
    ]
    assert [field.name for field in fields(schema_mod.Golden)] == [
        "id", "facts", "protected_spans", "orders", "shape", "language", "style", "boundary"
    ]
    for public_type in (
        schema_mod.Turn, schema_mod.Case, schema_mod.Fact, schema_mod.ProtectedSpan,
        schema_mod.Golden, schema_mod.SnapshotRecord, schema_mod.AssertionResult,
        schema_mod.RecordGrade, schema_mod.GradeReport,
    ):
        assert public_type.__dataclass_params__.frozen is True
    with pytest.raises(FrozenInstanceError):
        cases[0].id = "changed"


@pytest.mark.parametrize("mutation", [
    "unknown-key", "yaml-alias", "custom-tag", "duplicate-id",
    "bad-regex", "orphan-golden", "missing-persistence-checkpoint",
])
def test_invalid_fixture_is_rejected_for_the_declared_reason(mutation, tmp_path):
    case_text = (FIXTURES / "valid-case.yaml").read_text(encoding="utf-8")
    golden_text = (FIXTURES / "valid-golden.yaml").read_text(encoding="utf-8")
    case_path = tmp_path / "case.yaml"
    golden_path = tmp_path / "golden.yaml"
    case_path.write_text(case_text, encoding="utf-8")
    golden_path.write_text(golden_text, encoding="utf-8")

    if mutation == "unknown-key":
        case_path.write_text(
            case_text.replace("    repetitions: 1\n", "    repetitions: 1\n    nope: true\n"),
            encoding="utf-8",
        )
        action = lambda: schema_mod.load_cases((case_path,))
        message = "Additional properties"
    elif mutation == "yaml-alias":
        case_path.write_text(case_text.replace("cases:\n", "anchor: &bad [one]\ncases:\n") + "alias: *bad\n", encoding="utf-8")
        action = lambda: schema_mod.load_cases((case_path,))
        message = "aliases"
    elif mutation == "custom-tag":
        case_path.write_text(case_text.replace("schema_version: 1", "schema_version: !evil 1"), encoding="utf-8")
        action = lambda: schema_mod.load_cases((case_path,))
        message = "tags"
    elif mutation == "duplicate-id":
        duplicate = case_text.split("  - id: smoke-case", 1)[1]
        case_path.write_text(case_text + "  - id: smoke-case" + duplicate, encoding="utf-8")
        action = lambda: schema_mod.load_cases((case_path,))
        message = "duplicate case id"
    elif mutation == "bad-regex":
        golden_path.write_text(golden_text.replace("regex: null", 'regex: "("'), encoding="utf-8")
        action = lambda: schema_mod.load_goldens((golden_path,))
        message = "invalid fact regex"
    elif mutation == "orphan-golden":
        case_path.write_text(case_text.replace("golden-core", "missing-golden"), encoding="utf-8")
        cases = schema_mod.load_cases((case_path,))
        goldens = schema_mod.load_goldens((golden_path,))
        action = lambda: schema_mod.validate_fixture_matrix(cases, goldens)
        message = "unknown golden"
    else:
        block = """      - index: 100
        prompt: Финал.
        golden_id: golden-core
        checkpoint: true
"""
        case_path.write_text(case_text.replace(block, ""), encoding="utf-8")
        action = lambda: schema_mod.load_cases((case_path,))
        message = "checkpoints"

    with pytest.raises(schema_mod.SchemaError, match=message):
        action()


def test_alias_is_rejected_by_token_scan_before_safe_load(monkeypatch, tmp_path):
    path = tmp_path / "alias.yaml"
    path.write_text("schema_version: 1\nanchor: &x [one]\ncases: *x\n", encoding="utf-8")

    def should_not_parse(value):
        raise AssertionError("safe_load must not run after an alias token")

    monkeypatch.setattr(schema_mod.yaml, "safe_load", should_not_parse)
    with pytest.raises(schema_mod.SchemaError, match="aliases"):
        schema_mod.load_cases((path,))


def test_uses_yaml_safe_load(monkeypatch):
    calls = []
    real_safe_load = yaml.safe_load

    def wrapped(value):
        calls.append(value)
        return real_safe_load(value)

    monkeypatch.setattr(schema_mod.yaml, "safe_load", wrapped)
    schema_mod.load_cases((FIXTURES / "valid-case.yaml",))
    assert len(calls) == 1


@pytest.mark.parametrize("fixture_name", ["valid-case.yaml", "valid-golden.yaml"])
def test_schema_version_must_be_one(fixture_name, tmp_path):
    path = tmp_path / fixture_name
    path.write_text(
        (FIXTURES / fixture_name).read_text(encoding="utf-8").replace("schema_version: 1", "schema_version: 2"),
        encoding="utf-8",
    )
    loader = schema_mod.load_cases if "case" in fixture_name else schema_mod.load_goldens
    with pytest.raises(schema_mod.SchemaError, match="schema_version"):
        loader((path,))


def test_protected_span_requires_exactly_one_representation_and_positive_count(tmp_path):
    source = (FIXTURES / "valid-golden.yaml").read_text(encoding="utf-8")
    span_line = "      - text: \"TypeError: Cannot read properties of undefined (reading 'map')\"\n"
    both = source.replace(span_line, span_line + "        utf8_hex: d09a\n")
    missing = source.replace(span_line, "      - {}\n")
    zero = source.replace("occurrences: 1", "occurrences: 0", 1)
    for index, invalid in enumerate((both, missing, zero)):
        path = tmp_path / ("invalid-%d.yaml" % index)
        path.write_text(invalid, encoding="utf-8")
        with pytest.raises(schema_mod.SchemaError):
            schema_mod.load_goldens((path,))


def test_all_json_schemas_are_draft_2020_12_and_reject_unknown_top_level_keys():
    for name in ("case", "golden", "snapshot", "grade"):
        document = json.loads((SCHEMAS / (name + ".schema.json")).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["additionalProperties"] is False
        with pytest.raises(ValidationError):
            Draft202012Validator(document).validate({"unexpected": True})


def test_runtime_annotations_parse_on_python39_without_pep604_or_builtin_generics():
    source = (Path(schema_mod.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.extend(arg.annotation for arg in node.args.args if arg.annotation is not None)
            if node.returns is not None:
                annotations.append(node.returns)
    assert not any(isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr) for ann in annotations for part in ast.walk(ann))
    builtin_generics = {"list", "dict", "tuple", "set", "frozenset", "type"}
    assert not any(
        isinstance(part, ast.Subscript) and isinstance(part.value, ast.Name) and part.value.id in builtin_generics
        for ann in annotations
        for part in ast.walk(ann)
    )


def test_non_persistence_case_may_have_no_checkpoints(tmp_path):
    path = tmp_path / "ordinary.yaml"
    path.write_text(
        """schema_version: 1
cases:
  - id: ordinary-case
    suite: ordinary
    kind: simple-safe
    tags: [core]
    hosts: [prime]
    repetitions: 1
    turns:
      - index: 1
        prompt: Ответь.
        golden_id: golden-core
        checkpoint: false
""",
        encoding="utf-8",
    )
    assert schema_mod.load_cases((path,))[0].turns[0].checkpoint is False


def test_schema_version_float_one_is_rejected(tmp_path):
    path = tmp_path / "float-version.yaml"
    path.write_text((FIXTURES / "valid-case.yaml").read_text(encoding="utf-8").replace("schema_version: 1", "schema_version: 1.0"), encoding="utf-8")
    with pytest.raises(schema_mod.SchemaError, match="schema_version"):
        schema_mod.load_cases((path,))


def test_persistence_case_cannot_omit_checkpoint_markers(tmp_path):
    path = tmp_path / "persistence.yaml"
    text = (FIXTURES / "valid-case.yaml").read_text(encoding="utf-8")
    text = text.replace("suite: smoke", "suite: persistence").replace("kind: deterministic", "kind: persistence")
    text = text.replace("checkpoint: true", "checkpoint: false")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(schema_mod.SchemaError, match="persistence checkpoints"):
        schema_mod.load_cases((path,))
