"""Provenance, licensing, and release identity tests for koroche-blyat 1.0.0."""
import hashlib
import os
import re
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

# ── Constants from the approved plan ──────────────────────────────────────────

REPO_URL = "https://github.com/maksimryzhov614/koroche-blyat"
RELEASE = "1.0.0"
COPYRIGHT_LINE = "Copyright (c) 2026 Koroche Blyat contributors"

CAVEMAN_COMMIT = "099327780ef69ad88c4cfc15c54314579ac367a4"
POHUY_COMMIT = "cac2698fae1260347d3d8c7efbc1bee98e041f6d"
RUSSIAN_SWEARS_COMMIT = "5be4828435629f9e5f966cde5b54d2eb2a5ba7e7"

CAVEMAN_LICENSE_SHA = (
    "1cd9aa70ec104afb3b0d2dc2e5343230f74737dc01fdc8dad585c9da6449d5a5"
)
POHUY_LICENSE_SHA = (
    "27cd410525efac04b5fc0706333cbf92fcc7cefc246d5be33a3e1c77ace71205"
)
CAVEMAN_SKILL_SHA = (
    "daf9cec496ebd039809d8236f99f17fa1b4beaadf8ce4e2d532d0da51d70afce"
)
POHUY_SKILL_SHA = (
    "1ca42e7d65251c331eb2bb30bad744306b9b85fac34db05e96daf4ba024f1663"
)
RUSSIAN_SWEARS_README_SHA = (
    "ed5e474e94dfdd8a37626e0d030988f049fbfff0de1e1aa7190bd6db5c66628f"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── VERSION ───────────────────────────────────────────────────────────────────

class TestVersion:
    def test_version_file_is_exactly_release_with_newline(self):
        vf = ROOT / "VERSION"
        assert vf.exists(), "VERSION file missing"
        raw = vf.read_bytes()
        assert raw == b"1.0.0\n", f"VERSION must be exactly '1.0.0\\n', got {raw!r}"


# ── Root LICENSE ──────────────────────────────────────────────────────────────

class TestRootLicense:
    def test_root_license_exists(self):
        assert (ROOT / "LICENSE").is_file()

    def test_root_license_is_mit(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "MIT License" in text or "Permission is hereby granted" in text

    def test_root_license_has_correct_copyright(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert COPYRIGHT_LINE in text


# ── Skill LICENSE.txt ─────────────────────────────────────────────────────────

class TestSkillLicense:
    SKILL_LIC = ROOT / "skills" / "koroche-blyat" / "LICENSE.txt"

    def test_skill_license_exists(self):
        assert self.SKILL_LIC.is_file()

    def test_skill_license_is_byte_identical_to_root(self):
        root_bytes = (ROOT / "LICENSE").read_bytes()
        skill_bytes = self.SKILL_LIC.read_bytes()
        assert root_bytes == skill_bytes, (
            "skills/koroche-blyat/LICENSE.txt must be byte-identical to root LICENSE"
        )


# ── Redistributed upstream licenses ──────────────────────────────────────────

class TestRedistributedLicenses:
    LICENSES_DIR = ROOT / "skills" / "koroche-blyat" / "licenses"

    def test_caveman_license_exists(self):
        assert (self.LICENSES_DIR / "caveman-MIT.txt").is_file()

    def test_pohuy_license_exists(self):
        assert (self.LICENSES_DIR / "pohuy-MIT.txt").is_file()

    def test_caveman_license_hash_matches_pinned(self):
        h = _sha256(self.LICENSES_DIR / "caveman-MIT.txt")
        assert h == CAVEMAN_LICENSE_SHA, f"caveman hash drift: {h}"

    def test_pohuy_license_hash_matches_pinned(self):
        h = _sha256(self.LICENSES_DIR / "pohuy-MIT.txt")
        assert h == POHUY_LICENSE_SHA, f"pohuy hash drift: {h}"


# ── NOTICE.md ─────────────────────────────────────────────────────────────────

class TestNotice:
    def _root_notice(self) -> str:
        return (ROOT / "NOTICE.md").read_text(encoding="utf-8")

    def _skill_notice(self) -> str:
        return (ROOT / "skills" / "koroche-blyat" / "NOTICE.md").read_text(
            encoding="utf-8"
        )

    def test_root_notice_exists(self):
        assert (ROOT / "NOTICE.md").is_file()

    def test_skill_notice_exists(self):
        assert (ROOT / "skills" / "koroche-blyat" / "NOTICE.md").is_file()

    def test_root_notice_names_julius_brussee(self):
        assert "Julius Brussee" in self._root_notice()

    def test_root_notice_names_serge_shima(self):
        assert "Serge Shima" in self._root_notice()

    def test_root_notice_states_no_affiliation(self):
        text = self._root_notice().lower()
        assert "no affiliation" in text or "not affiliated" in text

    def test_root_notice_states_caveman_trademark_limits(self):
        text = self._root_notice().lower()
        assert "nominative" in text or "trademark" in text or "attribution" in text

    def test_root_notice_states_excluded_bsl_paths(self):
        text = self._root_notice()
        assert "BSL" in text or "Business Source License" in text

    def test_root_notice_states_clean_room_treatment(self):
        text = self._root_notice().lower()
        assert "clean-room" in text or "clean room" in text

    def test_skill_notice_names_both_upstream_authors(self):
        text = self._skill_notice()
        assert "Julius Brussee" in text
        assert "Serge Shima" in text


# ── UPSTREAMS.yml ─────────────────────────────────────────────────────────────

class TestUpstreamsManifest:
    @pytest.fixture()
    def manifest(self):
        path = ROOT / "UPSTREAMS.yml"
        assert path.is_file(), "UPSTREAMS.yml missing"
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data

    # -- schema basics --

    def test_schema_version_is_one(self, manifest):
        assert manifest["schema_version"] == 1

    def test_has_upstreams_list(self, manifest):
        assert isinstance(manifest["upstreams"], list)
        assert len(manifest["upstreams"]) >= 3

    # -- upstream IDs --

    def test_required_upstream_ids(self, manifest):
        ids = {u["id"] for u in manifest["upstreams"]}
        assert {"caveman", "pohuy", "russian-swears-excluded"} <= ids

    # -- commit SHA format (40-char hex, no branches) --

    def test_commits_are_40_char_hex(self, manifest):
        for u in manifest["upstreams"]:
            sha = u["commit"]
            assert re.fullmatch(r"[0-9a-f]{40}", sha), (
                f"upstream {u['id']}: commit must be 40-char lowercase hex, got {sha!r}"
            )

    # -- no branch URLs --

    def test_repository_urls_are_https_github(self, manifest):
        for u in manifest["upstreams"]:
            url = u["repository"]
            assert url.startswith("https://github.com/"), (
                f"upstream {u['id']}: bad repo URL {url!r}"
            )
            # must not contain branch refs
            assert "/tree/" not in url
            assert "/blob/" not in url

    # -- pinned commits match plan --

    def test_caveman_commit_pinned(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["caveman"]["commit"] == CAVEMAN_COMMIT

    def test_pohuy_commit_pinned(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["pohuy"]["commit"] == POHUY_COMMIT

    def test_russian_swears_commit_pinned(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["russian-swears-excluded"]["commit"] == RUSSIAN_SWEARS_COMMIT

    # -- distributed flag --

    def test_caveman_and_pohuy_are_distributed(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["caveman"]["distributed"] is True
        assert by_id["pohuy"]["distributed"] is True

    def test_russian_swears_is_not_distributed(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["russian-swears-excluded"]["distributed"] is False

    # -- source entries and use values --

    VALID_USE_VALUES = {
        "adapted",
        "redistributed",
        "excluded-clean-room-evidence",
    }

    def test_source_use_values_are_strict(self, manifest):
        for u in manifest["upstreams"]:
            for src in u.get("sources", []):
                assert src["use"] in self.VALID_USE_VALUES, (
                    f"upstream {u['id']}, path {src['path']}: "
                    f"unknown use value {src['use']!r}"
                )

    def test_no_duplicate_paths_within_upstream(self, manifest):
        for u in manifest["upstreams"]:
            paths = [s["path"] for s in u.get("sources", [])]
            assert len(paths) == len(set(paths)), (
                f"upstream {u['id']}: duplicate source paths {paths}"
            )

    def test_no_duplicate_paths_across_upstreams(self, manifest):
        all_redistributed = []
        for u in manifest["upstreams"]:
            for src in u.get("sources", []):
                if src.get("redistributed_as"):
                    all_redistributed.append(src["redistributed_as"])
        assert len(all_redistributed) == len(set(all_redistributed)), (
            f"duplicate redistributed_as paths: {all_redistributed}"
        )

    # -- redistributed files must exist and match hash --

    def test_redistributed_files_exist_and_hash_matches(self, manifest):
        for u in manifest["upstreams"]:
            for src in u.get("sources", []):
                if src.get("redistributed_as"):
                    local = ROOT / src["redistributed_as"]
                    assert local.is_file(), (
                        f"upstream {u['id']}: redistributed file missing: "
                        f"{src['redistributed_as']}"
                    )
                    actual = _sha256(local)
                    assert actual == src["sha256"], (
                        f"upstream {u['id']}, {src['path']}: hash drift "
                        f"expected {src['sha256']}, got {actual}"
                    )

    # -- missing redistributed_as for distributed sources rejects --

    def test_distributed_upstream_license_has_redistributed_file(self, manifest):
        for u in manifest["upstreams"]:
            if not u["distributed"]:
                continue
            license_sources = [
                s for s in u.get("sources", []) if s["use"] == "redistributed"
            ]
            for src in license_sources:
                assert src.get("redistributed_as"), (
                    f"upstream {u['id']}: distributed license source "
                    f"{src['path']} must have redistributed_as"
                )

    # -- excluded sources must NOT have redistributed file --

    def test_excluded_sources_are_not_redistributed(self, manifest):
        for u in manifest["upstreams"]:
            for src in u.get("sources", []):
                if src["use"] == "excluded-clean-room-evidence":
                    assert not src.get("redistributed_as"), (
                        f"upstream {u['id']}: excluded source {src['path']} "
                        f"must not be redistributed"
                    )

    # -- adapted sources have sha256 --

    def test_adapted_sources_have_sha256(self, manifest):
        for u in manifest["upstreams"]:
            for src in u.get("sources", []):
                if src["use"] == "adapted":
                    assert src.get("sha256"), (
                        f"upstream {u['id']}: adapted source {src['path']} "
                        f"must have sha256"
                    )

    # -- plan-pinned source hashes --

    def test_caveman_skill_hash(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        caveman_sources = {
            s["path"]: s for s in by_id["caveman"]["sources"]
        }
        skill = caveman_sources["skills/caveman/SKILL.md"]
        assert skill["sha256"] == CAVEMAN_SKILL_SHA
        assert skill["use"] == "adapted"

    def test_pohuy_skill_hash(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        pohuy_sources = {s["path"]: s for s in by_id["pohuy"]["sources"]}
        skill = pohuy_sources["skills/pohuy/SKILL.md"]
        assert skill["sha256"] == POHUY_SKILL_SHA
        assert skill["use"] == "adapted"

    def test_excluded_readme_hash(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        swears = by_id["russian-swears-excluded"]
        readme_sources = [
            s for s in swears.get("sources", [])
            if s["path"] == "README.md"
        ]
        assert len(readme_sources) == 1
        src = readme_sources[0]
        assert src["use"] == "excluded-clean-room-evidence"
        assert src["sha256"] == RUSSIAN_SWEARS_README_SHA

    # -- license fields --

    def test_caveman_license_field(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["caveman"]["license"] == "MIT-for-skills"

    def test_pohuy_license_field(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["pohuy"]["license"] == "MIT"

    def test_russian_swears_license_field(self, manifest):
        by_id = {u["id"]: u for u in manifest["upstreams"]}
        assert by_id["russian-swears-excluded"]["license"] == "NOASSERTION"


# ── scripts/check_upstreams.py ────────────────────────────────────────────────

class TestCheckUpstreams:
    def test_check_upstreams_module_exists(self):
        assert (ROOT / "scripts" / "check_upstreams.py").is_file()

    def test_check_upstreams_importable(self):
        from scripts import check_upstreams  # noqa: F401

    def test_load_manifest_returns_provenance_manifest(self):
        from scripts.check_upstreams import load_manifest

        m = load_manifest(ROOT / "UPSTREAMS.yml")
        assert hasattr(m, "schema_version")
        assert hasattr(m, "upstreams")
        assert m.schema_version == 1

    def test_check_offline_passes_valid_tree(self):
        from scripts.check_upstreams import check_offline, load_manifest

        m = load_manifest(ROOT / "UPSTREAMS.yml")
        errors = check_offline(ROOT, m)
        assert errors == [], f"offline check errors: {errors}"

    def test_check_offline_detects_hash_drift(self, tmp_path):
        from scripts.check_upstreams import check_offline, load_manifest
        import shutil

        # Copy the real tree partially into tmp_path
        manifest_src = ROOT / "UPSTREAMS.yml"
        manifest_dst = tmp_path / "UPSTREAMS.yml"
        shutil.copy2(manifest_src, manifest_dst)

        # Create the redistributed files dir but with wrong content
        lic_dir = tmp_path / "skills" / "koroche-blyat" / "licenses"
        lic_dir.mkdir(parents=True)
        (lic_dir / "caveman-MIT.txt").write_bytes(b"wrong content\n")
        (lic_dir / "pohuy-MIT.txt").write_bytes(b"also wrong\n")

        m = load_manifest(manifest_dst)
        errors = check_offline(tmp_path, m)
        assert len(errors) > 0, "should detect hash drift"

    def test_check_offline_detects_missing_redistributed(self, tmp_path):
        from scripts.check_upstreams import check_offline, load_manifest
        import shutil

        manifest_src = ROOT / "UPSTREAMS.yml"
        manifest_dst = tmp_path / "UPSTREAMS.yml"
        shutil.copy2(manifest_src, manifest_dst)
        # Don't create the license files at all

        m = load_manifest(manifest_dst)
        errors = check_offline(tmp_path, m)
        assert len(errors) > 0, "should detect missing redistributed files"


# ── Manifest schema rejection tests ──────────────────────────────────────────

class TestManifestRejections:
    def test_rejects_branch_url_in_commit(self):
        from scripts.check_upstreams import load_manifest

        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            upstreams:
              - id: bad
                repository: https://github.com/example/repo
                commit: main
                license: MIT
                distributed: false
                sources: []
        """)
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as f:
            f.write(bad_yaml)
            f.flush()
            with pytest.raises((ValueError, SystemExit)):
                load_manifest(Path(f.name))
            os.unlink(f.name)

    def test_rejects_partial_sha_in_commit(self):
        from scripts.check_upstreams import load_manifest

        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            upstreams:
              - id: bad
                repository: https://github.com/example/repo
                commit: abc1234
                license: MIT
                distributed: false
                sources: []
        """)
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as f:
            f.write(bad_yaml)
            f.flush()
            with pytest.raises((ValueError, SystemExit)):
                load_manifest(Path(f.name))
            os.unlink(f.name)

    def test_rejects_duplicate_paths(self):
        from scripts.check_upstreams import load_manifest

        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            upstreams:
              - id: dup
                repository: https://github.com/example/repo
                commit: 0123456789abcdef0123456789abcdef01234567
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: aaaa000000000000000000000000000000000000000000000000000000000000
                    use: adapted
                  - path: README.md
                    sha256: bbbb000000000000000000000000000000000000000000000000000000000000
                    use: adapted
        """)
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as f:
            f.write(bad_yaml)
            f.flush()
            with pytest.raises((ValueError, SystemExit)):
                load_manifest(Path(f.name))
            os.unlink(f.name)

    def test_rejects_unknown_use_value(self):
        from scripts.check_upstreams import load_manifest

        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            upstreams:
              - id: baduse
                repository: https://github.com/example/repo
                commit: 0123456789abcdef0123456789abcdef01234567
                license: MIT
                distributed: false
                sources:
                  - path: README.md
                    sha256: aaaa000000000000000000000000000000000000000000000000000000000000
                    use: stolen
        """)
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as f:
            f.write(bad_yaml)
            f.flush()
            with pytest.raises((ValueError, SystemExit)):
                load_manifest(Path(f.name))
            os.unlink(f.name)


# ── CLI exit codes ────────────────────────────────────────────────────────────

class TestCheckUpstreamsCLI:
    def test_cli_exit_0_on_valid_offline(self):
        import subprocess
        result = subprocess.run(
            ["uv", "run", "python", "-m", "scripts.check_upstreams"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"check_upstreams offline should exit 0, "
            f"stderr: {result.stderr.decode()}"
        )


def test_root_notice_has_release_repository_url():
    assert "https://github.com/maksimryzhov614/koroche-blyat" in (ROOT / "NOTICE.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["engine/", "proxy/", "mcp/", "shrink/", "browse/", "shared/platform/"])
def test_root_notice_names_each_excluded_bsl_path(path):
    assert path in (ROOT / "NOTICE.md").read_text(encoding="utf-8")


def test_notice_clean_room_claim_is_scoped_to_authoring_and_use():
    text = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    assert "was not inspected or used for authoring" in text
    assert "not copied or" in text
    assert "redistributed" in text
