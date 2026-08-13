"""Hardening tests for check_upstreams.py — Task 4.

TDD RED: all tests here cover flaws enumerated in task-4-fix-brief.md.
Tests are deterministic, use no network, and import the project module directly.
"""
from __future__ import annotations

import hashlib
import os
import re
import textwrap
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch
import tempfile

import pytest
import yaml

# Import the module under test directly (project interpreter, no shell uv)
from scripts.check_upstreams import (
    load_manifest,
    check_offline,
    check_online,
    main,
    ProvenanceManifest,
    SourceEntry,
    Upstream,
)

ROOT = Path(__file__).resolve().parent.parent

# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write YAML string to a temp file and return the path."""
    p = tmp_path / "UPSTREAMS.yml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_VALID_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_VALID_SHA256 = "a" * 64

def _minimal_yaml(overrides: str = "") -> str:
    """Minimal valid YAML for a single upstream."""
    base = f"""\
        schema_version: 1
        upstreams:
          - id: test
            repository: https://github.com/owner/repo
            tag: null
            commit: {_VALID_COMMIT}
            license: MIT
            distributed: false
            sources:
              - path: README.md
                sha256: {_VALID_SHA256}
                use: adapted
    """
    return base if not overrides else overrides


# ══════════════════════════════════════════════════════════════════════════════
# 1. CLI exit codes: schema/content mismatch → 1, network/usage → 2
# ══════════════════════════════════════════════════════════════════════════════

class TestCLIExitCodes:
    """CLI must return exit 1 for schema/content errors, exit 2 only for network/usage."""

    def test_schema_version_mismatch_exit_1(self, tmp_path):
        """Unsupported schema_version is a schema error → exit 1, not 2."""
        p = _write_yaml(tmp_path, """\
            schema_version: 99
            upstreams:
              - id: x
                repository: https://github.com/a/b
                commit: 0123456789abcdef0123456789abcdef01234567
                license: MIT
                distributed: false
                sources: []
        """)
        rc = main(["--manifest", str(p), "--root", str(tmp_path)])
        assert rc == 1, f"schema_version mismatch must exit 1, got {rc}"

    def test_content_mismatch_exit_1_not_2(self, tmp_path):
        """Missing required field is schema/content error → exit 1."""
        p = _write_yaml(tmp_path, """\
            schema_version: 1
            upstreams:
              - id: x
                repository: https://github.com/a/b
                commit: bad
                license: MIT
                distributed: false
                sources: []
        """)
        rc = main(["--manifest", str(p), "--root", str(tmp_path)])
        assert rc == 1, f"content error must exit 1, got {rc}"

    def test_missing_manifest_exit_1(self, tmp_path):
        """Missing manifest file is a content error → exit 1."""
        rc = main(["--manifest", str(tmp_path / "nonexistent.yml")])
        assert rc == 1, f"missing manifest must exit 1, got {rc}"

    def test_network_error_dominates_exit_2(self, tmp_path):
        """When both network and content errors exist, network dominates → exit 2."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        # Mock check_online to return both network and content errors
        with patch("scripts.check_upstreams.check_online") as mock_online:
            mock_online.return_value = [
                "upstream test, README.md: online hash mismatch — expected aaa, got bbb",
                "upstream test, README.md: network error — ConnectionError",
            ]
            rc = main(["--manifest", str(p), "--root", str(tmp_path), "--online"])
            assert rc == 2, f"network error must dominate → exit 2, got {rc}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. YAML aliases/anchors rejection (pre-parse)
# ══════════════════════════════════════════════════════════════════════════════

class TestYAMLAliasRejection:
    """YAML anchors, aliases, and merge keys must be rejected before parse."""

    def test_reject_yaml_anchor(self, tmp_path):
        p = _write_yaml(tmp_path, """\
            schema_version: 1
            defaults: &defaults
              license: MIT
              distributed: false
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                commit: 0123456789abcdef0123456789abcdef01234567
                <<: *defaults
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)alias|anchor|merge"):
            load_manifest(p)

    def test_reject_yaml_alias_star(self, tmp_path):
        p = _write_yaml(tmp_path, """\
            schema_version: 1
            base: &base
              license: MIT
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                commit: 0123456789abcdef0123456789abcdef01234567
                license: *base
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)alias|anchor|merge"):
            load_manifest(p)

    def test_reject_merge_key(self, tmp_path):
        """Merge key <<: must be rejected."""
        p = _write_yaml(tmp_path, """\
            schema_version: 1
            x: &x
              license: MIT
            upstreams:
              - <<: *x
                id: merged
                repository: https://github.com/owner/repo
                commit: 0123456789abcdef0123456789abcdef01234567
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)alias|anchor|merge"):
            load_manifest(p)

    def test_allow_star_in_url_text(self, tmp_path):
        """URLs with * in text values must NOT trigger false rejection."""
        # This tests that we don't naively reject * in strings
        p = _write_yaml(tmp_path, """\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: 0123456789abcdef0123456789abcdef01234567
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
                    use: adapted
        """)
        # Should NOT raise — no actual YAML anchors/aliases here
        m = load_manifest(p)
        assert m.schema_version == 1


# ══════════════════════════════════════════════════════════════════════════════
# 3. Strict schema validation: required fields, types, unknown fields
# ══════════════════════════════════════════════════════════════════════════════

class TestStrictSchemaValidation:
    """Reject missing required fields, wrong types, unknown fields."""

    def test_reject_missing_license_field(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)license"):
            load_manifest(p)

    def test_reject_missing_sha256_in_source(self, tmp_path):
        """Source entry without sha256 must be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: file.txt
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)sha256"):
            load_manifest(p)

    def test_reject_distributed_string_false(self, tmp_path):
        """distributed: 'false' (string) must NOT become True via bool()."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: "false"
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)distributed|bool"):
            load_manifest(p)

    def test_reject_unknown_upstream_fields(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                extra_field: surprise
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)unknown.*field"):
            load_manifest(p)

    def test_reject_unknown_source_fields(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
                    bonus: hack
        """)
        with pytest.raises(ValueError, match="(?i)unknown.*field"):
            load_manifest(p)

    def test_tag_null_accepted(self, tmp_path):
        """tag: null is valid."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        m = load_manifest(p)
        assert m.upstreams[0].tag is None

    def test_tag_string_accepted(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: v1.0.0
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        m = load_manifest(p)
        assert m.upstreams[0].tag == "v1.0.0"

    def test_reject_tag_integer(self, tmp_path):
        """tag must be null or string, not integer."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: 42
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)tag"):
            load_manifest(p)

    def test_reject_duplicate_upstream_ids(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: dup
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
              - id: dup
                repository: https://github.com/owner/repo2
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)duplicate.*id"):
            load_manifest(p)

    def test_reject_malformed_sha256(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: ZZZZ0000000000000000000000000000000000000000000000000000000000ZZ
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)sha256"):
            load_manifest(p)

    def test_reject_empty_sha256(self, tmp_path):
        """Empty sha256 string must be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: ""
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)sha256"):
            load_manifest(p)

    def test_reject_unknown_root_fields(self, tmp_path):
        """Unknown root-level keys must be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            version: 2
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)unknown.*field"):
            load_manifest(p)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Path safety: repository URL, source paths
# ══════════════════════════════════════════════════════════════════════════════

class TestPathSafety:
    """Repository URLs and source paths must be safe."""

    def test_reject_repo_with_query(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo?ref=evil
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)repo"):
            load_manifest(p)

    def test_reject_repo_with_fragment(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo#readme
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)repo"):
            load_manifest(p)

    def test_reject_repo_with_userinfo(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://user:pass@github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)repo"):
            load_manifest(p)

    def test_reject_repo_with_extra_path_segments(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo/tree/main
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)repo"):
            load_manifest(p)

    def test_reject_repo_owner_with_dots(self, tmp_path):
        """Owner/repo segments must be safe (no path traversal tricks)."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/../etc
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)repo|segment|URL"):
            load_manifest(p)

    def test_reject_source_path_dotdot(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: ../../../etc/passwd
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)

    def test_reject_source_path_backslash(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: dir\\file.txt
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)

    def test_reject_source_path_absolute(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: /etc/passwd
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)

    def test_reject_source_path_empty_segment(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: dir//file.txt
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)

    def test_reject_source_path_dot_segment(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: ./file.txt
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)

    def test_reject_redistributed_as_traversal(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: true
                sources:
                  - path: LICENSE
                    sha256: {_VALID_SHA256}
                    use: redistributed
                    redistributed_as: ../../../evil.txt
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)

    def test_reject_source_path_with_control_chars(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: "file\\x00.txt"
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Offline: symlink and traversal checks
# ══════════════════════════════════════════════════════════════════════════════

class TestOfflineSymlinkTraversal:
    """check_offline must reject symlink files and paths escaping root."""

    def test_reject_symlink_redistributed_file(self, tmp_path):
        """Symlinked redistributed file must be rejected."""
        # Create a real file outside root
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.txt").write_bytes(b"evil")

        root = tmp_path / "project"
        root.mkdir()
        lic_dir = root / "skills" / "koroche-blyat" / "licenses"
        lic_dir.mkdir(parents=True)
        # Create symlink
        (lic_dir / "caveman-MIT.txt").symlink_to(outside / "evil.txt")

        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=True,
                sources=[SourceEntry(
                    path="LICENSE",
                    sha256=hashlib.sha256(b"evil").hexdigest(),
                    use="redistributed",
                    redistributed_as="skills/koroche-blyat/licenses/caveman-MIT.txt",
                )],
            )],
        )
        errors = check_offline(root, manifest)
        assert any("symlink" in e.lower() for e in errors), \
            f"must reject symlink, got: {errors}"

    def test_reject_path_escaping_root(self, tmp_path):
        """Resolved path escaping root must be rejected."""
        root = tmp_path / "project"
        root.mkdir()

        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=True,
                sources=[SourceEntry(
                    path="LICENSE",
                    sha256=_VALID_SHA256,
                    use="redistributed",
                    redistributed_as="../outside.txt",
                )],
            )],
        )
        errors = check_offline(root, manifest)
        assert len(errors) > 0, "must reject path escaping root"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Online: redirect and URL validation
# ══════════════════════════════════════════════════════════════════════════════

class TestOnlineRedirectSafety:
    """check_online must refuse malicious redirects and validate final URL."""

    @staticmethod
    def _make_mock_opener(mock_resp):
        """Create a mock opener whose open() returns mock_resp as context manager."""
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        return mock_opener

    def test_reject_evil_hostname_prefix(self):
        """raw.githubusercontent.com.evil must be rejected (not a prefix match)."""
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        mock_resp = MagicMock()
        mock_resp.url = "https://raw.githubusercontent.com.evil/owner/repo/main/README.md"
        mock_resp.read.return_value = b"x" * 100
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.build_opener", return_value=self._make_mock_opener(mock_resp)):
            errors = check_online(manifest)
            assert any("redirect" in e.lower() or "host" in e.lower() for e in errors), \
                f"must reject evil hostname prefix, got: {errors}"

    def test_reject_redirect_to_non_github(self):
        """Redirect to non-raw.githubusercontent.com must be refused."""
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        mock_resp = MagicMock()
        mock_resp.url = "https://evil.com/payload"
        mock_resp.read.return_value = b"x"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.build_opener", return_value=self._make_mock_opener(mock_resp)):
            errors = check_online(manifest)
            assert any("redirect" in e.lower() or "host" in e.lower() for e in errors), \
                f"must reject non-github redirect, got: {errors}"

    def test_online_skip_excluded_sources(self):
        """Excluded clean-room sources must NOT be fetched."""
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="excl",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="NOASSERTION",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="excluded-clean-room-evidence",
                )],
            )],
        )
        with patch("urllib.request.build_opener") as mock_builder:
            errors = check_online(manifest)
            mock_builder.assert_not_called()
            assert errors == []

    def test_online_hash_mismatch_returns_error(self):
        """Online hash mismatch must return content error, not network error."""
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        mock_resp = MagicMock()
        mock_resp.url = "https://raw.githubusercontent.com/owner/repo/" + _VALID_COMMIT + "/README.md"
        mock_resp.read.return_value = b"different content"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.build_opener", return_value=self._make_mock_opener(mock_resp)):
            errors = check_online(manifest)
            assert len(errors) == 1
            assert "mismatch" in errors[0].lower()
            assert "network" not in errors[0].lower()

    def test_online_network_error_returns_network_error(self):
        """Network failure must be distinguishable from content mismatch."""
        import urllib.error
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("timeout")

        with patch("urllib.request.build_opener", return_value=mock_opener):
            errors = check_online(manifest)
            assert len(errors) == 1
            assert "network error" in errors[0].lower()

    def test_online_commit_url_uses_40_char(self):
        """URLs built by check_online must use the full 40-char commit."""
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=hashlib.sha256(b"ok").hexdigest(),
                    use="adapted",
                )],
            )],
        )
        mock_resp = MagicMock()
        mock_resp.url = f"https://raw.githubusercontent.com/owner/repo/{_VALID_COMMIT}/README.md"
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        mock_opener = self._make_mock_opener(mock_resp)
        with patch("urllib.request.build_opener", return_value=mock_opener):
            check_online(manifest)
            call_args = mock_opener.open.call_args
            req = call_args[0][0]
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            assert _VALID_COMMIT in url


# ══════════════════════════════════════════════════════════════════════════════
# 7. Offline: clean-room never fetched (covered in online tests above)
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# 8-9. CLI exit code tests (no subprocess, direct main()) and mutation tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCLIMainDirect:
    """Call main() directly — no subprocess, no network."""

    def test_exit_0_valid_offline(self):
        rc = main(["--manifest", str(ROOT / "UPSTREAMS.yml"), "--root", str(ROOT)])
        assert rc == 0

    def test_exit_1_bad_commit(self, tmp_path):
        p = _write_yaml(tmp_path, """\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                commit: short
                license: MIT
                distributed: false
                sources: []
        """)
        rc = main(["--manifest", str(p), "--root", str(tmp_path)])
        assert rc == 1

    def test_exit_1_missing_hash_redistributed(self, tmp_path):
        """Redistributed file missing from disk → exit 1."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: true
                sources:
                  - path: LICENSE
                    sha256: {_VALID_SHA256}
                    use: redistributed
                    redistributed_as: licenses/test-MIT.txt
        """)
        rc = main(["--manifest", str(p), "--root", str(tmp_path)])
        assert rc == 1

    def test_exit_1_online_mismatch(self, tmp_path):
        """Online hash mismatch (no network error) → exit 1."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        with patch("scripts.check_upstreams.check_online") as mock_online:
            mock_online.return_value = [
                "upstream test, README.md: online hash mismatch — expected aaa, got bbb",
            ]
            rc = main(["--manifest", str(p), "--root", str(tmp_path), "--online"])
            assert rc == 1

    def test_exit_1_missing_manifest(self, tmp_path):
        """Missing manifest is content/schema error → exit 1."""
        rc = main(["--manifest", str(tmp_path / "nope.yml")])
        assert rc == 1

    def test_exit_2_network_error_online(self, tmp_path):
        """Network error during online check → exit 2."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        with patch("scripts.check_upstreams.check_online") as mock_online:
            mock_online.return_value = [
                "upstream test, README.md: network error — ConnectionError",
            ]
            rc = main(["--manifest", str(p), "--root", str(tmp_path), "--online"])
            assert rc == 2


# ══════════════════════════════════════════════════════════════════════════════
# 10. check_online typed results (covered by tests 6 above)
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# 11. Root MIT license: exact standard text
# ══════════════════════════════════════════════════════════════════════════════

class TestRootLicenseExactText:
    """Root LICENSE must contain exact MIT standard clauses."""

    def test_mit_permission_clause(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "Permission is hereby granted, free of charge" in text

    def test_mit_conditions_clause(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "The above copyright notice and this permission notice" in text
        assert "shall be included in all" in text

    def test_mit_warranty_disclaimer(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text

    def test_mit_no_liability_clause(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "IN NO EVENT SHALL" in text

    def test_skill_license_bytes_identical(self):
        root_bytes = (ROOT / "LICENSE").read_bytes()
        skill_bytes = (ROOT / "skills" / "koroche-blyat" / "LICENSE.txt").read_bytes()
        assert root_bytes == skill_bytes


class TestNoticeExactSemantics:
    """NOTICE.md must have exact semantic clauses."""

    def test_notice_states_no_affiliation(self):
        text = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
        assert "not affiliated" in text.lower() or "no affiliation" in text.lower()

    def test_notice_clean_room_mention(self):
        text = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
        assert "clean-room" in text.lower() or "clean room" in text.lower()

    def test_notice_both_upstream_authors(self):
        text = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
        assert "Julius Brussee" in text
        assert "Serge Shima" in text


# ══════════════════════════════════════════════════════════════════════════════
# Round 2 hardening tests (adversarial pass A–G)
# ══════════════════════════════════════════════════════════════════════════════


# ── A. YAML alias scanner: no false positives on quoted/comment ──────────────

class TestYAMLAliasScannerV2:
    """Token-level alias/anchor rejection must not false-positive on comments/quotes."""

    def test_allow_anchor_in_comment(self, tmp_path):
        """# &anchor in a comment must NOT trigger rejection."""
        p = _write_yaml(tmp_path, f"""\
            # &anchor is just a comment
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        m = load_manifest(p)
        assert m.schema_version == 1

    def test_allow_anchor_in_quoted_string(self, tmp_path):
        """'&anchor' in a quoted scalar must NOT trigger rejection."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: "&not-an-anchor"
                commit: {_VALID_COMMIT}
                license: "MIT &special"
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        m = load_manifest(p)
        assert m.upstreams[0].tag == "&not-an-anchor"

    def test_allow_alias_in_quoted_string(self, tmp_path):
        """'*alias' in a quoted scalar must NOT trigger rejection."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: "*not-alias"
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        m = load_manifest(p)
        assert m.upstreams[0].tag == "*not-alias"

    def test_reject_real_anchor_token(self, tmp_path):
        """Actual YAML anchor token &x must still be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            defaults: &defaults
              license: MIT
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)anchor|alias|merge"):
            load_manifest(p)

    def test_reject_real_alias_token(self, tmp_path):
        """Actual YAML alias token *x must still be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            base: &base
              license: MIT
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: *base
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)anchor|alias|merge"):
            load_manifest(p)

    def test_reject_merge_key_token(self, tmp_path):
        """Merge key <<: *x must still be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            x: &x
              license: MIT
            upstreams:
              - <<: *x
                id: merged
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)anchor|alias|merge"):
            load_manifest(p)


# ── B. HTTP status code classification ───────────────────────────────────────

class TestOnlineHTTPStatusCodes:
    """HTTP 404/410 → content mismatch (exit 1). 401/403/429/5xx → network (exit 2)."""

    @staticmethod
    def _single_source_manifest():
        return ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )

    def _run_with_http_error(self, code):
        """Mock opener to raise HTTPError with given code."""
        import urllib.error
        manifest = self._single_source_manifest()
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.HTTPError(
            url="https://raw.githubusercontent.com/owner/repo/abc/README.md",
            code=code,
            msg=f"HTTP {code}",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.build_opener", return_value=mock_opener):
            return check_online(manifest)

    def test_404_is_content_mismatch(self):
        errors = self._run_with_http_error(404)
        assert len(errors) == 1
        assert "network error" not in errors[0].lower()

    def test_410_is_content_mismatch(self):
        errors = self._run_with_http_error(410)
        assert len(errors) == 1
        assert "network error" not in errors[0].lower()

    def test_401_is_network_error(self):
        errors = self._run_with_http_error(401)
        assert len(errors) == 1
        assert "network error" in errors[0].lower()

    def test_403_is_network_error(self):
        errors = self._run_with_http_error(403)
        assert len(errors) == 1
        assert "network error" in errors[0].lower()

    def test_429_is_network_error(self):
        errors = self._run_with_http_error(429)
        assert len(errors) == 1
        assert "network error" in errors[0].lower()

    def test_500_is_network_error(self):
        errors = self._run_with_http_error(500)
        assert len(errors) == 1
        assert "network error" in errors[0].lower()

    def test_503_is_network_error(self):
        errors = self._run_with_http_error(503)
        assert len(errors) == 1
        assert "network error" in errors[0].lower()

    def test_404_cli_exit_1(self, tmp_path):
        """HTTP 404 → CLI exit 1."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        with patch("scripts.check_upstreams.check_online") as mock_online:
            mock_online.return_value = [
                "upstream test, README.md: pinned source absent (HTTP 404)",
            ]
            rc = main(["--manifest", str(p), "--root", str(tmp_path), "--online"])
            assert rc == 1

    def test_500_cli_exit_2(self, tmp_path):
        """HTTP 500 network error → CLI exit 2."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        with patch("scripts.check_upstreams.check_online") as mock_online:
            mock_online.return_value = [
                "upstream test, README.md: network error — HTTP 500",
            ]
            rc = main(["--manifest", str(p), "--root", str(tmp_path), "--online"])
            assert rc == 2


# ── C. Unsafe redirect → content/security exit 1, not network 2 ─────────────

class TestRedirectExitCodes:
    """Redirect to unsafe host is a security/content error, not network."""

    def test_unsafe_redirect_error_is_not_network(self):
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        mock_resp = MagicMock()
        mock_resp.url = "https://evil.com/payload"
        mock_resp.read.return_value = b"x"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp

        with patch("urllib.request.build_opener", return_value=mock_opener):
            errors = check_online(manifest)
            assert len(errors) >= 1
            # Must NOT contain "network error" — it's a security/content issue
            for e in errors:
                assert "network error" not in e.lower(), \
                    f"redirect error must not be network error: {e}"

    def test_unsafe_redirect_cli_exit_1(self, tmp_path):
        """Unsafe redirect → CLI exit 1 (content/security), not 2."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        with patch("scripts.check_upstreams.check_online") as mock_online:
            mock_online.return_value = [
                "upstream test, README.md: redirect outside raw host to https://evil.com",
            ]
            rc = main(["--manifest", str(p), "--root", str(tmp_path), "--online"])
            assert rc == 1, f"unsafe redirect must exit 1, got {rc}"


# ── D. Strict schema exact types/presence ────────────────────────────────────

class TestStrictSchemaTypesV2:
    """Exact type enforcement: schema_version int not bool, all fields required."""

    def test_schema_version_bool_true_rejected(self, tmp_path):
        """schema_version: true (bool) must be rejected even though bool is int subclass."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: true
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)schema_version"):
            load_manifest(p)

    def test_upstream_requires_all_7_fields(self, tmp_path):
        """All 7 upstream fields must be present (id, repository, tag, commit, license, distributed, sources)."""
        # Missing tag
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)tag.*required|required.*tag|missing.*tag"):
            load_manifest(p)

    def test_upstream_requires_sources_field(self, tmp_path):
        """sources field must be explicitly present."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
        """)
        with pytest.raises(ValueError, match="(?i)sources.*required|required.*sources|missing.*sources"):
            load_manifest(p)

    def test_commit_must_be_string_not_int(self, tmp_path):
        """commit value that is an int must be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: 12345
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError):
            load_manifest(p)

    def test_unknown_upstream_fields_sorted_message(self, tmp_path):
        """Unknown fields diagnostic must be deterministic (sorted)."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
                zzz_extra: 1
                aaa_extra: 2
        """)
        with pytest.raises(ValueError) as exc_info:
            load_manifest(p)
        msg = str(exc_info.value)
        # aaa must appear before zzz in the diagnostic
        assert msg.index("aaa_extra") < msg.index("zzz_extra"), \
            f"unknown fields must be sorted: {msg}"

    def test_source_path_must_be_string(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: 42
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)path"):
            load_manifest(p)

    def test_source_use_must_be_string(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: true
        """)
        with pytest.raises(ValueError):
            load_manifest(p)


# ── E. Sources non-empty, redistributed_as iff redistributed, etc. ───────────

class TestSourceConsistencyV2:
    """Enforce source list/consistency invariants."""

    def test_sources_must_be_non_empty(self, tmp_path):
        """sources list must contain at least one entry."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources: []
        """)
        with pytest.raises(ValueError, match="(?i)source.*non-empty|empty.*source"):
            load_manifest(p)

    def test_redistributed_as_requires_use_redistributed(self, tmp_path):
        """redistributed_as only valid with use=redistributed."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: adapted
                    redistributed_as: copy.md
        """)
        with pytest.raises(ValueError, match="(?i)redistributed"):
            load_manifest(p)

    def test_use_redistributed_requires_redistributed_as(self, tmp_path):
        """use=redistributed without redistributed_as must be rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: true
                sources:
                  - path: LICENSE
                    sha256: {_VALID_SHA256}
                    use: redistributed
        """)
        with pytest.raises(ValueError, match="(?i)redistributed.*requires|requires.*redistributed"):
            load_manifest(p)

    def test_excluded_upstream_must_not_be_distributed(self, tmp_path):
        """Upstream with only excluded-clean-room sources and distributed=true rejected."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: NOASSERTION
                distributed: true
                sources:
                  - path: README.md
                    sha256: {_VALID_SHA256}
                    use: excluded-clean-room-evidence
        """)
        with pytest.raises(ValueError, match="(?i)distribut|excluded"):
            load_manifest(p)

    def test_distributed_upstream_must_have_redistributed_source(self, tmp_path):
        """A distributed upstream must include at least one redistributed source (license evidence)."""
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: true
                sources:
                  - path: SKILL.md
                    sha256: {_VALID_SHA256}
                    use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)distribut.*redistributed|license.*evidence"):
            load_manifest(p)


# ── F. _validate_final_url rejects query/fragment; redirect handler validates ─

class TestFinalURLValidationV2:
    """Final URL must reject query/fragment; redirect handler validates before follow."""

    def test_reject_final_url_with_query(self):
        from scripts.check_upstreams import _validate_final_url
        err = _validate_final_url("https://raw.githubusercontent.com/o/r/c/f?token=abc")
        assert err is not None and "query" in err.lower()

    def test_reject_final_url_with_fragment(self):
        from scripts.check_upstreams import _validate_final_url
        err = _validate_final_url("https://raw.githubusercontent.com/o/r/c/f#section")
        assert err is not None and "fragment" in err.lower()

    def test_accept_clean_final_url(self):
        from scripts.check_upstreams import _validate_final_url
        err = _validate_final_url(f"https://raw.githubusercontent.com/o/r/{_VALID_COMMIT}/f")
        assert err is None


# ── G. check_offline root validation ─────────────────────────────────────────

class TestOfflineRootValidation:
    """check_offline must validate root directory itself."""

    def test_reject_missing_root(self, tmp_path):
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        errors = check_offline(tmp_path / "nonexistent", manifest)
        assert len(errors) > 0
        assert any("root" in e.lower() for e in errors)

    def test_reject_root_is_file(self, tmp_path):
        fake_root = tmp_path / "not_a_dir"
        fake_root.write_text("I am a file")
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        errors = check_offline(fake_root, manifest)
        assert len(errors) > 0
        assert any("root" in e.lower() for e in errors)

    def test_reject_root_is_symlink(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        manifest = ProvenanceManifest(
            schema_version=1,
            upstreams=[Upstream(
                id="test",
                repository="https://github.com/owner/repo",
                tag=None,
                commit=_VALID_COMMIT,
                license="MIT",
                distributed=False,
                sources=[SourceEntry(
                    path="README.md",
                    sha256=_VALID_SHA256,
                    use="adapted",
                )],
            )],
        )
        errors = check_offline(link, manifest)
        assert len(errors) > 0
        assert any("root" in e.lower() or "symlink" in e.lower() for e in errors)

    def test_root_permission_error_cli_exit_2(self, tmp_path):
        """Permission error reading root → CLI exit 2 (IO environment)."""
        p = _write_yaml(tmp_path, _minimal_yaml())
        # Simulate by passing a root that triggers an OSError in check_offline
        # We'll mock check_offline to raise OSError
        with patch("scripts.check_upstreams.check_offline", side_effect=PermissionError("denied")):
            rc = main(["--manifest", str(p), "--root", str(tmp_path)])
            assert rc == 2, f"permission error must exit 2, got {rc}"


# ══════════════════════════════════════════════════════════════════════════════
# Round 3: token, type, redirect-target, mixed diagnostics, and URL encoding
# ══════════════════════════════════════════════════════════════════════════════

class TestRound3Hardening:
    def test_reject_plain_merge_key_without_anchor_or_alias(self, tmp_path):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - <<:
                  id: test
                  repository: https://github.com/owner/repo
                  tag: null
                  commit: {_VALID_COMMIT}
                  license: MIT
                  distributed: false
                  sources:
                    - path: README.md
                      sha256: {_VALID_SHA256}
                      use: adapted
        """)
        with pytest.raises(ValueError, match="(?i)merge"):
            load_manifest(p)

    def test_allow_quoted_merge_literal_as_unknown_field(self, tmp_path):
        p = _write_yaml(tmp_path, _minimal_yaml().replace(
            "            sources:", '            "<<": literal\n            sources:'
        ))
        with pytest.raises(ValueError, match="unknown upstream fields"):
            load_manifest(p)

    @pytest.mark.parametrize("field,value", [
        ("commit", "1234567890123456789012345678901234567890"),
        ("repository", "123"),
        ("id", "123"),
        ("license", "123"),
    ])
    def test_reject_wrong_scalar_types_without_typeerror(self, tmp_path, field, value):
        text = _minimal_yaml()
        if field == "commit":
            text = text.replace(f"commit: {_VALID_COMMIT}", f"commit: {value}")
        elif field == "repository":
            text = text.replace("repository: https://github.com/owner/repo", f"repository: {value}")
        elif field == "id":
            text = text.replace("id: test", f"id: {value}")
        else:
            text = text.replace("license: MIT", f"license: {value}")
        p = _write_yaml(tmp_path, text)
        with pytest.raises(ValueError):
            load_manifest(p)

    def test_reject_numeric_64_digit_sha_without_typeerror(self, tmp_path):
        p = _write_yaml(tmp_path, _minimal_yaml().replace(
            f"sha256: {_VALID_SHA256}", "sha256: " + "1" * 64
        ))
        with pytest.raises(ValueError, match="sha256"):
            load_manifest(p)

    def test_reject_list_use_as_valueerror(self, tmp_path):
        p = _write_yaml(tmp_path, _minimal_yaml().replace("use: adapted", "use: [adapted]"))
        with pytest.raises(ValueError, match="use"):
            load_manifest(p)

    @pytest.mark.parametrize("target", [
        "http://raw.githubusercontent.com/o/r/c/f",
        "https://user@raw.githubusercontent.com/o/r/c/f",
        "https://raw.githubusercontent.com:444/o/r/c/f",
        "https://raw.githubusercontent.com/o/r/c/f?x=1",
        "https://raw.githubusercontent.com/o/r/c/f#frag",
        "https://raw.githubusercontent.com.evil/o/r/c/f",
    ])
    def test_redirect_handler_rejects_unsafe_target_before_follow(self, target):
        import urllib.error
        import urllib.request
        import scripts.check_upstreams as mod
        handler = mod._SafeRawRedirectHandler()
        req = urllib.request.Request("https://raw.githubusercontent.com/o/r/c/f")
        with pytest.raises(mod._UnsafeRedirectError):
            handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_mixed_network_failure_prints_offline_content_and_network_once(self, tmp_path, capsys):
        p = _write_yaml(tmp_path, f"""\
            schema_version: 1
            upstreams:
              - id: test
                repository: https://github.com/owner/repo
                tag: null
                commit: {_VALID_COMMIT}
                license: MIT
                distributed: true
                sources:
                  - path: LICENSE
                    sha256: {_VALID_SHA256}
                    use: redistributed
                    redistributed_as: licenses/missing.txt
        """)
        with patch("scripts.check_upstreams.check_online", return_value=[
            "upstream test, LICENSE: online hash mismatch",
            "upstream test, LICENSE: network error — timeout",
        ]):
            assert main(["--manifest", str(p), "--root", str(tmp_path), "--online"]) == 2
        err = capsys.readouterr().err
        assert err.count("redistributed file missing") == 1
        assert err.count("online hash mismatch") == 1
        assert err.count("network error") == 1

    def test_online_url_percent_encodes_source_path(self):
        import scripts.check_upstreams as mod
        src = SourceEntry(
            path="dir/file name-%-ж.md",
            sha256=hashlib.sha256(b"ok").hexdigest(),
            use="adapted",
        )
        manifest = ProvenanceManifest(1, [Upstream(
            id="test", repository="https://github.com/owner/repo", tag=None,
            commit=_VALID_COMMIT, license="MIT", distributed=False, sources=[src],
        )])
        response = MagicMock()
        response.url = (
            "https://raw.githubusercontent.com/owner/repo/" + _VALID_COMMIT +
            "/dir/file%20name-%25-%D0%B6.md"
        )
        response.read.return_value = b"ok"
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            assert check_online(manifest) == []
        request = opener.open.call_args.args[0]
        assert request.full_url == response.url


class TestRound3MappingStrictness:
    @pytest.mark.parametrize("yaml_text", [
        "schema_version: 1\n1: bad\nupstreams: []\n",
        "schema_version: 1\ntrue: bad\nupstreams: []\n",
    ])
    def test_non_string_mapping_keys_are_valueerror(self, tmp_path, yaml_text):
        p = _write_yaml(tmp_path, yaml_text)
        with pytest.raises(ValueError, match="mapping keys must be strings"):
            load_manifest(p)

    def test_duplicate_yaml_mapping_key_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, _minimal_yaml().replace(
            "            license: MIT", "            license: MIT\n            license: Apache-2.0"
        ))
        with pytest.raises(ValueError, match="duplicate YAML key"):
            load_manifest(p)


@pytest.mark.parametrize("bad_url", [
    "https://raw.githubusercontent.com:bad/o/r/c/f",
    "https://[broken/o/r/c/f",
    None,
])
def test_malformed_final_url_returns_content_error_not_exception(bad_url):
    import scripts.check_upstreams as mod
    error = mod._validate_final_url(bad_url)
    assert error is not None


def test_check_online_classifies_redirect_handler_rejection_as_content():
    import scripts.check_upstreams as mod
    manifest = ProvenanceManifest(1, [Upstream(
        id="test", repository="https://github.com/owner/repo", tag=None,
        commit=_VALID_COMMIT, license="MIT", distributed=False,
        sources=[SourceEntry("README.md", _VALID_SHA256, "adapted")],
    )])
    opener = MagicMock()
    opener.open.side_effect = mod._UnsafeRedirectError("forbidden target")
    with patch("urllib.request.build_opener", return_value=opener):
        errors = check_online(manifest)
    assert len(errors) == 1
    assert "unsafe redirect" in errors[0]
    assert "network error" not in errors[0]
