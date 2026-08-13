from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.installer.patch_json import (
    json_remove_owned,
    json_scalar_raw_token,
    json_set_scalar,
    json_upsert_array_entry,
    parse_json_document,
)


FIXTURES = Path(__file__).parent / "fixtures" / "config"
ARRAY_PATH = ["hooks", "UserPromptSubmit"]
COMMAND = "/bin/sh '/absolute path/user-prompt-reminder.sh'"
CODEX_GROUP = {
    "hooks": [
        {
            "type": "command",
            "command": COMMAND,
            "timeout": 5,
            "additionalContextLimit": 512,
        }
    ]
}
CLAUDE_GROUP = {
    "hooks": [{"type": "command", "command": COMMAND, "timeout": 5}]
}
MATCHER = {"type": "command", "command": COMMAND}


def _owned_entry(entry: dict) -> dict:
    return {"kind": "array_entry", "matcher": MATCHER, "installed": entry}


@pytest.mark.parametrize(
    "fixture", ["unusual.json", "commas-next-line.json", "crlf.json", "no-final.json"]
)
def test_insert_and_remove_hook_preserve_every_unrelated_byte(fixture: str) -> None:
    raw = (FIXTURES / fixture).read_bytes()
    inserted = json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    assert json.loads(inserted)["hooks"]["UserPromptSubmit"][-1] == CODEX_GROUP
    assert json_remove_owned(inserted, ARRAY_PATH, _owned_entry(CODEX_GROUP)) == raw


def test_empty_crlf_array_keeps_crlf_outside_inserted_bytes() -> None:
    raw = (FIXTURES / "crlf.json").read_bytes()
    inserted = json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CLAUDE_GROUP, "claude-hook")
    before, after = raw.split(b"[]")
    assert inserted.startswith(before + b"[")
    assert inserted.endswith(b"]" + after)
    assert inserted.count(b"\r\n") == raw.count(b"\r\n")


def test_existing_exact_hook_is_noop_and_changed_owned_hook_conflicts() -> None:
    raw = (FIXTURES / "no-final.json").read_bytes()
    installed = json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    assert json_upsert_array_entry(installed, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook") == installed
    changed = installed.replace(b'"timeout":5', b'"timeout":99')
    with pytest.raises(ValueError, match="owned JSON entry changed"):
        json_upsert_array_entry(changed, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    with pytest.raises(ValueError, match="owned JSON entry changed"):
        json_remove_owned(changed, ARRAY_PATH, _owned_entry(CODEX_GROUP))
    forced = json_remove_owned(changed + b" ", ARRAY_PATH, _owned_entry(CODEX_GROUP), force=True)
    assert forced == raw + b" "


def test_duplicate_nested_command_identity_is_conflict() -> None:
    raw = (FIXTURES / "no-final.json").read_bytes()
    once = json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    document = json.loads(once)
    document["hooks"]["UserPromptSubmit"].append(CODEX_GROUP)
    duplicate = json.dumps(document, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="more than one matching command identity"):
        json_upsert_array_entry(duplicate, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")


def test_unrelated_later_edit_survives_hook_removal_byte_for_byte() -> None:
    raw = (FIXTURES / "unusual.json").read_bytes()
    inserted = json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    later = inserted.replace(b'"other": true', b'"other": false /* impossible */')
    # Keep the edit valid JSON while retaining a byte-distinct later sibling.
    later = later.replace(b'false /* impossible */', b'false              ')
    removed = json_remove_owned(later, ARRAY_PATH, _owned_entry(CODEX_GROUP))
    assert removed == raw.replace(b'"other": true', b'"other": false              ')


def test_set_and_restore_scalar_preserves_original_raw_token_and_siblings() -> None:
    raw = (FIXTURES / "scalar-unicode.json").read_bytes()
    previous_raw = json_scalar_raw_token(raw, ["outputStyle"])
    assert previous_raw == br'"\u006f\u006c\u0064"'
    installed = json_set_scalar(raw, ["outputStyle"], "koroche-blyat", "claude-output-style")
    assert json.loads(installed)["outputStyle"] == "koroche-blyat"
    owned = {
        "kind": "scalar",
        "installed": "koroche-blyat",
        "existed": True,
        "previous_raw": previous_raw,
    }
    assert json_remove_owned(installed, ["outputStyle"], owned) == raw


def test_new_scalar_removal_restores_object_exactly() -> None:
    raw = b'{\n  "sibling": 1\n}\n'
    installed = json_set_scalar(raw, ["outputStyle"], "koroche-blyat", "claude-output-style")
    owned = {"kind": "scalar", "installed": "koroche-blyat", "existed": False}
    assert json_remove_owned(installed, ["outputStyle"], owned) == raw


def test_user_changed_scalar_conflicts_and_force_preserves_user_value() -> None:
    raw = b'{"outputStyle":"old","keep":1}'
    installed = json_set_scalar(raw, ["outputStyle"], "koroche-blyat", "claude-output-style")
    changed = installed.replace(b'"koroche-blyat"', b'"user-choice"')
    owned = {
        "kind": "scalar", "installed": "koroche-blyat", "existed": True,
        "previous_raw": b'"old"',
    }
    with pytest.raises(ValueError, match="owned JSON scalar changed"):
        json_remove_owned(changed, ["outputStyle"], owned)
    assert json_remove_owned(changed, ["outputStyle"], owned, force=True) == changed


def test_malformed_json_reports_path_line_and_column_before_mutation() -> None:
    raw = b'{\n  "hooks": [1,\n}\n'
    with pytest.raises(ValueError, match=r"broken settings.json:3:[0-9]+:"):
        parse_json_document(raw, "broken settings.json")
    with pytest.raises(ValueError, match=r"broken settings.json:3:[0-9]+:"):
        json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook", path="broken settings.json")


def test_tokenizer_reports_complete_nonoverlapping_byte_spans() -> None:
    raw = br'{ "s": "escaped \" \u041f", "n": -1.2e+3, "b": true, "z": null }'
    parsed, tokens = parse_json_document(raw, "tokens.json")
    assert parsed == {"s": 'escaped " П', "n": -1200.0, "b": True, "z": None}
    assert {kind for _, _, kind in tokens} == {"STRING", "NUMBER", "BOOL", "NULL", "PUNCT", "TRIVIA"}
    assert tokens[0][0] == 0
    assert tokens[-1][1] == len(raw)
    assert all(a < b and (index == 0 or tokens[index - 1][1] == a) for index, (a, b, _) in enumerate(tokens))


def test_exact_host_group_shapes_have_no_forbidden_fields() -> None:
    assert CODEX_GROUP == {
        "hooks": [{"type": "command", "command": COMMAND, "timeout": 5, "additionalContextLimit": 512}]
    }
    assert CLAUDE_GROUP == {
        "hooks": [{"type": "command", "command": COMMAND, "timeout": 5}]
    }
    for group in (CODEX_GROUP, CLAUDE_GROUP):
        encoded = json.dumps(group)
        assert "matcher" not in encoded
        assert "statusMessage" not in encoded


def test_nested_identity_only_and_duplicate_inside_one_group_conflict() -> None:
    top_level_decoy = b'{"hooks":{"UserPromptSubmit":[{"type":"command","command":"' + COMMAND.encode() + b'","hooks":[]}]}}'
    inserted = json_upsert_array_entry(top_level_decoy, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    assert len(json.loads(inserted)["hooks"]["UserPromptSubmit"]) == 2

    duplicate_group = {
        "hooks": [CODEX_GROUP["hooks"][0], CODEX_GROUP["hooks"][0]]
    }
    raw = json.dumps({"hooks": {"UserPromptSubmit": [duplicate_group]}}, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="more than one matching command identity"):
        json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")


def test_missing_path_roundtrip_prunes_only_created_empty_containers() -> None:
    raw = b"{}"
    inserted = json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    owned = {
        **_owned_entry(CODEX_GROUP),
        "created_paths": [["hooks"], ["hooks", "UserPromptSubmit"]],
    }
    assert json_remove_owned(inserted, ARRAY_PATH, owned) == raw


def test_nan_and_infinity_are_rejected_with_path_line_column() -> None:
    for constant in (b"NaN", b"Infinity", b"-Infinity"):
        raw = b'{"managed":' + constant + b'}'
        with pytest.raises(ValueError, match=r"bad.json:1:[0-9]+:"):
            parse_json_document(raw, "bad.json")


def test_scalar_contract_is_type_strict_and_excludes_numbers() -> None:
    raw = b'{"outputStyle":true}'
    with pytest.raises(ValueError, match="must be string, boolean, or null"):
        json_set_scalar(raw, ["outputStyle"], 1, "scalar")
    installed = json_set_scalar(raw, ["outputStyle"], False, "scalar")
    assert json.loads(installed)["outputStyle"] is False


def test_missing_owned_hook_or_scalar_conflicts_unless_force_relinquishes() -> None:
    raw = (FIXTURES / "no-final.json").read_bytes()
    with pytest.raises(ValueError, match="owned JSON entry is missing"):
        json_remove_owned(raw, ARRAY_PATH, _owned_entry(CODEX_GROUP))
    assert json_remove_owned(raw, ARRAY_PATH, _owned_entry(CODEX_GROUP), force=True) == raw

    scalar_owned = {"kind": "scalar", "installed": "koroche-blyat", "existed": False}
    with pytest.raises(ValueError, match="owned JSON scalar is missing"):
        json_remove_owned(b'{"keep":1}', ["outputStyle"], scalar_owned)
    assert json_remove_owned(b'{"keep":1}', ["outputStyle"], scalar_owned, force=True) == b'{"keep":1}'


def test_duplicate_object_keys_are_rejected_before_any_mutation() -> None:
    raw = b'{\n "hooks": {},\n "hooks": {"UserPromptSubmit": []}\n}'
    with pytest.raises(ValueError, match=r"duplicate.json:3:2: duplicate JSON object key hooks"):
        parse_json_document(raw, "duplicate.json")


def test_pruning_never_removes_a_preexisting_empty_parent() -> None:
    raw = b'{"hooks":{},"keep":1}'
    inserted = json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, CODEX_GROUP, "codex-hook")
    owned = {
        **_owned_entry(CODEX_GROUP),
        "created_paths": [["hooks", "UserPromptSubmit"]],
    }
    assert json_remove_owned(inserted, ARRAY_PATH, owned) == raw


def test_nested_scalar_prunes_only_explicitly_created_members() -> None:
    original_existing = b'{"prefs":{},"keep":1}'
    installed_existing = json_set_scalar(
        original_existing, ["prefs", "outputStyle"], "koroche-blyat", "style"
    )
    owned_existing = {
        "kind": "scalar", "installed": "koroche-blyat", "existed": False,
        "created_paths": [["prefs", "outputStyle"]],
    }
    assert json_remove_owned(installed_existing, ["prefs", "outputStyle"], owned_existing) == original_existing

    original_new = b'{}'
    installed_new = json_set_scalar(original_new, ["prefs", "outputStyle"], "koroche-blyat", "style")
    owned_new = {
        "kind": "scalar", "installed": "koroche-blyat", "existed": False,
        "created_paths": [["prefs"], ["prefs", "outputStyle"]],
    }
    assert json_remove_owned(installed_new, ["prefs", "outputStyle"], owned_new) == original_new


@pytest.mark.parametrize(
    "owned",
    [
        {"kind": "array_entry", "matcher": {}, "installed": CODEX_GROUP},
        {"kind": "array_entry", "matcher": MATCHER, "installed": {"hooks": []}},
        {"kind": "array_entry", "matcher": MATCHER, "installed": CODEX_GROUP, "created_paths": [["user"]]},
        {"kind": "scalar", "existed": False},
        {"kind": "scalar", "installed": None, "existed": 1},
        {"kind": "scalar", "installed": "koroche-blyat", "existed": True},
    ],
)
def test_remove_rejects_incomplete_or_unrelated_ownership_metadata(owned: dict) -> None:
    raw = b'{"hooks":{"UserPromptSubmit":[]},"user":{},"outputStyle":null}'
    with pytest.raises(ValueError, match="ownership metadata"):
        json_remove_owned(raw, ARRAY_PATH if owned["kind"] == "array_entry" else ["outputStyle"], owned)


def test_nonstandard_constant_and_duplicate_key_report_exact_character_coordinates() -> None:
    raw = b'{\n  "label":"NaN",\n  "x":NaN\n}'
    with pytest.raises(ValueError, match=r"bad.json:3:7: non-standard JSON constant NaN"):
        parse_json_document(raw, "bad.json")
    duplicate = '{"é":1,"é":2}'.encode("utf-8")
    with pytest.raises(ValueError, match=r"dup.json:1:8: duplicate JSON object key é"):
        parse_json_document(duplicate, "dup.json")


def test_upsert_rejects_non_json_domain_values_before_mutation() -> None:
    raw = (FIXTURES / "no-final.json").read_bytes()
    bad = {**CODEX_GROUP, "extra": (1, 2)}
    with pytest.raises(ValueError, match="strict JSON-domain"):
        json_upsert_array_entry(raw, ARRAY_PATH, MATCHER, bad, "codex-hook")


def test_json_depth_limit_reports_deterministic_value_error() -> None:
    raw = b"[" * 300 + b"0" + b"]" * 300
    with pytest.raises(ValueError, match=r"deep.json:1:129: JSON nesting exceeds limit"):
        parse_json_document(raw, "deep.json")


def test_invalid_utf8_coordinate_counts_unicode_characters() -> None:
    with pytest.raises(ValueError, match=r"bad\.json:1:9: invalid UTF-8"):
        parse_json_document(b'{"\xc3\xa9":"x"\xff}', "bad.json")
