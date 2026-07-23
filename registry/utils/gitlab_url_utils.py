"""
GitLab-specific URL utilities for API v4 endpoint translation.

Provides functions to translate GitLab web URLs (/-/raw/) to API v4
authenticated endpoints, derive resource URLs within GitLab repos,
and build tree API URLs for resource discovery.

These helpers are not needed for GitHub-hosted skills and can be
omitted from upstream distributions.
"""

from urllib.parse import quote as url_quote
from urllib.parse import unquote

# Literal markers used for linear (non-regex) URL parsing. Matching on fixed
# substrings instead of ambiguous regexes avoids polynomial-time backtracking
# (ReDoS) on attacker-controlled URLs.
_RAW_MARKER = "/-/raw/"
_API_PROJECTS_PREFIX = "/api/v4/projects/"
_API_FILES_MARKER = "/repository/files/"
_API_RAW_SUFFIX = "/raw?ref="


class _GitLabParts:
    """Parsed components of a GitLab /-/raw/ or API v4 file URL."""

    __slots__ = ("base", "project", "ref", "filepath")

    def __init__(self, base: str, project: str, ref: str, filepath: str):
        self.base = base
        self.project = project
        self.ref = ref
        self.filepath = filepath

    @property
    def encoded_project(self) -> str:
        return url_quote(self.project, safe="")

    @property
    def file_dir(self) -> str:
        """Directory containing the file (everything before the last /)."""
        return self.filepath.rsplit("/", 1)[0] if "/" in self.filepath else ""


def _split_scheme_host(url: str) -> tuple[str, str] | None:
    """Split ``scheme://host`` from the rest of the path.

    Returns ``(base, rest)`` where *base* is ``https?://host`` (host non-empty,
    no slash) and *rest* is the remaining path starting with ``/``. Returns
    None if *url* has no recognised scheme, an empty host, or no path.
    """
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            after_scheme = url[len(scheme) :]
            slash = after_scheme.find("/")
            # host must be non-empty ([^/]+) and a path must follow.
            if slash <= 0:
                return None
            split_at = len(scheme) + slash
            return url[:split_at], url[split_at:]
    return None


def parse_gitlab_url(url: str) -> _GitLabParts | None:
    """Parse a GitLab raw-web or API v4 file URL into its components.

    Handles two forms:
      /-/raw/  web URLs:  https://host/group/repo/-/raw/branch/path/to/file
      API v4 file URLs:   https://host/api/v4/projects/group%2Frepo/repository/files/path%2Fto%2Ffile/raw?ref=branch

    Returns None if the URL matches neither pattern.

    Parsing uses linear substring matching (no regexes) to avoid
    polynomial-time backtracking on attacker-controlled URLs.
    """
    split = _split_scheme_host(url)
    if split is None:
        return None
    base, rest = split

    # The raw-web form is checked before the API v4 form; a URL matching both
    # (e.g. an /api/v4/... path that also contains /-/raw/) is parsed as
    # raw-web. This preserves the original regex evaluation order.

    # /-/raw/ web URL: /<project>/-/raw/<ref>/<filepath>
    raw_idx = rest.find(_RAW_MARKER)
    if raw_idx > 1:  # rest[0] is '/', project (rest[1:raw_idx]) must be non-empty
        project = rest[1:raw_idx]
        after = rest[raw_idx + len(_RAW_MARKER) :]
        ref_slash = after.find("/")
        # ref ([^/]+) must be non-empty; filepath (.+) must be non-empty.
        if ref_slash > 0 and ref_slash + 1 < len(after):
            ref = after[:ref_slash]
            filepath = after[ref_slash + 1 :]
            return _GitLabParts(base, project, ref, filepath)

    # API v4 file URL: /api/v4/projects/<project>/repository/files/<path>/raw?ref=<ref>
    if rest.startswith(_API_PROJECTS_PREFIX):
        after_prefix = rest[len(_API_PROJECTS_PREFIX) :]
        proj_slash = after_prefix.find("/")
        if proj_slash > 0:  # project ([^/]+) is non-empty and slash-free
            encoded_project = after_prefix[:proj_slash]
            remainder = after_prefix[proj_slash:]
            if remainder.startswith(_API_FILES_MARKER):
                after_files = remainder[len(_API_FILES_MARKER) :]
                suffix_idx = after_files.find(_API_RAW_SUFFIX)
                if suffix_idx > 0:  # encoded_path (.+?) must be non-empty
                    encoded_path = after_files[:suffix_idx]
                    ref = after_files[suffix_idx + len(_API_RAW_SUFFIX) :]
                    if ref:  # ref (.+) must be non-empty
                        return _GitLabParts(
                            base, unquote(encoded_project), ref, unquote(encoded_path)
                        )

    return None


def translate_gitlab_to_api_url(url: str) -> str | None:
    """Translate a GitLab web raw URL to a GitLab API v4 raw file endpoint.

    GitLab's /-/raw/ web URLs require session cookies for private repos.
    The API v4 endpoint accepts PRIVATE-TOKEN header authentication.

    Returns None if the URL doesn't match the expected GitLab pattern.
    """
    parts = parse_gitlab_url(url)
    if not parts:
        return None
    encoded_path = url_quote(parts.filepath, safe="")
    return (
        f"{parts.base}/api/v4/projects/{parts.encoded_project}"
        f"/repository/files/{encoded_path}/raw?ref={parts.ref}"
    )


def derive_gitlab_resource_url(skill_md_url: str, resource_path: str) -> str | None:
    """Derive a resource URL from a GitLab SKILL.md URL.

    Returns an API v4 file URL for the resource, or None if the
    skill_md_url is not a recognised GitLab URL.
    """
    parts = parse_gitlab_url(skill_md_url)
    if not parts:
        return None

    file_dir = parts.file_dir
    new_path = f"{file_dir}/{resource_path}" if file_dir else resource_path
    encoded_path = url_quote(new_path, safe="")
    return (
        f"{parts.base}/api/v4/projects/{parts.encoded_project}"
        f"/repository/files/{encoded_path}/raw?ref={parts.ref}"
    )


def translate_gitlab_tree_api_url(skill_md_url: str) -> tuple[str, str, str, str] | None:
    """Derive a GitLab API v4 tree endpoint from a skill's URL.

    Returns (tree_api_url, project_encoded, ref, skill_dir) or None if not a
    GitLab URL.  *skill_dir* is the directory prefix that the tree API will
    prepend to every returned path (e.g. ``skills/jira-to-pr``).
    """
    parts = parse_gitlab_url(skill_md_url)
    if not parts:
        return None

    skill_dir = parts.file_dir
    if not skill_dir:
        return None

    tree_url = (
        f"{parts.base}/api/v4/projects/{parts.encoded_project}/repository/tree"
        f"?path={url_quote(skill_dir, safe='')}&ref={parts.ref}&recursive=true&per_page=100"
    )
    return tree_url, parts.encoded_project, parts.ref, skill_dir
