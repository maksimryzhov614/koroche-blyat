"""Host runners and the live matrix — Task 11.

Exercised against fake executables that replay pinned event fixtures, so the
argv contract, isolation, timeout and error semantics are all tested without a
single paid call.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.host_runners import (
    CREDENTIAL_ENV,
    HostEventError,
    RunConfig,
    build_argv,
    effective_seed,
    parse_events,
    prepare_environment,
    redact,
    run_host,
)
from evals.run_live import BLACK_BOX_ARMS, plan_calls

ROOT = Path(__file__).parents[1]
FAKE_BIN = ROOT / "tests/fixtures/fake-bin"
EVENTS = ROOT / "tests/fixtures/host-events"
FIXTURES = {
    "prime": EVENTS / "prime.jsonl",
    "codex": EVENTS / "codex.jsonl",
    "claude": EVENTS / "claude.json",
}
BINARIES = {"prime": "prime-agent", "codex": "codex", "claude": "claude"}
CONFIG = RunConfig(model="example-model", provider="example", timeout_s=20)


def _environment(tmp_path: Path, host: str, **extra) -> dict:
    environment = prepare_environment(
        {"PATH": "%s:%s" % (FAKE_BIN, os.environ["PATH"])}, tmp_path / "home"
    )
    environment["FAKE_HOST_FIXTURE"] = str(FIXTURES[host])
    environment.update(extra)
    return environment


# --- argv contract -----------------------------------------------------------

def test_prime_argv_is_the_pinned_family():
    argv = build_argv("prime", "привет", CONFIG, session_dir=Path("/tmp/s"))
    assert argv[:5] == ["prime-agent", "--mode", "json", "-p", "--no-tools"]
    assert argv[-2:] == ["--", "привет"]


def test_codex_argv_switches_to_resume_without_losing_flags():
    fresh = build_argv("codex", "p", CONFIG)
    resumed = build_argv("codex", "p", CONFIG, resume="th_1")
    assert fresh[:2] == ["codex", "exec"]
    assert resumed[:4] == ["codex", "exec", "resume", "th_1"]
    assert "--json" in resumed and "--skip-git-repo-check" in resumed


def test_claude_argv_uses_session_id_or_resume_but_never_both():
    fresh = build_argv("claude", "p", CONFIG, session_id="uuid-1")
    resumed = build_argv("claude", "p", CONFIG, session_id="uuid-1", resume="uuid-1")
    assert "--session-id" in fresh and "--resume" not in fresh
    assert "--resume" in resumed and "--session-id" not in resumed


def test_optional_flags_appear_only_when_configured():
    bare = build_argv("prime", "p", RunConfig())
    assert "--model" not in bare and "--provider" not in bare


def test_a_prompt_with_shell_metacharacters_stays_one_argument():
    prompt = "; rm -rf / && echo $(whoami) `id`"
    argv = build_argv("codex", prompt, CONFIG)
    assert argv[-1] == prompt


def test_unknown_host_is_rejected():
    with pytest.raises(ValueError):
        build_argv("cursor", "p", CONFIG)


# --- isolation ---------------------------------------------------------------

def test_every_config_root_is_redirected_into_the_isolated_home(tmp_path):
    environment = prepare_environment({"PATH": "/usr/bin"}, tmp_path / "home")
    home = str(tmp_path / "home")
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                 "CODEX_HOME", "CLAUDE_CONFIG_DIR", "PRIME_AGENT_CODING_AGENT_DIR"):
        assert environment[name].startswith(home), name
    assert environment["HOME"] == home


def test_only_allowlisted_credentials_are_forwarded(tmp_path):
    base = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-secret", "MY_PRIVATE_TOKEN": "nope"}
    environment = prepare_environment(base, tmp_path / "home")
    assert environment["ANTHROPIC_API_KEY"] == "sk-secret"
    assert "MY_PRIVATE_TOKEN" not in environment
    assert set(CREDENTIAL_ENV) >= {"ANTHROPIC_API_KEY"}


def test_secrets_are_redacted_from_captured_output():
    assert "sk-" not in redact("token sk-abcdef0123456789 end")
    assert "[redacted]" in redact("token sk-abcdef0123456789 end")


# --- event parsing -----------------------------------------------------------

@pytest.mark.parametrize("host", sorted(FIXTURES))
def test_pinned_fixtures_parse_into_normalized_usage(host):
    parsed = parse_events(host, FIXTURES[host].read_text(encoding="utf-8"))
    assert parsed["text"]
    assert parsed["output_tokens"] > 0
    assert parsed["input_tokens"] > 0


def test_prime_fixture_is_a_real_capture_not_an_invention():
    """The Prime fixture must carry the event shape the host actually emits.

    An earlier version of this file used session_start/assistant_message/usage,
    which Prime 0.7.2 never emits, so every test here passed against fiction.
    """
    types = {
        json.loads(line)["type"]
        for line in FIXTURES["prime"].read_text(encoding="utf-8").splitlines() if line.strip()
    }
    assert {"session", "turn_end", "agent_end"} <= types
    assert "assistant_message" not in types


def test_claude_cache_fields_map_to_the_normalized_names():
    parsed = parse_events("claude", FIXTURES["claude"].read_text(encoding="utf-8"))
    assert parsed["cache_read_tokens"] == 32
    assert parsed["cache_write_tokens"] == 8


def test_codex_reports_no_cache_write_as_null_not_zero():
    parsed = parse_events("codex", FIXTURES["codex"].read_text(encoding="utf-8"))
    assert parsed["cache_write_tokens"] is None
    assert parsed["cache_read_tokens"] == 64


@pytest.mark.parametrize("host", sorted(FIXTURES))
def test_a_changed_event_shape_raises_instead_of_reporting_zero_usage(host):
    with pytest.raises(HostEventError):
        parse_events(host, '{"type":"something-else"}\n')


@pytest.mark.parametrize("host", sorted(FIXTURES))
def test_empty_output_is_an_infrastructure_error(host):
    with pytest.raises(HostEventError):
        parse_events(host, "")


# --- subprocess control ------------------------------------------------------

def _expected(host):
    """Expectations come from the fixture, never from a hand-written constant.

    The first version of this suite asserted invented numbers against an
    invented Prime fixture, so it passed while the parser could not read a
    single real transcript.
    """
    return parse_events(host, FIXTURES[host].read_text(encoding="utf-8"))


@pytest.mark.parametrize("host", sorted(FIXTURES))
def test_run_host_executes_the_fake_binary_and_records_usage(tmp_path, host):
    environment = _environment(tmp_path, host)
    argv = build_argv(host, "prompt", CONFIG)
    argv[0] = str(FAKE_BIN / BINARIES[host])
    result = run_host(host, argv, environment, tmp_path, 20)
    expected = _expected(host)
    assert result.exit_code == 0
    assert result.output_tokens == expected["output_tokens"] > 0
    assert result.text == expected["text"]


def test_argv_reaches_the_host_exactly_as_built(tmp_path):
    log = tmp_path / "argv.jsonl"
    environment = _environment(tmp_path, "codex", FAKE_HOST_ARGV_LOG=str(log))
    argv = build_argv("codex", "живой промпт", CONFIG)
    argv[0] = str(FAKE_BIN / "codex")
    run_host("codex", argv, environment, tmp_path, 20)
    recorded = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["argv"]
    assert recorded[1:] == argv[1:]


def test_a_working_directory_with_spaces_is_supported(tmp_path):
    workdir = tmp_path / "work dir with spaces"
    workdir.mkdir()
    environment = _environment(tmp_path, "prime")
    argv = build_argv("prime", "prompt", CONFIG, session_dir=workdir / "s")
    argv[0] = str(FAKE_BIN / "prime-agent")
    result = run_host("prime", argv, environment, workdir, 20)
    assert result.exit_code == 0


def test_a_nonzero_exit_is_an_infrastructure_error(tmp_path):
    environment = _environment(tmp_path, "prime", FAKE_HOST_MODE="nonzero")
    argv = build_argv("prime", "prompt", CONFIG)
    argv[0] = str(FAKE_BIN / "prime-agent")
    with pytest.raises(HostEventError):
        run_host("prime", argv, environment, tmp_path, 20)


def test_garbage_output_is_an_infrastructure_error(tmp_path):
    environment = _environment(tmp_path, "prime", FAKE_HOST_MODE="garbage")
    argv = build_argv("prime", "prompt", CONFIG)
    argv[0] = str(FAKE_BIN / "prime-agent")
    with pytest.raises(HostEventError):
        run_host("prime", argv, environment, tmp_path, 20)


def test_a_hung_host_is_killed_by_process_group(tmp_path):
    environment = _environment(tmp_path, "prime", FAKE_HOST_MODE="hang")
    argv = build_argv("prime", "prompt", CONFIG)
    argv[0] = str(FAKE_BIN / "prime-agent")
    with pytest.raises(HostEventError) as error:
        run_host("prime", argv, environment, tmp_path, 1)
    assert "timed out" in str(error.value)


# --- seeds -------------------------------------------------------------------

@pytest.mark.parametrize("host", sorted(FIXTURES))
def test_an_unsupported_seed_is_recorded_as_null(host):
    assert effective_seed(host, RunConfig(seed=7)) is None


# --- planning and the CLI ----------------------------------------------------

def test_black_box_mode_allows_only_baseline_and_merged():
    assert BLACK_BOX_ARMS == ("baseline", "merged")


def _cli(args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "evals.run_live"] + args,
        cwd=ROOT, capture_output=True, text=True, **kwargs
    )


def test_dry_run_prints_the_matrix_and_starts_no_process(tmp_path):
    result = _cli(["--mode", "black-box", "--host", "prime", "--arm", "baseline",
                   "--repetitions", "1", "--dry-run"])
    assert result.returncode == 0, result.stderr
    matrix = json.loads(result.stdout)
    assert matrix["dry_run"] is True
    assert matrix["planned_calls"] == len(matrix["calls"]) > 0
    assert matrix["seeds"]["prime"] is None


def test_live_run_refuses_without_confirmation(tmp_path):
    result = _cli(["--mode", "black-box", "--host", "prime", "--arm", "baseline",
                   "--repetitions", "1", "--output", str(tmp_path / "out")])
    assert result.returncode == 2
    assert "--confirm-live" in result.stderr
    assert not (tmp_path / "out").exists()


def test_controlled_arm_is_rejected_in_black_box_mode(tmp_path):
    result = _cli(["--mode", "black-box", "--arm", "voice-only", "--dry-run"])
    assert result.returncode == 2
    assert "not allowed" in result.stderr


def test_existing_output_is_rejected_before_any_host_runs(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    result = _cli(["--mode", "black-box", "--host", "prime", "--arm", "baseline",
                   "--repetitions", "1", "--output", str(output), "--confirm-live",
                   "--provider", "example", "--model", "example-model"])
    assert result.returncode == 2
    assert "immutable" in result.stderr


def test_live_run_requires_provider_and_model(tmp_path):
    result = _cli(["--mode", "black-box", "--host", "prime", "--arm", "baseline",
                   "--repetitions", "1", "--output", str(tmp_path / "out"), "--confirm-live"])
    assert result.returncode == 2
    assert "--provider" in result.stderr


def test_unknown_host_is_rejected_with_exit_two():
    assert _cli(["--mode", "black-box", "--host", "cursor", "--dry-run"]).returncode == 2


def test_plan_calls_expands_every_turn_of_a_multi_turn_case():
    from evals.schema import load_cases

    cases = load_cases([ROOT / "evals/cases/token-sessions.yaml"])
    calls = plan_calls(cases, ["prime"], ["merged"], 2)
    assert len(calls) == (1 + 5 + 20) * 2
    assert {call["session_length"] for call in calls} == {1, 5, 20}


def test_full_run_against_fake_hosts_records_every_call(tmp_path):
    environment = dict(os.environ)
    environment["PATH"] = "%s:%s" % (FAKE_BIN, environment["PATH"])
    output = tmp_path / "capture"
    result = _cli(
        ["--mode", "black-box", "--host", "prime", "--arm", "merged",
         "--cases", "evals/cases/token-sessions.yaml", "--repetitions", "1",
         "--output", str(output), "--confirm-live",
         "--provider", "example", "--model", "example-model"],
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (output / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert manifest["expected"] == manifest["recorded"] == len(records) == 26
    assert manifest["failures"] == 0
    expected = _expected("prime")
    assert all(record["output_tokens"] == expected["output_tokens"] for record in records)
    assert all(record["seed"] is None for record in records)


@pytest.fixture
def broken_fake_host():
    """Fail the fake host through a file, not the environment.

    prepare_environment strips everything outside its allowlist, so an
    environment variable would never reach the child — which is exactly the
    isolation guarantee this suite is meant to keep.
    """
    marker = FAKE_BIN / "MODE"
    marker.write_text("garbage\n", encoding="utf-8")
    try:
        yield
    finally:
        marker.unlink()


def test_a_broken_host_keeps_the_call_in_the_denominator(tmp_path, broken_fake_host):
    environment = dict(os.environ)
    environment["PATH"] = "%s:%s" % (FAKE_BIN, environment["PATH"])
    output = tmp_path / "capture"
    result = _cli(
        ["--mode", "black-box", "--host", "prime", "--arm", "merged",
         "--cases", "evals/cases/token-sessions.yaml", "--repetitions", "1",
         "--output", str(output), "--confirm-live",
         "--provider", "example", "--model", "example-model"],
        env=environment,
    )
    assert result.returncode == 1
    records = [
        json.loads(line)
        for line in (output / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 26
    assert all(record["infrastructure_error"] for record in records)
    assert all(record["output_tokens"] is None for record in records)
