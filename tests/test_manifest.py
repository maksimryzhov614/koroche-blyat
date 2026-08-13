from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import stat

import pytest

from scripts.installer.manifest import dump_manifest, empty_manifest, encode_manifest, load_manifest
from scripts.installer.model import OwnedResource, OwnershipManifest


HASH_A = "a" * 64
HASH_B = "b" * 64


def _record(**changes) -> OwnedResource:
    values = {
        "id": "codex-global-policy",
        "kind": "text_block",
        "target_path": ".codex/AGENTS.md",
        "hosts": ("codex",),
        "locator": {"block_id": "codex-always-on"},
        "baseline": {
            "owned_span_sha256": HASH_A,
            "separator_hex": "0a",
            "separator_anchor_length": 1,
            "separator_anchor_sha256": HASH_B,
            "target_existed": True,
        },
        "installed_sha256": HASH_A,
        "installed_value": None,
        "source_sha256": HASH_B,
        "mode": 0o644,
    }
    values.update(changes)
    return OwnedResource(**values)


def _manifest(*records: OwnedResource) -> OwnershipManifest:
    return OwnershipManifest(
        schema_version=1,
        package="koroche-blyat",
        release="1.0.0",
        installed_hosts=("codex",),
        resources=tuple(records or (_record(),)),
    )


def test_missing_manifest_loads_strict_empty_value_without_creating_state(tmp_path: Path) -> None:
    path = tmp_path / "state" / "manifest.json"
    manifest = load_manifest(path, tmp_path)
    assert manifest == empty_manifest()
    assert not path.parent.exists()


def test_manifest_roundtrip_is_deterministic_sorted_lf_and_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "nested" / "manifest.json"
    manifest = _manifest()
    dump_manifest(path, manifest, tmp_path)
    first = path.read_bytes()
    dump_manifest(path, manifest, tmp_path)
    assert path.read_bytes() == first
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    document = json.loads(first)
    assert tuple(document) == ("installed_hosts", "package", "records", "release", "schema_version")
    assert document["records"][0]["owner_set"] == ["codex"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_manifest(path, tmp_path) == manifest


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d.update(package="other"), "package"),
        (lambda d: d.update(release="1.0.1"), "release"),
        (lambda d: d.update(installed_hosts=["unknown"]), "installed_hosts"),
        (lambda d: d.update(extra=True), "unknown manifest fields"),
        (lambda d: d.pop("records"), "missing manifest fields"),
    ],
)
def test_manifest_top_level_schema_is_exact(tmp_path: Path, mutate, match: str) -> None:
    document = json.loads(json.dumps({
        "schema_version": 1, "package": "koroche-blyat", "release": "1.0.0",
        "installed_hosts": ["codex"], "records": [],
    }))
    mutate(document)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_manifest(path, tmp_path)


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"target_path": "/absolute/config"}, "HOME-relative"),
        ({"target_path": "../escape"}, "HOME-relative"),
        ({"target_path": "dir\\\\windows"}, "HOME-relative"),
        ({"kind": "mystery"}, "kind"),
        ({"installed_sha256": "ABC"}, "installed_sha256"),
        ({"source_sha256": "g" * 64}, "source_sha256"),
        ({"mode": 0o10000}, "mode"),
        ({"hosts": ()}, "owner_set"),
        ({"locator": {"previous_raw": "SECRET"}}, "locator"),
        ({"baseline": {"content": "whole config"}}, "baseline"),
    ],
)
def test_manifest_rejects_unsafe_record_fields(tmp_path: Path, changes: dict, match: str) -> None:
    record = _record(**changes)
    with pytest.raises(ValueError, match=match):
        dump_manifest(tmp_path / "manifest.json", _manifest(record), tmp_path)


def test_manifest_rejects_duplicate_ids_and_target_locators(tmp_path: Path) -> None:
    first = _record()
    duplicate_id = _record(target_path=".codex/other.md")
    with pytest.raises(ValueError, match="duplicate record id"):
        dump_manifest(tmp_path / "one.json", _manifest(first, duplicate_id), tmp_path)
    duplicate_locator = _record(id="other")
    with pytest.raises(ValueError, match="duplicate target locator"):
        dump_manifest(tmp_path / "two.json", _manifest(first, duplicate_locator), tmp_path)


def test_manifest_never_contains_config_bytes_or_secret_values(tmp_path: Path) -> None:
    record = _record()
    path = tmp_path / "manifest.json"
    dump_manifest(path, _manifest(record), tmp_path)
    raw = path.read_bytes()
    assert b"SECRET_SHOULD_NOT_LEAK" not in raw
    assert b'\\"unrelated-user-setting\\"' not in raw
    assert b"previous_raw" not in raw
    assert b"content" not in raw


def test_manifest_path_must_resolve_under_home(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-manifest.json"
    with pytest.raises(ValueError, match="state path must be inside HOME"):
        dump_manifest(outside, _manifest(), tmp_path)


def test_manifest_rejects_noncanonical_paths_and_symlinked_state_parent(tmp_path: Path) -> None:
    for unsafe in (".", "./x", "a//b", "a/./b", "a/", "a/../b", "tab\tpath"):
        with pytest.raises(ValueError, match="HOME-relative"):
            dump_manifest(
                tmp_path / ("m-" + str(abs(hash(unsafe))) + ".json"),
                _manifest(_record(target_path=unsafe)), tmp_path,
            )
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        dump_manifest(alias / "manifest.json", _manifest(), tmp_path)


def test_dump_manifest_secures_every_created_state_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = home / "state" / "nested" / "manifest.json"
    dump_manifest(path, _manifest(), home)
    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "state/nested").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "field",
    [
        "id", "kind", "target_path", "owner_set", "locator", "baseline",
        "installed_sha256", "installed_value", "source_sha256", "mode",
    ],
)
def test_manifest_raw_record_requires_every_exact_field(tmp_path: Path, field: str) -> None:
    document = json.loads(encode_manifest(_manifest()))
    document["records"][0].pop(field)
    path = tmp_path / (field + ".json")
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="missing record fields"):
        load_manifest(path, tmp_path)


def test_manifest_rejects_secret_installed_value_and_owner_outside_installed_set(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="installed_value"):
        dump_manifest(
            tmp_path / "secret.json",
            _manifest(_record(installed_value="SECRET_SHOULD_NOT_LEAK")), tmp_path,
        )
    manifest = replace(_manifest(), installed_hosts=("prime",))
    with pytest.raises(ValueError, match="not installed"):
        dump_manifest(tmp_path / "owners.json", manifest, tmp_path)


def test_manifest_rejects_special_leaf_without_reading_or_replacing_it(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        load_manifest(path, tmp_path)
    with pytest.raises(ValueError, match="regular file"):
        dump_manifest(path, _manifest(), tmp_path)
    assert path.exists()


def test_manifest_rejects_file_directory_physical_target_collision(tmp_path: Path) -> None:
    file_record = _record(kind="file", locator={}, baseline={})
    directory_record = _record(
        id="directory", kind="directory", locator={}, baseline={},
        installed_sha256=None,
    )
    with pytest.raises(ValueError, match="physical target collides"):
        dump_manifest(
            tmp_path / "collision.json",
            _manifest(file_record, directory_record), tmp_path,
        )


def test_load_manifest_rejects_public_manifest_or_state_directory_mode(tmp_path: Path) -> None:
    state = tmp_path / "state"
    path = state / "manifest.json"
    dump_manifest(path, _manifest(), tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="manifest mode"):
        load_manifest(path, tmp_path)
    path.chmod(0o600)
    state.chmod(0o755)
    with pytest.raises(ValueError, match="state directory mode"):
        load_manifest(path, tmp_path)


def test_manifest_rejects_casefold_alias_and_ancestor_collisions(tmp_path: Path) -> None:
    directory = _record(
        id="directory", kind="directory", target_path=".X", locator={}, baseline={},
        installed_sha256=None,
    )
    file_record = _record(
        id="file", kind="file", target_path=".x/child", locator={}, baseline={},
    )
    with pytest.raises(ValueError, match="ancest"):
        dump_manifest(
            tmp_path / "casefold.json", _manifest(directory, file_record), tmp_path,
        )
    left = _record(id="left", kind="file", target_path=".X", locator={}, baseline={})
    right = _record(id="right", kind="file", target_path=".x", locator={}, baseline={})
    with pytest.raises(ValueError, match="physical path alias|physical target collides"):
        dump_manifest(
            tmp_path / "casefold-same.json", _manifest(left, right), tmp_path,
        )


def test_manifest_rejects_casefold_alias_between_structured_records(tmp_path: Path) -> None:
    array_record = _record(
        id="hook", kind="json_array_entry", target_path=".Claude/settings.json",
        locator={"path": ["hooks", "UserPromptSubmit"], "command_sha256": HASH_A},
        baseline={
            "installed_entry_sha256": HASH_A, "created_paths": [],
            "target_existed": True,
        },
    )
    scalar_record = _record(
        id="scalar", kind="json_scalar", target_path=".claude/settings.json",
        locator={"path": ["outputStyle"]},
        baseline={
            "existed": False, "target_existed": True,
            "created_paths": [["outputStyle"]],
        },
        installed_value="koroche-blyat",
    )
    with pytest.raises(ValueError, match="physical path alias"):
        dump_manifest(
            tmp_path / "structured-alias.json",
            _manifest(array_record, scalar_record), tmp_path,
        )


def test_manifest_rejects_unicode_normalization_alias_between_structured_records(
    tmp_path: Path,
) -> None:
    array_record = _record(
        id="hook-nfd", kind="json_array_entry",
        target_path=".claude\u0301/settings.json",
        locator={"path": ["hooks", "UserPromptSubmit"], "command_sha256": HASH_A},
        baseline={
            "installed_entry_sha256": HASH_A, "created_paths": [],
            "target_existed": True,
        },
    )
    scalar_record = _record(
        id="scalar-nfc", kind="json_scalar",
        target_path=".claud\u00e9/settings.json",
        locator={"path": ["outputStyle"]},
        baseline={
            "existed": False, "target_existed": True,
            "created_paths": [["outputStyle"]],
        },
        installed_value="koroche-blyat",
    )
    with pytest.raises(ValueError, match="physical path alias"):
        dump_manifest(
            tmp_path / "unicode-alias.json",
            _manifest(array_record, scalar_record), tmp_path,
        )
