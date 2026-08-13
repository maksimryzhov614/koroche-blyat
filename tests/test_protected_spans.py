"""Protected-span regressions — Task 10 Step 4.

These grade synthetic responses against the *real* release goldens, so a
mutation in either the grader or the fixtures shows up here. Each mutation
class from the plan gets a paired positive control, because a test that only
asserts failure would still pass if the grader rejected everything.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evals.grade import grade_response
from evals.schema import SnapshotRecord, load_goldens

ROOT = Path(__file__).parents[1]
GOLDEN_FILES = (ROOT / "evals/goldens/protected-spans.yaml",)
LEXICON = yaml.safe_load((ROOT / "evals/goldens/lexicon.yaml").read_text(encoding="utf-8"))

NFC_CAFE = "café"
NFD_CAFE = "café"
ZWJ_LOGIN = "dev‍ops"
EXACT_ERROR = "TypeError: Cannot read properties of undefined (reading 'map')"


def _goldens():
    return {golden.id: golden for golden in load_goldens(GOLDEN_FILES)}


def _grade(text, golden_id):
    golden = _goldens()[golden_id]
    record = SnapshotRecord("prime", "merged", "case-under-test", 1, 1, text, golden.id)
    return grade_response(record, golden, LEXICON)


def _spans_ok(grade):
    return grade.protected_total > 0 and grade.protected_passed == grade.protected_total


# --- baseline: the fixtures themselves are satisfiable -----------------------

def test_faithful_response_satisfies_every_protected_span():
    grade = _grade(
        "Ошибка ясная: %s — массив не пришёл, приходит undefined." % EXACT_ERROR,
        "g-protected-exact-error",
    )
    assert _spans_ok(grade)


# --- deletion ----------------------------------------------------------------

def test_deleted_span_fails_even_when_the_answer_is_otherwise_correct():
    grade = _grade(
        "Массив не пришёл, поэтому падает на map: приходит undefined.",
        "g-protected-exact-error",
    )
    assert not _spans_ok(grade)
    assert not grade.passed


# --- substitution ------------------------------------------------------------

def test_substituted_digits_fail_the_span():
    ok = _grade("Health-check должен идти на 8081, а не на приложение.", "g-protected-port")
    bad = _grade("Health-check должен идти на 8080, а не на приложение.", "g-protected-port")
    assert _spans_ok(ok)
    assert not _spans_ok(bad)


def test_reworded_error_text_fails_the_span():
    grade = _grade(
        "Ошибка: TypeError: cannot read properties of undefined (reading 'map') — undefined.",
        "g-protected-exact-error",
    )
    assert not _spans_ok(grade)


# --- normalization -----------------------------------------------------------

def test_nfd_normalized_into_nfc_fails_because_bytes_changed():
    faithful = _grade(
        "Разные байты: %s и %s — юникод-нормализация." % (NFC_CAFE, NFD_CAFE),
        "g-protected-nfc-nfd",
    )
    normalized = _grade(
        "Разные байты: %s и %s — юникод-нормализация." % (NFC_CAFE, NFC_CAFE),
        "g-protected-nfc-nfd",
    )
    assert _spans_ok(faithful)
    assert not _spans_ok(normalized)


def test_stripping_a_zero_width_joiner_fails_the_span():
    faithful = _grade(
        "Логин содержит невидимый символ нулевой ширины: %s" % ZWJ_LOGIN,
        "g-protected-zwj",
    )
    stripped = _grade(
        "Логин содержит невидимый символ нулевой ширины: devops",
        "g-protected-zwj",
    )
    assert _spans_ok(faithful)
    assert not _spans_ok(stripped)


# --- duplication -------------------------------------------------------------

def test_duplicated_span_fails_an_exact_occurrence_count():
    grade = _grade(
        "Health-check идёт на 8081. Повторю: 8081.",
        "g-protected-port",
    )
    assert not _spans_ok(grade)


def test_occurrence_count_is_exact_not_at_least():
    once = _grade("Нужный порт — 8081, сервис слушает другой.", "g-protected-port")
    twice = _grade("Порт 8081, ещё раз 8081, и снова 8081.", "g-protected-port")
    assert _spans_ok(once)
    assert not _spans_ok(twice)


# --- order inversion ---------------------------------------------------------

def test_correct_step_order_passes_the_order_rule():
    grade = _grade(
        "Порядок такой: снять нагрузку, затем сделать снимок, затем восстановить.",
        "g-protected-ordering",
    )
    assert grade.order_total == 1
    assert grade.order_passed == 1


def test_transposed_steps_fail_the_order_rule_while_bytes_survive():
    grade = _grade(
        "Порядок такой: восстановить, затем сделать снимок, затем снять нагрузку.",
        "g-protected-ordering",
    )
    assert _spans_ok(grade), "the three step phrases are still present exactly once"
    assert grade.order_passed == 0
    assert not grade.passed


@pytest.mark.parametrize(
    "golden_id",
    ["g-protected-fenced-code", "g-protected-shell-argv", "g-protected-url", "g-protected-sha256"],
)
def test_every_declared_span_is_reproducible_from_its_own_bytes(golden_id):
    golden = _goldens()[golden_id]
    text = " ".join(
        span.expected_bytes().decode("utf-8") * span.occurrences
        for span in golden.protected_spans
    )
    record = SnapshotRecord("prime", "merged", "case-under-test", 1, 1, text, golden.id)
    grade = grade_response(record, golden, LEXICON)
    assert _spans_ok(grade), golden_id
