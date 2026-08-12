import json
import subprocess
import sys
from pathlib import Path
from typing import Mapping
from unittest.mock import patch, MagicMock

import pytest

FIXTURE = Path("tests/fixtures/host-capabilities-v1.json")

# ── existing test (keep) ──────────────────────────────────────────────────────

def test_verified_hosts_have_explicit_activation_and_bypass_contracts():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert {item["id"] for item in data["hosts"]} == {"prime", "codex", "claude"}
    by_id = {item["id"]: item for item in data["hosts"]}
    assert by_id["prime"]["minimum_version"] == "0.7.1"
    assert by_id["prime"]["config_env"] == "PRIME_AGENT_CODING_AGENT_DIR"
    assert by_id["codex"]["minimum_version"] == "0.147.0"
    assert by_id["codex"]["requires_manual_hook_trust"] is True
    assert by_id["codex"]["global_instruction_precedence"] == [
        "AGENTS.override.md", "AGENTS.md"
    ]
    assert by_id["claude"]["minimum_version"] == "2.1.197"
    assert by_id["claude"]["config_env"] == "CLAUDE_CONFIG_DIR"
    assert by_id["claude"]["required_output_style_field"] == {
        "keep-coding-instructions": True
    }
    assert all(item["explicit_bypasses"] for item in data["hosts"])


# ── Task-0 focused contract tests ─────────────────────────────────────────────

from scripts.probe_hosts import probe_host, HostCapability, FLOORS


# 1. Exact interface: env is required, Mapping accepted
class TestProbeHostInterface:
    def test_env_required_no_default(self):
        """probe_host must NOT accept being called without env argument."""
        import inspect
        sig = inspect.signature(probe_host)
        env_param = sig.parameters.get("env")
        assert env_param is not None, "probe_host must have env parameter"
        assert env_param.default is inspect.Parameter.empty, (
            "env must be required (no default)"
        )

    def test_accepts_mapping(self):
        """probe_host must accept any Mapping[str,str]."""
        env = {"HOME": "/tmp/testhome"}
        cap = probe_host("prime", env)
        assert isinstance(cap, HostCapability)

    def test_unknown_host_raises(self):
        with pytest.raises(ValueError):
            probe_host("unknown_host", {"HOME": "/tmp"})

    def test_returns_host_capability(self):
        for host in ("prime", "codex", "claude"):
            cap = probe_host(host, {"HOME": "/tmp/testhome"})
            assert isinstance(cap, HostCapability)
            assert cap.host == host


# 2. Config-dir defaults from HOME only when env key absent
class TestConfigDirDefaults:
    def test_prime_default_from_home(self):
        """When PRIME_AGENT_CODING_AGENT_DIR absent, default = HOME/.prime/agent"""
        cap = probe_host("prime", {"HOME": "/fakehome"})
        assert cap.config_dir == "/fakehome/.prime/agent", (
            f"Expected /fakehome/.prime/agent, got {cap.config_dir!r}"
        )

    def test_codex_default_from_home(self):
        """When CODEX_HOME absent, default = HOME/.codex"""
        cap = probe_host("codex", {"HOME": "/fakehome"})
        assert cap.config_dir == "/fakehome/.codex", (
            f"Expected /fakehome/.codex, got {cap.config_dir!r}"
        )

    def test_claude_default_from_home(self):
        """When CLAUDE_CONFIG_DIR absent, default = HOME/.claude"""
        cap = probe_host("claude", {"HOME": "/fakehome"})
        assert cap.config_dir == "/fakehome/.claude", (
            f"Expected /fakehome/.claude, got {cap.config_dir!r}"
        )

    def test_env_key_overrides_default(self):
        """When host-specific env key is present, use it."""
        cap = probe_host("prime", {
            "HOME": "/fakehome",
            "PRIME_AGENT_CODING_AGENT_DIR": "/custom/prime",
        })
        assert cap.config_dir == "/custom/prime"

    def test_codex_env_key_overrides_default(self):
        cap = probe_host("codex", {
            "HOME": "/fakehome",
            "CODEX_HOME": "/custom/codex",
        })
        assert cap.config_dir == "/custom/codex"

    def test_claude_env_key_overrides_default(self):
        cap = probe_host("claude", {
            "HOME": "/fakehome",
            "CLAUDE_CONFIG_DIR": "/custom/claude",
        })
        assert cap.config_dir == "/custom/claude"

    def test_no_home_falls_back_to_path_home(self):
        """If HOME also absent, use Path.home() as base."""
        # Should not raise, and should not return empty string
        cap = probe_host("prime", {})
        assert cap.config_dir != "", "config_dir must not be empty when HOME absent"
        assert ".prime/agent" in cap.config_dir or "prime" in cap.config_dir.lower()


# 3. _detect_version reads stderr too (Prime emits version via stderr)
class TestVersionDetectionStderr:
    def test_prime_reads_stderr(self):
        """When prime-agent outputs version only on stderr, it must be detected."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = "prime-agent 0.9.0"

        with patch("subprocess.run", return_value=fake_result) as mock_run:
            cap = probe_host("prime", {"HOME": "/tmp/h"})
            # version should be detected from stderr
            assert cap.version == "0.9.0", (
                f"Expected 0.9.0 from stderr, got {cap.version!r}"
            )

    def test_codex_reads_stdout(self):
        """codex --version emits to stdout; must still work."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "0.200.0"
        fake_result.stderr = ""

        with patch("subprocess.run", return_value=fake_result):
            cap = probe_host("codex", {"HOME": "/tmp/h"})
            assert cap.version == "0.200.0"


# 4. Malformed version output must cause CLI exit code 2
class TestMalformedVersionCLIExit:
    def _run_cli(self, extra_args, env_override=None):
        env = dict(env_override or {"HOME": "/tmp/testhome"})
        result = subprocess.run(
            [sys.executable, "-m", "scripts.probe_hosts"] + extra_args,
            capture_output=True, text=True, env=env,
            cwd=str(Path.cwd()),
        )
        return result

    def test_malformed_version_exits_2(self):
        """If version output contains no digit tokens, exit code must be 2."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "not-a-version-at-all"
        fake_result.stderr = ""

        with patch("subprocess.run", return_value=fake_result):
            # probe_host itself should raise or return a sentinel
            # CLI should exit 2 on malformed
            # We test probe_host returns a version or raises
            # The CLI must exit 2 — test via direct call to main()
            from scripts.probe_hosts import main
            import io
            from contextlib import redirect_stdout
            # patch subprocess to return malformed for all hosts
            with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
                # malformed → exit 2 for probe of specific host
                with patch("sys.argv", ["probe_hosts", "--host", "prime"]):
                    code = main(["--host", "prime"])
                    assert code == 2, f"Expected exit 2 on malformed version, got {code}"

    def test_command_missing_exits_2(self):
        """FileNotFoundError (command not found) must yield exit code 2."""
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            from scripts.probe_hosts import main
            code = main(["--host", "prime"])
            assert code == 2, f"Expected exit 2 on missing command, got {code}"

    def test_timeout_exits_2(self):
        """TimeoutExpired must yield exit code 2."""
        import subprocess as sp
        with patch("subprocess.run", side_effect=sp.TimeoutExpired("cmd", 10)):
            from scripts.probe_hosts import main
            code = main(["--host", "prime"])
            assert code == 2, f"Expected exit 2 on timeout, got {code}"

    def test_nonzero_returncode_exits_2(self):
        """Non-zero returncode from CLI must yield exit code 2."""
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = ""
        with patch("subprocess.run", return_value=fake_result):
            from scripts.probe_hosts import main
            code = main(["--host", "prime"])
            assert code == 2, f"Expected exit 2 on nonzero returncode, got {code}"


# 5. Below-floor version yields CLI exit code 1
class TestBelowFloorCLIExit:
    def test_below_floor_exits_1(self):
        """Detected version below floor must yield exit code 1."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "0.1.0"   # below prime floor 0.7.1
        fake_result.stderr = ""

        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            from scripts.probe_hosts import main
            code = main(["--host", "prime"])
            assert code == 1, f"Expected exit 1 for below-floor version, got {code}"

    def test_at_floor_exits_0(self):
        """Detected version at floor must yield exit code 0."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = FLOORS["prime"]
        fake_result.stderr = ""

        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            from scripts.probe_hosts import main
            code = main(["--host", "prime"])
            assert code == 0, f"Expected exit 0 at floor version, got {code}"

    def test_above_floor_exits_0(self):
        """Detected version above floor must yield exit code 0."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "99.0.0"
        fake_result.stderr = ""

        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            from scripts.probe_hosts import main
            code = main(["--host", "prime"])
            assert code == 0, f"Expected exit 0 above floor, got {code}"

    def test_codex_below_floor_exits_1(self):
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "0.100.0"  # below codex floor 0.147.0
        fake_result.stderr = ""
        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            from scripts.probe_hosts import main
            code = main(["--host", "codex"])
            assert code == 1

    def test_claude_below_floor_exits_1(self):
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "1.0.0"  # below claude floor 2.1.197
        fake_result.stderr = ""
        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            from scripts.probe_hosts import main
            code = main(["--host", "claude"])
            assert code == 1


# 6. Serialized live output must not contain unrelated absolute paths
class TestPathRedactionInOutput:
    def test_no_absolute_paths_outside_config_root(self):
        """Serialized output must not expose absolute paths outside config root."""
        import io
        from contextlib import redirect_stdout

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = FLOORS["prime"]
        fake_result.stderr = ""

        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            from scripts.probe_hosts import probe_host as ph
            env = {
                "HOME": "/fakehome",
                "SECRET_PATH": "/etc/passwd",  # not related
            }
            cap = ph("prime", env)
            d = cap.to_dict()
            for v in d.values():
                if isinstance(v, str):
                    # config_dir is allowed, no other absolute paths
                    if v.startswith("/") and v != cap.config_dir:
                        pytest.fail(f"Unredacted absolute path in output: {v!r}")


# 7. _build_fixture must not exist
class TestNoBuildFixture:
    def test_build_fixture_removed(self):
        import scripts.probe_hosts as m
        assert not hasattr(m, "_build_fixture"), (
            "_build_fixture must be removed as dead code"
        )


# 8. --check fixture stays deterministic
class TestCheckFixture:
    def test_check_passes_on_valid_fixture(self):
        """--check against committed fixture must exit 0."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.probe_hosts",
             "--check", str(FIXTURE)],
            capture_output=True, text=True, cwd=str(Path.cwd()),
        )
        assert result.returncode == 0, (
            f"--check failed: {result.stdout} {result.stderr}"
        )


# 9. Native invocation regression — exact command must work without PYTHONPATH hack
class TestNativeInvocation:
    def test_exact_command_importable_without_pythonpath(self):
        """uv run pytest tests/test_host_capabilities.py -q must collect without ModuleNotFoundError.

        Verifies that pythonpath = ["."] in pyproject.toml makes `scripts`
        importable natively, with no PYTHONPATH env override required.
        """
        import sys
        # If we are here, the import at module level (line 36) already succeeded
        # without any PYTHONPATH manipulation in the test env.
        from scripts.probe_hosts import probe_host, HostCapability, FLOORS  # noqa: F401
        assert probe_host is not None
        assert HostCapability is not None
        assert isinstance(FLOORS, dict) and len(FLOORS) == 3


# ── Task-0: Exact seven keys serialization ─────────────────────────────────────

class TestHostCapabilitySerializationExactKeys:
    """Verify to_dict() returns exactly seven keys, no internal markers."""

    def test_to_dict_exact_seven_keys(self):
        """HostCapability.to_dict() must return exactly 7 keys."""
        from scripts.probe_hosts import HostCapability
        cap = HostCapability(
            host="prime",
            version="0.8.0",
            config_dir="/home/user/.prime/agent",
            instruction_source="global extension before_agent_start",
            hook_events=["before_agent_start"],
            manual_actions=[],
            limitations=["RLM child inheritance not yet proven in black-box eval"],
        )
        d = cap.to_dict()
        expected_keys = {
            "host", "version", "config_dir", "instruction_source",
            "hook_events", "manual_actions", "limitations"
        }
        actual_keys = set(d.keys())
        assert actual_keys == expected_keys, (
            f"Expected exactly {expected_keys}, got {actual_keys}"
        )
        assert len(d) == 7, f"Expected 7 keys, got {len(d)}"

    def test_to_dict_no_internal_markers(self):
        """to_dict() must not expose any internal markers or fields."""
        from scripts.probe_hosts import HostCapability
        cap = HostCapability(
            host="codex",
            version="0.200.0",
            config_dir="/home/user/.codex",
            instruction_source="AGENTS.override.md or AGENTS.md",
            hook_events=["UserPromptSubmit"],
            manual_actions=["User must run /hooks"],
            limitations=["v1 registers only reminder-only UserPromptSubmit"],
        )
        d = cap.to_dict()
        assert "_detect_ok" not in d, "_detect_ok must not appear in serialized output"
        assert "_extra" not in d, "_extra must not be in serialized output"
        for key in d:
            assert not key.startswith("_"), (
                f"Internal field {key!r} must not be in serialized output"
            )

    def test_live_probe_serializes_exactly_seven(self):
        """Live probe output must serialize exactly seven keys per capability."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "0.8.0"
        fake_result.stderr = ""

        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            cap = probe_host("prime", {"HOME": "/tmp/h"})
            d = cap.to_dict()
            assert len(d) == 7, f"Expected 7 keys in live probe output, got {len(d)}"
            expected_keys = {
                "host", "version", "config_dir", "instruction_source",
                "hook_events", "manual_actions", "limitations"
            }
            assert set(d.keys()) == expected_keys


# ── Task-0: Malformed direct probe behavior ─────────────────────────────────────

class TestMalformedDirectProbe:
    """Direct malformed probe must surface version == "<unknown>"."""

    def test_malformed_version_returns_unknown(self):
        """Malformed version detection must set version == "<unknown>"."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "not-a-version"
        fake_result.stderr = ""

        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            cap = probe_host("prime", {"HOME": "/tmp/h"})
            assert cap.version == "<unknown>", (
                f"Expected <unknown>, got {cap.version!r}"
            )
            assert len(cap.to_dict()) == 7

    def test_command_missing_returns_unknown(self):
        """FileNotFoundError must yield version == "<unknown>"."""
        with patch("scripts.probe_hosts.subprocess.run", side_effect=FileNotFoundError()):
            cap = probe_host("codex", {"HOME": "/tmp/h"})
            assert cap.version == "<unknown>"
            assert len(cap.to_dict()) == 7

    def test_timeout_returns_unknown(self):
        """TimeoutExpired must yield version == "<unknown>"."""
        import subprocess as sp
        with patch("scripts.probe_hosts.subprocess.run", side_effect=sp.TimeoutExpired("cmd", 10)):
            cap = probe_host("claude", {"HOME": "/tmp/h"})
            assert cap.version == "<unknown>"
            assert len(cap.to_dict()) == 7

    def test_nonzero_returncode_returns_unknown(self):
        """Non-zero returncode must yield version == "<unknown>"."""
        fake_result = MagicMock()
        fake_result.returncode = 127
        fake_result.stdout = ""
        fake_result.stderr = ""
        with patch("scripts.probe_hosts.subprocess.run", return_value=fake_result):
            cap = probe_host("prime", {"HOME": "/tmp/h"})
            assert cap.version == "<unknown>"
            assert len(cap.to_dict()) == 7


# ── Task-0: Fixture drift detection ────────────────────────────────────────────

class TestCheckFixtureDrift:
    """_check_fixture() must detect any drift from canonical contract via full comparison."""

    def test_check_detects_instruction_source_drift(self):
        """Mutating instruction_source in fixture must be detected as drift (exit 1)."""
        # Create a drifted fixture with modified instruction_source for codex
        from scripts.probe_hosts import _generate_contract
        canon = _generate_contract()
        drifted = json.loads(json.dumps(canon))  # Deep copy

        # Mutate instruction_source for codex
        for host in drifted["hosts"]:
            if host["id"] == "codex":
                host["instruction_source"] = "MODIFIED.md"

        # Write to temp file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(drifted, f)
            temp_path = Path(f.name)

        try:
            from scripts.probe_hosts import _check_fixture
            result = _check_fixture(temp_path)
            assert result == 1, (
                f"Drifted instruction_source must return exit 1, got {result}"
            )
        finally:
            temp_path.unlink()

    def test_check_detects_hook_events_drift(self):
        """Mutating hook_events must be detected as drift (exit 1)."""
        from scripts.probe_hosts import _generate_contract
        canon = _generate_contract()
        drifted = json.loads(json.dumps(canon))

        # Mutate hook_events for prime
        for host in drifted["hosts"]:
            if host["id"] == "prime":
                host["hook_events"].append("NewEvent")

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(drifted, f)
            temp_path = Path(f.name)

        try:
            from scripts.probe_hosts import _check_fixture
            result = _check_fixture(temp_path)
            assert result == 1, (
                f"Drifted hook_events must return exit 1, got {result}"
            )
        finally:
            temp_path.unlink()

    def test_check_detects_missing_field_drift(self):
        """Removing a required field must be detected as drift (exit 1)."""
        from scripts.probe_hosts import _generate_contract
        canon = _generate_contract()
        drifted = json.loads(json.dumps(canon))

        # Remove explicit_bypasses from one host
        for host in drifted["hosts"]:
            if host["id"] == "claude":
                del host["explicit_bypasses"]

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(drifted, f)
            temp_path = Path(f.name)

        try:
            from scripts.probe_hosts import _check_fixture
            result = _check_fixture(temp_path)
            assert result == 1, (
                f"Missing required field must return exit 1, got {result}"
            )
        finally:
            temp_path.unlink()

    def test_check_passes_canonical(self):
        """Canonical contract must pass _check_fixture."""
        from scripts.probe_hosts import _generate_contract, _check_fixture
        canon = _generate_contract()

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(canon, f)
            temp_path = Path(f.name)

        try:
            result = _check_fixture(temp_path)
            assert result == 0, (
                f"Canonical contract must pass _check_fixture, got exit {result}"
            )
        finally:
            temp_path.unlink()
