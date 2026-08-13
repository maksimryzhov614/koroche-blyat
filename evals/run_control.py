"""Gated fresh-context baseline runner for the skill TDD matrix."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from evals.schema import Case, SchemaError, load_cases

_SCHEMA_VERSION = 1
_GRADER_VERSION = 1
_SUPPORTED_ARMS = ("no-guidance", "concise-control", "core-only", "full-skill")
_LIVE_HOSTS = ("prime",)
_CONCISE_CONTROL = (
    "Отвечай по-русски кратко и естественно, обычно двумя-пятью фразами. "
    "Сохраняй все технические факты, порядок, отрицания и точные фрагменты. "
    "Код, команды, ошибки и публичные артефакты оставляй чистыми."
)


class RunnerError(ValueError):
    """Raised when the requested capture cannot be run safely."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture immutable fresh-context behavior evidence.")
    parser.add_argument("--arm", action="append", choices=_SUPPORTED_ARMS, required=True)
    parser.add_argument("--cases", nargs="+", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", choices=_LIVE_HOSTS, default="prime")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--skip-smoke", action="store_true",
                        help="skip the preflight probe (only for tests)")
    return parser


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"], check=False, capture_output=True, timeout=10,
    )
    text = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not text:
        raise RunnerError("cannot determine host version for %s" % executable)
    return text.splitlines()[0]


def _policy(arm: str, root: Path) -> Optional[str]:
    if arm == "no-guidance":
        return None
    if arm == "concise-control":
        return _CONCISE_CONTROL
    if arm == "core-only":
        generated = root / "adapters/generated/always-on.md"
        if not generated.exists():
            raise RunnerError("core-only arm requires %s" % generated)
        return generated.read_text(encoding="utf-8")
    skill = root / "skills/koroche-blyat/SKILL.md"
    if not skill.exists():
        raise RunnerError("full-skill arm requires %s" % skill)
    return skill.read_text(encoding="utf-8")


def _plans(cases: Sequence[Case], arms: Sequence[str], repetitions: int) -> Tuple[Mapping[str, Any], ...]:
    plans = []
    for arm in arms:
        for case in sorted(cases, key=lambda item: item.id):
            for repetition in range(1, repetitions + 1):
                for turn in case.turns:
                    prompt_bytes = turn.prompt.encode("utf-8")
                    plans.append({
                        "arm": arm, "case_id": case.id, "golden_id": turn.golden_id,
                        "repetition": repetition, "turn": turn.index,
                        "prompt_sha256": _sha256(prompt_bytes),
                    })
    return tuple(plans)


def _command(
    executable: str, prompt: str, arm: str, provider: Optional[str], model: Optional[str],
    root: Path, cwd: Path, daemon_socket: Optional[Path] = None,
) -> Tuple[str, ...]:
    # JSON event output instead of --print: --print concatenates the agent's
    # spoken plan with its answer, and 13% of the 2026-08-12 baseline begins
    # with a preamble like "Сначала быстро найду место, где падает map".
    # The shape gate then counts the plan as part of the answer, so it grades
    # something the contract never promised. The JSON stream separates the
    # final assistant message, and it also carries token usage, which the
    # text mode never provided at all.
    command = [
        executable, "--mode", "json", "-p", "--no-session", "--no-tools",
        "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
        "--no-context-files", "--cwd", str(cwd),
    ]
    if daemon_socket is not None:
        # Isolating PRIME_AGENT_CODING_AGENT_DIR alone is not isolation: every
        # call still reaches the one shared daemon socket under TMPDIR, so a
        # capture competes with the developer's own daemon and with itself.
        # That is what silently emptied 43 of 190 responses in an earlier run.
        command.extend(("--daemon-socket", str(daemon_socket)))
    policy = _policy(arm, root)
    if policy is not None:
        command.extend(("--system-prompt", policy))
    if provider:
        command.extend(("--provider", provider))
    if model:
        command.extend(("--model", model))
    command.extend(("--", prompt))
    return tuple(command)


def _prepare_environment(home: Path) -> Mapping[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("PRIME_AGENT_INTERNAL_")
    }
    source_text = environment.get("PRIME_AGENT_CODING_AGENT_DIR")
    source = Path(source_text) if source_text else Path.home() / ".prime/agent"
    isolated = home / ".prime/agent"
    isolated.mkdir(parents=True, mode=0o700)
    for name in ("auth.json", "models.json"):
        candidate = source / name
        if candidate.is_file():
            target = isolated / name
            shutil.copyfile(str(candidate), str(target))
            target.chmod(0o600)
    settings = json.dumps({"onboardingShown": True}, sort_keys=True).encode("utf-8") + b"\n"
    settings_path = isolated / "settings.json"
    settings_path.write_bytes(settings)
    settings_path.chmod(0o600)
    environment["HOME"] = str(home)
    environment["XDG_CONFIG_HOME"] = str(home / ".config")
    environment["XDG_DATA_HOME"] = str(home / ".local/share")
    environment["PRIME_AGENT_CODING_AGENT_DIR"] = str(isolated)
    environment["KOROCHE_BLYAT_UNATTENDED"] = "0"
    return environment


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.tmp" % path.name)
    temporary.write_bytes(data)
    os.replace(str(temporary), str(path))


def _partial_path(output: Path) -> Path:
    return output.with_name(".%s.partial" % output.name)


SMOKE_PROMPT = "Ответь одним словом: готов."


def smoke_check(
    executable: str, arm: str, provider: Optional[str], model: Optional[str], root: Path
) -> Tuple[bool, str]:
    """Spend one call to prove the environment answers before spending the matrix.

    Twice this runner burned a full 190-call capture into a host that returned
    nothing: once because the daemon was gone, once because the flag
    combination silently produced empty output with exit 0. Both were only
    visible after the money was spent. One probe with the exact command shape
    of the real run makes that failure cost a single call.
    """
    with tempfile.TemporaryDirectory(prefix="koroche-blyat-smoke-") as home_name, \
            tempfile.TemporaryDirectory(prefix="koroche-blyat-smoke-cwd-") as cwd_name:
        command = _command(
            executable, SMOKE_PROMPT, arm, provider, model,
            root, Path(cwd_name), Path(home_name) / "daemon.sock",
        )
        try:
            result = subprocess.run(
                command, env=_prepare_environment(Path(home_name)), check=False,
                capture_output=True, timeout=180,
            )
        except subprocess.SubprocessError as exc:
            return False, "smoke call failed to run: %s" % exc
    if result.returncode != 0:
        return False, "smoke call exited %d" % result.returncode
    raw = bytes(result.stdout).decode("utf-8", errors="replace").strip()
    if not raw:
        return False, (
            "smoke call produced no output with exit 0; the host is not "
            "answering under these flags, so the capture would record silence "
            "as data"
        )
    # Parse with the same reader the capture uses. A non-empty transcript is
    # not proof of an answer: the JSON stream can carry session and turn events
    # and no assistant message at all, and a probe that only measured length
    # would wave that through into a full capture.
    from evals.host_runners import HostEventError, parse_events
    try:
        answer = parse_events("prime", raw)["text"]
    except HostEventError as error:
        return False, "smoke transcript is unreadable: %s" % error
    if not answer.strip():
        return False, "smoke transcript carried no answer text"
    return True, answer.strip().replace("\n", " ")[:120]


def _run_live(args: argparse.Namespace, cases: Sequence[Case], plans: Sequence[Mapping[str, Any]]) -> int:
    if args.host != "prime":
        raise RunnerError("live host is not implemented: %s" % args.host)
    executable = shutil.which("prime-agent")
    if executable is None:
        raise RunnerError("prime-agent executable not found")
    root = Path(__file__).parents[1]
    host_version = _version(executable)
    try:
        runner_git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, timeout=10,
        ).stdout.decode("ascii").strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise RunnerError("cannot determine runner git SHA: %s" % exc) from exc
    by_case = {case.id: case for case in cases}
    final_output = args.output.resolve()
    output = _partial_path(final_output)
    if final_output.exists() or output.exists():
        raise RunnerError("output and partial paths must not exist before immutable capture: %s" % final_output)
    if not getattr(args, "skip_smoke", False):
        arms = sorted({planned["arm"] for planned in plans})
        for arm in arms:
            healthy, detail = smoke_check(executable, arm, args.provider, args.model, root)
            if not healthy:
                raise RunnerError("preflight smoke failed for arm %s: %s" % (arm, detail))
            sys.stderr.write("smoke ok (%s): %s\n" % (arm, detail))
    records = []
    for planned in plans:
        case = by_case[planned["case_id"]]
        turn = next(item for item in case.turns if item.index == planned["turn"])
        identity = "%s--%s--r%d--t%d" % (
            planned["arm"], planned["case_id"], planned["repetition"], planned["turn"],
        )
        with tempfile.TemporaryDirectory(prefix="koroche-blyat-home-") as home_name, tempfile.TemporaryDirectory(prefix="koroche-blyat-cwd-") as cwd_name:
            command = _command(
                executable, turn.prompt, planned["arm"], args.provider, args.model,
                root, Path(cwd_name), Path(home_name) / "daemon.sock",
            )
            started = time.monotonic()
            try:
                result = subprocess.run(
                    command, env=_prepare_environment(Path(home_name)), check=False,
                    capture_output=True, timeout=300,
                )
                stdout = bytes(result.stdout)
                stderr = bytes(result.stderr)
                exit_code = result.returncode
                infrastructure_error = result.returncode != 0
            except subprocess.TimeoutExpired as exc:
                stdout = bytes(exc.stdout or b"")
                stderr = bytes(exc.stderr or b"") + b"\nrunner timeout\n"
                exit_code = 124
                infrastructure_error = True
            duration_ms = int((time.monotonic() - started) * 1000)
        stdout_path = "raw/%s.stdout" % identity
        stderr_path = "raw/%s.stderr" % identity
        _atomic_write(output / stdout_path, stdout)
        _atomic_write(output / stderr_path, stderr)
        usage = {}
        try:
            raw_text = stdout.decode("utf-8")
        except UnicodeDecodeError:
            raw_text = stdout.decode("utf-8", errors="replace")
            infrastructure_error = True
        text = raw_text
        if not infrastructure_error and raw_text.strip():
            from evals.host_runners import HostEventError, parse_events
            try:
                parsed = parse_events("prime", raw_text)
                text = parsed["text"]
                usage = {
                    "input_tokens": parsed.get("input_tokens"),
                    "cache_read_tokens": parsed.get("cache_read_tokens"),
                    "cache_write_tokens": parsed.get("cache_write_tokens"),
                    "output_tokens": parsed.get("output_tokens"),
                    "total_tokens": parsed.get("total_tokens"),
                }
            except HostEventError as error:
                # A transcript this runner cannot read is infrastructure, not a
                # zero-length answer.
                infrastructure_error = True
                text = ""
                usage = {}
                stderr = stderr + ("\nparse error: %s\n" % error).encode("utf-8")
        if not text.strip():
            # Exit 0 with no output is not a zero-token answer, it is a failed
            # call. Recording it as a valid empty response hides the failure
            # from every downstream gate and biases any comparison.
            infrastructure_error = True
        response_sha = _sha256(stdout)
        records.append({
            "host": args.host, "arm": planned["arm"], "case_id": planned["case_id"],
            "repetition": planned["repetition"], "turn": planned["turn"], "text": text,
            "golden_id": planned["golden_id"], "infrastructure_error": infrastructure_error,
            "host_version": host_version, "provider": args.provider, "model": args.model,
            "policy_sha256": None if _policy(planned["arm"], root) is None else _sha256(_policy(planned["arm"], root).encode("utf-8")),
            "seed": args.seed, "session_id": None, "session_length": 1, "prompt": turn.prompt,
            "prompt_sha256": planned["prompt_sha256"], "response_sha256": response_sha,
            "input_tokens": usage.get("input_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"), "exit_code": exit_code,
            "duration_ms": duration_ms, "stdout_path": stdout_path, "stderr_path": stderr_path,
            "runner_git_sha": runner_git_sha, "schema_version": _SCHEMA_VERSION,
            "grader_version": _GRADER_VERSION,
        })
    responses = b"".join(
        (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for record in records
    )
    manifest = {
        "schema_version": _SCHEMA_VERSION, "expected": len(plans), "host": args.host,
        "host_version": host_version, "provider": args.provider, "model": args.model,
        "arms": list(dict.fromkeys(args.arm)), "repetitions": args.repetitions,
        "seed": args.seed, "grader_version": _GRADER_VERSION, "runner_git_sha": runner_git_sha,
        "responses_sha256": _sha256(responses), "planned_calls": list(plans),
    }
    _atomic_write(output / "responses.jsonl", responses)
    _atomic_write(
        output / "manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    os.replace(str(output), str(final_output))
    return 2 if any(record["infrastructure_error"] for record in records) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.repetitions < 1:
            raise RunnerError("--repetitions must be positive")
        if args.dry_run and args.confirm_live:
            raise RunnerError("--dry-run and --confirm-live are mutually exclusive")
        cases = load_cases(tuple(args.cases))
        plans = _plans(cases, tuple(dict.fromkeys(args.arm)), args.repetitions)
        if args.dry_run:
            sys.stdout.write(json.dumps({
                "schema_version": _SCHEMA_VERSION, "dry_run": True,
                "host": args.host, "provider": args.provider, "model": args.model,
                "arms": list(dict.fromkeys(args.arm)), "repetitions": args.repetitions,
                "planned_calls": len(plans), "calls": plans,
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            return 0
        if not args.confirm_live:
            raise RunnerError("live inference is disabled; inspect --dry-run, then pass --confirm-live explicitly")
        if not args.provider or not args.model:
            raise RunnerError("live inference requires explicit --provider and --model")
        if args.seed is not None:
            raise RunnerError("prime-agent does not expose a seed option; omit --seed so evidence records null")
        if args.output.exists() or _partial_path(args.output).exists():
            raise RunnerError("output and partial paths must not exist before immutable capture: %s" % args.output)
        return _run_live(args, cases, plans)
    except (RunnerError, SchemaError, OSError, subprocess.SubprocessError, ValueError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
