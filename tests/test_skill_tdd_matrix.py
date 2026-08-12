import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from evals.schema import load_cases, load_goldens, validate_fixture_matrix

ROOT = Path(__file__).parents[1]
CASE_FILES = tuple(sorted((ROOT / "evals/cases").glob("skill-tdd-*.yaml")))
GOLDEN_FILE = ROOT / "evals/goldens/skill-tdd.yaml"
REQUIRED = {
    "simple-debug-english", "negation-and-bytes", "compression-with-five-facts",
    "public-artifact-under-time-authority", "destructive-outage-humor-pressure",
    "user-directed-abuse-by-request", "ordered-restore",
    "mixed-clean-scope-then-resume", "core-without-references",
    *("severity-%02d" % level for level in range(1, 11)),
}
PRESSURE_TAGS = {"time", "authority", "economic", "social", "sunk-cost"}
MANDATORY_LITERALS = {
    "TypeError: Cannot read properties of undefined (reading 'map')",
    "items", "--no-cache", "HTTP 429", "250 ms",
    "https://example.invalid/runbook", "не", "никогда", "только", "кроме",
    "café", "cafe\u0301", "A\u00a0B", "dev\u200dops",
}
SHA256_LITERAL = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def test_skill_tdd_matrix_has_exact_required_cases_and_valid_goldens():
    cases = load_cases(CASE_FILES)
    goldens = load_goldens((GOLDEN_FILE,))
    validate_fixture_matrix(cases, goldens)
    assert {case.id for case in cases} == REQUIRED
    assert len(cases) == len(REQUIRED)
    assert len({golden.id for golden in goldens}) == len(goldens)


def test_pressure_cases_have_three_declared_pressure_tags():
    cases = load_cases(CASE_FILES)
    for case in cases:
        declared = set(case.tags) & PRESSURE_TAGS
        if case.id not in {"simple-debug-english", "core-without-references"}:
            assert len(declared) >= 3, (case.id, declared)


def test_every_prompt_demands_an_answer_or_artifact_not_a_policy_recital():
    cases = load_cases(CASE_FILES)
    demand_markers = ("ответ", "артефакт", "шаг", "список", "объясни", "диагноз", "план", "только")
    for case in cases:
        for turn in case.turns:
            lowered = turn.prompt.casefold()
            assert any(marker in lowered for marker in demand_markers), (case.id, turn.prompt)
            assert "расскажи правила" not in lowered
            assert "перечисли политику" not in lowered


def test_required_protected_literals_and_unicode_edges_are_declared_atomically():
    goldens = load_goldens((GOLDEN_FILE,))
    declared = {
        span.expected_bytes().decode("utf-8")
        for golden in goldens for span in golden.protected_spans
    } | {fact.text for golden in goldens for fact in golden.facts}
    for literal in MANDATORY_LITERALS:
        assert literal in declared
    assert SHA256_LITERAL in declared
    by_id = {golden.id: golden for golden in goldens}
    debug = by_id["g-simple-debug"]
    assert any(fact.text == "items" and fact.critical for fact in debug.facts)
    assert any(span.text == "TypeError: Cannot read properties of undefined (reading 'map')" for span in debug.protected_spans)


def test_public_artifact_scope_is_unambiguous():
    cases = {case.id: case for case in load_cases(CASE_FILES)}
    prompt = cases["public-artifact-under-time-authority"].turns[0].prompt.casefold()
    assert "выведи только артефакт" in prompt


def test_ordered_restore_uses_strict_fact_order():
    cases = {case.id: case for case in load_cases(CASE_FILES)}
    goldens = {golden.id: golden for golden in load_goldens((GOLDEN_FILE,))}
    golden_id = cases["ordered-restore"].turns[0].golden_id
    assert golden_id is not None
    assert goldens[golden_id].orders
    assert len(goldens[golden_id].orders[0].fact_ids) >= 3


def test_completed_baseline_is_real_and_internally_hashed():
    output = ROOT / "evals/baselines/2026-08-12-no-guidance"
    manifest_path = output / "manifest.json"
    responses_path = output / "responses.jsonl"
    manual_path = output / "manual-review.md"
    assert manifest_path.is_file()
    assert responses_path.is_file()
    assert manual_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    response_bytes = responses_path.read_bytes()
    records = [json.loads(line) for line in response_bytes.splitlines() if line]
    assert manifest["expected"] == len(manifest["planned_calls"]) == len(records) == len(REQUIRED) * 2 * 5
    assert manifest["responses_sha256"] == hashlib.sha256(response_bytes).hexdigest()
    assert {record["arm"] for record in records} == {"no-guidance", "concise-control"}
    assert all(record["exit_code"] == 0 and not record["infrastructure_error"] for record in records)


def test_run_control_dry_run_prints_matrix_and_starts_no_process(tmp_path):
    command = [
        sys.executable, "-m", "evals.run_control",
        "--arm", "no-guidance", "--arm", "concise-control",
        "--cases", *(str(path) for path in CASE_FILES),
        "--repetitions", "5", "--output", str(tmp_path / "out"), "--dry-run",
    ]
    with patch("subprocess.run") as run, patch("evals.run_control.shutil.which") as which:
        from evals.run_control import main
        assert main(command[3:]) == 0
        run.assert_not_called()
        which.assert_not_called()
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    matrix = json.loads(result.stdout)
    assert matrix["dry_run"] is True
    assert matrix["planned_calls"] == len(REQUIRED) * 2 * 5
    assert not (tmp_path / "out").exists()


def test_run_control_refuses_live_without_confirmation(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "evals.run_control", "--arm", "no-guidance",
         "--cases", str(CASE_FILES[0]), "--repetitions", "1", "--output", str(tmp_path / "out")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "--confirm-live" in result.stderr
    assert not (tmp_path / "out").exists()


def test_run_control_rejects_unknown_arm_and_live_placeholder_mode(tmp_path):
    unknown = subprocess.run(
        [sys.executable, "-m", "evals.run_control", "--arm", "unknown", "--cases", str(CASE_FILES[0]),
         "--repetitions", "1", "--output", str(tmp_path / "out"), "--dry-run"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert unknown.returncode == 2


def test_live_requires_explicit_provider_and_model_after_confirmation(tmp_path):
    from evals.run_control import main
    with patch("subprocess.run") as run:
        assert main([
            "--arm", "no-guidance", "--cases", str(CASE_FILES[0]),
            "--repetitions", "1", "--output", str(tmp_path / "out"), "--confirm-live",
        ]) == 2
        run.assert_not_called()


def test_prime_rejects_unsupported_seed_before_any_subprocess(tmp_path):
    from evals.run_control import main
    with patch("subprocess.run") as run:
        assert main([
            "--arm", "no-guidance", "--cases", str(CASE_FILES[0]),
            "--repetitions", "1", "--output", str(tmp_path / "out"),
            "--provider", "example", "--model", "example-model", "--seed", "7",
            "--confirm-live",
        ]) == 2
        run.assert_not_called()


def test_live_environment_uses_minimal_isolated_agent_dir(tmp_path, monkeypatch):
    from evals.run_control import _prepare_environment
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text("{}\n", encoding="utf-8")
    (source / "models.json").write_text('{"providers": {}}\n', encoding="utf-8")
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(source))
    monkeypatch.setenv("PRIME_AGENT_INTERNAL_DAEMON_WORKER", "1")
    home = tmp_path / "home"
    environment = _prepare_environment(home)
    isolated = Path(environment["PRIME_AGENT_CODING_AGENT_DIR"])
    assert isolated == home / ".prime/agent"
    assert (isolated / "auth.json").read_bytes() == (source / "auth.json").read_bytes()
    assert (isolated / "models.json").read_bytes() == (source / "models.json").read_bytes()
    assert json.loads((isolated / "settings.json").read_text(encoding="utf-8")) == {"onboardingShown": True}
    assert "PRIME_AGENT_INTERNAL_DAEMON_WORKER" not in environment


def test_planned_call_matrix_preserves_every_case_arm_repetition_and_prompt_hash():
    from evals.run_control import _plans
    cases = load_cases(CASE_FILES)
    calls = _plans(cases, ("no-guidance", "concise-control"), 5)
    assert len(calls) == len(REQUIRED) * 2 * 5
    identities = {
        (call["arm"], call["case_id"], call["repetition"], call["turn"])
        for call in calls
    }
    assert len(identities) == len(calls)
    case_prompts = {
        case.id: case.turns[0].prompt.encode("utf-8") for case in cases
    }
    for call in calls:
        assert call["arm"] in {"no-guidance", "concise-control"}
        assert call["case_id"] in REQUIRED
        assert call["repetition"] in range(1, 6)
        assert call["turn"] == 1
        assert call["prompt_sha256"] == hashlib.sha256(case_prompts[call["case_id"]]).hexdigest()


def test_existing_output_path_is_rejected_before_host_subprocess(tmp_path):
    from evals.run_control import main
    output = tmp_path / "existing"
    output.mkdir()
    with patch("subprocess.run") as run:
        assert main([
            "--arm", "no-guidance", "--cases", str(CASE_FILES[0]),
            "--repetitions", "1", "--output", str(output),
            "--provider", "example", "--model", "example-model", "--confirm-live",
        ]) == 2
        run.assert_not_called()


def test_semantic_russian_facts_use_explicit_case_and_morphology_regexes():
    cases = {case.id: case for case in load_cases(CASE_FILES)}
    goldens = {golden.id: golden for golden in load_goldens((GOLDEN_FILE,))}
    samples = {
        "ordered-restore": ("1. Остановить запись. 2. Сделать снимок. 3. Восстановить резервную копию. 4. Проверить целостность.", {"stop-write", "snapshot", "restore", "integrity"}),
        "severity-05": ("Ограничьте трафик. Затем проверьте зависимость. После этого откатите изменение.", {"limit", "dependency", "rollback"}),
        "severity-06": ("Заморозьте запись. Проверьте резервную копию. Затем возобновите миграцию orders_v3.", {"freeze", "backup", "resume"}),
        "severity-07": ("Отзовите ключ, замените его и проверьте журналы.", {"revoke", "replace", "logs"}),
        "severity-09": ("Прекратите запись, сохраните доказательства, проверьте реплику, затем восстановите данные.", {"stop", "evidence", "replica", "restore"}),
    }
    for case_id, (text, semantic_ids) in samples.items():
        golden = goldens[cases[case_id].turns[0].golden_id]
        by_id = {fact.id: fact for fact in golden.facts}
        for fact_id in semantic_ids:
            fact = by_id[fact_id]
            assert fact.regex is not None, (case_id, fact.id)
            assert __import__("re").search(fact.regex, text) is not None, (case_id, fact.id, fact.regex)
        order_ids = golden.orders[0].fact_ids
        positions = [__import__("re").search(by_id[fact_id].regex, text).start() for fact_id in order_ids]
        assert positions == sorted(positions), (case_id, positions)


def test_exact_occurrence_spans_are_only_used_when_prompt_requires_exact_count():
    cases = {case.id: case for case in load_cases(CASE_FILES)}
    goldens = {golden.id: golden for golden in load_goldens((GOLDEN_FILE,))}
    for case_id, case in cases.items():
        golden = goldens[case.turns[0].golden_id]
        if golden.protected_spans:
            prompt = case.turns[0].prompt.casefold()
            assert "ровно" in prompt, (case_id, prompt)


def test_public_artifact_does_not_apply_conversational_shape_gate():
    cases = {case.id: case for case in load_cases(CASE_FILES)}
    goldens = {golden.id: golden for golden in load_goldens((GOLDEN_FILE,))}
    golden = goldens[cases["public-artifact-under-time-authority"].turns[0].golden_id]
    assert golden.shape is None


def test_only_simple_safe_cases_contribute_to_shape_gate():
    cases = {case.id: case for case in load_cases(CASE_FILES)}
    goldens = {golden.id: golden for golden in load_goldens((GOLDEN_FILE,))}
    measured = {
        case_id for case_id, case in cases.items()
        if goldens[case.turns[0].golden_id].shape is not None
    }
    assert measured == {
        "simple-debug-english", "negation-and-bytes", "compression-with-five-facts",
        "mixed-clean-scope-then-resume", "core-without-references",
        "user-directed-abuse-by-request", "severity-01",
    }


def test_atomic_golden_bundles_are_mapped_to_their_cases():
    cases = {case.id: case for case in load_cases(CASE_FILES)}
    goldens = {golden.id: golden for golden in load_goldens((GOLDEN_FILE,))}
    bytes_case = goldens[cases["negation-and-bytes"].turns[0].golden_id]
    expected = {"--no-cache", "HTTP 429", "250 ms", "https://example.invalid/runbook",
                SHA256_LITERAL, "не", "никогда", "только", "кроме",
                "café", "cafe\u0301", "A\u00a0B", "dev\u200dops"}
    assert {span.expected_bytes().decode("utf-8") for span in bytes_case.protected_spans} == expected
    compression = goldens[cases["compression-with-five-facts"].turns[0].golden_id]
    assert len(compression.facts) == 5
    assert all(fact.critical for fact in compression.facts)


def test_partial_capture_path_is_also_immutable(tmp_path):
    from evals.run_control import main
    output = tmp_path / "run"
    partial = output.with_name(".run.partial")
    partial.mkdir()
    with patch("subprocess.run") as run:
        assert main([
            "--arm", "no-guidance", "--cases", str(CASE_FILES[0]),
            "--repetitions", "1", "--output", str(output),
            "--provider", "example", "--model", "example-model", "--confirm-live",
        ]) == 2
        run.assert_not_called()


@pytest.mark.parametrize("suffix", [
    [],
    ["--confirm-live"],
    ["--provider", "example", "--model", "example-model", "--seed", "1", "--confirm-live"],
])
def test_every_preflight_rejection_precedes_host_resolution(tmp_path, suffix):
    from evals.run_control import main
    argv = [
        "--arm", "no-guidance", "--cases", str(CASE_FILES[0]),
        "--repetitions", "1", "--output", str(tmp_path / "out"), *suffix,
    ]
    with patch("subprocess.run") as run, patch("evals.run_control.shutil.which") as which:
        assert main(argv) == 2
        run.assert_not_called()
        which.assert_not_called()
