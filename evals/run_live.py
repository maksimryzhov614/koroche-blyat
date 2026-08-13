"""Live host runs — Task 11.

`--dry-run` prints the call matrix and starts no process at all. No live
subprocess runs without `--confirm-live`; that flag is the authorization gate,
and preflight rejections happen before any host is even resolved, so a
misconfigured invocation cannot spend money.

`black-box` allows only the baseline and merged arms and applies no prompt
override: merged means the real installed package, not an injected string.
`controlled` allows all five arms and injects a policy purely to isolate the
experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from evals.host_runners import HOSTS, HostEventError, RunConfig, effective_seed
from evals.schema import load_cases

BLACK_BOX_ARMS = ("baseline", "merged")
CONTROLLED_ARMS = (
    "baseline", "concise-russian-control", "compression-only", "voice-only", "merged",
)
MODES = ("black-box", "controlled")


def plan_calls(cases, hosts: Sequence[str], arms: Sequence[str], repetitions: int) -> List[Dict[str, object]]:
    calls = []
    for host in hosts:
        for arm in arms:
            for case in cases:
                if case.hosts and host not in case.hosts:
                    continue
                for repetition in range(1, repetitions + 1):
                    for turn in case.turns:
                        calls.append({
                            "host": host,
                            "arm": arm,
                            "case_id": case.id,
                            "repetition": repetition,
                            "turn": turn.index,
                            "session_length": len(case.turns),
                            "prompt_sha256": hashlib.sha256(turn.prompt.encode("utf-8")).hexdigest(),
                        })
    return calls


def _fail(message: str) -> int:
    sys.stderr.write("error: %s\n" % message)
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.run_live",
        description="Run the behaviour matrix against real hosts. Exit 0 when every planned "
                    "call was recorded, 1 when a call failed, 2 on CLI, schema or preflight error.",
    )
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--suite", default="release")
    parser.add_argument("--host", action="append", dest="hosts", default=None)
    parser.add_argument("--arm", action="append", dest="arms", default=None)
    parser.add_argument("--cases", default="evals/cases")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    try:
        options = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return 0 if error.code == 0 else 2

    hosts = tuple(options.hosts or HOSTS)
    unknown_hosts = [host for host in hosts if host not in HOSTS]
    if unknown_hosts:
        return _fail("unknown host(s): %s" % ", ".join(sorted(unknown_hosts)))

    allowed = BLACK_BOX_ARMS if options.mode == "black-box" else CONTROLLED_ARMS
    arms = tuple(options.arms or allowed)
    forbidden = [arm for arm in arms if arm not in allowed]
    if forbidden:
        return _fail(
            "arm(s) %s are not allowed in %s mode" % (", ".join(sorted(forbidden)), options.mode)
        )

    if options.repetitions < 1:
        return _fail("--repetitions must be positive")

    case_dir = Path(options.cases)
    case_files = tuple(sorted(case_dir.glob("*.yaml"))) if case_dir.is_dir() else (case_dir,)
    try:
        cases = [case for case in load_cases(case_files) if case.suite == options.suite]
    except Exception as error:  # noqa: BLE001 - reported as a preflight failure
        return _fail(str(error))
    if not cases:
        return _fail("suite %r matched no cases" % options.suite)

    calls = plan_calls(cases, hosts, arms, options.repetitions)
    config = RunConfig(
        model=options.model, provider=options.provider,
        timeout_s=options.timeout, seed=options.seed,
    )
    matrix = {
        "dry_run": bool(options.dry_run),
        "mode": options.mode,
        "suite": options.suite,
        "hosts": list(hosts),
        "arms": list(arms),
        "repetitions": options.repetitions,
        "planned_calls": len(calls),
        "seeds": {host: effective_seed(host, config) for host in hosts},
        "calls": calls,
    }

    if options.dry_run:
        sys.stdout.write(json.dumps(matrix, sort_keys=True) + "\n")
        return 0

    # Every remaining check runs before a host binary is resolved, so a
    # misconfigured live invocation cannot start a paid call.
    if not options.confirm_live:
        return _fail("live runs require --confirm-live")
    if not options.output:
        return _fail("live runs require --output")
    output = Path(options.output)
    if output.exists() or output.with_name("." + output.name + ".partial").exists():
        return _fail("output path %s already exists; captured evidence is immutable" % output)
    if not options.model or not options.provider:
        return _fail("live runs require --provider and --model")

    return _execute(calls, cases, config, output, matrix)


def _execute(calls, cases, config: RunConfig, output: Path, matrix) -> int:
    import os

    from evals.host_runners import (
        build_argv, prepare_environment, resolve_host_binary, run_host,
    )

    prompts = {
        (case.id, turn.index): turn.prompt for case in cases for turn in case.turns
    }
    goldens = {
        (case.id, turn.index): turn.golden_id for case in cases for turn in case.turns
    }
    partial = output.with_name("." + output.name + ".partial")
    (partial / "raw").mkdir(parents=True, exist_ok=True)

    records = []
    failures = 0
    for index, call in enumerate(calls):
        host = call["host"]
        home = partial / "homes" / ("%s-%s-%s-%d" % (host, call["arm"], call["case_id"], call["repetition"]))
        environment = prepare_environment(os.environ, home)
        binary = resolve_host_binary(host, environment)
        if binary is None:
            sys.stderr.write("error: %s binary not found on PATH\n" % host)
            return 2
        prompt = prompts[(call["case_id"], call["turn"])]
        argv = build_argv(host, prompt, config, session_dir=home / "sessions")
        argv[0] = binary
        record = {
            "host": host, "arm": call["arm"], "case_id": call["case_id"],
            "repetition": call["repetition"], "turn": call["turn"],
            "session_length": call["session_length"],
            "golden_id": goldens[(call["case_id"], call["turn"])],
            "provider": config.provider, "model": config.model,
            "seed": effective_seed(host, config),
            "prompt": prompt, "prompt_sha256": call["prompt_sha256"],
        }
        try:
            result = run_host(host, argv, environment, output.parent, config.timeout_s)
        except HostEventError as error:
            # A changed event shape is infrastructure, never zero usage and
            # never a skipped case: the call stays in the denominator.
            failures += 1
            record.update({
                "text": "", "infrastructure_error": True, "error": str(error),
                "input_tokens": None, "cache_read_tokens": None,
                "cache_write_tokens": None, "output_tokens": None, "total_tokens": None,
                "exit_code": None, "duration_ms": None,
            })
            records.append(record)
            continue
        stdout_path = "raw/%06d.stdout" % index
        stderr_path = "raw/%06d.stderr" % index
        (partial / stdout_path).write_text(result.stdout, encoding="utf-8")
        (partial / stderr_path).write_text(result.stderr, encoding="utf-8")
        record.update({
            "text": result.text, "infrastructure_error": False,
            "response_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            "input_tokens": result.input_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "cache_write_tokens": result.cache_write_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
            "exit_code": result.exit_code, "duration_ms": result.duration_ms,
            "session_id": result.session_id,
            "stdout_path": stdout_path, "stderr_path": stderr_path,
        })
        records.append(record)

    lines = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    (partial / "responses.jsonl").write_text(lines, encoding="utf-8")
    manifest = dict(matrix)
    manifest.pop("calls", None)
    manifest["expected"] = len(calls)
    manifest["recorded"] = len(records)
    manifest["failures"] = failures
    manifest["responses_sha256"] = hashlib.sha256(lines.encode("utf-8")).hexdigest()
    (partial / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    partial.rename(output)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
