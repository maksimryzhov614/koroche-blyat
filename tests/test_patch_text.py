from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.installer.patch_text import remove_marker_block, upsert_marker_block


FIXTURES = Path(__file__).parent / "fixtures" / "config"
BLOCK_ID = "codex-always-on"
PAYLOAD = b"canonical policy\nsecond line\n"
BEGIN = b"<!-- BEGIN KOROCHE-BLYAT MANAGED: codex-always-on v1 -->\n"
END = b"<!-- END KOROCHE-BLYAT MANAGED: codex-always-on v1 -->\n"


def _managed_span(mutated: bytes) -> bytes:
    start = mutated.index(BEGIN)
    end = mutated.index(END, start) + len(END)
    return mutated[start:end]


def _owned(raw: bytes, installed: bytes) -> dict:
    span = _managed_span(installed)
    separator = installed[len(raw):installed.index(span)]
    anchor_length = min(len(raw), 64)
    anchor = raw[-anchor_length:] if anchor_length else b""
    return {
        "locator": {"block_id": BLOCK_ID},
        "baseline": {
            "owned_span_sha256": hashlib.sha256(span).hexdigest(),
            "separator_hex": separator.hex(),
            "separator_anchor_length": anchor_length,
            "separator_anchor_sha256": hashlib.sha256(anchor).hexdigest(),
            "target_existed": True,
        },
    }


@pytest.mark.parametrize(
    "fixture,separator",
    [("lf.md", b"\n"), ("crlf.md", b"\n"), ("no-final-newline.md", b"\n\n")],
)
def test_upsert_preserves_every_original_byte_and_uses_exact_separator(
    fixture: str, separator: bytes
) -> None:
    raw = (FIXTURES / fixture).read_bytes()
    mutated = upsert_marker_block(raw, BLOCK_ID, PAYLOAD)
    assert mutated == raw + separator + BEGIN + PAYLOAD + END
    assert b"\r" not in _managed_span(mutated)


def test_path_with_spaces_and_user_edit_outside_owned_block_survive(tmp_path: Path) -> None:
    path = tmp_path / "config path with spaces" / "AGENTS override.md"
    path.parent.mkdir()
    original = b"outside before\n"
    installed = upsert_marker_block(original, BLOCK_ID, PAYLOAD)
    edited = b"user edit before\n" + installed + b"user edit after\n"
    updated = upsert_marker_block(edited, BLOCK_ID, b"updated policy\n", previous=PAYLOAD)
    assert updated.startswith(b"user edit before\n" + original)
    assert updated.endswith(b"user edit after\n")
    assert BEGIN + b"updated policy\n" + END in updated


@pytest.mark.parametrize(
    "raw",
    [
        BEGIN + BEGIN + PAYLOAD + END,
        b"prefix\n" + END,
        BEGIN + PAYLOAD,
        b"prefix " + BEGIN + PAYLOAD + END,
    ],
)
def test_duplicate_or_orphan_or_non_line_markers_reject_before_mutation(raw: bytes) -> None:
    before = bytes(raw)
    with pytest.raises(ValueError, match="managed marker"):
        upsert_marker_block(raw, BLOCK_ID, PAYLOAD)
    assert raw == before


def test_update_rejects_changed_owned_payload_but_allows_expected_previous() -> None:
    installed = upsert_marker_block(b"base\n", BLOCK_ID, PAYLOAD)
    with pytest.raises(ValueError, match="owned block changed"):
        upsert_marker_block(installed, BLOCK_ID, b"new\n", previous=b"wrong\n")
    assert upsert_marker_block(installed, BLOCK_ID, b"new\n", previous=PAYLOAD).endswith(
        BEGIN + b"new\n" + END
    )


@pytest.mark.parametrize("fixture", ["lf.md", "crlf.md", "no-final-newline.md"])
def test_remove_uses_owned_hash_and_exact_recorded_separator(fixture: str) -> None:
    raw = (FIXTURES / fixture).read_bytes()
    installed = upsert_marker_block(raw, BLOCK_ID, PAYLOAD)
    assert remove_marker_block(installed, _owned(raw, installed), force=False) == raw


def test_remove_conflicts_on_changed_span_and_force_removes_only_owned_bounds() -> None:
    raw = b"outside\n"
    installed = upsert_marker_block(raw, BLOCK_ID, PAYLOAD)
    owned = _owned(raw, installed)
    changed = installed.replace(b"second line", b"user changed owned line") + b"later outside edit\n"
    with pytest.raises(ValueError, match="owned block changed"):
        remove_marker_block(changed, owned, force=False)
    assert remove_marker_block(changed, owned, force=True) == raw + b"later outside edit\n"


def test_multiple_managed_blocks_are_independent() -> None:
    one = upsert_marker_block(b"base\n", "codex-always-on", b"one\n")
    two = upsert_marker_block(one, "other-block", b"two\n")
    assert upsert_marker_block(two, "codex-always-on", b"updated\n", previous=b"one\n").count(
        b"<!-- BEGIN KOROCHE-BLYAT MANAGED:"
    ) == 2


@pytest.mark.parametrize("block_id", ["", "UPPER", "bad space", "../escape", "trailing-"])
def test_block_id_is_strict(block_id: str) -> None:
    with pytest.raises(ValueError, match="block id"):
        upsert_marker_block(b"", block_id, b"x\n")


def test_user_edit_between_owned_separator_and_marker_keeps_every_user_byte() -> None:
    raw = b"base\n"
    installed = upsert_marker_block(raw, BLOCK_ID, PAYLOAD)
    owned = _owned(raw, installed)
    edited = installed.replace(BEGIN, b"user line\n" + BEGIN)
    assert remove_marker_block(edited, owned) == b"base\nuser line\n"


@pytest.mark.parametrize("token", [BEGIN.rstrip(b"\n"), END.rstrip(b"\n")])
def test_payload_cannot_create_managed_marker_tokens(token: bytes) -> None:
    with pytest.raises(ValueError, match="payload contains managed marker"):
        upsert_marker_block(b"base\n", BLOCK_ID, b"before\n" + token + b"\nafter\n")


def test_force_tolerates_unambiguous_boundary_line_ending_edits() -> None:
    raw = b"base\n"
    installed = upsert_marker_block(raw, BLOCK_ID, PAYLOAD)
    owned = _owned(raw, installed)
    crlf_end = installed.replace(END, END.rstrip(b"\n") + b"\r\n")
    with pytest.raises(ValueError, match="managed marker"):
        remove_marker_block(crlf_end, owned)
    assert remove_marker_block(crlf_end, owned, force=True) == raw
    missing_inner_lf = installed.replace(b"second line\n" + END, b"second line" + END)
    assert remove_marker_block(missing_inner_lf, owned, force=True) == raw


def test_manifest_roundtrip_ownership_removes_exact_separator(tmp_path: Path) -> None:
    from scripts.installer.manifest import dump_manifest, load_manifest
    from scripts.installer.model import OwnedResource, OwnershipManifest

    raw = b"base without newline"
    installed = upsert_marker_block(raw, BLOCK_ID, PAYLOAD)
    fields = _owned(raw, installed)
    record = OwnedResource(
        id="codex-global-policy", kind="text_block", target_path=".codex/AGENTS.md",
        hosts=("codex",), locator=fields["locator"], baseline=fields["baseline"],
        installed_sha256=hashlib.sha256(installed).hexdigest(), installed_value=None,
        source_sha256="a" * 64, mode=0o644,
    )
    manifest = OwnershipManifest(1, "koroche-blyat", "1.0.0", ("codex",), (record,))
    path = tmp_path / "state/manifest.json"
    dump_manifest(path, manifest, tmp_path)
    loaded = load_manifest(path, tmp_path).resources[0]
    assert remove_marker_block(installed, loaded) == raw


def test_removal_requires_cryptographic_ownership_hash_unless_forced() -> None:
    foreign = upsert_marker_block(b"user\n", BLOCK_ID, PAYLOAD)
    incomplete = {"locator": {"block_id": BLOCK_ID}, "baseline": {}}
    with pytest.raises(ValueError, match="owned span hash is required"):
        remove_marker_block(foreign, incomplete)
    with pytest.raises(ValueError, match="owned span hash is required"):
        remove_marker_block(foreign, BLOCK_ID)
    assert remove_marker_block(foreign, incomplete, force=True) == b"user\n\n"
