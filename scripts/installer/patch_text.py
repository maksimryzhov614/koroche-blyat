from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Optional, Tuple


_BLOCK_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_ALLOWED_SEPARATORS = (b"", b"\n", b"\n\n")


def _markers(block_id: str) -> Tuple[bytes, bytes]:
    if not isinstance(block_id, str) or _BLOCK_ID.fullmatch(block_id) is None:
        raise ValueError("invalid managed block id")
    encoded = block_id.encode("ascii")
    return (
        b"<!-- BEGIN KOROCHE-BLYAT MANAGED: " + encoded + b" v1 -->",
        b"<!-- END KOROCHE-BLYAT MANAGED: " + encoded + b" v1 -->",
    )


def _locate(
    raw: bytes, block_id: str, strict_boundaries: bool = True
) -> Optional[Tuple[int, int, int, int]]:
    begin, end = _markers(block_id)
    begin_positions = [match.start() for match in re.finditer(re.escape(begin), raw)]
    end_positions = [match.start() for match in re.finditer(re.escape(end), raw)]
    if not begin_positions and not end_positions:
        return None
    if len(begin_positions) != 1 or len(end_positions) != 1:
        raise ValueError("managed marker is duplicate or orphaned")
    begin_start = begin_positions[0]
    end_start = end_positions[0]
    if begin_start >= end_start:
        raise ValueError("managed marker order is invalid")
    begin_end = begin_start + len(begin)
    end_end = end_start + len(end)
    if strict_boundaries:
        if begin_start > 0 and raw[begin_start - 1:begin_start] != b"\n":
            raise ValueError("managed marker must be on its own line")
        if raw[begin_end:begin_end + 1] != b"\n":
            raise ValueError("managed marker must be on its own LF line")
        if end_start == 0 or raw[end_start - 1:end_start] != b"\n":
            raise ValueError("managed marker must be on its own LF line")
        if end_end < len(raw) and raw[end_end:end_end + 1] != b"\n":
            raise ValueError("managed marker must be on its own line")
        inner_start = begin_end + 1
    else:
        inner_start = begin_end + (1 if raw[begin_end:begin_end + 1] == b"\n" else 0)
    if raw[end_end:end_end + 2] == b"\r\n":
        span_end = end_end + 2
    elif raw[end_end:end_end + 1] == b"\n":
        span_end = end_end + 1
    else:
        span_end = end_end
    return begin_start, inner_start, end_start, span_end


def _payload_bytes(payload: bytes) -> bytes:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    return payload if payload.endswith(b"\n") else payload + b"\n"


def upsert_marker_block(
    raw: bytes,
    block_id: str,
    payload: bytes,
    previous: Optional[bytes] = None,
) -> bytes:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    begin, end = _markers(block_id)
    normalized = _payload_bytes(payload)
    if begin in normalized or end in normalized:
        raise ValueError("payload contains managed marker")
    located = _locate(raw, block_id)
    if located is not None:
        _span_start, inner_start, inner_end, _span_end = located
        current = raw[inner_start:inner_end]
        if previous is not None and current != _payload_bytes(previous):
            raise ValueError("owned block changed")
        candidate = raw[:inner_start] + normalized + raw[inner_end:]
        _locate(candidate, block_id)
        return candidate

    block = begin + b"\n" + normalized + end + b"\n"
    if not raw:
        separator = b""
    elif raw.endswith(b"\n"):
        separator = b"\n"
    else:
        separator = b"\n\n"
    candidate = raw + separator + block
    _locate(candidate, block_id)
    return candidate


def _owned_fields(
    owned: Any,
) -> Tuple[str, Optional[str], bytes, Optional[int], Optional[str]]:
    if isinstance(owned, str):
        return owned, None, b"", None, None
    if isinstance(owned, Mapping):
        locator = owned.get("locator", owned)
        baseline = owned.get("baseline", owned)
    else:
        locator = getattr(owned, "locator", {})
        baseline = getattr(owned, "baseline", {})
    if not isinstance(locator, Mapping) or not isinstance(baseline, Mapping):
        raise ValueError("owned marker metadata is invalid")
    block_id = locator.get("block_id")
    digest = baseline.get("owned_span_sha256")
    separator_hex = baseline.get("separator_hex", "")
    anchor_length = baseline.get("separator_anchor_length")
    anchor_hash = baseline.get("separator_anchor_sha256")
    if not isinstance(block_id, str):
        raise ValueError("owned marker block id is missing")
    if digest is not None and not isinstance(digest, str):
        raise ValueError("owned span hash is invalid")
    if not isinstance(separator_hex, str):
        raise ValueError("owned separator is invalid")
    try:
        separator = bytes.fromhex(separator_hex)
    except ValueError as error:
        raise ValueError("owned separator is invalid") from error
    if separator not in _ALLOWED_SEPARATORS:
        raise ValueError("owned separator is invalid")
    if anchor_length is not None and (type(anchor_length) is not int or not 0 <= anchor_length <= 64):
        raise ValueError("owned separator anchor is invalid")
    if anchor_hash is not None and (
        not isinstance(anchor_hash, str) or re.fullmatch(r"[0-9a-f]{64}", anchor_hash) is None
    ):
        raise ValueError("owned separator anchor is invalid")
    if (anchor_length is None) != (anchor_hash is None):
        raise ValueError("owned separator anchor is incomplete")
    return block_id, digest, separator, anchor_length, anchor_hash


def _separator_interval(
    raw: bytes,
    span_start: int,
    separator: bytes,
    anchor_length: Optional[int],
    anchor_hash: Optional[str],
) -> Optional[Tuple[int, int]]:
    if not separator:
        return None
    if anchor_length is None or anchor_hash is None or anchor_length == 0:
        return None
    prefix = raw[:span_start]
    candidates = []
    for start in range(0, len(prefix) - anchor_length + 1):
        end = start + anchor_length
        if hashlib.sha256(prefix[start:end]).hexdigest() != anchor_hash:
            continue
        if prefix[end:end + len(separator)] == separator:
            candidates.append((end, end + len(separator)))
    if len(candidates) == 1:
        return candidates[0]
    return None


def remove_marker_block(raw: bytes, owned: Any, force: bool = False) -> bytes:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    block_id, expected_hash, separator, anchor_length, anchor_hash = _owned_fields(owned)
    if not force and (
        expected_hash is None or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
    ):
        raise ValueError("owned span hash is required")
    located = _locate(raw, block_id, strict_boundaries=not force)
    if located is None:
        return raw
    span_start, _inner_start, _inner_end, span_end = located
    span = raw[span_start:span_end]
    actual_hash = hashlib.sha256(span).hexdigest()
    if expected_hash is not None and actual_hash != expected_hash and not force:
        raise ValueError("owned block changed")
    interval = _separator_interval(
        raw, span_start, separator, anchor_length, anchor_hash
    )
    if interval is None:
        return raw[:span_start] + raw[span_end:]
    separator_start, separator_end = interval
    if separator_end > span_start:
        return raw[:span_start] + raw[span_end:]
    return raw[:separator_start] + raw[separator_end:span_start] + raw[span_end:]
