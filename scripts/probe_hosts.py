"""Read-only capability probe for koroche-blyat supported hosts.

Usage:
    uv run python -m scripts.probe_hosts --host all --output tests/fixtures/host-capabilities-v1.json
    uv run python -m scripts.probe_hosts --host all --check tests/fixtures/host-capabilities-v1.json

Exit codes:
    0 - all requested hosts meet floor / check passed
    1 - a valid detected version is below floor or known capability unsupported
    2 - command missing / timeout / nonzero returncode / malformed output / invalid arguments
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

# ──────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

HOST_IDS: Tuple[str, ...] = ("prime", "codex", "claude")

# Verified floor versions
FLOORS: Dict[str, str] = {
    "prime": "0.7.1",
    "codex": "0.147.0",
    "claude": "2.1.197",
}

# Config environment variables per host
CONFIG_ENVS: Dict[str, str] = {
    "prime": "PRIME_AGENT_CODING_AGENT_DIR",
    "codex": "CODEX_HOME",
    "claude": "CLAUDE_CONFIG_DIR",
}

# Default config paths relative to HOME
CONFIG_DEFAULTS: Dict[str, str] = {
    "prime": ".prime/agent",
    "codex": ".codex",
    "claude": ".claude",
}


class HostCapability:
    """Serializable host capability record."""

    __slots__ = (
        "host", "version", "config_dir", "instruction_source",
        "hook_events", "manual_actions", "limitations",
    )

    def __init__(
        self,
        host: str,
        version: str,
        config_dir: str,
        instruction_source: str,
        hook_events: Sequence[str],
        manual_actions: Sequence[str],
        limitations: Sequence[str],
    ) -> None:
        self.host = host
        self.version = version
        self.config_dir = config_dir
        self.instruction_source = instruction_source
        self.hook_events = list(hook_events)
        self.manual_actions = list(manual_actions)
        self.limitations = list(limitations)

    def to_dict(self) -> Dict[str, Any]:
        """Return exactly seven public fields, no internal markers."""
        d: Dict[str, Any] = {
            "host": self.host,
            "version": self.version,
            "config_dir": self.config_dir,
            "instruction_source": self.instruction_source,
            "hook_events": self.hook_events,
            "manual_actions": self.manual_actions,
            "limitations": self.limitations,
        }
        return _redact_output(d, self.config_dir)


# ──────────────────────────────────────────────────────────────
# Version comparison
# ──────────────────────────────────────────────────────────────

def _parse_version(v: str) -> Optional[Tuple[int, ...]]:
    """Parse a dot-separated numeric version string. Returns None if unparseable."""
    try:
        segments = v.strip().split(".")
        parsed = []
        for seg in segments:
            if seg.isdigit():
                parsed.append(int(seg))
            else:
                return None
        return tuple(parsed) if parsed else None
    except (ValueError, AttributeError):
        return None


def _version_meets_floor(version: str, floor: str) -> bool:
    """Return True iff version >= floor (numeric dot comparison)."""
    v = _parse_version(version)
    f = _parse_version(floor)
    if v is None or f is None:
        return False
    # Pad to same length
    max_len = max(len(v), len(f))
    v = v + (0,) * (max_len - len(v))
    f = f + (0,) * (max_len - len(f))
    return v >= f


# ──────────────────────────────────────────────────────────────
# Path redaction
# ──────────────────────────────────────────────────────────────

def _redact_output(d: Dict[str, Any], config_dir: str) -> Dict[str, Any]:
    """Redact any absolute paths in string values that are not the config_dir."""
    result: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _redact_str(v, config_dir)
        elif isinstance(v, list):
            result[k] = [
                _redact_str(item, config_dir) if isinstance(item, str) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _redact_str(value: str, config_dir: str) -> str:
    """Redact absolute paths outside the config root."""
    if not value.startswith("/"):
        return value
    if config_dir and value == config_dir:
        return value
    if config_dir and value.startswith(config_dir.rstrip("/") + "/"):
        return value
    return "<redacted>"


# ──────────────────────────────────────────────────────────────
# Probe logic
# ──────────────────────────────────────────────────────────────

# Sentinel for detection errors (missing/timeout/nonzero/malformed)
_DETECT_ERROR = object()


def _config_dir_default(host: str, env: Mapping[str, str]) -> str:
    """Derive config_dir from HOME (or Path.home() fallback) when host env key absent."""
    home = env.get("HOME")
    if home:
        base = Path(home)
    else:
        try:
            base = Path.home()
        except RuntimeError:
            base = Path("/tmp")
    return str(base / CONFIG_DEFAULTS[host])


def _detect_version(host: str, env: Mapping[str, str]):
    """Attempt to detect host version from CLI.

    Returns:
        str  - detected version string (valid digit-separated)
        _DETECT_ERROR - command missing / timeout / nonzero / malformed
    """
    version_cmds: Dict[str, List[str]] = {
        "prime": ["prime-agent", "--version"],
        "codex": ["codex", "--version"],
        "claude": ["claude", "--version"],
    }
    cmd = version_cmds.get(host)
    if not cmd:
        return _DETECT_ERROR
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            shell=False, env=dict(env),
        )
        if result.returncode != 0:
            return _DETECT_ERROR
        # Merge stdout + stderr (Prime emits version via stderr)
        output = (result.stdout + " " + result.stderr).strip()
        # Extract first token that looks like a numeric version
        for token in output.split():
            token = token.strip("v").strip()
            if token and token[0].isdigit() and _parse_version(token) is not None:
                return token
        # No valid version token found → malformed
        return _DETECT_ERROR
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return _DETECT_ERROR


def probe_host(
    name: Literal["prime", "codex", "claude"],
    env: Mapping[str, str],
) -> HostCapability:
    """Probe a single host and return its capability record.

    Parameters
    ----------
    name : Literal["prime", "codex", "claude"]
    env : mapping used to resolve config paths and run version commands
    """
    if name not in HOST_IDS:
        raise ValueError(f"Unknown host: {name!r}; expected one of {HOST_IDS}")

    config_env_var = CONFIG_ENVS[name]
    # Use host-specific env key if present, else derive from HOME
    if config_env_var in env and env[config_env_var]:
        config_dir = env[config_env_var]
    else:
        config_dir = _config_dir_default(name, env)

    version_result = _detect_version(name, env)
    if version_result is _DETECT_ERROR:
        # Use floor as placeholder; caller (main) checks and emits exit 2
        version = _DETECT_ERROR  # type: ignore[assignment]
    else:
        version = version_result  # type: ignore[assignment]

    if name == "prime":
        return _make_capability(
            name=name,
            version=version,
            config_dir=config_dir,
            instruction_source="global extension before_agent_start",
            hook_events=["before_agent_start"],
            manual_actions=[],
            limitations=["RLM child inheritance not yet proven in black-box eval"],
        )
    elif name == "codex":
        return _make_capability(
            name=name,
            version=version,
            config_dir=config_dir,
            instruction_source="AGENTS.override.md or AGENTS.md (first non-empty global file)",
            hook_events=["SessionStart", "UserPromptSubmit", "SubagentStart"],
            manual_actions=["User must run /hooks and trust the definition-hash-bound hook"],
            limitations=[
                "v1 registers only reminder-only UserPromptSubmit",
                "command hook trust is manual and definition-hash-bound",
                "SubagentStart available but not registered in v1",
            ],
        )
    else:  # claude
        return _make_capability(
            name=name,
            version=version,
            config_dir=config_dir,
            instruction_source="output style with keep-coding-instructions: true plus reminder-only UserPromptSubmit hook",
            hook_events=["UserPromptSubmit", "SubagentStart"],
            manual_actions=[],
            limitations=[
                "SubagentStart available but not registered in v1 without continuation evidence",
                "project/managed output style can override user scalar",
                "higher-priority managed policy defeats always-on",
            ],
        )


def _make_capability(
    name: str,
    version: Any,
    config_dir: str,
    instruction_source: str,
    hook_events: List[str],
    manual_actions: List[str],
    limitations: List[str],
) -> HostCapability:
    """Create HostCapability. On detection error, version is "<unknown>"; else the detected string."""
    if version is _DETECT_ERROR:
        ver_str = "<unknown>"
    else:
        ver_str = str(version)

    cap = HostCapability(
        host=name,
        version=ver_str,
        config_dir=config_dir,
        instruction_source=instruction_source,
        hook_events=hook_events,
        manual_actions=manual_actions,
        limitations=limitations,
    )
    return cap


# ──────────────────────────────────────────────────────────────
# Fixture generation and verification
# ──────────────────────────────────────────────────────────────

def _generate_contract() -> Dict[str, Any]:
    """Generate the canonical fixture data from hardcoded verified facts."""
    return {
        "schema_version": SCHEMA_VERSION,
        "hosts": [
            {
                "id": "prime",
                "name": "Prime Agent",
                "minimum_version": FLOORS["prime"],
                "config_env": CONFIG_ENVS["prime"],
                "instruction_source": "global extension before_agent_start",
                "hook_events": ["before_agent_start"],
                "activation": "always-on via global extension; RLM child inheritance requires later black-box proof",
                "explicit_bypasses": ["--no-extensions", "--no-context-files"],
                "limitations": ["RLM child inheritance not yet proven in black-box eval"],
            },
            {
                "id": "codex",
                "name": "Codex CLI",
                "minimum_version": FLOORS["codex"],
                "config_env": CONFIG_ENVS["codex"],
                "instruction_source": "AGENTS.override.md or AGENTS.md (first non-empty global file)",
                "hook_events": ["SessionStart", "UserPromptSubmit", "SubagentStart"],
                "registered_hooks_v1": ["UserPromptSubmit"],
                "hook_type_v1": "reminder-only",
                "requires_manual_hook_trust": True,
                "manual_action": "User must run /hooks and trust the definition-hash-bound hook",
                "global_instruction_precedence": ["AGENTS.override.md", "AGENTS.md"],
                "activation": "always-on via non-empty AGENTS.override.md global file with reminder-only UserPromptSubmit hook",
                "explicit_bypasses": ["manual /hooks distrust", "empty/absent global instruction files", "managed-policy override"],
                "limitations": [
                    "v1 registers only reminder-only UserPromptSubmit",
                    "command hook trust is manual and definition-hash-bound",
                    "SubagentStart available but not registered in v1",
                ],
            },
            {
                "id": "claude",
                "name": "Claude Code",
                "minimum_version": FLOORS["claude"],
                "config_env": CONFIG_ENVS["claude"],
                "instruction_source": "output style with keep-coding-instructions: true plus reminder-only UserPromptSubmit hook",
                "hook_events": ["UserPromptSubmit", "SubagentStart"],
                "registered_hooks_v1": ["UserPromptSubmit"],
                "hook_type_v1": "reminder-only",
                "required_output_style_field": {"keep-coding-instructions": True},
                "activation": "always-on via output style and reminder-only UserPromptSubmit hook",
                "explicit_bypasses": ["--safe-mode", "--bare", "disabled/managed-only hooks", "project/managed output style override"],
                "limitations": [
                    "SubagentStart available but not registered in v1 without continuation evidence",
                    "project/managed output style can override user scalar",
                    "higher-priority managed policy defeats always-on",
                ],
            },
        ],
    }


def _check_fixture(path: Path) -> int:
    """Verify the fixture file matches the canonical contract. Returns exit code.

    After valid JSON parse, compares the complete data object to _generate_contract().
    Any valid drift returns 1; malformed input returns 2.
    """
    if not path.exists():
        print(f"ERROR: fixture not found: {path}", file=sys.stderr)
        return 2

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: malformed JSON: {exc}", file=sys.stderr)
        return 2

    canonical = _generate_contract()

    # Compare complete data object for exact match
    if data != canonical:
        print(f"FAIL: fixture drifts from canonical contract", file=sys.stderr)
        return 1

    print("OK: fixture matches canonical capability contract")
    return 0


def _output_fixture(path: Path) -> int:
    """Generate and write the canonical fixture. Returns exit code."""
    data = _generate_contract()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Written: {path}")
    return 0


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe host capabilities for koroche-blyat."
    )
    parser.add_argument(
        "--host", choices=[*HOST_IDS, "all"], default="all",
        help="Host to probe (default: all)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Write fixture JSON to this path",
    )
    parser.add_argument(
        "--check", type=Path, default=None,
        help="Verify an existing fixture file against the canonical contract",
    )

    args = parser.parse_args(argv)

    if args.check:
        return _check_fixture(args.check)

    if args.output:
        return _output_fixture(args.output)

    # Default: probe and print
    hosts = HOST_IDS if args.host == "all" else (args.host,)
    env = dict(os.environ)

    results = []
    exit_code = 0

    for h in hosts:
        cap = probe_host(h, env)
        parsed = _parse_version(cap.version)

        if parsed is None:
            # Detection error: version is "<unknown>" and unparseable
            exit_code = max(exit_code, 2)
        elif not _version_meets_floor(cap.version, FLOORS[h]):
            # Detected version below floor
            exit_code = max(exit_code, 1)

        d = cap.to_dict()
        results.append(d)

    print(json.dumps(results, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
