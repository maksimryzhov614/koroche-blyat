"""Provenance manifest loader and offline/online checker for UPSTREAMS.yml.

CLI exits:
  0 — all checks pass
  1 — content or schema mismatch (invalid YAML, unknown fields, missing
      manifest, missing redistributed, hash mismatch, HTTP 404/410)
  2 — network error (timeout/DNS/TLS/connection/HTTP 401,403,429,5xx)
      or argparse usage / manifest permission / IO environment error.
      Network errors dominate concurrent content mismatches.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import quote, urlparse
import urllib.error
import urllib.request

# PyYAML is a project tooling dependency; runtime installer code does not import it.
import yaml

# ── Constants ─────────────────────────────────────────────────────────────────

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_USE = frozenset({"adapted", "redistributed", "excluded-clean-room-evidence"})
_RAW_GH = "https://raw.githubusercontent.com"
_RAW_GH_HOST = "raw.githubusercontent.com"

# HTTP status codes that indicate pinned source absence (content error, not network)
_CONTENT_HTTP_CODES = frozenset({404, 410})

# Allowed root-level keys
_ROOT_KEYS = frozenset({"schema_version", "upstreams"})

# Allowed upstream-level keys
_UPSTREAM_KEYS = frozenset({
    "id", "repository", "tag", "commit", "license",
    "distributed", "sources",
})

# Allowed source-level keys
_SOURCE_KEYS = frozenset({"path", "sha256", "use", "redistributed_as"})

# Safe GitHub owner/repo segment: alphanumeric, hyphen, underscore, dot
# but not . or .. alone, and no leading/trailing dots
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

# Control characters (ASCII 0x00–0x1F, 0x7F)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceEntry:
    path: str
    sha256: str
    use: str
    redistributed_as: Optional[str] = None


@dataclass(frozen=True)
class Upstream:
    id: str
    repository: str
    tag: Optional[str]
    commit: str
    license: str
    distributed: bool
    sources: List[SourceEntry] = field(default_factory=list)


@dataclass(frozen=True)
class ProvenanceManifest:
    schema_version: int
    upstreams: List[Upstream]


# ── Path validation ──────────────────────────────────────────────────────────

def _validate_safe_path(value: str, label: str) -> None:
    """Validate that a path is a safe relative POSIX path."""
    if not value:
        raise ValueError(f"{label}: path is empty")
    if "\\" in value:
        raise ValueError(f"{label}: backslash in path: {value!r}")
    if value.startswith("/"):
        raise ValueError(f"{label}: absolute path: {value!r}")
    if _CONTROL_RE.search(value):
        raise ValueError(f"{label}: control characters in path: {value!r}")
    if "?" in value or "#" in value:
        raise ValueError(f"{label}: query/fragment in path: {value!r}")
    segments = value.split("/")
    for seg in segments:
        if seg == "":
            raise ValueError(f"{label}: empty segment in path: {value!r}")
        if seg == ".":
            raise ValueError(f"{label}: dot segment in path: {value!r}")
        if seg == "..":
            raise ValueError(f"{label}: dotdot traversal in path: {value!r}")


def _validate_repository_url(url: str, uid: str) -> None:
    """Validate repository URL is exactly https://github.com/owner/repo."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"upstream {uid}: bad repository URL scheme: {url!r}")
    if parsed.hostname != "github.com":
        raise ValueError(f"upstream {uid}: bad repository URL host: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"upstream {uid}: credentials in repository URL: {url!r}")
    if parsed.query:
        raise ValueError(f"upstream {uid}: query in repository URL: {url!r}")
    if parsed.fragment:
        raise ValueError(f"upstream {uid}: fragment in repository URL: {url!r}")
    if parsed.port is not None:
        raise ValueError(f"upstream {uid}: explicit port in repository URL: {url!r}")

    # Path must be exactly /owner/repo (with optional trailing slash)
    path = parsed.path.rstrip("/")
    parts = path.split("/")
    # parts[0] is '' (leading /), parts[1] is owner, parts[2] is repo
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise ValueError(f"upstream {uid}: bad repository URL path: {url!r}")

    owner, repo = parts[1], parts[2]
    if not _SAFE_SEGMENT_RE.match(owner):
        raise ValueError(f"upstream {uid}: unsafe owner segment in URL: {url!r}")
    if not _SAFE_SEGMENT_RE.match(repo):
        raise ValueError(f"upstream {uid}: unsafe repo segment in URL: {url!r}")


# ── YAML alias/anchor pre-parse check ────────────────────────────────────────

def _reject_yaml_aliases(raw: str) -> None:
    """Reject YAML anchors, aliases, and plain merge keys before parsing.

    Token scanning ignores comments and quoted literals. Scanner failures are
    allowed to propagate as YAML schema errors rather than being swallowed.
    """
    for token in yaml.scan(raw):
        if isinstance(token, yaml.tokens.AnchorToken):
            raise ValueError(
                f"YAML anchor &{token.value} is forbidden in UPSTREAMS.yml"
            )
        if isinstance(token, yaml.tokens.AliasToken):
            raise ValueError(
                f"YAML alias *{token.value} is forbidden in UPSTREAMS.yml"
            )
        if (
            isinstance(token, yaml.tokens.ScalarToken)
            and token.value == "<<"
            and token.style is None
        ):
            raise ValueError("YAML merge key << is forbidden in UPSTREAMS.yml")


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate and non-string mapping keys."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.nodes.MappingNode):
            raise ValueError("YAML mapping node expected")
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str:
                raise ValueError("YAML mapping keys must be strings")
            if key in mapping:
                raise ValueError(f"duplicate YAML key: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


# ── Loader with validation ────────────────────────────────────────────────────

def load_manifest(path: Path) -> ProvenanceManifest:
    """Load and validate UPSTREAMS.yml, raising ValueError on schema violations."""
    raw = path.read_text(encoding="utf-8")

    # Pre-parse: reject anchors/aliases/merge keys
    _reject_yaml_aliases(raw)

    data = yaml.load(raw, Loader=_StrictSafeLoader)

    if not isinstance(data, dict):
        raise ValueError("UPSTREAMS.yml root must be a mapping")

    # Reject unknown root-level keys
    unknown_root = sorted(set(data.keys()) - _ROOT_KEYS)
    if unknown_root:
        raise ValueError(f"unknown root-level fields: {unknown_root}")

    sv = data.get("schema_version")
    if type(sv) is not int or sv != 1:
        raise ValueError(f"unsupported schema_version: {sv!r} (must be integer 1)")

    upstreams_raw = data.get("upstreams")
    if not isinstance(upstreams_raw, list) or not upstreams_raw:
        raise ValueError("upstreams must be a non-empty list")

    upstreams: List[Upstream] = []
    seen_ids: set = set()

    for u in upstreams_raw:
        if not isinstance(u, dict):
            raise ValueError(f"upstream entry must be a mapping, got {type(u)}")

        # Reject unknown upstream fields
        unknown_up = sorted(set(u.keys()) - _UPSTREAM_KEYS)
        if unknown_up:
            raise ValueError(f"unknown upstream fields: {unknown_up}")

        # Require all upstream fields present
        missing_up = sorted(_UPSTREAM_KEYS - set(u.keys()))
        if missing_up:
            raise ValueError(
                f"upstream {u.get('id', '?')}: missing required fields: {missing_up}"
            )

        uid = u.get("id")
        if type(uid) is not str or not uid:
            raise ValueError("upstream id must be a non-empty string")

        if uid in seen_ids:
            raise ValueError(f"duplicate upstream id: {uid}")
        seen_ids.add(uid)

        # -- license: required, must be string --
        lic = u.get("license")
        if type(lic) is not str or not lic:
            raise ValueError(
                f"upstream {uid}: missing or invalid license field"
            )

        # -- commit: 40-char hex --
        commit = u.get("commit")
        if type(commit) is not str or not _SHA_RE.fullmatch(commit):
            raise ValueError(
                f"upstream {uid}: commit must be 40-char lowercase hex, "
                f"got {commit!r}"
            )

        # -- repository: strict URL --
        repo = u.get("repository")
        if type(repo) is not str or not repo:
            raise ValueError(f"upstream {uid}: repository must be a non-empty string")
        _validate_repository_url(repo, uid)

        # -- tag: required, null or string --
        if "tag" not in u:
            raise ValueError(f"upstream {uid}: tag field is required")
        tag = u["tag"]
        if tag is not None and not isinstance(tag, str):
            raise ValueError(
                f"upstream {uid}: tag must be null or string, got {type(tag).__name__}"
            )

        # -- distributed: must be actual bool --
        dist_raw = u.get("distributed")
        if not isinstance(dist_raw, bool):
            raise ValueError(
                f"upstream {uid}: distributed must be boolean, "
                f"got {type(dist_raw).__name__}: {dist_raw!r}"
            )

        if "sources" not in u:
            raise ValueError(f"upstream {uid}: sources field is required")
        sources_raw = u["sources"]
        if not isinstance(sources_raw, list):
            raise ValueError(f"upstream {uid}: sources must be a list")

        if not sources_raw:
            raise ValueError(f"upstream {uid}: sources must be non-empty")

        sources: List[SourceEntry] = []
        seen_paths: set = set()

        for s in sources_raw:
            if not isinstance(s, dict):
                raise ValueError(f"upstream {uid}: source entry must be a mapping")

            # Reject unknown source fields
            unknown_src = sorted(set(s.keys()) - _SOURCE_KEYS)
            if unknown_src:
                raise ValueError(
                    f"upstream {uid}: unknown source fields: {unknown_src}"
                )

            spath = s.get("path")
            if type(spath) is not str or not spath:
                raise ValueError(f"upstream {uid}: source missing path")

            # Validate path safety
            _validate_safe_path(spath, f"upstream {uid}, source path")

            if spath in seen_paths:
                raise ValueError(
                    f"upstream {uid}: duplicate source path: {spath}"
                )
            seen_paths.add(spath)

            use = s.get("use")
            if type(use) is not str or use not in _VALID_USE:
                raise ValueError(
                    f"upstream {uid}, path {spath}: unknown use value {use!r}"
                )

            sha = s.get("sha256")
            if type(sha) is not str or not sha:
                raise ValueError(
                    f"upstream {uid}, path {spath}: sha256 is required and must be a string"
                )
            if not _SHA256_RE.fullmatch(sha):
                raise ValueError(
                    f"upstream {uid}, path {spath}: bad sha256 {sha!r}"
                )
            sha_str = sha

            # Validate redistributed_as path safety
            redist = s.get("redistributed_as")
            if redist is not None:
                if not isinstance(redist, str) or not redist:
                    raise ValueError(
                        f"upstream {uid}, path {spath}: "
                        f"redistributed_as must be a non-empty string"
                    )
                _validate_safe_path(redist, f"upstream {uid}, redistributed_as")

            # Consistency: redistributed_as iff use == "redistributed"
            if use == "redistributed" and not redist:
                raise ValueError(
                    f"upstream {uid}, path {spath}: "
                    f"use=redistributed requires redistributed_as"
                )
            if use != "redistributed" and redist:
                raise ValueError(
                    f"upstream {uid}, path {spath}: "
                    f"redistributed_as only valid with use=redistributed"
                )

            # Excluded sources must not be redistributed
            if use == "excluded-clean-room-evidence" and redist:
                raise ValueError(
                    f"upstream {uid}: excluded source {spath} "
                    f"must not have redistributed_as"
                )

            sources.append(SourceEntry(
                path=spath,
                sha256=sha_str,
                use=use,
                redistributed_as=redist,
            ))

        # E. Distributed consistency: excluded-only upstream must not be distributed
        all_excluded = all(s.use == "excluded-clean-room-evidence" for s in sources)
        if dist_raw and all_excluded:
            raise ValueError(
                f"upstream {uid}: distributed=true but all sources are "
                f"excluded-clean-room-evidence"
            )

        # E. Distributed upstream must include at least one redistributed source
        has_redistributed = any(s.use == "redistributed" for s in sources)
        if dist_raw and not has_redistributed:
            raise ValueError(
                f"upstream {uid}: distributed upstream must include at least "
                f"one redistributed source (license evidence)"
            )

        upstreams.append(Upstream(
            id=uid,
            repository=repo,
            tag=tag,
            commit=commit,
            license=lic,
            distributed=dist_raw,
            sources=sources,
        ))

    return ProvenanceManifest(schema_version=1, upstreams=upstreams)


# ── Offline check ─────────────────────────────────────────────────────────────

def check_offline(root: Path, manifest: ProvenanceManifest) -> List[str]:
    """Verify redistributed file existence and hash integrity. Returns errors."""
    errors: List[str] = []

    # Validate root directory itself
    if not root.exists():
        return [f"root directory does not exist: {root}"]
    if root.is_symlink():
        return [f"root directory is a symlink: {root}"]
    if not root.is_dir():
        return [f"root path is not a directory: {root}"]

    resolved_root = root.resolve()
    all_redistributed: List[str] = []

    for u in manifest.upstreams:
        for src in u.sources:
            # Redistributed files must exist and match hash
            if src.redistributed_as:
                all_redistributed.append(src.redistributed_as)
                local = root / src.redistributed_as
                if not local.exists():
                    errors.append(
                        f"upstream {u.id}: redistributed file missing: "
                        f"{src.redistributed_as}"
                    )
                    continue

                # Reject symlinks
                if local.is_symlink():
                    errors.append(
                        f"upstream {u.id}: redistributed file is a symlink: "
                        f"{src.redistributed_as}"
                    )
                    continue

                # Reject if not a regular file
                if not local.is_file():
                    errors.append(
                        f"upstream {u.id}: redistributed path is not a regular file: "
                        f"{src.redistributed_as}"
                    )
                    continue

                # Reject if resolved path escapes root
                resolved_local = local.resolve()
                try:
                    resolved_local.relative_to(resolved_root)
                except ValueError:
                    errors.append(
                        f"upstream {u.id}: redistributed file escapes root: "
                        f"{src.redistributed_as}"
                    )
                    continue

                actual = hashlib.sha256(local.read_bytes()).hexdigest()
                if actual != src.sha256:
                    errors.append(
                        f"upstream {u.id}, {src.path}: hash drift — "
                        f"expected {src.sha256}, got {actual}"
                    )

            # Distributed upstreams with redistributed use must have local file
            if (
                u.distributed
                and src.use == "redistributed"
                and not src.redistributed_as
            ):
                errors.append(
                    f"upstream {u.id}: distributed license source "
                    f"{src.path} has no redistributed_as"
                )

            # Excluded sources must NOT have redistributed files
            if src.use == "excluded-clean-room-evidence" and src.redistributed_as:
                errors.append(
                    f"upstream {u.id}: excluded source {src.path} "
                    f"must not be redistributed"
                )

    # Check for duplicate redistributed_as paths
    if len(all_redistributed) != len(set(all_redistributed)):
        errors.append(f"duplicate redistributed_as paths: {all_redistributed}")

    return errors


# ── Online check (opt-in) ────────────────────────────────────────────────────

def _validate_final_url(url: str) -> Optional[str]:
    """Validate that the final response URL is safe.

    Returns error string if invalid, None if OK.
    """
    if type(url) is not str or not url:
        return f"final URL is missing or not a string: {url!r}"
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except (TypeError, ValueError) as exc:
        return f"final URL is malformed: {url!r} ({exc})"
    if parsed.scheme != "https":
        return f"final URL scheme is not https: {url}"
    if hostname != _RAW_GH_HOST:
        return f"redirect outside raw host to {url}"
    if port is not None:
        return f"final URL has explicit port: {url}"
    if username or password:
        return f"final URL has credentials: {url}"
    if parsed.query:
        return f"final URL has query string: {url}"
    if parsed.fragment:
        return f"final URL has fragment: {url}"
    return None


class _UnsafeRedirectError(Exception):
    """A redirect target violated the pinned raw-host policy."""


class _SafeRawRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect before urllib is allowed to follow it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        error = _validate_final_url(newurl)
        if error:
            raise _UnsafeRedirectError(error)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_online(manifest: ProvenanceManifest) -> List[str]:
    """Verify upstream source hashes against raw.githubusercontent.com.

    Only checks non-excluded, non-clean-room-evidence sources.
    Returns errors list; empty means all passed.
    Error strings contain 'network error' for network issues and
    other text for content/redirect mismatches, enabling CLI exit
    code classification.
    """
    errors: List[str] = []

    for u in manifest.upstreams:
        # Parse owner/repo from repository URL
        parsed_repo = urlparse(u.repository)
        parts = parsed_repo.path.rstrip("/").split("/")
        if len(parts) < 3:
            errors.append(f"upstream {u.id}: cannot parse owner/repo from URL")
            continue
        owner = parts[1]
        repo = parts[2]

        for src in u.sources:
            # Skip excluded clean-room evidence — never fetch
            if src.use == "excluded-clean-room-evidence":
                continue

            encoded_path = quote(src.path, safe="/-._~")
            url = f"{_RAW_GH}/{owner}/{repo}/{u.commit}/{encoded_path}"

            try:
                opener = urllib.request.build_opener(_SafeRawRedirectHandler)
                req = urllib.request.Request(url)
                with opener.open(req, timeout=30) as resp:
                    # Validate final URL
                    url_err = _validate_final_url(resp.url)
                    if url_err:
                        errors.append(
                            f"upstream {u.id}, {src.path}: {url_err}"
                        )
                        continue

                    data = resp.read()
                    actual = hashlib.sha256(data).hexdigest()
                    if actual != src.sha256:
                        errors.append(
                            f"upstream {u.id}, {src.path}: online hash mismatch — "
                            f"expected {src.sha256}, got {actual}"
                        )
            except _UnsafeRedirectError as exc:
                errors.append(
                    f"upstream {u.id}, {src.path}: unsafe redirect — {exc}"
                )
            except urllib.error.HTTPError as exc:
                if exc.code in _CONTENT_HTTP_CODES:
                    # 404/410 = pinned source absent → content mismatch
                    errors.append(
                        f"upstream {u.id}, {src.path}: "
                        f"pinned source absent (HTTP {exc.code})"
                    )
                else:
                    # 401/403/429/5xx = network/environment error
                    errors.append(
                        f"upstream {u.id}, {src.path}: "
                        f"network error — HTTP {exc.code}"
                    )
            except (urllib.error.URLError, OSError) as exc:
                errors.append(
                    f"upstream {u.id}, {src.path}: network error — {exc}"
                )

    return errors


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_upstreams",
        description="Verify koroche-blyat provenance manifest",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("UPSTREAMS.yml"),
        help="Path to UPSTREAMS.yml (default: UPSTREAMS.yml in cwd)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Project root for redistributed file checks (default: cwd)",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Also verify hashes against raw.githubusercontent.com",
    )
    args = parser.parse_args(argv)

    # Load manifest — schema/content errors → exit 1, IO/env → exit 2
    try:
        manifest = load_manifest(args.manifest)
    except (ValueError, yaml.YAMLError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (PermissionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        errors = check_offline(args.root, manifest)
    except (PermissionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.online:
        online_errors = check_online(manifest)
        if online_errors:
            net_errors = [e for e in online_errors if "network error" in e]
            content_errors = [e for e in online_errors if "network error" not in e]
            errors.extend(content_errors)
            if net_errors:
                # Print every diagnostic exactly once in deterministic category order.
                for e in errors:
                    print(f"FAIL: {e}", file=sys.stderr)
                for e in net_errors:
                    print(f"NETWORK: {e}", file=sys.stderr)
                # Network error dominates — exit 2.
                return 2

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1

    print("OK: all provenance checks passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
