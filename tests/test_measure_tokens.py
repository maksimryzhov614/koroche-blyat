"""Paired token measurement — Task 12 Steps 1-2.

The point of this module is to make a public percentage hard to earn. Missing
pairs fail the release instead of quietly shrinking the denominator, a lower
bound of zero forbids a claim, and one failed hard-safety grade forbids it
regardless of how good the numbers look.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals.measure_tokens import (
    BOOTSTRAP_SEED,
    claim_text,
    measure,
    pair_records,
)
from evals.schema import SnapshotRecord

ROOT = Path(__file__).parents[1]
CONTROL = "concise-russian-control"
TREATMENT = "merged"


def _record(arm, case_id, repetition, session_length, output, turn=1, **kwargs):
    values = dict(
        host="prime", provider="example", model="example-model",
        seed=None, session_length=session_length, output_tokens=output,
        input_tokens=100, total_tokens=100 + output,
        cache_read_tokens=None, cache_write_tokens=None,
    )
    values.update(kwargs)
    return SnapshotRecord(
        values.pop("host"), arm, case_id, repetition, turn, "text", None,
        False, None, values.pop("provider"), values.pop("model"), **values
    )


def _matrix(lengths=(1, 5, 20), cases=10, repetitions=3, control=100, treatment=80):
    records = []
    for length in lengths:
        for index in range(cases):
            for repetition in range(1, repetitions + 1):
                case_id = "case-%02d" % index
                records.append(_record(CONTROL, case_id, repetition, length, control))
                records.append(_record(TREATMENT, case_id, repetition, length, treatment))
    return records


# --- pairing -----------------------------------------------------------------

def test_pair_key_covers_every_identity_field():
    records = [
        _record(CONTROL, "case-01", 1, 1, 100),
        _record(TREATMENT, "case-01", 1, 1, 80),
    ]
    pairs, missing = pair_records(records, CONTROL, TREATMENT)
    assert missing == ()
    assert len(pairs) == 1
    key = pairs[0].key
    assert (key.host, key.provider, key.model, key.case_id, key.repetition,
            key.session_length, key.seed) == ("prime", "example", "example-model", "case-01", 1, 1, None)


def test_a_missing_counterpart_is_reported_not_dropped():
    records = [
        _record(CONTROL, "case-01", 1, 1, 100),
        _record(TREATMENT, "case-01", 1, 1, 80),
        _record(CONTROL, "case-02", 1, 1, 100),
    ]
    pairs, missing = pair_records(records, CONTROL, TREATMENT)
    assert len(pairs) == 1
    assert len(missing) == 1
    assert "case-02" in missing[0]


def test_missing_pairs_forbid_a_claim_even_when_the_rest_look_good():
    records = _matrix()
    records.append(_record(CONTROL, "case-99", 1, 1, 100))
    report = measure(records, CONTROL, TREATMENT)
    assert report.missing
    assert not report.claim_allowed
    assert any("missing" in reason for reason in report.reasons)


def test_a_seed_participates_in_the_pair_identity():
    records = [
        _record(CONTROL, "case-01", 1, 1, 100, seed=1),
        _record(TREATMENT, "case-01", 1, 1, 80, seed=2),
    ]
    pairs, missing = pair_records(records, CONTROL, TREATMENT)
    assert pairs == ()
    assert len(missing) == 2


def test_multi_turn_sessions_sum_host_reported_usage():
    records = [
        _record(CONTROL, "case-01", 1, 5, 10, turn=turn) for turn in range(1, 6)
    ] + [
        _record(TREATMENT, "case-01", 1, 5, 6, turn=turn) for turn in range(1, 6)
    ]
    pairs, missing = pair_records(records, CONTROL, TREATMENT)
    assert missing == ()
    assert pairs[0].control.output == 50
    assert pairs[0].treatment.output == 30


# --- nullable usage ----------------------------------------------------------

def test_absent_cache_fields_stay_null_and_never_become_zero():
    records = _matrix(lengths=(1,), cases=10, repetitions=3)
    report = measure(records, CONTROL, TREATMENT)
    assert report.totals["control"].cache_read is None
    assert report.totals["treatment"].cache_write is None


def test_reported_cache_fields_are_summed_separately():
    records = [
        _record(CONTROL, "case-01", 1, 1, 100, cache_read_tokens=7, cache_write_tokens=3),
        _record(TREATMENT, "case-01", 1, 1, 80, cache_read_tokens=2, cache_write_tokens=1),
    ]
    report = measure(records, CONTROL, TREATMENT)
    assert report.totals["control"].cache_read == 7
    assert report.totals["treatment"].cache_write == 1
    assert report.totals["control"].input == 100


# --- statistics --------------------------------------------------------------

def test_saving_is_the_ratio_of_summed_output_tokens():
    report = measure(_matrix(), CONTROL, TREATMENT)
    assert report.saving == pytest.approx(1 - 80 / 100)


def test_bootstrap_is_deterministic_under_the_fixed_seed():
    records = _matrix()
    first = measure(records, CONTROL, TREATMENT)
    second = measure(records, CONTROL, TREATMENT)
    assert BOOTSTRAP_SEED == 20260812
    assert (first.lower, first.upper) == (second.lower, second.upper)


def test_a_lower_bound_of_zero_forbids_a_positive_claim():
    # Treatment equals control, so the interval must include zero.
    records = _matrix(control=100, treatment=100)
    report = measure(records, CONTROL, TREATMENT)
    assert report.lower is not None and report.lower <= 0
    assert not report.claim_allowed


def test_a_clear_saving_with_complete_data_allows_a_claim():
    report = measure(_matrix(), CONTROL, TREATMENT)
    assert report.lower is not None and report.lower > 0
    assert report.claim_allowed, report.reasons


# --- release gates -----------------------------------------------------------

def test_one_failed_hard_safety_grade_forbids_a_claim():
    report = measure(_matrix(), CONTROL, TREATMENT, hard_safety_failures=1)
    assert not report.claim_allowed
    assert any("safety" in reason for reason in report.reasons)


def test_fewer_than_thirty_pairs_at_one_length_forbids_a_claim():
    records = _matrix(lengths=(1, 5), cases=10, repetitions=3)
    records += [
        record for record in _matrix(lengths=(20,), cases=10, repetitions=1)
    ]
    report = measure(records, CONTROL, TREATMENT)
    assert not report.claim_allowed
    assert any("30" in reason or "thirty" in reason for reason in report.reasons)


def test_fewer_than_ten_case_ids_forbids_a_claim():
    report = measure(_matrix(cases=9, repetitions=4), CONTROL, TREATMENT)
    assert not report.claim_allowed
    assert any("case" in reason for reason in report.reasons)


def test_every_session_length_must_be_present():
    report = measure(_matrix(lengths=(1, 5)), CONTROL, TREATMENT)
    assert not report.claim_allowed


# --- generated claim ---------------------------------------------------------

def test_a_forbidden_claim_renders_the_explicit_decline():
    report = measure(_matrix(control=100, treatment=100), CONTROL, TREATMENT)
    assert claim_text(report) == "No fixed token-saving percentage is claimed for 1.0.0."


def test_an_allowed_claim_states_the_lower_bound_not_the_point_estimate():
    report = measure(_matrix(), CONTROL, TREATMENT)
    text = claim_text(report)
    assert "%" in text
    assert "at least" in text


# --- report shape ------------------------------------------------------------

def test_report_breaks_results_down_by_session_length():
    report = measure(_matrix(), CONTROL, TREATMENT)
    assert sorted(report.by_session_length) == [1, 5, 20]
    for summary in report.by_session_length.values():
        assert summary["pairs"] == 30


def test_report_is_json_serialisable_without_timestamps():
    report = measure(_matrix(), CONTROL, TREATMENT)
    payload = json.dumps(report.to_json(), sort_keys=True)
    assert "timestamp" not in payload
    assert "20260812" in payload


# --- CLI ---------------------------------------------------------------------

def _write_snapshots(directory: Path, records):
    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        lines.append(json.dumps({
            "host": record.host, "arm": record.arm, "case_id": record.case_id,
            "repetition": record.repetition, "turn": record.turn, "text": record.text,
            "golden_id": record.golden_id, "infrastructure_error": record.infrastructure_error,
            "provider": record.provider, "model": record.model, "seed": record.seed,
            "session_length": record.session_length,
            "input_tokens": record.input_tokens, "output_tokens": record.output_tokens,
            "cache_read_tokens": record.cache_read_tokens,
            "cache_write_tokens": record.cache_write_tokens,
            "total_tokens": record.total_tokens,
        }))
    (directory / "responses.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cli_exits_one_when_the_claim_is_unsupported(tmp_path):
    snapshots = tmp_path / "snap"
    _write_snapshots(snapshots, _matrix(control=100, treatment=100))
    result = subprocess.run(
        [sys.executable, "-m", "evals.measure_tokens", "--snapshots", str(snapshots),
         "--control", CONTROL, "--treatment", TREATMENT,
         "--out-json", str(tmp_path / "tokens.json")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))
    assert payload["claim_allowed"] is False


def test_cli_exits_zero_when_the_claim_is_supported(tmp_path):
    snapshots = tmp_path / "snap"
    _write_snapshots(snapshots, _matrix())
    result = subprocess.run(
        [sys.executable, "-m", "evals.measure_tokens", "--snapshots", str(snapshots),
         "--control", CONTROL, "--treatment", TREATMENT,
         "--out-json", str(tmp_path / "tokens.json")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_exits_two_on_a_malformed_record(tmp_path):
    snapshots = tmp_path / "snap"
    snapshots.mkdir()
    (snapshots / "responses.jsonl").write_text('{"host": "prime"}\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "evals.measure_tokens", "--snapshots", str(snapshots),
         "--control", CONTROL, "--treatment", TREATMENT],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 2


def test_cli_help_documents_the_exit_contract():
    result = subprocess.run(
        [sys.executable, "-m", "evals.measure_tokens", "--help"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--bootstrap" in result.stdout
