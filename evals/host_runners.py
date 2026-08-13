"""Black-box host runners — Task 11.

Every run is a real subprocess with an argv list and `shell=False`: no string
is ever handed to a shell, so a prompt containing quotes or semicolons is data
rather than syntax. Each run gets its own HOME and config roots so a host
cannot read the developer's real settings or write to them.

A changed host event shape is an `infrastructure_error`, never zero usage and
never a silently skipped case. Treating an unparseable response as "0 output
tokens" would quietly bias any later measurement toward the treatment arm.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

HOSTS = ("prime", "codex", "claude")

# Credential variables are copied into the child environment so a live run can
# authenticate, and are never written into a record, an argv log or a report.
CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
    "PRIME_AGENT_API_KEY", "CODEX_API_KEY",
)
REDACTED = "[redacted]"
SECRET_PATTERN = re.compile(r"(?i)(?:sk-|gho_|ghp_)[A-Za-z0-9_\-]{8,}")

# Only Prime accepts a seed today. An unsupported seed is recorded as null
# rather than echoed back as if the host had honoured it.
SEED_SUPPORT = {"prime": False, "codex": False, "claude": False}


class HostEventError(RuntimeError):
    """The host emitted a shape this runner does not recognise."""


@dataclass(frozen=True)
class RunConfig:
    model: Optional[str] = None
    provider: Optional[str] = None
    timeout_s: int = 120
    seed: Optional[int] = None


@dataclass(frozen=True)
class HostResult:
    text: str
    session_id: Optional[str]
    input_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str


def redact(value: str) -> str:
    return SECRET_PATTERN.sub(REDACTED, value)


def build_argv(
    host: str, prompt: str, config: RunConfig,
    session_dir: Optional[Path] = None, session_id: Optional[str] = None,
    resume: Optional[str] = None,
) -> List[str]:
    """Pinned command families. Optional flags appear only when configured."""
    if host == "prime":
        argv = ["prime-agent", "--mode", "json", "-p", "--no-tools"]
        if session_dir is not None:
            argv += ["--session-dir", str(session_dir)]
        if config.provider:
            argv += ["--provider", config.provider]
        if config.model:
            argv += ["--model", config.model]
        return argv + ["--", prompt]
    if host == "codex":
        argv = ["codex", "exec"]
        if resume:
            argv += ["resume", resume]
        argv += ["--json", "--skip-git-repo-check"]
        if config.model:
            argv += ["-m", config.model]
        return argv + [prompt]
    if host == "claude":
        argv = ["claude", "-p", "--output-format", "json"]
        if resume:
            argv += ["--resume", resume]
        elif session_id:
            argv += ["--session-id", session_id]
        if config.model:
            argv += ["--model", config.model]
        return argv + ["--tools", "", prompt]
    raise ValueError("unknown host: %s" % host)


def prepare_environment(base: Mapping[str, str], home: Path) -> Dict[str, str]:
    """Isolate every root a host might read or write."""
    home.mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "PATH": base.get("PATH", "/usr/bin:/bin"),
        "PYTHONUTF8": "1",
        "LC_ALL": base.get("LC_ALL", "C.UTF-8"),
        "LANG": base.get("LANG", "C.UTF-8"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PRIME_AGENT_CODING_AGENT_DIR": str(home / ".prime/agent"),
    }
    for name in CREDENTIAL_ENV:
        if base.get(name):
            environment[name] = base[name]
    return environment


def _prime_text(message: Mapping[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for item in content or ():
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "".join(parts)


def _usage_from_prime(message: Mapping[str, object]) -> Dict[str, Optional[int]]:
    # Captured from Prime 0.7.2 rather than assumed: usage rides on the
    # assistant message of turn_end/agent_end and uses camelCase keys.
    usage = message.get("usage") or {}
    return {
        "input_tokens": usage.get("input"),
        "cache_read_tokens": usage.get("cacheRead"),
        "cache_write_tokens": usage.get("cacheWrite"),
        "output_tokens": usage.get("output"),
        "total_tokens": usage.get("totalTokens"),
    }


def parse_events(host: str, stdout: str) -> Dict[str, object]:
    """Normalize a host transcript, or raise HostEventError."""
    text = stdout.strip()
    if not text:
        raise HostEventError("%s produced no output" % host)
    try:
        if host == "claude":
            payload = json.loads(text)
            if payload.get("type") != "result" or payload.get("is_error"):
                raise HostEventError("claude did not report a successful result")
            usage = payload.get("usage") or {}
            return {
                "text": payload["result"],
                "session_id": payload.get("session_id"),
                "input_tokens": usage.get("input_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": None,
            }
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (ValueError, KeyError) as error:
        raise HostEventError("%s emitted an unreadable transcript: %s" % (host, error))

    if host == "prime":
        # The final assistant message only. --print concatenates the agent's
        # spoken plan with its answer; the event stream keeps them apart, and
        # the plan is not what the shape gate is meant to grade.
        message = None
        for event in events:
            if event.get("type") in ("turn_end", "agent_end"):
                candidate = event.get("message")
                if candidate is None:
                    messages = event.get("messages") or []
                    candidate = messages[-1] if messages else None
                if isinstance(candidate, dict) and candidate.get("role") == "assistant":
                    message = candidate
        if message is None:
            raise HostEventError("prime emitted no assistant turn")
        answer = _prime_text(message)
        if not answer.strip():
            raise HostEventError("prime assistant message carried no text")
        result = {
            "text": answer,
            "session_id": next(
                (event.get("id") for event in events if event.get("type") == "session"), None
            ),
        }
        result.update(_usage_from_prime(message))
        return result

    if host == "codex":
        message = None
        usage = None
        for event in events:
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    message = item.get("text")
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
        if message is None or usage is None:
            raise HostEventError("codex emitted no completed turn")
        return {
            "text": message,
            "session_id": next(
                (event.get("thread_id") for event in events if event.get("type") == "thread.started"), None
            ),
            "input_tokens": usage.get("input_tokens"),
            "cache_read_tokens": usage.get("cached_input_tokens"),
            "cache_write_tokens": None,
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": None,
        }
    raise ValueError("unknown host: %s" % host)


def run_host(
    host: str, argv: Sequence[str], environment: Mapping[str, str],
    cwd: Path, timeout_s: int,
) -> HostResult:
    started = time.time()
    process = subprocess.Popen(
        list(argv), cwd=str(cwd), env=dict(environment), shell=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # start_new_session put the child in its own process group, so the
        # whole tree dies rather than leaking orphaned helpers.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        stdout, stderr = process.communicate()
        raise HostEventError("%s timed out after %ds" % (host, timeout_s))
    duration = int((time.time() - started) * 1000)
    parsed = parse_events(host, stdout)
    return HostResult(
        text=parsed["text"], session_id=parsed.get("session_id"),
        input_tokens=parsed.get("input_tokens"),
        cache_read_tokens=parsed.get("cache_read_tokens"),
        cache_write_tokens=parsed.get("cache_write_tokens"),
        output_tokens=parsed.get("output_tokens"),
        total_tokens=parsed.get("total_tokens"),
        exit_code=process.returncode, duration_ms=duration,
        stdout=redact(stdout), stderr=redact(stderr),
    )


def resolve_host_binary(host: str, environment: Mapping[str, str]) -> Optional[str]:
    executable = {"prime": "prime-agent", "codex": "codex", "claude": "claude"}[host]
    return shutil.which(executable, path=environment.get("PATH"))


def effective_seed(host: str, config: RunConfig) -> Optional[int]:
    """A seed the host cannot honour is recorded as null, not echoed back."""
    return config.seed if SEED_SUPPORT.get(host) else None
