from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Host:
    id: str
    minimum_version: str
    config_env: str
    requires_manual_hook_trust: bool = False


@dataclass(frozen=True)
class Action:
    id: str
    kind: str
    path: str
    change: str


@dataclass(frozen=True)
class ResourceKind:
    name: str
    host: str
    scope: str
    description: str


@dataclass(frozen=True)
class Options:
    action: str
    requested_hosts: Tuple[str, ...]
    dry_run: bool
    force: bool
    all: bool


@dataclass(frozen=True)
class Snapshot:
    path: str
    bytes_sha256: Optional[str]
    mode: Optional[int]
    exists: bool
    content: Optional[bytes]
    file_type: str = "file"
    symlink_target: Optional[str] = None


@dataclass(frozen=True)
class OwnedResource:
    id: str
    kind: str
    target_path: str
    hosts: Tuple[str, ...]
    locator: Mapping[str, Any]
    baseline: Mapping[str, Any]
    installed_sha256: Optional[str]
    installed_value: Optional[Any]
    source_sha256: str
    mode: Optional[int] = None


@dataclass(frozen=True)
class LogicalChange:
    id: str
    kind: str
    target_path: str
    old_sha256: Optional[str]
    new_sha256: Optional[str]
    conflict: Optional[str]


@dataclass(frozen=True)
class DirectoryMutation:
    path: str
    old_exists: bool
    old_mode: Optional[int]
    change: str
    new_mode: Optional[int]


@dataclass(frozen=True)
class FileMutation:
    path: str
    old_snapshot: Snapshot
    new_content: Optional[bytes]
    new_mode: Optional[int]


@dataclass(frozen=True)
class InstallPlan:
    action: str
    requested_hosts: Tuple[str, ...]
    effective_hosts: Tuple[str, ...]
    release: str
    operations: Tuple[Action, ...]
    conflicts: Tuple[str, ...] = ()
    manual_actions: Tuple[str, ...] = ()
    mutations: Tuple[FileMutation, ...] = ()
    paths: Mapping[str, Any] = field(default_factory=dict)
    state_dir: Optional[str] = None
    manifest_path: Optional[str] = None
    manifest_mutation: Optional[FileMutation] = None
    result_manifest: Optional["OwnershipManifest"] = None
    directory_mutations: Tuple[DirectoryMutation, ...] = ()


@dataclass(frozen=True)
class OwnershipManifest:
    schema_version: int
    package: str
    release: str
    installed_hosts: Tuple[str, ...]
    resources: Tuple[OwnedResource, ...]
