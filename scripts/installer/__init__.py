from .model import (
    Action,
    DirectoryMutation,
    FileMutation,
    Host,
    InstallPlan,
    LogicalChange,
    Options,
    OwnedResource,
    OwnershipManifest,
    ResourceKind,
    Snapshot,
)

__all__ = [
    "Action",
    "DirectoryMutation",
    "FileMutation",
    "Host",
    "InstallPlan",
    "LogicalChange",
    "Options",
    "OwnedResource",
    "OwnershipManifest",
    "ResourceKind",
    "Snapshot",
]

from .patch_json import (
    json_remove_owned,
    json_scalar_raw_token,
    json_set_scalar,
    json_upsert_array_entry,
    parse_json_document,
)
from .patch_text import remove_marker_block, upsert_marker_block

__all__ += [
    "json_remove_owned",
    "json_scalar_raw_token",
    "json_set_scalar",
    "json_upsert_array_entry",
    "parse_json_document",
    "remove_marker_block",
    "upsert_marker_block",
]

from .hosts import resolve_config_dirs
from .manifest import dump_manifest, empty_manifest, encode_manifest, load_manifest
from .plan import build_install_plan, build_uninstall_plan
from .sources import SourceAsset, load_sources

__all__ += [
    "SourceAsset",
    "build_install_plan",
    "build_uninstall_plan",
    "dump_manifest",
    "empty_manifest",
    "encode_manifest",
    "load_manifest",
    "load_sources",
    "resolve_config_dirs",
]

from .journal import recover_pending
from .transaction import (
    REAL_FS, RollbackFailure, TransactionFailure, execute_transaction, rollback,
    snapshot, validate_committed,
)

__all__ += [
    "REAL_FS", "RollbackFailure", "TransactionFailure", "execute_transaction",
    "recover_pending", "rollback", "snapshot", "validate_committed",
]
