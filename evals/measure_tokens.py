"""Paired token measurement — Task 12.

A public percentage has to be earned. Every gate here exists to stop a
plausible-looking number from reaching marketing copy:

* a session with no counterpart is reported and fails the release, because
  dropping it would shrink the denominator in the treatment's favour;
* absent cache usage stays null — a host that did not report cache reads is
  not the same as a host that read nothing;
* the claim quotes the lower bound of a paired bootstrap, never the point
  estimate, and a bound at or below zero forbids any positive claim;
* one failed hard-safety grade forbids a claim regardless of the numbers.

Bootstrap resampling is stratified by (host, model, session_length) and seeded,
so the same snapshots always produce the same interval.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

BOOTSTRAP_SEED = 20260812
BOOTSTRAP_RESAMPLES = 10000
REQUIRED_SESSION_LENGTHS = (1, 5, 20)
MINIMUM_PAIRS_PER_LENGTH = 30
MINIMUM_CASE_IDS = 10
DECLINE = "No fixed token-saving percentage is claimed for 1.0.0."

USAGE_FIELDS = ("input", "cache_read", "cache_write", "output", "total")


@dataclass(frozen=True)
class PairKey:
    host: str
    provider: Optional[str]
    model: Optional[str]
    case_id: str
    repetition: int
    session_length: int
    seed: Optional[int]


@dataclass(frozen=True)
class Usage:
    input: Optional[int]
    cache_read: Optional[int]
    cache_write: Optional[int]
    output: Optional[int]
    total: Optional[int]

    def to_json(self) -> Dict[str, Optional[int]]:
        return {name: getattr(self, name) for name in USAGE_FIELDS}


@dataclass(frozen=True)
class Pair:
    key: PairKey
    control: Usage
    treatment: Usage


@dataclass(frozen=True)
class TokenReport:
    pairs: Tuple[Pair, ...]
    missing: Tuple[str, ...]
    saving: Optional[float]
    lower: Optional[float]
    upper: Optional[float]
    totals: Mapping[str, Usage]
    by_session_length: Mapping[int, Mapping[str, object]]
    claim_allowed: bool
    reasons: Tuple[str, ...]

    def to_json(self) -> Dict[str, object]:
        return {
            "seed": BOOTSTRAP_SEED,
            "resamples": BOOTSTRAP_RESAMPLES,
            "pairs": len(self.pairs),
            "missing": list(self.missing),
            "saving": self.saving,
            "lower": self.lower,
            "upper": self.upper,
            "totals": {name: usage.to_json() for name, usage in sorted(self.totals.items())},
            "by_session_length": {
                str(length): dict(summary)
                for length, summary in sorted(self.by_session_length.items())
            },
            "claim_allowed": self.claim_allowed,
            "reasons": list(self.reasons),
            "claim": claim_text(self),
        }


def _add(left: Optional[int], right: Optional[int]) -> Optional[int]:
    """Sum usage while preserving the difference between null and zero."""
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _key_of(record) -> PairKey:
    return PairKey(
        record.host, record.provider, record.model, record.case_id,
        record.repetition,
        record.session_length if record.session_length is not None else 1,
        record.seed,
    )


def _accumulate(usage: Optional[Usage], record) -> Usage:
    if usage is None:
        usage = Usage(None, None, None, None, None)
    return Usage(
        _add(usage.input, record.input_tokens),
        _add(usage.cache_read, record.cache_read_tokens),
        _add(usage.cache_write, record.cache_write_tokens),
        _add(usage.output, record.output_tokens),
        _add(usage.total, record.total_tokens),
    )


def pair_records(
    records: Sequence, control_arm: str, treatment_arm: str
) -> Tuple[Tuple[Pair, ...], Tuple[str, ...]]:
    sessions: Dict[Tuple[PairKey, str], Usage] = {}
    for record in records:
        if record.arm not in (control_arm, treatment_arm):
            continue
        key = (_key_of(record), record.arm)
        sessions[key] = _accumulate(sessions.get(key), record)

    keys = sorted(
        {key for key, _ in sessions},
        key=lambda item: (item.host, str(item.provider), str(item.model),
                          item.case_id, item.repetition, item.session_length, str(item.seed)),
    )
    pairs: List[Pair] = []
    missing: List[str] = []
    for key in keys:
        control = sessions.get((key, control_arm))
        treatment = sessions.get((key, treatment_arm))
        if control is None or treatment is None:
            absent = control_arm if control is None else treatment_arm
            missing.append(
                "missing %s session for %s/%s repetition %d length %d"
                % (absent, key.case_id, key.host, key.repetition, key.session_length)
            )
            continue
        pairs.append(Pair(key, control, treatment))
    return tuple(pairs), tuple(missing)


def _saving(pairs: Sequence[Pair]) -> Optional[float]:
    control = sum(pair.control.output or 0 for pair in pairs)
    treatment = sum(pair.treatment.output or 0 for pair in pairs)
    if control == 0:
        return None
    return 1.0 - (float(treatment) / float(control))


def bootstrap(
    pairs: Sequence[Pair], resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> Tuple[Optional[float], Optional[float]]:
    if not pairs:
        return None, None
    strata: Dict[Tuple[str, Optional[str], int], List[Pair]] = {}
    for pair in pairs:
        strata.setdefault((pair.key.host, pair.key.model, pair.key.session_length), []).append(pair)
    ordered = [strata[name] for name in sorted(strata, key=lambda item: (item[0], str(item[1]), item[2]))]

    generator = random.Random(seed)
    samples = []
    for _ in range(resamples):
        control = 0
        treatment = 0
        for stratum in ordered:
            size = len(stratum)
            for _ in range(size):
                pair = stratum[generator.randrange(size)]
                control += pair.control.output or 0
                treatment += pair.treatment.output or 0
        if control:
            samples.append(1.0 - (float(treatment) / float(control)))
    if not samples:
        return None, None
    samples.sort()
    lower = samples[int(0.025 * (len(samples) - 1))]
    upper = samples[int(0.975 * (len(samples) - 1))]
    return lower, upper


def _totals(pairs: Sequence[Pair]) -> Dict[str, Usage]:
    result = {}
    for name, extract in (("control", lambda p: p.control), ("treatment", lambda p: p.treatment)):
        usage = Usage(None, None, None, None, None)
        for pair in pairs:
            side = extract(pair)
            usage = Usage(
                _add(usage.input, side.input),
                _add(usage.cache_read, side.cache_read),
                _add(usage.cache_write, side.cache_write),
                _add(usage.output, side.output),
                _add(usage.total, side.total),
            )
        result[name] = usage
    return result


def measure(
    records: Sequence,
    control_arm: str,
    treatment_arm: str,
    hard_safety_failures: int = 0,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> TokenReport:
    pairs, missing = pair_records(records, control_arm, treatment_arm)
    saving = _saving(pairs)
    lower, upper = bootstrap(pairs, resamples=resamples)

    by_length: Dict[int, Dict[str, object]] = {}
    for pair in pairs:
        summary = by_length.setdefault(
            pair.key.session_length, {"pairs": 0, "case_ids": set(), "saving": None}
        )
        summary["pairs"] = int(summary["pairs"]) + 1
        summary["case_ids"].add(pair.key.case_id)  # type: ignore[union-attr]
    for length, summary in by_length.items():
        subset = [pair for pair in pairs if pair.key.session_length == length]
        summary["saving"] = _saving(subset)
        summary["case_ids"] = len(summary["case_ids"])  # type: ignore[arg-type]

    reasons: List[str] = []
    if missing:
        reasons.append("%d missing paired session(s)" % len(missing))
    if hard_safety_failures:
        reasons.append("%d failed hard-safety grade(s)" % hard_safety_failures)
    for length in REQUIRED_SESSION_LENGTHS:
        summary = by_length.get(length)
        if summary is None:
            reasons.append("session length %d has no pairs" % length)
            continue
        if int(summary["pairs"]) < MINIMUM_PAIRS_PER_LENGTH:
            reasons.append(
                "session length %d has %d pairs; at least %d are required"
                % (length, int(summary["pairs"]), MINIMUM_PAIRS_PER_LENGTH)
            )
        if int(summary["case_ids"]) < MINIMUM_CASE_IDS:
            reasons.append(
                "session length %d covers %d case ids; at least %d are required"
                % (length, int(summary["case_ids"]), MINIMUM_CASE_IDS)
            )
    if lower is None or lower <= 0:
        reasons.append("the lower bound does not exceed zero")

    return TokenReport(
        pairs=pairs, missing=missing, saving=saving, lower=lower, upper=upper,
        totals=_totals(pairs), by_session_length=by_length,
        claim_allowed=not reasons, reasons=tuple(reasons),
    )


def claim_text(report: TokenReport) -> str:
    if not report.claim_allowed or report.lower is None:
        return DECLINE
    # The claim quotes the lower bound, never the point estimate.
    return "Measured on paired sessions: at least %.1f%% fewer output tokens than the concise control." % (
        report.lower * 100.0
    )


def _load_records(directory: Path) -> List:
    from evals.schema import SnapshotRecord

    path = directory / "responses.jsonl"
    if not path.is_file():
        raise ValueError("no responses.jsonl under %s" % directory)
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            # Keyword arguments only: SnapshotRecord carries optional fields
            # such as policy_sha256 between model and seed, and positional
            # construction silently shifts every later value by one.
            records.append(SnapshotRecord(
                host=payload["host"], arm=payload["arm"], case_id=payload["case_id"],
                repetition=payload["repetition"], turn=payload["turn"],
                text=payload["text"], golden_id=payload.get("golden_id"),
                infrastructure_error=payload.get("infrastructure_error", False),
                host_version=payload.get("host_version"),
                provider=payload.get("provider"), model=payload.get("model"),
                seed=payload.get("seed"), session_id=payload.get("session_id"),
                session_length=payload.get("session_length"), prompt=payload.get("prompt"),
                input_tokens=payload.get("input_tokens"),
                cache_read_tokens=payload.get("cache_read_tokens"),
                cache_write_tokens=payload.get("cache_write_tokens"),
                output_tokens=payload.get("output_tokens"),
                total_tokens=payload.get("total_tokens"),
            ))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("line %d of %s is malformed: %s" % (number, path, error))
    return records


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.measure_tokens",
        description="Paired token measurement. Exit 0 when the requested claim is supported, "
                    "1 when the data is complete but the claim is not, 2 on malformed input.",
    )
    parser.add_argument("--snapshots", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--treatment", required=True)
    parser.add_argument("--grades", default=None)
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_RESAMPLES)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    try:
        options = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return 0 if error.code == 0 else 2

    try:
        records = _load_records(Path(options.snapshots))
    except (OSError, ValueError) as error:
        sys.stderr.write("error: %s\n" % error)
        return 2

    failures = 0
    if options.grades:
        try:
            grades = json.loads(Path(options.grades).read_text(encoding="utf-8"))
            failures = int(grades.get("hard_safety_failures", 0))
        except (OSError, ValueError) as error:
            sys.stderr.write("error: %s\n" % error)
            return 2

    report = measure(records, options.control, options.treatment,
                     hard_safety_failures=failures, resamples=options.bootstrap)
    payload = report.to_json()
    if options.out_json:
        target = Path(options.out_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if options.out_md:
        target = Path(options.out_md)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Token measurement", "", claim_text(report), "",
                 "- pairs: %d" % len(report.pairs)]
        for reason in report.reasons:
            lines.append("- blocked: %s" % reason)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sys.stdout.write(claim_text(report) + "\n")
    for reason in report.reasons:
        sys.stdout.write("blocked: %s\n" % reason)
    return 0 if report.claim_allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
