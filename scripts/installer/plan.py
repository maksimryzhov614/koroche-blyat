from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .manifest import encode_manifest
from .model import (
    Action, DirectoryMutation, FileMutation, InstallPlan, Options, OwnedResource,
    OwnershipManifest, Snapshot,
)
from .patch_json import (
    json_remove_owned, json_scalar_raw_token, json_set_scalar, json_upsert_array_entry,
    parse_json_document,
)
from .patch_text import remove_marker_block, upsert_marker_block
from .sources import SourceAsset


_HOSTS = ("prime", "codex", "claude")
_BINARIES = {"prime": "prime-agent", "codex": "codex", "claude": "claude"}
_VERIFIED = {"prime": "0.7.2", "codex": "0.147.0", "claude": "2.1.197"}
_FLOORS = {"prime": "0.7.1", "codex": "0.147.0", "claude": "2.1.197"}
_LABELS = {"prime": "Prime", "codex": "Codex", "claude": "Claude"}
_DIRECTORY_SOURCE_SHA256 = hashlib.sha256(b"koroche-blyat owned directory v1").hexdigest()
_VERSION = re.compile(r"(?<![0-9])([0-9]+(?:\.[0-9]+){1,3})(?![0-9])")


def _version_tuple(value: str) -> Tuple[int, ...]:
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (4 - len(parts))


def _probe(host: str) -> Optional[str]:
    binary = shutil.which(_BINARIES[host])
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION.search(result.stdout + "\n" + result.stderr)
    return match.group(1) if match else None


def _effective(options: Options, manifest: OwnershipManifest) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    requested = set(options.requested_hosts)
    effective = set(manifest.installed_hosts) if options.action == "install" else set()
    manual: List[str] = []
    for host in options.requested_hosts:
        version = _probe(host)
        if version is None:
            if options.all:
                continue
            raise ValueError("%s is not installed" % host)
        if _version_tuple(version) < _version_tuple(_FLOORS[host]):
            raise ValueError("%s %s is below verified floor %s" % (
                _LABELS[host], version, _FLOORS[host],
            ))
        effective.add(host)
        if _version_tuple(version) > _version_tuple(_VERIFIED[host]):
            manual.append("%s %s is newer than verified %s; treat as DEGRADED until checked" % (
                _LABELS[host], version, _VERIFIED[host],
            ))
    return tuple(host for host in _HOSTS if host in effective), tuple(manual)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_target(path: Path, home: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("target path must be canonical and inside HOME")
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise ValueError("target path must be inside HOME") from error
    current = home
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise ValueError("target path ancestor must not be a symlink")
        if current.exists() and not current.is_dir():
            raise ValueError("target path ancestor must be a directory")
    try:
        path.resolve(strict=False).relative_to(home)
    except ValueError as error:
        raise ValueError("target path must be inside HOME") from error

def _snapshot(path: Path, home: Optional[Path] = None) -> Snapshot:
    if home is not None:
        _safe_target(path, home)
    if not path.exists() and not path.is_symlink():
        return Snapshot(str(path), None, None, False, None)
    if path.is_symlink() or not path.is_file():
        raise ValueError("target is not a regular file: %s" % path)
    content = path.read_bytes()
    return Snapshot(str(path), _sha(content), path.stat().st_mode & 0o777, True, content)


def _relative(path: Path, home: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(home).as_posix()
    except ValueError as error:
        raise ValueError("target path must be inside HOME") from error


def _record_map(manifest: OwnershipManifest) -> Dict[str, OwnedResource]:
    return {record.id: record for record in manifest.resources}


def _baseline_for_separator(
    raw: bytes, installed: bytes, begin: bytes, target_existed: bool
) -> Mapping[str, Any]:
    span_start = installed.index(begin)
    separator = installed[len(raw):span_start]
    anchor_length = min(len(raw), 64)
    anchor = raw[-anchor_length:] if anchor_length else b""
    end_marker = b"<!-- END KOROCHE-BLYAT MANAGED: codex-always-on v1 -->\n"
    span_end = installed.index(end_marker, span_start) + len(end_marker)
    return {
        "owned_span_sha256": _sha(installed[span_start:span_end]),
        "target_existed": target_existed,
        "separator_hex": separator.hex(),
        "separator_anchor_length": anchor_length,
        "separator_anchor_sha256": _sha(anchor),
    }


def _owned_span_sha256(installed: bytes, begin: bytes) -> str:
    span_start = installed.index(begin)
    end_marker = b"<!-- END KOROCHE-BLYAT MANAGED: codex-always-on v1 -->\n"
    span_end = installed.index(end_marker, span_start) + len(end_marker)
    return _sha(installed[span_start:span_end])


class _Builder:
    def __init__(
        self,
        options: Options,
        paths: Mapping[str, Path],
        sources: Mapping[str, SourceAsset],
        manifest: OwnershipManifest,
        effective: Tuple[str, ...],
        manual: Tuple[str, ...],
    ) -> None:
        self.options = options
        self.paths = paths
        self.sources = sources
        self.manifest = manifest
        self.effective = effective
        self.manual = list(manual)
        self.old_records = _record_map(manifest)
        self.records: Dict[str, OwnedResource] = dict(self.old_records)
        self.actions: List[Action] = []
        self.mutations: List[FileMutation] = []
        self.home = paths["home"]

    def _record(
        self, identifier: str, kind: str, target: Path, owners: Tuple[str, ...],
        locator: Mapping[str, Any], baseline: Mapping[str, Any],
        installed_sha256: Optional[str], installed_value: Any,
        source_sha256: str, mode: int,
    ) -> None:
        self.records[identifier] = OwnedResource(
            identifier, kind, _relative(target, self.home), owners, locator, baseline,
            installed_sha256, installed_value, source_sha256, mode,
        )

    def _own_parent_directories(self, target: Path, owners: Tuple[str, ...]) -> None:
        current = target.parent
        ancestors: List[Path] = []
        while current != self.home:
            ancestors.append(current)
            current = current.parent
        for directory in reversed(ancestors):
            relative = _relative(directory, self.home)
            identifier = "directory-" + hashlib.sha256(
                relative.encode("utf-8")
            ).hexdigest()[:20]
            old = self.records.get(identifier)
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink() or not directory.is_dir():
                    raise ValueError(
                        "target parent is not a regular directory: %s" % directory
                    )
                if old is None:
                    continue
            combined = set(owners)
            if old is not None:
                if old.kind != "directory" or old.target_path != relative:
                    raise ValueError("directory ownership collision")
                if directory.exists():
                    actual_mode = directory.stat().st_mode & 0o777
                    if actual_mode != old.mode:
                        raise ValueError("owned directory mode changed: %s" % relative)
                combined.update(old.hosts)
            canonical = tuple(host for host in _HOSTS if host in combined)
            mode = (
                0o700
                if directory == self.paths["state"]
                or self.paths["state"] in directory.parents
                else 0o755
            )
            self._record(
                identifier, "directory", directory, canonical, {}, {}, None, None,
                _DIRECTORY_SOURCE_SHA256, mode,
            )

    def file(
        self, identifier: str, target: Path, asset: SourceAsset, owners: Tuple[str, ...]
    ) -> None:
        self._own_parent_directories(target, owners)
        snapshot = _snapshot(target, self.home)
        old = self.old_records.get(identifier)
        if snapshot.exists and old is None:
            raise ValueError("existing unowned target: %s" % _relative(target, self.home))
        relative = _relative(target, self.home)
        if old is not None and old.target_path != relative:
            raise ValueError("owned target path changed: %s" % identifier)
        if old is not None and not snapshot.exists:
            raise ValueError("owned file is missing: %s" % relative)
        if old is not None and snapshot.bytes_sha256 != old.installed_sha256:
            raise ValueError("owned file changed: %s" % relative)
        if old is not None and snapshot.mode != old.mode:
            raise ValueError("owned file mode changed: %s" % relative)
        mode = asset.mode
        self._record(identifier, "file", target, owners, {}, {}, asset.sha256, None, asset.sha256, mode)
        if snapshot.content == asset.content and snapshot.mode == mode:
            return
        change = "create" if not snapshot.exists else "update"
        self.actions.append(Action(identifier, "file", _relative(target, self.home), change))
        self.mutations.append(FileMutation(str(target), snapshot, asset.content, mode))

    def text_block(self, target: Path, asset: SourceAsset) -> None:
        identifier = "codex-global-policy"
        self._own_parent_directories(target, ("codex",))
        snapshot = _snapshot(target, self.home)
        raw = snapshot.content or b""
        begin_token = b"<!-- BEGIN KOROCHE-BLYAT MANAGED: codex-always-on v1 -->"
        old = self.old_records.get(identifier)
        relative = _relative(target, self.home)
        if begin_token in raw and old is None:
            raise ValueError("existing unowned target marker")
        if old is not None and old.target_path != relative:
            raise ValueError("owned Codex policy target changed")
        if old is not None:
            if begin_token not in raw:
                raise ValueError("owned block is missing")
            remove_marker_block(raw, old, force=False)
        installed = upsert_marker_block(raw, "codex-always-on", asset.content)
        mode = snapshot.mode if snapshot.exists and snapshot.mode is not None else 0o644
        if old is not None:
            baseline = dict(old.baseline)
            baseline["owned_span_sha256"] = _owned_span_sha256(
                installed, begin_token + b"\n"
            )
        else:
            baseline = _baseline_for_separator(
                raw, installed, begin_token + b"\n", snapshot.exists
            )
        self._record(
            identifier, "text_block", target, ("codex",), {"block_id": "codex-always-on"},
            baseline, _sha(installed), None, asset.sha256, mode,
        )
        if installed == raw:
            return
        self.actions.append(Action(identifier, "text_block", _relative(target, self.home), "create" if not snapshot.exists else "update"))
        self.mutations.append(FileMutation(str(target), snapshot, installed, mode))

    def migrate_text_block(
        self, old_record: OwnedResource, target: Path, asset: SourceAsset
    ) -> None:
        identifier = "codex-global-policy"
        old_target = self.home / PurePosixPath(old_record.target_path)
        self._own_parent_directories(target, ("codex",))
        old_snapshot = _snapshot(old_target, self.home)
        old_new_content: Optional[bytes] = None
        if old_snapshot.exists and old_snapshot.content is not None:
            old_after = remove_marker_block(
                old_snapshot.content, old_record, force=False
            )
            old_target_existed = bool(old_record.baseline.get("target_existed"))
            old_new_content = old_after
            if not old_target_existed and old_after == b"":
                old_new_content = None
        new_snapshot = _snapshot(target, self.home)
        new_raw = new_snapshot.content or b""
        begin_token = b"<!-- BEGIN KOROCHE-BLYAT MANAGED: codex-always-on v1 -->"
        if begin_token in new_raw:
            raise ValueError("existing unowned target marker")
        installed = upsert_marker_block(
            new_raw, "codex-always-on", asset.content
        )
        mode = (
            new_snapshot.mode
            if new_snapshot.exists and new_snapshot.mode is not None
            else 0o644
        )
        baseline = _baseline_for_separator(
            new_raw, installed, begin_token + b"\n", new_snapshot.exists
        )
        self._record(
            identifier, "text_block", target, ("codex",),
            {"block_id": "codex-always-on"}, baseline, _sha(installed), None,
            asset.sha256, mode,
        )
        if old_new_content != old_snapshot.content:
            self.actions.append(Action(
                "codex-global-policy-old", "text_block", old_record.target_path,
                "remove",
            ))
            self.mutations.append(FileMutation(
                str(old_target), old_snapshot, old_new_content,
                old_snapshot.mode if old_new_content is not None else None,
            ))
        self.actions.append(Action(
            identifier, "text_block", _relative(target, self.home),
            "create" if not new_snapshot.exists else "update",
        ))
        self.mutations.append(FileMutation(
            str(target), new_snapshot, installed, mode,
        ))

    def json_config(self, host: str, target: Path, hook_group: Mapping[str, Any], command: str) -> None:
        self._own_parent_directories(target, (host,))
        snapshot = _snapshot(target, self.home)
        raw = snapshot.content if snapshot.exists else b"{}\n"
        matcher = {"type": "command", "command": command}
        hook_id = "%s-user-prompt-hook" % host
        old_hook_record = self.old_records.get(hook_id)
        if old_hook_record is not None:
            owned_hook = {
                "kind": "array_entry",
                "matcher": matcher,
                "installed": hook_group,
                "created_paths": list(old_hook_record.baseline.get("created_paths", [])),
            }
            json_remove_owned(
                raw, ["hooks", "UserPromptSubmit"], owned_hook,
                force=False, path=str(target),
            )
        hook_installed = json_upsert_array_entry(
            raw, ["hooks", "UserPromptSubmit"], matcher, hook_group, hook_id,
            path=str(target),
        )
        if hook_installed == raw and old_hook_record is None:
            raise ValueError("existing unowned JSON hook")
        final = hook_installed
        scalar_previous = None
        if host == "claude":
            scalar_previous = json_scalar_raw_token(raw, ["outputStyle"], path=str(target))
            final = json_set_scalar(final, ["outputStyle"], "koroche-blyat", "claude-output-style", path=str(target))
        mode = snapshot.mode if snapshot.exists and snapshot.mode is not None else 0o600
        created_paths = []
        semantic = json.loads(raw)
        if "hooks" not in semantic:
            created_paths.append(["hooks"])
        if not isinstance(semantic.get("hooks"), dict) or "UserPromptSubmit" not in semantic.get("hooks", {}):
            created_paths.append(["hooks", "UserPromptSubmit"])
        relative = _relative(target, self.home)
        if old_hook_record is not None and old_hook_record.target_path != relative:
            raise ValueError("owned JSON target changed: %s" % hook_id)
        hook_baseline = old_hook_record.baseline if old_hook_record is not None else {
            "installed_entry_sha256": _sha(json.dumps(
                hook_group, sort_keys=True, separators=(",", ":")
            ).encode()),
            "created_paths": created_paths,
            "target_existed": snapshot.exists,
        }
        self._record(
            hook_id, "json_array_entry", target, (host,),
            {"path": ["hooks", "UserPromptSubmit"], "command_sha256": _sha(command.encode())},
            hook_baseline,
            _sha(json.dumps(hook_group, sort_keys=True, separators=(",", ":")).encode()),
            None, self.sources["adapters/generated/reminder.txt"].sha256, mode,
        )
        if host == "claude":
            old_scalar_record = self.old_records.get("claude-output-style-setting")
            if old_scalar_record is not None and old_scalar_record.target_path != relative:
                raise ValueError("owned JSON target changed: claude-output-style-setting")
            existed = scalar_previous is not None
            if old_scalar_record is not None:
                if scalar_previous is None:
                    raise ValueError("owned JSON scalar is missing")
                if json.loads(scalar_previous.decode("utf-8")) != old_scalar_record.installed_value:
                    raise ValueError("owned JSON scalar changed")
                scalar_baseline = dict(old_scalar_record.baseline)
                if scalar_baseline.get("existed"):
                    if scalar_baseline.get("baseline_ref") != "baselines/claude-output-style-setting.token":
                        raise ValueError("scalar baseline backup is missing")
                    backup_path = self.paths["state"] / "baselines" / "claude-output-style-setting.token"
                    backup_snapshot = _snapshot(backup_path, self.home)
                    if (
                        not backup_snapshot.exists
                        or backup_snapshot.content is None
                        or backup_snapshot.mode != 0o600
                        or _sha(backup_snapshot.content) != scalar_baseline.get("previous_token_sha256")
                    ):
                        raise ValueError("scalar baseline backup is missing or invalid")
            else:
                if existed and json.loads(scalar_previous.decode("utf-8")) == "koroche-blyat":
                    raise ValueError("existing unowned JSON scalar")
                scalar_baseline = {"existed": existed}
                if existed:
                    assert scalar_previous is not None
                    backup_path = self.paths["state"] / "baselines" / "claude-output-style-setting.token"
                    self._own_parent_directories(backup_path, ("claude",))
                    backup_snapshot = _snapshot(backup_path, self.home)
                    if backup_snapshot.exists:
                        raise ValueError("existing unowned scalar baseline backup")
                    scalar_baseline["previous_token_sha256"] = _sha(scalar_previous)
                    scalar_baseline["baseline_ref"] = "baselines/claude-output-style-setting.token"
                    self.actions.append(Action(
                        "claude-output-style-baseline", "file",
                        _relative(backup_path, self.home), "create",
                    ))
                    self.mutations.append(FileMutation(
                        str(backup_path), backup_snapshot, scalar_previous, 0o600,
                    ))
                else:
                    scalar_baseline["created_paths"] = [["outputStyle"]]
                scalar_baseline["target_existed"] = snapshot.exists
            self._record(
                "claude-output-style-setting", "json_scalar", target, ("claude",),
                {"path": ["outputStyle"]}, scalar_baseline,
                _sha(b'"koroche-blyat"'), "koroche-blyat",
                self.sources["adapters/generated/claude-output-style.md"].sha256, mode,
            )
        if final != raw or not snapshot.exists:
            change = "create" if not snapshot.exists else "update"
            if hook_installed != raw or not snapshot.exists:
                self.actions.append(Action(hook_id, "json_array_entry", relative, change))
            if host == "claude" and final != hook_installed:
                self.actions.append(Action(
                    "claude-output-style-setting", "json_scalar", relative, change,
                ))
            self.mutations.append(FileMutation(str(target), snapshot, final, mode))

    def result(self) -> InstallPlan:
        manifest_path = self.paths["state"] / "manifest.json"
        if self.effective:
            self._own_parent_directories(manifest_path, self.effective)
        result_manifest = OwnershipManifest(
            1, "koroche-blyat", "1.0.0", self.effective,
            tuple(sorted(self.records.values(), key=lambda record: record.id)),
        )
        manifest_snapshot = _snapshot(manifest_path, self.home)
        encoded = encode_manifest(result_manifest)
        manifest_mutation = None
        empty_result = not result_manifest.installed_hosts and not result_manifest.resources
        if not (
            empty_result and not manifest_snapshot.exists
        ) and manifest_snapshot.content != encoded:
            manifest_mutation = FileMutation(
                str(manifest_path), manifest_snapshot, encoded, 0o600
            )
        operations = tuple(sorted(self.actions, key=lambda item: (item.path, item.id)))
        mutations = tuple(sorted(self.mutations, key=lambda item: item.path))
        directory_mutations = []
        old_ids = {record.id for record in self.manifest.resources}
        for record in result_manifest.resources:
            if record.kind != "directory" or record.id in old_ids:
                continue
            directory = self.home / PurePosixPath(record.target_path)
            _safe_target(directory, self.home)
            if directory.exists() or directory.is_symlink():
                continue
            directory_mutations.append(DirectoryMutation(
                str(directory), False, None, "create", record.mode,
            ))
        directory_mutations.sort(
            key=lambda item: (len(Path(item.path).parts), item.path)
        )
        return InstallPlan(
            "install", self.options.requested_hosts, self.effective, "1.0.0",
            operations, (), tuple(sorted(set(self.manual))), mutations,
            dict(self.paths), str(self.paths["state"]), str(manifest_path),
            manifest_mutation, result_manifest,
            tuple(directory_mutations),
        )


def _skill_assets(sources: Mapping[str, SourceAsset]) -> Iterable[Tuple[str, SourceAsset]]:
    prefix = "skills/koroche-blyat/"
    for key in sorted(sources):
        if key.startswith(prefix):
            yield key[len(prefix):], sources[key]


def _codex_hooks_disabled(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError("Codex config is not a regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("cannot read Codex config.toml") from error
    table: Tuple[str, ...] = ()
    disabled = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        table_match = re.fullmatch(r"\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?", line)
        if table_match is not None:
            table = tuple(table_match.group(1).split("."))
            continue
        assignment = re.match(r"([A-Za-z0-9_.-]+)\s*=\s*(true|false)\s*(?:#.*)?$", line)
        if assignment is None:
            continue
        key = tuple(assignment.group(1).split("."))
        full = table + key
        if full == ("features", "hooks"):
            disabled = assignment.group(2) == "false"
    return disabled


def build_install_plan(
    options: Options,
    paths: Mapping[str, Path],
    sources: Mapping[str, SourceAsset],
    manifest: OwnershipManifest,
) -> InstallPlan:
    if options.action != "install":
        return build_uninstall_plan(options, paths, sources, manifest)
    effective, manual = _effective(options, manifest)
    builder = _Builder(options, paths, sources, manifest, effective, manual)
    shared_owners = tuple(host for host in ("prime", "codex") if host in effective)
    if shared_owners:
        root = paths["home"] / ".agents" / "skills" / "koroche-blyat"
        for relative, asset in _skill_assets(sources):
            identifier = "shared-skill-" + relative.replace("/", "-").replace(".", "-")
            builder.file(identifier, root / relative, asset, shared_owners)
    if "claude" in effective:
        root = paths["claude"] / "skills" / "koroche-blyat"
        for relative, asset in _skill_assets(sources):
            identifier = "claude-skill-" + relative.replace("/", "-").replace(".", "-")
            builder.file(identifier, root / relative, asset, ("claude",))
    if "prime" in effective:
        root = paths["prime"] / "extensions" / "koroche-blyat"
        builder.file("prime-extension-index", root / "index.ts", sources["adapters/prime/extension.ts"], ("prime",))
        builder.file("prime-extension-policy", root / "always-on.md", sources["adapters/generated/always-on.md"], ("prime",))
        builder.file("prime-extension-reminder", root / "reminder.txt", sources["adapters/generated/reminder.txt"], ("prime",))
    if "codex" in effective:
        codex_toml = paths["codex"] / "config.toml"
        if _codex_hooks_disabled(codex_toml):
            builder.manual.append(
                "DEGRADED: Run codex features enable hooks, then trust the three hooks with /hooks"
            )
        else:
            builder.manual.append(
                "Run /hooks and trust the koroche-blyat UserPromptSubmit hook"
            )
        override = paths["codex"] / "AGENTS.override.md"
        policy_target = (
            override
            if override.is_file() and override.stat().st_size > 0
            else paths["codex"] / "AGENTS.md"
        )
        old_policy = builder.old_records.get("codex-global-policy")
        if (
            old_policy is not None
            and old_policy.target_path != _relative(policy_target, paths["home"])
        ):
            builder.migrate_text_block(
                old_policy, policy_target, sources["adapters/generated/always-on.md"]
            )
        else:
            builder.text_block(
                policy_target, sources["adapters/generated/always-on.md"]
            )
        hook_root = paths["codex"] / "hooks" / "koroche-blyat"
        builder.file("codex-hook-script", hook_root / "user-prompt-reminder.sh", sources["adapters/codex/user-prompt-reminder.sh"], ("codex",))
        builder.file("codex-hook-reminder", hook_root / "reminder.txt", sources["adapters/generated/reminder.txt"], ("codex",))
        command = "/bin/sh " + shlex.quote(str(hook_root / "user-prompt-reminder.sh"))
        group = {"hooks": [{"type": "command", "command": command, "timeout": 5, "additionalContextLimit": 512}]}
        builder.json_config("codex", paths["codex"] / "hooks.json", group, command)
    if "claude" in effective:
        builder.file("claude-output-style", paths["claude"] / "output-styles" / "koroche-blyat.md", sources["adapters/generated/claude-output-style.md"], ("claude",))
        hook_root = paths["claude"] / "hooks" / "koroche-blyat"
        builder.file("claude-hook-script", hook_root / "user-prompt-reminder.sh", sources["adapters/claude/user-prompt-reminder.sh"], ("claude",))
        builder.file("claude-hook-reminder", hook_root / "reminder.txt", sources["adapters/generated/reminder.txt"], ("claude",))
        command = "/bin/sh " + shlex.quote(str(hook_root / "user-prompt-reminder.sh"))
        group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        builder.json_config("claude", paths["claude"] / "settings.json", group, command)
    return builder.result()


def _recorded_hook_definition(
    host: str, manifest: OwnershipManifest, home: Path
) -> Tuple[str, Mapping[str, Any]]:
    script_id = "%s-hook-script" % host
    script_record = next(
        (record for record in manifest.resources if record.id == script_id), None
    )
    if script_record is None or script_record.kind != "file":
        raise ValueError("owned hook script record is missing")
    script_path = home / PurePosixPath(script_record.target_path)
    command = "/bin/sh " + shlex.quote(str(script_path))
    command_body: Dict[str, Any] = {
        "type": "command", "command": command, "timeout": 5,
    }
    if host == "codex":
        command_body["additionalContextLimit"] = 512
    return command, {"hooks": [command_body]}


def _manifest_mutation(
    paths: Mapping[str, Path], manifest: OwnershipManifest
) -> Optional[FileMutation]:
    manifest_path = paths["state"] / "manifest.json"
    snapshot = _snapshot(manifest_path, paths["home"])
    empty = not manifest.installed_hosts and not manifest.resources
    if empty:
        if not snapshot.exists:
            return None
        return FileMutation(str(manifest_path), snapshot, None, None)
    encoded = encode_manifest(manifest)
    if snapshot.content == encoded:
        return None
    return FileMutation(str(manifest_path), snapshot, encoded, 0o600)


def _empty_uninstall_plan(
    options: Options, paths: Mapping[str, Path], manifest: OwnershipManifest
) -> InstallPlan:
    manifest_path = paths["state"] / "manifest.json"
    return InstallPlan(
        "uninstall", options.requested_hosts, manifest.installed_hosts, "1.0.0",
        (), (), (), (), dict(paths), str(paths["state"]), str(manifest_path),
        None, manifest,
    )


def build_uninstall_plan(
    options: Options,
    paths: Mapping[str, Path],
    sources: Mapping[str, SourceAsset],
    manifest: OwnershipManifest,
) -> InstallPlan:
    del sources
    manifest_path = paths["state"] / "manifest.json"
    if not manifest.installed_hosts and not manifest.resources and not manifest_path.exists():
        return _empty_uninstall_plan(options, paths, manifest)
    requested = set(options.requested_hosts).intersection(manifest.installed_hosts)
    if not requested:
        return _empty_uninstall_plan(options, paths, manifest)
    records: List[OwnedResource] = []
    actions: List[Action] = []
    mutation_by_path: Dict[Path, FileMutation] = {}
    structural_by_path: Dict[Path, List[OwnedResource]] = {}
    removed_directories: List[OwnedResource] = []
    for record in manifest.resources:
        removed_owners = requested.intersection(record.hosts)
        if not removed_owners:
            records.append(record)
            continue
        remaining = tuple(host for host in record.hosts if host not in requested)
        if remaining:
            records.append(replace(record, hosts=remaining))
            continue
        target = paths["home"] / PurePosixPath(record.target_path)
        if record.kind == "directory":
            _safe_target(target, paths["home"])
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError("owned directory type changed: %s" % record.target_path)
            if not target.exists():
                if not options.force:
                    raise ValueError("owned directory is missing: %s" % record.target_path)
                continue
            actual_mode = target.stat().st_mode & 0o777
            if actual_mode != record.mode and not options.force:
                raise ValueError("owned directory mode changed: %s" % record.target_path)
            removed_directories.append(record)
            continue
        if record.kind == "file":
            snapshot = _snapshot(target, paths["home"])
            if not snapshot.exists:
                if not options.force:
                    raise ValueError("owned file is missing: %s" % record.target_path)
                continue
            if (
                snapshot.bytes_sha256 != record.installed_sha256
                or snapshot.mode != record.mode
            ) and not options.force:
                raise ValueError("owned file changed: %s" % record.target_path)
            mutation_by_path[target] = FileMutation(str(target), snapshot, None, None)
            actions.append(Action(record.id, "file", record.target_path, "remove"))
        elif record.kind in ("text_block", "json_array_entry", "json_scalar"):
            structural_by_path.setdefault(target, []).append(record)
        else:
            raise ValueError("unsupported owned resource kind: %s" % record.kind)

    for target, target_records in sorted(
        structural_by_path.items(), key=lambda item: str(item[0])
    ):
        snapshot = _snapshot(target, paths["home"])
        if not snapshot.exists:
            if not options.force:
                raise ValueError("owned configuration is missing: %s" % _relative(target, paths["home"]))
            for record in target_records:
                if record.kind == "json_scalar" and record.baseline.get("existed"):
                    backup_path = paths["state"] / "baselines" / "claude-output-style-setting.token"
                    backup_snapshot = _snapshot(backup_path, paths["home"])
                    if backup_snapshot.exists:
                        mutation_by_path[backup_path] = FileMutation(
                            str(backup_path), backup_snapshot, None, None,
                        )
            continue
        assert snapshot.content is not None
        raw = snapshot.content
        updated = raw
        ordered = sorted(
            target_records,
            key=lambda record: (0 if record.kind == "json_array_entry" else 1, record.id),
        )
        for record in ordered:
            if record.kind == "text_block":
                block_id = record.locator.get("block_id")
                begin = ("<!-- BEGIN KOROCHE-BLYAT MANAGED: %s v1 -->" % block_id).encode()
                if begin not in updated and not options.force:
                    raise ValueError("owned block is missing")
                updated = remove_marker_block(updated, record, force=options.force)
            elif record.kind == "json_array_entry":
                host = record.hosts[0]
                command, group = _recorded_hook_definition(host, manifest, paths["home"])
                if _sha(command.encode()) != record.locator.get("command_sha256"):
                    raise ValueError("owned hook identity is inconsistent")
                owned = {
                    "kind": "array_entry",
                    "matcher": {"type": "command", "command": command},
                    "installed": group,
                    "created_paths": list(record.baseline.get("created_paths", [])),
                }
                updated = json_remove_owned(
                    updated, ["hooks", "UserPromptSubmit"], owned,
                    force=options.force, path=str(target),
                )
            elif record.kind == "json_scalar":
                semantic, _tokens = parse_json_document(updated, str(target))
                current_owned = (
                    isinstance(semantic, dict)
                    and semantic.get("outputStyle") == record.installed_value
                    and type(semantic.get("outputStyle")) is str
                )
                if not current_owned:
                    if not options.force:
                        raise ValueError("owned JSON scalar changed")
                else:
                    existed = bool(record.baseline.get("existed"))
                    owned: Dict[str, Any] = {
                        "kind": "scalar", "installed": record.installed_value,
                        "existed": existed,
                        "created_paths": list(record.baseline.get("created_paths", [])),
                    }
                    if existed:
                        if record.baseline.get("baseline_ref") != "baselines/claude-output-style-setting.token":
                            raise ValueError("scalar baseline backup is missing")
                        backup_path = paths["state"] / "baselines" / "claude-output-style-setting.token"
                        backup_snapshot = _snapshot(backup_path, paths["home"])
                        if (
                            not backup_snapshot.exists
                            or backup_snapshot.content is None
                            or backup_snapshot.mode != 0o600
                        ):
                            raise ValueError("scalar baseline backup is missing or invalid")
                        previous_raw = backup_snapshot.content
                        if _sha(previous_raw) != record.baseline.get("previous_token_sha256"):
                            raise ValueError("scalar baseline backup hash mismatch")
                        owned["previous_raw"] = previous_raw
                    updated = json_remove_owned(
                        updated, ["outputStyle"], owned,
                        force=False, path=str(target),
                    )
                if record.baseline.get("existed"):
                    backup_path = paths["state"] / "baselines" / "claude-output-style-setting.token"
                    backup_snapshot = _snapshot(backup_path, paths["home"])
                    if backup_snapshot.exists:
                        mutation_by_path[backup_path] = FileMutation(
                            str(backup_path), backup_snapshot, None, None,
                        )
                        actions.append(Action(
                            "claude-output-style-baseline", "file",
                            _relative(backup_path, paths["home"]), "remove",
                        ))
        target_existed = any(
            bool(record.baseline.get("target_existed")) for record in target_records
        )
        new_content: Optional[bytes] = updated
        if not target_existed:
            try:
                empty_json = json.loads(updated) == {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                empty_json = False
            if empty_json or updated == b"":
                new_content = None
        if new_content != raw:
            mutation_by_path[target] = FileMutation(
                str(target), snapshot, new_content,
                snapshot.mode if new_content is not None else None,
            )
        for record in target_records:
            actions.append(Action(record.id, record.kind, record.target_path, "remove"))

    installed_hosts = tuple(
        host for host in manifest.installed_hosts if host not in requested
    )
    result_manifest = OwnershipManifest(
        1, "koroche-blyat", "1.0.0", installed_hosts,
        tuple(sorted(records, key=lambda record: record.id)),
    )
    operations = tuple(sorted(actions, key=lambda item: (item.path, item.id)))
    mutations = tuple(sorted(mutation_by_path.values(), key=lambda item: item.path))
    directory_mutations = tuple(
        DirectoryMutation(
            str(paths["home"] / PurePosixPath(record.target_path)),
            True, record.mode, "remove_if_empty", None,
        )
        for record in sorted(
            removed_directories,
            key=lambda item: (-len(PurePosixPath(item.target_path).parts), item.target_path),
        )
    )
    return InstallPlan(
        "uninstall", options.requested_hosts, installed_hosts, "1.0.0",
        operations, (), (), mutations, dict(paths), str(paths["state"]),
        str(manifest_path), _manifest_mutation(paths, result_manifest),
        result_manifest, directory_mutations,
    )
