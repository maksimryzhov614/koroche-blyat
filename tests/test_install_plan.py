from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import stat

import pytest

from scripts.install import parse_args
from scripts.installer.hosts import resolve_config_dirs
from scripts.installer.manifest import empty_manifest
from scripts.installer.plan import build_install_plan, build_uninstall_plan
from scripts.installer.sources import load_sources


ROOT = Path(__file__).resolve().parents[1]
HOSTS = ("prime", "codex", "claude")


def _paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed=HOSTS):
    home = tmp_path / "Home With Spaces"
    home.mkdir(parents=True)
    bin_dir = home / "bin"
    bin_dir.mkdir()
    versions = {"prime": "0.7.2", "codex": "0.147.0", "claude": "2.1.197"}
    binaries = {"prime": "prime-agent", "codex": "codex", "claude": "claude"}
    for host in installed:
        binary = bin_dir / binaries[host]
        binary.write_text("#!/bin/sh\nprintf '%s\n' '" + versions[host] + "'\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin:/bin")
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(home / ".prime" / "agent"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".state"))
    return resolve_config_dirs(os.environ)


def _plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str, installed=HOSTS, manifest=None):
    paths = _paths(tmp_path, monkeypatch, installed=installed)
    return build_install_plan(
        parse_args(list(args)), paths, load_sources(ROOT), manifest or empty_manifest()
    )


def _operations(plan, host: str):
    return [operation for operation in plan.operations if operation.id.startswith(host + "-")]


def test_resolve_config_dirs_respects_environment_and_stays_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    assert tuple(paths) == ("home", "prime", "codex", "claude", "state")
    assert all(path.is_absolute() for path in paths.values())
    assert paths["state"].name == "koroche-blyat"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path.parent / "outside"))
    with pytest.raises(ValueError, match="inside HOME"):
        resolve_config_dirs(os.environ)


def test_source_bundle_is_exact_allowlisted_and_hash_validated(tmp_path: Path) -> None:
    sources = load_sources(ROOT)
    assert "skills/koroche-blyat/SKILL.md" in sources
    assert "adapters/generated/always-on.md" in sources
    assert "adapters/prime/extension.ts" in sources
    assert "adapters/codex/user-prompt-reminder.sh" in sources
    assert sources["adapters/generated/reminder.txt"].sha256 == hashlib.sha256(
        sources["adapters/generated/reminder.txt"].content
    ).hexdigest()
    copied = tmp_path / "repo"
    import shutil
    shutil.copytree(ROOT / "skills", copied / "skills")
    shutil.copytree(ROOT / "adapters", copied / "adapters")
    target = copied / "adapters" / "generated" / "reminder.txt"
    target.write_bytes(target.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="source SHA-256 mismatch"):
        load_sources(copied)


def test_exact_host_resource_matrix_and_hook_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch, "--all")
    assert plan.conflicts == ()
    assert plan.effective_hosts == HOSTS
    paths = {operation.path for operation in plan.operations}
    assert ".agents/skills/koroche-blyat/SKILL.md" in paths
    assert ".prime/agent/extensions/koroche-blyat/index.ts" in paths
    assert ".prime/agent/extensions/koroche-blyat/always-on.md" in paths
    assert ".prime/agent/extensions/koroche-blyat/reminder.txt" in paths
    assert ".codex/AGENTS.md" in paths
    assert ".codex/hooks/koroche-blyat/user-prompt-reminder.sh" in paths
    assert ".codex/hooks/koroche-blyat/reminder.txt" in paths
    assert ".codex/hooks.json" in paths
    assert ".claude/skills/koroche-blyat/SKILL.md" in paths
    assert ".claude/output-styles/koroche-blyat.md" in paths
    assert ".claude/hooks/koroche-blyat/user-prompt-reminder.sh" in paths
    assert ".claude/hooks/koroche-blyat/reminder.txt" in paths
    assert ".claude/settings.json" in paths

    mutation_by_path = {Path(mutation.path): mutation for mutation in plan.mutations}
    codex_hooks = json.loads(mutation_by_path[plan.paths["codex"] / "hooks.json"].new_content)
    claude_settings = json.loads(mutation_by_path[plan.paths["claude"] / "settings.json"].new_content)
    codex_group = codex_hooks["hooks"]["UserPromptSubmit"]
    claude_group = claude_settings["hooks"]["UserPromptSubmit"]
    assert len(codex_group) == len(claude_group) == 1
    codex_command = codex_group[0]["hooks"][0]
    claude_command = claude_group[0]["hooks"][0]
    assert codex_command == {
        "type": "command",
        "command": "/bin/sh " + shlex.quote(str(plan.paths["codex"] / "hooks/koroche-blyat/user-prompt-reminder.sh")),
        "timeout": 5,
        "additionalContextLimit": 512,
    }
    assert claude_command == {
        "type": "command",
        "command": "/bin/sh " + shlex.quote(str(plan.paths["claude"] / "hooks/koroche-blyat/user-prompt-reminder.sh")),
        "timeout": 5,
    }
    assert claude_settings["outputStyle"] == "koroche-blyat"
    assert "matcher" not in json.dumps([codex_group, claude_group])
    assert "statusMessage" not in json.dumps([codex_group, claude_group])
    assert len([m for m in plan.mutations if Path(m.path) == plan.paths["claude"] / "settings.json"]) == 1


def test_nonempty_codex_override_is_the_only_patched_global_instruction_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["codex"].mkdir(parents=True)
    (paths["codex"] / "AGENTS.override.md").write_bytes(b"user override\\n")
    plan = build_install_plan(parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest())
    operation_paths = {operation.path for operation in plan.operations}
    assert ".codex/AGENTS.override.md" in operation_paths
    assert ".codex/AGENTS.md" not in operation_paths


def test_empty_codex_override_falls_back_to_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["codex"].mkdir(parents=True)
    (paths["codex"] / "AGENTS.override.md").write_bytes(b"")
    plan = build_install_plan(parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest())
    operation_paths = {operation.path for operation in plan.operations}
    assert ".codex/AGENTS.md" in operation_paths
    assert ".codex/AGENTS.override.md" not in operation_paths


def test_missing_host_is_skipped_only_for_all_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    all_plan = _plan(tmp_path, monkeypatch, "--all", installed=("prime", "claude"))
    assert all_plan.effective_hosts == ("prime", "claude")
    with pytest.raises(ValueError, match="codex.*not installed"):
        _plan(tmp_path / "explicit", monkeypatch, "--codex", installed=("prime", "claude"))


def test_explicit_below_floor_aborts_and_newer_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    prime = paths["home"] / "bin" / "prime-agent"
    prime.write_text("#!/bin/sh\necho 0.6.9\n", encoding="utf-8")
    prime.chmod(0o755)
    with pytest.raises(ValueError, match="below verified floor"):
        build_install_plan(parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest())
    prime.write_text("#!/bin/sh\necho 0.8.0\n", encoding="utf-8")
    plan = build_install_plan(parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest())
    assert any("Prime 0.8.0 is newer than verified 0.7.2" in action for action in plan.manual_actions)


def test_operations_are_sorted_redacted_and_new_files_have_required_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch, "--all")
    assert list(plan.operations) == sorted(plan.operations, key=lambda item: (item.path, item.id))
    for operation in plan.operations:
        assert not operation.path.startswith("/")
        assert "SECRET" not in repr(operation)
    for mutation in plan.mutations:
        suffix = Path(mutation.path).suffix
        if mutation.new_content is not None and suffix in (".json",):
            assert mutation.new_mode == 0o600
        elif mutation.new_content is not None and suffix in (".sh",):
            assert mutation.new_mode == 0o755


def _apply(plan) -> None:
    if plan.result_manifest is not None and plan.action == "install":
        home = Path(plan.paths["home"])
        directories = sorted(
            (record for record in plan.result_manifest.resources if record.kind == "directory"),
            key=lambda record: len(Path(record.target_path).parts),
        )
        for record in directories:
            directory = home / record.target_path
            directory.mkdir(exist_ok=True)
            directory.chmod(record.mode)
    for mutation in (*plan.mutations, *((plan.manifest_mutation,) if plan.manifest_mutation else ())):
        path = Path(mutation.path)
        if mutation.new_content is None:
            if path.exists() or path.is_symlink():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(mutation.new_content)
        if mutation.new_mode is not None:
            path.chmod(mutation.new_mode)

    if plan.action == "uninstall":
        for mutation in plan.directory_mutations:
            path = Path(mutation.path)
            if mutation.change == "remove_if_empty" and path.exists():
                try:
                    path.rmdir()
                except OSError:
                    pass


def test_same_release_reinstall_is_true_noop_and_preserves_claude_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["claude"].mkdir(parents=True)
    (paths["claude"] / "settings.json").write_bytes(
        b'{\n "outputStyle": "\\u006f\\u006c\\u0064", "keep": 1\n}\n'
    )
    first = build_install_plan(parse_args(["--all"]), paths, load_sources(ROOT), empty_manifest())
    first_baseline = next(
        record.baseline for record in first.result_manifest.resources
        if record.id == "claude-output-style-setting"
    )
    _apply(first)
    second = build_install_plan(
        parse_args(["--all"]), paths, load_sources(ROOT), first.result_manifest
    )
    second_baseline = next(
        record.baseline for record in second.result_manifest.resources
        if record.id == "claude-output-style-setting"
    )
    assert second.mutations == ()
    assert second.manifest_mutation is None
    assert second.operations == ()
    assert second_baseline == first_baseline


def test_update_plans_every_previously_installed_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    prime = build_install_plan(parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest())
    _apply(prime)
    update = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), prime.result_manifest
    )
    assert update.requested_hosts == ("codex",)
    assert update.effective_hosts == ("prime", "codex")
    assert any(record.id.startswith("prime-") for record in update.result_manifest.resources)


def test_existing_unknown_file_inside_managed_skill_tree_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    custom = paths["home"] / ".agents/skills/koroche-blyat/custom.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("user file", encoding="utf-8")
    plan = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    assert custom.read_text(encoding="utf-8") == "user file"
    assert all(record.target_path != ".agents/skills/koroche-blyat/custom.md" for record in plan.result_manifest.resources)


def test_codex_hooks_disabled_adds_manual_action_without_toml_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["codex"].mkdir(parents=True)
    config = paths["codex"] / "config.toml"
    original = b'[features]\nhooks = false\nother = "keep"\n'
    config.write_bytes(original)
    plan = build_install_plan(parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest())
    assert plan.manual_actions == (
        "DEGRADED: Run codex features enable hooks, then trust the three hooks with /hooks",
    )
    assert all(Path(mutation.path) != config for mutation in plan.mutations)
    assert config.read_bytes() == original


def test_partial_uninstall_keeps_shared_skill_until_last_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(parse_args(["--prime", "--codex"]), paths, load_sources(ROOT), empty_manifest())
    _apply(installed)
    partial = build_uninstall_plan(
        parse_args(["--uninstall", "--prime"]), paths, load_sources(ROOT), installed.result_manifest
    )
    shared = [record for record in partial.result_manifest.resources if record.id.startswith("shared-skill-")]
    assert shared and all(record.hosts == ("codex",) for record in shared)
    assert not any(operation.id.startswith("shared-skill-") for operation in partial.operations)
    assert any(operation.id.startswith("prime-extension-") for operation in partial.operations)

    last = build_uninstall_plan(
        parse_args(["--uninstall", "--codex"]), paths, load_sources(ROOT), partial.result_manifest
    )
    assert any(operation.id.startswith("shared-skill-") for operation in last.operations)
    assert not any(record.id.startswith("shared-skill-") for record in last.result_manifest.resources)


def test_uninstall_plan_never_probes_removed_host_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest())
    _apply(installed)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    plan = build_uninstall_plan(
        parse_args(["--uninstall", "--prime"]), paths, load_sources(ROOT), installed.result_manifest
    )
    assert plan.action == "uninstall"


def test_claude_previous_output_style_restores_exact_raw_token_on_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["claude"].mkdir(parents=True)
    original = b'{\n "outputStyle": "\\u006f\\u006c\\u0064", "keep" : 1\n}\n'
    settings = paths["claude"] / "settings.json"
    settings.write_bytes(original)
    installed = build_install_plan(parse_args(["--claude"]), paths, load_sources(ROOT), empty_manifest())
    _apply(installed)
    uninstall = build_uninstall_plan(
        parse_args(["--uninstall", "--claude"]), paths, load_sources(ROOT), installed.result_manifest
    )
    _apply(uninstall)
    assert settings.read_bytes() == original


def test_existing_exact_unowned_hook_identity_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    hook = paths["codex"] / "hooks/koroche-blyat/user-prompt-reminder.sh"
    command = "/bin/sh " + shlex.quote(str(hook))
    group = {"hooks": [{
        "type": "command", "command": command, "timeout": 5,
        "additionalContextLimit": 512,
    }]}
    paths["codex"].mkdir(parents=True)
    (paths["codex"] / "hooks.json").write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [group]}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="existing unowned JSON hook"):
        build_install_plan(parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest())


def test_config_roots_expand_tilde_to_home_and_reject_relative_values(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert resolve_config_dirs({"HOME": str(home), "CODEX_HOME": "~"})["codex"] == home
    with pytest.raises(ValueError, match="HOME must be absolute"):
        resolve_config_dirs({"HOME": "relative-home"})
    with pytest.raises(ValueError, match="must be absolute or HOME-relative"):
        resolve_config_dirs({"HOME": str(home), "CODEX_HOME": "relative"})
    with pytest.raises(ValueError, match="must be canonical"):
        resolve_config_dirs({"HOME": str(home), "CODEX_HOME": str(home / "a/../b")})


def test_source_bundle_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    import shutil
    copied = tmp_path / "repo"
    shutil.copytree(ROOT / "skills", copied / "skills")
    shutil.copytree(ROOT / "adapters", copied / "adapters")
    real = tmp_path / "generated-real"
    (copied / "adapters" / "generated").rename(real)
    (copied / "adapters" / "generated").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="missing or unsafe"):
        load_sources(copied)


def test_missing_manifest_uninstall_is_absolute_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch, installed=())
    plan = build_uninstall_plan(
        parse_args(["--uninstall", "--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    assert plan.operations == plan.mutations == ()
    assert plan.manifest_mutation is None
    assert not paths["state"].exists()


def test_final_uninstall_deletes_manifest_instead_of_writing_empty_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    removed = build_uninstall_plan(
        parse_args(["--uninstall", "--prime"]), paths, load_sources(ROOT), installed.result_manifest
    )
    assert removed.result_manifest.installed_hosts == ()
    assert removed.result_manifest.resources == ()
    assert removed.manifest_mutation is not None
    assert removed.manifest_mutation.new_content is None


def test_missing_or_mode_changed_owned_file_conflicts_unless_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    target = paths["prime"] / "extensions/koroche-blyat/index.ts"
    target.unlink()
    with pytest.raises(ValueError, match="owned file is missing"):
        build_uninstall_plan(
            parse_args(["--uninstall", "--prime"]), paths, load_sources(ROOT), installed.result_manifest
        )
    force = build_uninstall_plan(
        parse_args(["--dry-run", "--uninstall", "--prime", "--force"]),
        paths, load_sources(ROOT), installed.result_manifest,
    )
    assert force.result_manifest.installed_hosts == ()

    _apply(installed)
    target.chmod(0o600)
    with pytest.raises(ValueError, match="owned file changed"):
        build_uninstall_plan(
            parse_args(["--uninstall", "--prime"]), paths, load_sources(ROOT), installed.result_manifest
        )


def test_uninstall_uses_manifest_hook_identity_after_codex_home_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    moved = dict(paths)
    moved["codex"] = paths["home"] / ".codex-new"
    removed = build_uninstall_plan(
        parse_args(["--uninstall", "--codex"]), moved, load_sources(ROOT), installed.result_manifest
    )
    hooks = paths["codex"] / "hooks.json"
    mutation = next(item for item in removed.mutations if Path(item.path) == hooks)
    assert json.loads(mutation.new_content or b"{}") == {}


def test_force_changed_claude_scalar_does_not_require_baseline_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["claude"].mkdir(parents=True)
    settings = paths["claude"] / "settings.json"
    settings.write_bytes(b'{"outputStyle":"old","keep":1}\n')
    installed = build_install_plan(
        parse_args(["--claude"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    raw = settings.read_bytes().replace(b'"koroche-blyat"', b'"user-choice"')
    settings.write_bytes(raw)
    baseline = paths["state"] / "baselines/claude-output-style-setting.token"
    baseline.unlink()
    removed = build_uninstall_plan(
        parse_args(["--uninstall", "--claude", "--force"]),
        paths, load_sources(ROOT), installed.result_manifest,
    )
    mutation = next(item for item in removed.mutations if Path(item.path) == settings)
    assert b'"user-choice"' in (mutation.new_content or b"")
    assert b'"keep":1' in (mutation.new_content or b"")


def test_install_rejects_owned_file_mode_drift_and_policy_span_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    script = paths["codex"] / "hooks/koroche-blyat/user-prompt-reminder.sh"
    script.chmod(0o644)
    with pytest.raises(ValueError, match="mode changed"):
        build_install_plan(
            parse_args(["--codex"]), paths, load_sources(ROOT), installed.result_manifest
        )
    script.chmod(0o755)
    policy = paths["codex"] / "AGENTS.md"
    policy.write_bytes(policy.read_bytes().replace(b"canonical-sha256", b"canonical-SHA256", 1))
    with pytest.raises(ValueError, match="owned block changed"):
        build_install_plan(
            parse_args(["--codex"]), paths, load_sources(ROOT), installed.result_manifest
        )


def test_config_root_existing_file_ancestor_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    blocker = home / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="ancestor must be a directory"):
        resolve_config_dirs({"HOME": str(home), "CODEX_HOME": str(blocker / "child")})


def test_result_manifest_never_copies_existing_config_secret_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.installer.manifest import encode_manifest
    paths = _paths(tmp_path, monkeypatch)
    paths["codex"].mkdir(parents=True)
    (paths["codex"] / "hooks.json").write_bytes(
        b'{"unrelated":"SECRET_SHOULD_NOT_LEAK"}\n'
    )
    plan = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest()
    )
    encoded = encode_manifest(plan.result_manifest)
    assert b"SECRET_SHOULD_NOT_LEAK" not in encoded
    assert b'"unrelated"' not in encoded


def test_exact_planned_unowned_file_is_not_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    target = paths["prime"] / "extensions/koroche-blyat/index.ts"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"sentinel")
    with pytest.raises(ValueError, match="existing unowned target"):
        build_install_plan(
            parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
        )
    assert target.read_bytes() == b"sentinel"


def test_source_bundle_rejects_unexpected_file(tmp_path: Path) -> None:
    import shutil
    copied = tmp_path / "repo"
    shutil.copytree(ROOT / "skills", copied / "skills")
    shutil.copytree(ROOT / "adapters", copied / "adapters")
    (copied / "adapters/generated/evil.txt").write_text("evil", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected files"):
        load_sources(copied)


def test_source_bundle_has_exact_fifteen_asset_paths() -> None:
    assert set(load_sources(ROOT)) == {
        "skills/koroche-blyat/LICENSE.txt",
        "skills/koroche-blyat/NOTICE.md",
        "skills/koroche-blyat/SKILL.md",
        "skills/koroche-blyat/licenses/caveman-MIT.txt",
        "skills/koroche-blyat/licenses/pohuy-MIT.txt",
        "skills/koroche-blyat/references/compression.md",
        "skills/koroche-blyat/references/ontologia.md",
        "skills/koroche-blyat/references/sceny.md",
        "skills/koroche-blyat/references/slovar.md",
        "adapters/generated/always-on.md",
        "adapters/generated/claude-output-style.md",
        "adapters/generated/reminder.txt",
        "adapters/prime/extension.ts",
        "adapters/codex/user-prompt-reminder.sh",
        "adapters/claude/user-prompt-reminder.sh",
    }


@pytest.mark.parametrize("host,expected_count", [("prime", 12), ("codex", 13), ("claude", 14)])
def test_each_host_has_exact_non_directory_operation_and_record_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, expected_count: int
) -> None:
    plan = _plan(tmp_path, monkeypatch, "--" + host)
    operation_matrix = {(item.id, item.kind, item.path) for item in plan.operations}
    record_matrix = {
        (item.id, item.kind, item.target_path)
        for item in plan.result_manifest.resources if item.kind != "directory"
    }
    assert operation_matrix == record_matrix
    assert len(operation_matrix) == expected_count
    expected_config_mutations = 0 if host == "prime" else 1
    assert sum(
        Path(item.path).name in ("hooks.json", "settings.json")
        for item in plan.mutations
    ) == expected_config_mutations


@pytest.mark.parametrize("replacement", [b'"user-new"', None])
def test_reinstall_rejects_changed_or_missing_owned_claude_scalar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--claude"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    settings = paths["claude"] / "settings.json"
    raw = settings.read_bytes()
    if replacement is None:
        document = json.loads(raw)
        del document["outputStyle"]
        settings.write_text(json.dumps(document), encoding="utf-8")
    else:
        settings.write_bytes(raw.replace(b'"koroche-blyat"', replacement))
    with pytest.raises(ValueError, match="owned JSON scalar"):
        build_install_plan(
            parse_args(["--claude"]), paths, load_sources(ROOT), installed.result_manifest
        )


@pytest.mark.parametrize("damage", ["missing", "hash", "mode"])
def test_reinstall_requires_intact_private_claude_scalar_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["claude"].mkdir(parents=True)
    (paths["claude"] / "settings.json").write_bytes(b'{"outputStyle":"old"}\n')
    installed = build_install_plan(
        parse_args(["--claude"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    baseline = paths["state"] / "baselines/claude-output-style-setting.token"
    if damage == "missing":
        baseline.unlink()
    elif damage == "hash":
        baseline.write_bytes(b'"other"')
    else:
        baseline.chmod(0o644)
    with pytest.raises(ValueError, match="baseline backup"):
        build_install_plan(
            parse_args(["--claude"]), paths, load_sources(ROOT), installed.result_manifest
        )


def test_reinstall_rejects_missing_owned_hook_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    (paths["codex"] / "hooks.json").write_bytes(b'{}\n')
    with pytest.raises(ValueError, match="owned JSON entry is missing"):
        build_install_plan(
            parse_args(["--codex"]), paths, load_sources(ROOT), installed.result_manifest
        )


def test_config_roots_reject_home_file_and_internal_symlink(tmp_path: Path) -> None:
    home_file = tmp_path / "home-file"
    home_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="regular directory"):
        resolve_config_dirs({"HOME": str(home_file)})
    home = tmp_path / "home"
    home.mkdir()
    real = home / "real"
    real.mkdir()
    alias = home / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        resolve_config_dirs({"HOME": str(home), "CODEX_HOME": str(alias)})


def test_all_mode_retains_previously_owned_host_after_binary_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    prime = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(prime)
    (paths["home"] / "bin/prime-agent").unlink()
    update = build_install_plan(
        parse_args(["--all"]), paths, load_sources(ROOT), prime.result_manifest
    )
    assert update.effective_hosts == ("prime", "codex", "claude")
    assert any(record.id.startswith("prime-") for record in update.result_manifest.resources)


def test_nonzero_version_command_is_not_accepted_from_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    binary = paths["home"] / "bin/prime-agent"
    binary.write_text("#!/bin/sh\nprintf '%s\\n' '0.7.2' >&2\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    with pytest.raises(ValueError, match="not installed"):
        build_install_plan(
            parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
        )


def test_sequential_hosts_union_and_reduce_shared_directory_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    prime = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(prime)
    both = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), prime.result_manifest
    )
    shared_dirs = [
        record for record in both.result_manifest.resources
        if record.kind == "directory" and record.target_path.startswith(".agents")
    ]
    assert shared_dirs and all(record.hosts == ("prime", "codex") for record in shared_dirs)
    _apply(both)
    partial = build_uninstall_plan(
        parse_args(["--uninstall", "--prime"]), paths, load_sources(ROOT), both.result_manifest
    )
    remaining_dirs = [
        record for record in partial.result_manifest.resources
        if record.kind == "directory" and record.target_path.startswith(".agents")
    ]
    assert remaining_dirs and all(record.hosts == ("codex",) for record in remaining_dirs)


def test_reinstall_rejects_owned_directory_mode_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    directory = paths["home"] / ".agents/skills/koroche-blyat"
    directory.chmod(0o700)
    with pytest.raises(ValueError, match="owned directory mode changed"):
        build_install_plan(
            parse_args(["--prime"]), paths, load_sources(ROOT), installed.result_manifest
        )


def test_config_root_inside_managed_package_tree_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with pytest.raises(ValueError, match="overlap"):
        resolve_config_dirs({
            "HOME": str(home),
            "CODEX_HOME": str(home / ".agents/skills/koroche-blyat"),
        })


def test_config_root_equal_to_other_host_managed_file_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with pytest.raises(ValueError, match="managed file target"):
        resolve_config_dirs({
            "HOME": str(home),
            "CODEX_HOME": str(home / ".claude/output-styles/koroche-blyat.md"),
        })


def test_uninstall_rejects_symlinked_managed_target_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    agents = paths["home"] / ".agents"
    outside = tmp_path / "outside-agents"
    agents.rename(outside)
    agents.symlink_to(outside, target_is_directory=True)
    sentinel = outside / "skills/koroche-blyat/SKILL.md"
    before = sentinel.read_bytes()
    with pytest.raises(ValueError, match="ancestor must not be a symlink"):
        build_uninstall_plan(
            parse_args(["--uninstall", "--prime"]), paths,
            load_sources(ROOT), installed.result_manifest,
        )
    assert sentinel.read_bytes() == before


def test_all_mode_with_no_detected_or_owned_hosts_is_absolute_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch, installed=())
    plan = build_install_plan(
        parse_args(["--all"]), paths, load_sources(ROOT), empty_manifest()
    )
    assert plan.effective_hosts == ()
    assert plan.operations == plan.mutations == ()
    assert plan.manifest_mutation is None


def test_config_root_symlink_resolving_exactly_to_home_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    alias = home / "self"
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        resolve_config_dirs({"HOME": str(home), "CODEX_HOME": str(alias)})


def test_casefold_aliasing_config_roots_and_managed_targets_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with pytest.raises(ValueError, match="overlap|collide|unique"):
        resolve_config_dirs({
            "HOME": str(home),
            "PRIME_AGENT_CODING_AGENT_DIR": str(home / ".x"),
            "CODEX_HOME": str(home / ".X/extensions/koroche-blyat/index.ts"),
        })


def test_source_bundle_rejects_special_file(tmp_path: Path) -> None:
    import shutil
    copied = tmp_path / "repo"
    shutil.copytree(ROOT / "skills", copied / "skills")
    shutil.copytree(ROOT / "adapters", copied / "adapters")
    fifo = copied / "skills/koroche-blyat/evil.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="unsafe entry"):
        load_sources(copied)


def test_uninstall_rejects_nonprivate_scalar_baseline_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths["claude"].mkdir(parents=True)
    (paths["claude"] / "settings.json").write_bytes(b'{"outputStyle":"old"}\n')
    installed = build_install_plan(
        parse_args(["--claude"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    baseline = paths["state"] / "baselines/claude-output-style-setting.token"
    baseline.chmod(0o644)
    with pytest.raises(ValueError, match="baseline backup"):
        build_uninstall_plan(
            parse_args(["--uninstall", "--claude"]), paths,
            load_sources(ROOT), installed.result_manifest,
        )


def test_install_and_final_uninstall_plan_owned_directories_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    assert installed.directory_mutations
    assert any(
        Path(item.path) == paths["state"] and item.change == "create"
        for item in installed.directory_mutations
    )
    assert [len(Path(item.path).parts) for item in installed.directory_mutations] == sorted(
        len(Path(item.path).parts) for item in installed.directory_mutations
    )
    _apply(installed)
    removed = build_uninstall_plan(
        parse_args(["--uninstall", "--prime"]), paths,
        load_sources(ROOT), installed.result_manifest,
    )
    assert removed.directory_mutations
    depths = [len(Path(item.path).parts) for item in removed.directory_mutations]
    assert depths == sorted(depths, reverse=True)
    assert any(Path(item.path) == paths["state"] for item in removed.directory_mutations)


def test_unknown_file_prevents_only_empty_directory_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--prime"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    unknown = paths["home"] / ".agents/skills/koroche-blyat/custom.md"
    unknown.write_text("keep", encoding="utf-8")
    removed = build_uninstall_plan(
        parse_args(["--uninstall", "--prime"]), paths,
        load_sources(ROOT), installed.result_manifest,
    )
    _apply(removed)
    assert unknown.read_text(encoding="utf-8") == "keep"
    assert unknown.parent.is_dir()


@pytest.mark.parametrize("value", [{"custom": True}, ["custom"], 7])
def test_force_changed_claude_output_style_any_type_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    installed = build_install_plan(
        parse_args(["--claude"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    settings = paths["claude"] / "settings.json"
    document = json.loads(settings.read_bytes())
    document["outputStyle"] = value
    settings.write_text(json.dumps(document), encoding="utf-8")
    removed = build_uninstall_plan(
        parse_args(["--uninstall", "--claude", "--force"]), paths,
        load_sources(ROOT), installed.result_manifest,
    )
    mutation = next(item for item in removed.mutations if Path(item.path) == settings)
    assert json.loads(mutation.new_content)["outputStyle"] == value


def test_codex_active_global_policy_migrates_between_agents_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    first = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(first)
    agents = paths["codex"] / "AGENTS.md"
    override = paths["codex"] / "AGENTS.override.md"
    override.write_bytes(b"user override\n")
    forward = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), first.result_manifest
    )
    assert {Path(item.path) for item in forward.mutations if Path(item.path).name.startswith("AGENTS")} == {
        agents, override,
    }
    assert next(
        record for record in forward.result_manifest.resources
        if record.id == "codex-global-policy"
    ).target_path == ".codex/AGENTS.override.md"
    _apply(forward)
    assert not agents.exists()
    assert b"BEGIN KOROCHE-BLYAT" in override.read_bytes()

    override.write_bytes(b"")
    backward = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), forward.result_manifest
    )
    assert {
        Path(item.path) for item in backward.mutations
        if Path(item.path).name.startswith("AGENTS")
    } == {agents}
    _apply(backward)
    assert b"BEGIN KOROCHE-BLYAT" in agents.read_bytes()
    assert override.read_bytes() == b""


def _tree_snapshot(root: Path):
    result = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result.append((relative, "symlink", os.readlink(path), None))
        elif path.is_dir():
            result.append((relative, "directory", None, stat.S_IMODE(path.stat().st_mode)))
        else:
            result.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode)))
    return result


def test_plan_level_full_install_uninstall_restores_exact_initial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    before = _tree_snapshot(paths["home"])
    installed = build_install_plan(
        parse_args(["--all"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(installed)
    removed = build_uninstall_plan(
        parse_args(["--uninstall", "--all"]), paths,
        load_sources(ROOT), installed.result_manifest,
    )
    _apply(removed)
    assert _tree_snapshot(paths["home"]) == before



def test_codex_policy_migrates_forward_when_package_created_agents_file_was_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    first = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(first)
    agents = paths["codex"] / "AGENTS.md"
    override = paths["codex"] / "AGENTS.override.md"
    agents.unlink()
    override.write_bytes(b"user override\n")

    migrated = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), first.result_manifest
    )
    assert {Path(item.path) for item in migrated.mutations if Path(item.path).name.startswith("AGENTS")} == {override}
    _apply(migrated)
    assert not agents.exists()
    assert override.read_bytes().startswith(b"user override\n")
    assert b"BEGIN KOROCHE-BLYAT" in override.read_bytes()
    assert build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), migrated.result_manifest
    ).mutations == ()


def test_codex_policy_migrates_back_when_owned_override_was_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    override = paths["codex"] / "AGENTS.override.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_bytes(b"user override\n")
    first = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), empty_manifest()
    )
    _apply(first)
    agents = paths["codex"] / "AGENTS.md"
    override.unlink()

    migrated = build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), first.result_manifest
    )
    assert {Path(item.path) for item in migrated.mutations if Path(item.path).name.startswith("AGENTS")} == {agents}
    _apply(migrated)
    assert not override.exists()
    assert b"BEGIN KOROCHE-BLYAT" in agents.read_bytes()
    assert build_install_plan(
        parse_args(["--codex"]), paths, load_sources(ROOT), migrated.result_manifest
    ).mutations == ()
