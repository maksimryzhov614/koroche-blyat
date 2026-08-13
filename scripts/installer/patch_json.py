from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


_SCALAR = (str, bool, type(None))
_NUMBER = re.compile(rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    kind: str


@dataclass
class _Member:
    key: str
    key_token: _Token
    value: "_Node"

    @property
    def start(self) -> int:
        return self.key_token.start

    @property
    def end(self) -> int:
        return self.value.end


@dataclass
class _Node:
    kind: str
    start: int
    end: int
    value: Any
    members: Optional[List[_Member]] = None
    items: Optional[List["_Node"]] = None


def _decode(raw: bytes, path: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        line, column = _line_column(raw, error.start)
        raise ValueError("%s:%d:%d: invalid UTF-8" % (path, line, column)) from error


def _line_column(raw: bytes, offset: int) -> Tuple[int, int]:
    prefix = raw[:offset]
    line = prefix.count(b"\n") + 1
    tail = prefix.rsplit(b"\n", 1)[-1]
    try:
        column = len(tail.decode("utf-8")) + 1
    except UnicodeDecodeError:
        column = len(tail) + 1
    return line, column


def _constant_offset(raw: bytes, token: bytes) -> int:
    index = 0
    while index < len(raw):
        if raw[index] == 0x22:
            index += 1
            while index < len(raw):
                if raw[index] == 0x5C:
                    index += 2
                    continue
                if raw[index] == 0x22:
                    index += 1
                    break
                index += 1
            continue
        if raw.startswith(token, index):
            return index
        index += 1
    return 0


def _semantic(raw: bytes, path: str) -> Any:
    text = _decode(raw, path)

    def reject_constant(value: str) -> Any:
        raise RuntimeError(value)

    try:
        return json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise ValueError("%s:%d:%d: %s" % (path, error.lineno, error.colno, error.msg)) from error
    except RuntimeError as error:
        token = str(error).encode("ascii")
        offset = _constant_offset(raw, token)
        line, column = _line_column(raw, offset)
        raise ValueError("%s:%d:%d: non-standard JSON constant %s" % (path, line, column, error)) from error


def _tokens(raw: bytes) -> List[_Token]:
    result: List[_Token] = []
    index = 0
    length = len(raw)
    while index < length:
        start = index
        byte = raw[index]
        if byte in b" \t\r\n":
            index += 1
            while index < length and raw[index] in b" \t\r\n":
                index += 1
            result.append(_Token(start, index, "TRIVIA"))
            continue
        if byte in b"{}[]:,":
            result.append(_Token(start, start + 1, "PUNCT"))
            index += 1
            continue
        if byte == 0x22:
            index += 1
            while index < length:
                if raw[index] == 0x5C:
                    index += 2
                    continue
                if raw[index] == 0x22:
                    index += 1
                    break
                index += 1
            result.append(_Token(start, index, "STRING"))
            continue
        number = _NUMBER.match(raw, index)
        if number is not None:
            index = number.end()
            result.append(_Token(start, index, "NUMBER"))
            continue
        if raw.startswith(b"true", index) or raw.startswith(b"false", index):
            index += 4 if raw.startswith(b"true", index) else 5
            result.append(_Token(start, index, "BOOL"))
            continue
        if raw.startswith(b"null", index):
            index += 4
            result.append(_Token(start, index, "NULL"))
            continue
        # Whole-document validation runs first, so reaching this branch means an
        # internal tokenizer bug rather than user-facing malformed JSON.
        raise ValueError("unsupported JSON token at byte %d" % index)
    return result


class _Parser:
    def __init__(self, raw: bytes, tokens: List[_Token]) -> None:
        self.raw = raw
        self.tokens = [token for token in tokens if token.kind != "TRIVIA"]
        self.index = 0

    def _peek(self) -> _Token:
        if self.index >= len(self.tokens):
            raise ValueError("unexpected end of validated JSON")
        return self.tokens[self.index]

    def _take(self) -> _Token:
        token = self._peek()
        self.index += 1
        return token

    def _punct(self, expected: bytes) -> _Token:
        token = self._take()
        if token.kind != "PUNCT" or self.raw[token.start:token.end] != expected:
            raise ValueError("unexpected token in validated JSON")
        return token

    def node(self) -> _Node:
        token = self._peek()
        raw_token = self.raw[token.start:token.end]
        if raw_token == b"{":
            return self.object()
        if raw_token == b"[":
            return self.array()
        token = self._take()
        value = json.loads(self.raw[token.start:token.end].decode("utf-8"))
        return _Node(token.kind.lower(), token.start, token.end, value)

    def object(self) -> _Node:
        opening = self._punct(b"{")
        members: List[_Member] = []
        if self.raw[self._peek().start:self._peek().end] != b"}":
            while True:
                key_token = self._take()
                if key_token.kind != "STRING":
                    raise ValueError("non-string key in validated JSON")
                key = json.loads(self.raw[key_token.start:key_token.end].decode("utf-8"))
                self._punct(b":")
                value = self.node()
                members.append(_Member(key, key_token, value))
                if self.raw[self._peek().start:self._peek().end] != b",":
                    break
                self._punct(b",")
        closing = self._punct(b"}")
        value = {member.key: member.value.value for member in members}
        return _Node("object", opening.start, closing.end, value, members=members)

    def array(self) -> _Node:
        opening = self._punct(b"[")
        items: List[_Node] = []
        if self.raw[self._peek().start:self._peek().end] != b"]":
            while True:
                items.append(self.node())
                if self.raw[self._peek().start:self._peek().end] != b",":
                    break
                self._punct(b",")
        closing = self._punct(b"]")
        return _Node("array", opening.start, closing.end, [item.value for item in items], items=items)



def _reject_duplicate_members(node: _Node, raw: bytes, path: str) -> None:
    if node.kind == "object" and node.members is not None:
        seen = set()
        for member in node.members:
            if member.key in seen:
                line, column = _line_column(raw, member.key_token.start)
                raise ValueError("%s:%d:%d: duplicate JSON object key %s" % (
                    path, line, column, member.key,
                ))
            seen.add(member.key)
            _reject_duplicate_members(member.value, raw, path)
    elif node.kind == "array" and node.items is not None:
        for item in node.items:
            _reject_duplicate_members(item, raw, path)

def _check_json_limits(raw: bytes, path: str) -> None:
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("%s:1:1: JSON document exceeds size limit" % path)
    depth = 0
    index = 0
    while index < len(raw):
        byte = raw[index]
        if byte == 0x22:
            index += 1
            while index < len(raw):
                if raw[index] == 0x5C:
                    index += 2
                    continue
                if raw[index] == 0x22:
                    index += 1
                    break
                index += 1
            continue
        if byte in (0x5B, 0x7B):
            depth += 1
            if depth > 128:
                line, column = _line_column(raw, index)
                raise ValueError("%s:%d:%d: JSON nesting exceeds limit" % (path, line, column))
        elif byte in (0x5D, 0x7D):
            depth -= 1
        index += 1


def _document(raw: bytes, path: str) -> Tuple[Any, List[_Token], _Node]:
    _check_json_limits(raw, path)
    semantic = _semantic(raw, path)
    tokens = _tokens(raw)
    parser = _Parser(raw, tokens)
    root = parser.node()
    if parser.index != len(parser.tokens):
        raise ValueError("trailing token in validated JSON")
    _reject_duplicate_members(root, raw, path)
    return semantic, tokens, root


def parse_json_document(raw: bytes, path: str) -> Tuple[Any, List[Tuple[int, int, str]]]:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    semantic, tokens, _root = _document(raw, path)
    return semantic, [(token.start, token.end, token.kind) for token in tokens]


def _member(node: _Node, key: str) -> Optional[_Member]:
    if node.kind != "object" or node.members is None:
        raise ValueError("JSON path parent is not an object")
    matches = [member for member in node.members if member.key == key]
    if len(matches) > 1:
        raise ValueError("duplicate JSON object key at managed path")
    return matches[0] if matches else None


def _at(root: _Node, path: Sequence[str]) -> _Node:
    node = root
    for component in path:
        member = _member(node, component)
        if member is None:
            raise ValueError("JSON path does not exist: %s" % ".".join(path))
        node = member.value
    return node


def _parent_member(root: _Node, path: Sequence[str]) -> Tuple[_Node, _Member]:
    if not path:
        raise ValueError("JSON path must not be empty")
    parent = _at(root, path[:-1]) if len(path) > 1 else root
    member = _member(parent, path[-1])
    if member is None:
        raise ValueError("JSON path does not exist: %s" % ".".join(path))
    return parent, member


def _strict_json_value(value: Any, label: str) -> None:
    if type(value) in (str, int, bool, type(None)):
        return
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("%s must be a strict JSON-domain value" % label)
        return
    if type(value) is list:
        for item in value:
            _strict_json_value(item, label)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("%s must be a strict JSON-domain value" % label)
            _strict_json_value(item, label)
        return
    raise ValueError("%s must be a strict JSON-domain value" % label)


def _compact(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _separator(raw: bytes, node: _Node) -> bytes:
    trailing_start = node.end - 1
    children_end = node.start + 1
    if node.kind == "object" and node.members:
        children_end = node.members[-1].end
    elif node.kind == "array" and node.items:
        children_end = node.items[-1].end
    trailing = raw[children_end:trailing_start]
    newline = b"\r\n" if b"\r\n" in trailing else b"\n"
    if newline in trailing:
        indent = trailing.rsplit(newline, 1)[1]
        return newline + indent
    return b" "


def _insert_member(raw: bytes, node: _Node, key: str, value: bytes) -> bytes:
    if node.kind != "object" or node.members is None:
        raise ValueError("JSON path parent is not an object")
    pair = _compact(key) + b":" + value
    if not node.members:
        position = node.start + 1
        return raw[:position] + pair + raw[position:]
    position = node.members[-1].end
    inserted = b"," + _separator(raw, node) + pair
    return raw[:position] + inserted + raw[position:]


def _insert_missing_path(raw: bytes, root: _Node, path: Sequence[str], leaf: Any) -> bytes:
    node = root
    for index, component in enumerate(path):
        member = _member(node, component)
        if member is None:
            nested: Any = leaf
            for remaining in reversed(path[index + 1:]):
                nested = {remaining: nested}
            return _insert_member(raw, node, component, _compact(nested))
        node = member.value
    raise ValueError("managed JSON path already exists")


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return bool(left == right)


def _nested_match_count(entry: Any, matcher: Mapping[str, str]) -> int:
    if not isinstance(entry, Mapping):
        return 0
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return 0
    return sum(
        1
        for hook in hooks
        if isinstance(hook, Mapping) and all(
            type(hook.get(key)) is str and hook.get(key) == value
            for key, value in matcher.items()
        )
    )


def _matching_items(node: _Node, matcher: Mapping[str, str]) -> List[Tuple[int, _Node]]:
    if node.kind != "array" or node.items is None:
        raise ValueError("managed JSON path is not an array")
    matches: List[Tuple[int, _Node]] = []
    for index, item in enumerate(node.items):
        matches.extend((index, item) for _ in range(_nested_match_count(item.value, matcher)))
    return matches


def json_upsert_array_entry(
    raw: bytes,
    path_to_array: Sequence[str],
    entry_matcher: Mapping[str, str],
    new_entry: Mapping[str, Any],
    owned_id: str,
    path: str = "<json>",
) -> bytes:
    if not owned_id or not isinstance(owned_id, str):
        raise ValueError("owned id is required")
    if type(new_entry) is not dict:
        raise ValueError("new JSON entry must be a strict JSON-domain object")
    _strict_json_value(new_entry, "new JSON entry")
    if type(entry_matcher) is not dict or not entry_matcher or not all(type(key) is str and type(value) is str for key, value in entry_matcher.items()):
        raise ValueError("entry matcher must contain string identities")
    if _nested_match_count(new_entry, entry_matcher) != 1:
        raise ValueError("new JSON entry must contain exactly one matching nested command")
    _semantic_value, _token_list, root = _document(raw, path)
    try:
        array = _at(root, path_to_array)
    except ValueError as error:
        if "does not exist" not in str(error):
            raise
        return _validate_mutation(_insert_missing_path(raw, root, path_to_array, [new_entry]), path)
    matches = _matching_items(array, entry_matcher)
    if len(matches) > 1:
        raise ValueError("more than one matching command identity")
    if matches:
        _index, item = matches[0]
        if not _json_equal(item.value, dict(new_entry)):
            raise ValueError("owned JSON entry changed")
        return raw
    assert array.items is not None
    encoded = _compact(new_entry)
    if not array.items:
        position = array.start + 1
        return _validate_mutation(raw[:position] + encoded + raw[position:], path)
    position = array.items[-1].end
    inserted = b"," + _separator(raw, array) + encoded
    return _validate_mutation(raw[:position] + inserted + raw[position:], path)


def json_scalar_raw_token(raw: bytes, path_to_key: Sequence[str], path: str = "<json>") -> Optional[bytes]:
    _semantic_value, _token_list, root = _document(raw, path)
    try:
        _parent, member = _parent_member(root, path_to_key)
    except ValueError as error:
        if "does not exist" in str(error):
            return None
        raise
    if type(member.value.value) not in _SCALAR:
        raise ValueError("managed JSON value is not scalar")
    return raw[member.value.start:member.value.end]


def json_set_scalar(
    raw: bytes,
    path_to_key: Sequence[str],
    value: Union[str, bool, None],
    owned_id: str,
    path: str = "<json>",
) -> bytes:
    if not owned_id or not isinstance(owned_id, str):
        raise ValueError("owned id is required")
    if type(value) not in _SCALAR:
        raise ValueError("managed JSON value must be string, boolean, or null")
    _semantic_value, _token_list, root = _document(raw, path)
    try:
        _parent, member = _parent_member(root, path_to_key)
    except ValueError as error:
        if "does not exist" not in str(error):
            raise
        if len(path_to_key) == 1:
            return _validate_mutation(_insert_member(raw, root, path_to_key[0], _compact(value)), path)
        return _validate_mutation(_insert_missing_path(raw, root, path_to_key, value), path)
    if type(member.value.value) not in _SCALAR:
        raise ValueError("managed JSON value is not scalar")
    if _json_equal(member.value.value, value):
        return raw
    return _validate_mutation(raw[:member.value.start] + _compact(value) + raw[member.value.end:], path)


def _remove_array_item(raw: bytes, array: _Node, index: int) -> bytes:
    assert array.items is not None
    item = array.items[index]
    if len(array.items) == 1:
        return raw[:item.start] + raw[item.end:]
    if index > 0:
        previous = array.items[index - 1]
        return raw[:previous.end] + raw[item.end:]
    following = array.items[1]
    between = raw[item.end:following.start]
    comma = between.find(b",")
    if comma < 0:
        raise ValueError("array delimiter is missing")
    return raw[:item.start] + raw[item.end + comma + 1:]


def _remove_member(raw: bytes, parent: _Node, member: _Member) -> bytes:
    assert parent.members is not None
    index = parent.members.index(member)
    if len(parent.members) == 1:
        return raw[:member.start] + raw[member.end:]
    if index > 0:
        previous = parent.members[index - 1]
        return raw[:previous.end] + raw[member.end:]
    following = parent.members[1]
    between = raw[member.end:following.start]
    comma = between.find(b",")
    if comma < 0:
        raise ValueError("object delimiter is missing")
    return raw[:member.start] + raw[member.end + comma + 1:]


def _validate_mutation(raw: bytes, path: str) -> bytes:
    _document(raw, path)
    return raw


def _prune_created_paths(raw: bytes, created_paths: Sequence[Sequence[str]], path: str) -> bytes:
    result = raw
    normalized = sorted(
        (tuple(parts) for parts in created_paths), key=lambda parts: len(parts), reverse=True
    )
    for created_path in normalized:
        if not created_path or not all(isinstance(part, str) for part in created_path):
            raise ValueError("created JSON path metadata is invalid")
        _semantic_value, _token_list, root = _document(result, path)
        parent_path = created_path[:-1]
        try:
            parent = _at(root, parent_path) if parent_path else root
        except ValueError:
            continue
        member = _member(parent, created_path[-1])
        if member is None:
            continue
        if member.value.value not in ({}, []):
            continue
        result = _remove_member(result, parent, member)
    return result


def _created_paths(value: Any, managed_path: Sequence[str]) -> Tuple[Tuple[str, ...], ...]:
    if value is None:
        return ()
    if type(value) is not list:
        raise ValueError("ownership metadata created_paths is invalid")
    result = []
    for item in value:
        if type(item) is not list or not item or not all(type(part) is str and part for part in item):
            raise ValueError("ownership metadata created_paths is invalid")
        candidate = tuple(item)
        managed = tuple(managed_path)
        if candidate != managed[:len(candidate)]:
            raise ValueError("ownership metadata created_paths is unrelated")
        if candidate in result:
            raise ValueError("ownership metadata created_paths is duplicated")
        result.append(candidate)
    return tuple(result)


def _array_ownership(owned: Mapping[str, Any], managed_path: Sequence[str]) -> Tuple[Mapping[str, str], Mapping[str, Any], Tuple[Tuple[str, ...], ...]]:
    allowed = {"kind", "matcher", "installed", "created_paths"}
    if set(owned) - allowed or not {"kind", "matcher", "installed"} <= set(owned):
        raise ValueError("array ownership metadata is incomplete")
    matcher = owned.get("matcher")
    installed = owned.get("installed")
    if type(matcher) is not dict or not matcher or not all(
        type(key) is str and type(value) is str for key, value in matcher.items()
    ):
        raise ValueError("array ownership metadata matcher is invalid")
    if type(installed) is not dict:
        raise ValueError("array ownership metadata installed entry is invalid")
    _strict_json_value(installed, "array ownership metadata")
    if _nested_match_count(installed, matcher) != 1:
        raise ValueError("array ownership metadata installed identity is invalid")
    return matcher, installed, _created_paths(owned.get("created_paths"), managed_path)


def _scalar_ownership(owned: Mapping[str, Any], managed_path: Sequence[str]) -> Tuple[Any, bool, Optional[bytes], Tuple[Tuple[str, ...], ...]]:
    allowed = {"kind", "installed", "existed", "previous_raw", "created_paths"}
    if set(owned) - allowed or not {"kind", "installed", "existed"} <= set(owned):
        raise ValueError("scalar ownership metadata is incomplete")
    installed = owned.get("installed")
    existed = owned.get("existed")
    if type(installed) not in _SCALAR or type(existed) is not bool:
        raise ValueError("scalar ownership metadata has invalid types")
    previous_raw = owned.get("previous_raw")
    if existed and not isinstance(previous_raw, bytes):
        raise ValueError("scalar ownership metadata previous token is missing")
    if not existed and "previous_raw" in owned:
        raise ValueError("scalar ownership metadata previous token is inconsistent")
    created = _created_paths(owned.get("created_paths"), managed_path)
    if existed and created:
        raise ValueError("scalar ownership metadata created paths are inconsistent")
    return installed, existed, previous_raw, created


def json_remove_owned(
    raw: bytes,
    path_to_item: Sequence[str],
    owned_id: Any,
    force: bool = False,
    path: str = "<json>",
) -> bytes:
    if not isinstance(owned_id, Mapping):
        raise ValueError("ownership metadata is required for JSON removal")
    kind = owned_id.get("kind")
    _semantic_value, _token_list, root = _document(raw, path)
    if kind == "array_entry":
        matcher, installed, created_paths = _array_ownership(owned_id, path_to_item)
        try:
            array = _at(root, path_to_item)
        except ValueError as error:
            if "does not exist" in str(error) and force:
                return raw
            if "does not exist" in str(error):
                raise ValueError("owned JSON entry is missing") from error
            raise
        matches = _matching_items(array, matcher)
        if len(matches) > 1:
            raise ValueError("more than one matching command identity")
        if not matches:
            if force:
                return raw
            raise ValueError("owned JSON entry is missing")
        index, item = matches[0]
        if not _json_equal(item.value, dict(installed)) and not force:
            raise ValueError("owned JSON entry changed")
        result = _remove_array_item(raw, array, index)
        result = _prune_created_paths(result, created_paths, path)
        return _validate_mutation(result, path)
    if kind == "scalar":
        installed, existed, previous_raw, created_paths = _scalar_ownership(owned_id, path_to_item)
        try:
            parent, member = _parent_member(root, path_to_item)
        except ValueError as error:
            if "does not exist" in str(error) and force:
                return raw
            if "does not exist" in str(error):
                raise ValueError("owned JSON scalar is missing") from error
            raise
        if not _json_equal(member.value.value, installed):
            if force:
                return raw
            raise ValueError("owned JSON scalar changed")
        if existed:
            assert previous_raw is not None
            previous_value = _semantic(previous_raw, "<owned scalar>")
            if type(previous_value) not in _SCALAR:
                raise ValueError("scalar ownership baseline is not scalar")
            result = raw[:member.value.start] + previous_raw + raw[member.value.end:]
            return _validate_mutation(result, path)
        result = _remove_member(raw, parent, member)
        result = _prune_created_paths(result, created_paths, path)
        return _validate_mutation(result, path)
    raise ValueError("unknown JSON ownership kind")
