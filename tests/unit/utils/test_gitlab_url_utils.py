"""
Unit tests for registry.utils.gitlab_url_utils.

Validates parsing of GitLab raw-web and API v4 file URLs, translation
between the two forms, derivation of sibling resource URLs, and
construction of tree API URLs for multi-file skill discovery.
"""

import pytest

from registry.utils.gitlab_url_utils import (
    derive_gitlab_resource_url,
    parse_gitlab_url,
    translate_gitlab_to_api_url,
    translate_gitlab_tree_api_url,
)


class TestParseGitlabUrl:
    """Tests for parse_gitlab_url."""

    def test_parses_raw_web_url(self):
        """Should parse a /-/raw/ web URL into base/project/ref/filepath."""
        url = "https://gitlab.example.com/group/repo/-/raw/main/skills/demo/SKILL.md"

        parts = parse_gitlab_url(url)

        assert parts is not None
        assert parts.base == "https://gitlab.example.com"
        assert parts.project == "group/repo"
        assert parts.ref == "main"
        assert parts.filepath == "skills/demo/SKILL.md"

    def test_parses_api_v4_file_url(self):
        """Should parse an API v4 raw-file URL and decode path components."""
        url = (
            "https://gitlab.example.com/api/v4/projects/group%2Frepo"
            "/repository/files/skills%2Fdemo%2FSKILL.md/raw?ref=main"
        )

        parts = parse_gitlab_url(url)

        assert parts is not None
        assert parts.project == "group/repo"
        assert parts.ref == "main"
        assert parts.filepath == "skills/demo/SKILL.md"

    def test_parts_properties(self):
        """file_dir and encoded_project should expose API-ready components."""
        nested = parse_gitlab_url("https://gitlab.example.com/g/sub/r/-/raw/main/a/b/c/SKILL.md")
        root = parse_gitlab_url("https://gitlab.example.com/g/r/-/raw/main/SKILL.md")

        assert nested is not None and root is not None
        assert nested.file_dir == "a/b/c"
        assert nested.encoded_project == "g%2Fsub%2Fr"
        assert root.file_dir == ""

    def test_parses_self_hosted_without_gitlab_in_hostname(self):
        """Should parse a /-/raw/ URL from a self-hosted instance without 'gitlab' in name."""
        url = "https://code.internal.corp/team/project/-/raw/develop/skills/jira/SKILL.md"

        parts = parse_gitlab_url(url)

        assert parts is not None
        assert parts.base == "https://code.internal.corp"
        assert parts.project == "team/project"
        assert parts.ref == "develop"
        assert parts.filepath == "skills/jira/SKILL.md"

    def test_parses_api_v4_url_from_self_hosted_without_gitlab_in_hostname(self):
        """Should parse an API v4 URL from a self-hosted instance without 'gitlab' in name."""
        url = (
            "https://scm.mycompany.io/api/v4/projects/ops%2Finfra"
            "/repository/files/skills%2Fdemo%2FSKILL.md/raw?ref=main"
        )

        parts = parse_gitlab_url(url)

        assert parts is not None
        assert parts.base == "https://scm.mycompany.io"
        assert parts.project == "ops/infra"
        assert parts.ref == "main"
        assert parts.filepath == "skills/demo/SKILL.md"

    def test_parses_http_scheme(self):
        """Should parse a plain http:// (non-TLS) /-/raw/ URL."""
        parts = parse_gitlab_url("http://gitlab.local/g/r/-/raw/main/SKILL.md")

        assert parts is not None
        assert parts.base == "http://gitlab.local"
        assert parts.project == "g/r"
        assert parts.ref == "main"
        assert parts.filepath == "SKILL.md"

    def test_raw_marker_in_filepath_uses_first_occurrence(self):
        """A /-/raw/ inside the filepath must not re-split; first marker wins.

        GitLab reserves /-/ at the project level, so the leftmost /-/raw/
        delimits project|ref, and any later occurrence stays in the filepath.
        """
        url = "https://gitlab.example.com/g/r/-/raw/main/docs/-/raw/notes.md"

        parts = parse_gitlab_url(url)

        assert parts is not None
        assert parts.project == "g/r"
        assert parts.ref == "main"
        assert parts.filepath == "docs/-/raw/notes.md"

    def test_url_matching_both_forms_parses_as_raw_web(self):
        """When a URL contains both markers, the raw-web branch takes priority."""
        url = "https://gitlab.example.com/api/v4/projects/x/-/raw/main/file.md"

        parts = parse_gitlab_url(url)

        assert parts is not None
        assert parts.project == "api/v4/projects/x"
        assert parts.ref == "main"
        assert parts.filepath == "file.md"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "https://github.com/owner/repo/blob/main/SKILL.md",
            "https://raw.githubusercontent.com/owner/repo/main/SKILL.md",
            "https://gitlab.example.com/group/repo/blob/main/SKILL.md",
            "not a url at all",
            # No host between scheme and first slash.
            "https:///group/repo/-/raw/main/SKILL.md",
            "http:///api/v4/projects/g%2Fr/repository/files/f/raw?ref=main",
            # Scheme + host but no path at all.
            "https://gitlab.example.com",
            # /-/raw/ present but empty project, ref, or filepath.
            "https://gitlab.example.com/-/raw/main/SKILL.md",
            "https://gitlab.example.com/g/r/-/raw//SKILL.md",
            "https://gitlab.example.com/g/r/-/raw/main/",
            # API form missing the /repository/files/ marker or the raw?ref= suffix.
            "https://gitlab.example.com/api/v4/projects/g%2Fr/repository/files/f",
            "https://gitlab.example.com/api/v4/projects//repository/files/f/raw?ref=main",
        ],
    )
    def test_returns_none_for_unrecognised_urls(self, url):
        """Should return None for any URL that matches neither pattern."""
        assert parse_gitlab_url(url) is None

    @pytest.mark.parametrize(
        "url",
        [
            "http://x/" + "http://x/a" * 50000,
            "http://x/a/-/raw/x/" + "a/-/raw/x/a" * 50000,
            "http://x/api/v4/projects/x/repository/files/"
            + "http://x/api/v4/projects/x/repository/files/a" * 20000,
            "http://x/api/v4/projects/x/repository/files/a/raw?ref=" + "a/raw?ref=a" * 50000,
        ],
    )
    def test_redos_payloads_parse_in_linear_time(self, url):
        """These strings previously drove the regexes to polynomial time. Linear
        substring parsing returns promptly; the test would hang under the old
        implementation.
        """
        # We only assert it returns (quickly); the value itself is irrelevant.
        parse_gitlab_url(url)


class TestTranslateGitlabToApiUrl:
    """Tests for translate_gitlab_to_api_url."""

    def test_translates_raw_web_url_to_api_v4(self):
        """Should rewrite a /-/raw/ URL to the API v4 raw file endpoint."""
        url = "https://gitlab.example.com/group/repo/-/raw/main/skills/demo/SKILL.md"

        result = translate_gitlab_to_api_url(url)

        assert result == (
            "https://gitlab.example.com/api/v4/projects/group%2Frepo"
            "/repository/files/skills%2Fdemo%2FSKILL.md/raw?ref=main"
        )

    def test_translates_nested_group_url(self):
        """Should encode nested-group project paths correctly."""
        url = "https://gitlab.example.com/g/sub/repo/-/raw/main/SKILL.md"

        result = translate_gitlab_to_api_url(url)

        assert result == (
            "https://gitlab.example.com/api/v4/projects/g%2Fsub%2Frepo"
            "/repository/files/SKILL.md/raw?ref=main"
        )

    def test_translation_is_idempotent_for_api_v4_input(self):
        """Re-translating an API v4 URL should yield an equivalent API v4 URL."""
        api_url = (
            "https://gitlab.example.com/api/v4/projects/group%2Frepo"
            "/repository/files/skills%2Fdemo%2FSKILL.md/raw?ref=main"
        )

        result = translate_gitlab_to_api_url(api_url)

        assert result == api_url

    def test_returns_none_for_non_gitlab_url(self):
        """Should return None when the URL is not recognisably GitLab."""
        url = "https://github.com/owner/repo/blob/main/SKILL.md"

        assert translate_gitlab_to_api_url(url) is None


class TestDeriveGitlabResourceUrl:
    """Tests for derive_gitlab_resource_url."""

    def test_derives_sibling_resource_in_same_directory(self):
        """Should resolve a sibling resource relative to the SKILL.md dir."""
        skill_md_url = "https://gitlab.example.com/g/r/-/raw/main/skills/demo/SKILL.md"

        result = derive_gitlab_resource_url(skill_md_url, "reference/notes.md")

        assert result == (
            "https://gitlab.example.com/api/v4/projects/g%2Fr"
            "/repository/files/skills%2Fdemo%2Freference%2Fnotes.md/raw?ref=main"
        )

    def test_derives_resource_when_skill_at_repo_root(self):
        """Should treat resource_path as the full path when SKILL.md is at root."""
        skill_md_url = "https://gitlab.example.com/g/r/-/raw/main/SKILL.md"

        result = derive_gitlab_resource_url(skill_md_url, "asset.png")

        assert result == (
            "https://gitlab.example.com/api/v4/projects/g%2Fr"
            "/repository/files/asset.png/raw?ref=main"
        )

    def test_encodes_special_characters_in_resource_path(self):
        """Should percent-encode spaces and reserved characters in the path."""
        skill_md_url = "https://gitlab.example.com/g/r/-/raw/main/dir/SKILL.md"

        result = derive_gitlab_resource_url(skill_md_url, "a b/c+d.txt")

        assert result is not None
        # Path should be fully quoted, including '/' between dir and resource.
        assert "/repository/files/dir%2Fa%20b%2Fc%2Bd.txt/raw?ref=main" in result

    def test_returns_none_for_non_gitlab_url(self):
        """Should return None when the skill URL is not a GitLab URL."""
        assert (
            derive_gitlab_resource_url("https://github.com/o/r/blob/main/SKILL.md", "ref.md")
            is None
        )


class TestTranslateGitlabTreeApiUrl:
    """Tests for translate_gitlab_tree_api_url."""

    def test_builds_tree_api_url_for_skill_directory(self):
        """Should return tree URL plus encoded project, ref, and skill_dir."""
        skill_md_url = "https://gitlab.example.com/g/r/-/raw/main/skills/demo/SKILL.md"

        result = translate_gitlab_tree_api_url(skill_md_url)

        assert result is not None
        tree_url, project_encoded, ref, skill_dir = result
        assert project_encoded == "g%2Fr"
        assert ref == "main"
        assert skill_dir == "skills/demo"
        assert tree_url == (
            "https://gitlab.example.com/api/v4/projects/g%2Fr/repository/tree"
            "?path=skills%2Fdemo&ref=main&recursive=true&per_page=100"
        )

    def test_encodes_nested_skill_directories(self):
        """Should percent-encode every slash in deeply nested skill paths."""
        skill_md_url = "https://gitlab.example.com/g/r/-/raw/main/a/b/c/SKILL.md"

        result = translate_gitlab_tree_api_url(skill_md_url)

        assert result is not None
        tree_url, _, _, skill_dir = result
        assert skill_dir == "a/b/c"
        assert "path=a%2Fb%2Fc" in tree_url

    def test_returns_none_when_skill_md_at_repo_root(self):
        """Tree resolution requires a parent directory; root files yield None."""
        skill_md_url = "https://gitlab.example.com/g/r/-/raw/main/SKILL.md"

        assert translate_gitlab_tree_api_url(skill_md_url) is None

    def test_returns_none_for_non_gitlab_url(self):
        """Should return None for non-GitLab URLs."""
        assert (
            translate_gitlab_tree_api_url("https://github.com/o/r/blob/main/skills/demo/SKILL.md")
            is None
        )
