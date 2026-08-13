from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import unicodedata
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .model import OwnedResource, OwnershipManifest


_SCHEMA_VERSION = 1
_PACKAGE = "koroche-blyat"
_RELEASE = "1.0.0"
_HOSTS = ("prime", "codex", "claude")
_KINDS = ("file", "directory", "text_block", "json_array_entry", "json_scalar")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = {"schema_version", "package", "release", "installed_hosts", "records"}
_RECORD_KEYS = {
    "id", "kind", "target_path", "owner_set", "locator", "baseline",
    "installed_sha256", "installed_value", "source_sha256", "mode",
}
_FORBIDDEN_METADATA_KEYS = {
    "content", "config", "original", "original_config", "previous_raw", "secret", "value_raw",
}


def empty_manifest() -> OwnershipManifest:
    return OwnershipManifest(
        schema_version=_SCHEMA_VERSION,
        package=_PACKAGE,
        release=_RELEASE,
        installed_hosts=(),
        resources=(),
    )


def _pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key: %s" % key)
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("cannot read manifest") from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("manifest is not valid UTF-8") from error
    try:
        return json.loads(text, object_pairs_hook=_pairs)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("manifest is not valid JSON: %s" % error) from error


def _exact_keys(value: Mapping[str, Any], expected: set, label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ValueError("missing %s fields: %s" % (label, ", ".join(missing)))
    if unknown:
        raise ValueError("unknown %s fields: %s" % (label, ", ".join(unknown)))


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("target_path must be a HOME-relative POSIX path")
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in value):
        raise ValueError("target_path must be a HOME-relative POSIX path")
    parts = value.split("/")
    if (
        value.startswith("/") or value.startswith("~")
        or any(part in ("", ".", "..") for part in parts)
        or PurePosixPath(value).as_posix() != value
    ):
        raise ValueError("target_path must be a HOME-relative POSIX path")
    return value

def _hash(value: Any, label: str, optional: bool = False) -> Any:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError("%s must be a lowercase SHA-256" % label)
    return value


def _host_list(value: Any, label: str, allow_empty: bool) -> Tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise ValueError("%s must be a host list" % label)
    if not allow_empty and not value:
        raise ValueError("%s must not be empty" % label)
    if len(set(value)) != len(value) or any(item not in _HOSTS for item in value):
        raise ValueError("%s contains duplicate or unknown hosts" % label)
    ordered = tuple(host for host in _HOSTS if host in value)
    if tuple(value) != ordered:
        raise ValueError("%s must use canonical host order" % label)
    return ordered


def _json_path(value: Any, expected: Sequence[str], label: str) -> List[str]:
    if not isinstance(value, list) or value != list(expected):
        raise ValueError("%s path is invalid" % label)
    return list(value)


def _exact_metadata(value: Any, keys: set, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % label)
    _exact_keys(value, keys, label)
    return value


def _created_paths(value: Any, allowed: Sequence[Sequence[str]]) -> List[List[str]]:
    allowed_lists = [list(item) for item in allowed]
    if not isinstance(value, list) or any(item not in allowed_lists for item in value):
        raise ValueError("created_paths is invalid")
    if len({tuple(item) for item in value}) != len(value):
        raise ValueError("created_paths contains duplicates")
    return [list(item) for item in value]


def _validate_kind_metadata(
    kind: str, locator: Any, baseline: Any, installed_sha256: Any,
    installed_value: Any, mode: Any,
) -> Tuple[Mapping[str, Any], Mapping[str, Any], Any, Any, int]:
    if type(mode) is not int or not 0 <= mode <= 0o777:
        raise ValueError("record mode is outside regular file permissions")
    installed_hash = _hash(installed_sha256, "installed_sha256", optional=kind == "directory")
    if kind != "json_scalar" and installed_value is not None:
        raise ValueError("installed_value is valid only for json_scalar")
    if kind == "json_scalar" and installed_value != "koroche-blyat":
        raise ValueError("installed_value is not a package-owned scalar")
    if kind in ("file", "directory"):
        loc = _exact_metadata(locator, set(), "locator")
        base = _exact_metadata(baseline, set(), "baseline")
        if kind == "directory" and installed_hash is not None:
            raise ValueError("directory installed_sha256 must be null")
    elif kind == "text_block":
        loc = _exact_metadata(locator, {"block_id"}, "locator")
        if loc["block_id"] != "codex-always-on":
            raise ValueError("text block locator is invalid")
        base = _exact_metadata(baseline, {
            "owned_span_sha256", "separator_hex", "separator_anchor_length",
            "separator_anchor_sha256", "target_existed",
        }, "baseline")
        _hash(base["owned_span_sha256"], "owned_span_sha256")
        _hash(base["separator_anchor_sha256"], "separator_anchor_sha256")
        if base["separator_hex"] not in ("", "0a", "0a0a"):
            raise ValueError("separator_hex is invalid")
        if type(base["separator_anchor_length"]) is not int or not 0 <= base["separator_anchor_length"] <= 64:
            raise ValueError("separator anchor length is invalid")
        if type(base["target_existed"]) is not bool:
            raise ValueError("target_existed must be boolean")
    elif kind == "json_array_entry":
        loc = _exact_metadata(locator, {"path", "command_sha256"}, "locator")
        _json_path(loc["path"], ("hooks", "UserPromptSubmit"), "locator")
        _hash(loc["command_sha256"], "command_sha256")
        base = _exact_metadata(
            baseline, {"installed_entry_sha256", "created_paths", "target_existed"},
            "baseline",
        )
        _hash(base["installed_entry_sha256"], "installed_entry_sha256")
        _created_paths(base["created_paths"], (("hooks",), ("hooks", "UserPromptSubmit")))
        if type(base["target_existed"]) is not bool:
            raise ValueError("target_existed must be boolean")
    else:
        loc = _exact_metadata(locator, {"path"}, "locator")
        _json_path(loc["path"], ("outputStyle",), "locator")
        if not isinstance(baseline, dict) or type(baseline.get("existed")) is not bool:
            raise ValueError("scalar baseline is invalid")
        if type(baseline.get("target_existed")) is not bool:
            raise ValueError("target_existed must be boolean")
        if baseline["existed"]:
            base = _exact_metadata(baseline, {
                "existed", "target_existed", "previous_token_sha256", "baseline_ref",
            }, "baseline")
            _hash(base["previous_token_sha256"], "previous_token_sha256")
            if base["baseline_ref"] != "baselines/claude-output-style-setting.token":
                raise ValueError("baseline_ref is invalid")
        else:
            base = _exact_metadata(
                baseline, {"existed", "target_existed", "created_paths"}, "baseline"
            )
            _created_paths(base["created_paths"], (("outputStyle",),))
    return loc, base, installed_hash, installed_value, mode


def _record(document: Any) -> OwnedResource:
    if not isinstance(document, dict):
        raise ValueError("manifest record must be an object")
    _exact_keys(document, _RECORD_KEYS, "record")
    identifier = document["id"]
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("record id must be non-empty")
    kind = document["kind"]
    if kind not in _KINDS:
        raise ValueError("record kind is unknown")
    locator, baseline, installed_hash, installed_value, mode = _validate_kind_metadata(
        kind, document["locator"], document["baseline"],
        document["installed_sha256"], document["installed_value"], document["mode"],
    )
    return OwnedResource(
        id=identifier,
        kind=kind,
        target_path=_relative_path(document["target_path"]),
        hosts=_host_list(document["owner_set"], "owner_set", allow_empty=False),
        locator=locator,
        baseline=baseline,
        installed_sha256=installed_hash,
        installed_value=installed_value,
        source_sha256=_hash(document["source_sha256"], "source_sha256"),
        mode=mode,
    )


def _physical_path_key(value: str) -> Tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(value).parts
    )


def _validate(manifest: OwnershipManifest) -> OwnershipManifest:
    if type(manifest.schema_version) is not int or manifest.schema_version != _SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    if manifest.package != _PACKAGE:
        raise ValueError("package must be koroche-blyat")
    if manifest.release != _RELEASE:
        raise ValueError("release must be 1.0.0")
    installed_hosts = _host_list(list(manifest.installed_hosts), "installed_hosts", allow_empty=True)
    records = tuple(_record(_record_document(record)) for record in manifest.resources)
    ids = set()
    locators = set()
    target_kinds: Dict[Tuple[str, ...], str] = {}
    target_exact: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    for record in records:
        if record.id in ids:
            raise ValueError("duplicate record id: %s" % record.id)
        ids.add(record.id)
        physical_key = _physical_path_key(record.target_path)
        exact_key = tuple(PurePosixPath(record.target_path).parts)
        previous_exact = target_exact.get(physical_key)
        if previous_exact is not None and previous_exact != exact_key:
            raise ValueError("structured target uses a physical path alias")
        target_exact[physical_key] = exact_key
        previous_kind = target_kinds.get(physical_key)
        if previous_kind is not None and (
            previous_kind in ("file", "directory")
            or record.kind in ("file", "directory")
        ):
            raise ValueError("physical target collides across resource records")
        locator_key = json.dumps(record.locator, sort_keys=True, separators=(",", ":"))
        target_locator = (physical_key, locator_key)
        if target_locator in locators:
            raise ValueError("duplicate target locator")
        locators.add(target_locator)
        target_kinds[physical_key] = record.kind
        if any(host not in installed_hosts for host in record.hosts):
            raise ValueError("owner_set contains a host not installed")
    file_targets = {
        _physical_path_key(record.target_path)
        for record in records if record.kind != "directory"
    }
    all_targets = {_physical_path_key(record.target_path) for record in records}
    for file_target in file_targets:
        for other in all_targets:
            if (
                other != file_target
                and len(other) > len(file_target)
                and other[:len(file_target)] == file_target
            ):
                raise ValueError("owned file target is an ancestor of another resource")
    exact_and_physical = [
        (
            tuple(unicodedata.normalize("NFC", part) for part in PurePosixPath(record.target_path).parts),
            _physical_path_key(record.target_path),
        )
        for record in records
    ]
    for exact_left, physical_left in exact_and_physical:
        for exact_right, physical_right in exact_and_physical:
            if (
                len(physical_right) > len(physical_left)
                and physical_right[:len(physical_left)] == physical_left
                and exact_right[:len(exact_left)] != exact_left
            ):
                raise ValueError("physical path alias appears in resource ancestry")
    return OwnershipManifest(
        schema_version=_SCHEMA_VERSION,
        package=_PACKAGE,
        release=_RELEASE,
        installed_hosts=installed_hosts,
        resources=records,
    )


def _record_document(record: OwnedResource) -> Dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "target_path": record.target_path,
        "owner_set": list(record.hosts),
        "locator": dict(record.locator),
        "baseline": dict(record.baseline),
        "installed_sha256": record.installed_sha256,
        "installed_value": record.installed_value,
        "source_sha256": record.source_sha256,
        "mode": record.mode,
    }


def _document(manifest: OwnershipManifest) -> Dict[str, Any]:
    validated = _validate(manifest)
    return {
        "schema_version": validated.schema_version,
        "package": validated.package,
        "release": validated.release,
        "installed_hosts": list(validated.installed_hosts),
        "records": [_record_document(record) for record in validated.resources],
    }


def _inside(path: Path, home: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("state path must be canonical and inside HOME")
    resolved_home = home.resolve(strict=False)
    try:
        relative = path.relative_to(home)
        path.resolve(strict=False).relative_to(resolved_home)
    except ValueError as error:
        raise ValueError("state path must be inside HOME") from error
    current = home
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("state path must not use symlinks")
        if current.exists() and current != path and not current.is_dir():
            raise ValueError("state path ancestor must be a directory")


def load_manifest(path: Path, home: Path) -> OwnershipManifest:
    path = Path(path)
    home = Path(home)
    if not home.is_absolute():
        raise ValueError("HOME must be absolute")
    _inside(path, home)
    if path.is_symlink():
        raise ValueError("manifest path must not be a symlink")
    if not path.exists():
        return empty_manifest()
    try:
        file_mode = os.lstat(path).st_mode
    except OSError as error:
        raise ValueError("cannot inspect manifest") from error
    if not stat.S_ISREG(file_mode):
        raise ValueError("manifest path must be a regular file")
    document = _read_json(path)
    if not isinstance(document, dict):
        raise ValueError("manifest must be an object")
    _exact_keys(document, _TOP_KEYS, "manifest")
    if type(document["schema_version"]) is not int or document["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    if document["package"] != _PACKAGE:
        raise ValueError("package must be koroche-blyat")
    if document["release"] != _RELEASE:
        raise ValueError("release must be 1.0.0")
    installed_hosts = _host_list(document["installed_hosts"], "installed_hosts", allow_empty=True)
    if not isinstance(document["records"], list):
        raise ValueError("records must be an array")
    records = tuple(_record(record) for record in document["records"])
    validated = _validate(OwnershipManifest(
        schema_version=_SCHEMA_VERSION,
        package=_PACKAGE,
        release=_RELEASE,
        installed_hosts=installed_hosts,
        resources=records,
    ))
    if stat.S_IMODE(file_mode) != 0o600:
        raise ValueError("manifest mode must be 0600")
    try:
        parent_mode = stat.S_IMODE(os.lstat(path.parent).st_mode)
    except OSError as error:
        raise ValueError("cannot inspect manifest state directory") from error
    if parent_mode != 0o700:
        raise ValueError("manifest state directory mode must be 0700")
    return validated


def encode_manifest(manifest: OwnershipManifest) -> bytes:
    document = _document(manifest)
    return (json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def dump_manifest(path: Path, manifest: OwnershipManifest, home: Path) -> None:
    path = Path(path)
    home = Path(home)
    if not home.is_absolute():
        raise ValueError("HOME must be absolute")
    _inside(path, home)
    if path.is_symlink():
        raise ValueError("manifest path must not be a symlink")
    if path.exists() and not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError("manifest path must be a regular file")
    encoded = encode_manifest(manifest)
    missing = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
