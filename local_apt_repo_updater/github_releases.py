import fnmatch
import logging
import re
import requests
from pathlib import Path

from .fetchers import fetch_if_modified_since

logger = logging.getLogger(__name__)

CONFIG_KEY = "github_releases"

# Matches common pre-release indicators in a version string.
# Covers keyword-style (1.2.3-alpha, 1.2.3-rc1) and
# PEP 440-style shorthand (1.2.3a1, 1.2.3b2).
_PRERELEASE_RE = re.compile(
    r"[-_.]?(alpha|beta|rc|dev|pre|preview|nightly)"
    r"|(?<=\d)(a|b)\d+",
    re.IGNORECASE,
)


def _is_stable_tag(tag: str) -> bool:
    """
    Return True if *tag* looks like a stable (non-pre-release) version.

    :param tag: Git tag name, optionally prefixed with ``v``.
    :return: True if the tag has no pre-release indicators.
    """
    version_str = tag.lstrip("v")
    if not re.match(r"^\d", version_str):
        return False
    return not _PRERELEASE_RE.search(version_str)


def get_latest_release(repo: str) -> str:
    """
    Resolve the latest release tag for a GitHub repository.

    :param repo: GitHub repository URL (e.g. https://github.com/owner/project).
    :return: The latest release tag string.
    """
    logger.debug(f"Checking latest GitHub release for {repo}")
    r = requests.head(f"{repo}/releases/latest", allow_redirects=False)
    r.raise_for_status()
    location = r.headers.get("Location", "")
    if "/releases/tag/" in location:
        tag = location.rstrip("/").split("/")[-1]
        logger.debug(f"Latest tag for {repo} is {tag}")
        return tag
    # Some repos redirect to /releases instead of /releases/tag/<tag> even when a
    # latest release exists. Fall back to the API in that case.
    logger.debug(
        f"Redirect for {repo} did not resolve to a specific tag ({location!r}), "
        f"falling back to GitHub API"
    )
    api_url = repo.replace("https://github.com/", "https://api.github.com/repos/", 1)
    r = requests.get(f"{api_url}/releases/latest")
    r.raise_for_status()
    tag = r.json()["tag_name"]
    logger.debug(f"Latest tag for {repo} is {tag} (via API fallback)")
    return tag


def get_latest_stable_release(repo: str) -> str:
    """
    Find the most recent stable release by iterating through the GitHub releases API.

    Releases are skipped when GitHub marks them as a pre-release or when the
    tag name contains pre-release indicators (alpha, beta, rc, dev, etc.).
    Pages through results until a stable release is found.

    :param repo: GitHub repository URL (e.g. https://github.com/owner/project).
    :return: The tag name of the most recent stable release.
    :raises ValueError: If no stable release is found across all pages.
    """
    api_url = repo.replace("https://github.com/", "https://api.github.com/repos/", 1)
    page = 1
    while True:
        r = requests.get(f"{api_url}/releases", params={"per_page": 100, "page": page})
        r.raise_for_status()
        releases = r.json()
        if not releases:
            raise ValueError(f"No stable release found for {repo}")
        for release in releases:
            if release.get("draft") or release.get("prerelease"):
                continue
            tag = release["tag_name"]
            if _is_stable_tag(tag):
                logger.debug(f"Found latest stable release {tag} for {repo}")
                return tag
            logger.debug(f"Skipping {tag!r} for {repo} (pre-release indicators in tag)")
        page += 1


def get_release_assets(repo: str, tag: str) -> list[dict]:
    """
    Retrieve the list of assets attached to a GitHub release tag.

    :param repo: GitHub repository URL (e.g. https://github.com/owner/project).
    :param tag: Release tag name.
    :return: List of asset dicts, each containing at least ``name`` and
             ``browser_download_url``.
    """
    api_url = repo.replace("https://github.com/", "https://api.github.com/repos/", 1)
    logger.debug(f"Fetching release assets for tag {tag} from {api_url}")
    r = requests.get(f"{api_url}/releases/tags/{tag}")
    r.raise_for_status()
    return r.json().get("assets", [])


def fetch_packages(entries: list[dict], repo_dir: Path, env: dict) -> bool:
    """
    Fetch .deb files from GitHub release assets.

    Each entry requires:
      - ``repo``: GitHub repository URL
      - ``filename``: filename template supporting ``{APP_VERSION}`` and OS release vars.
        When the expanded value contains ``*`` or ``?`` wildcards, the release asset list
        is fetched from the GitHub API and the pattern is matched against it to discover
        the real filename.  Otherwise the filename is used directly to construct the
        download URL.
      - ``use_latest_tag`` (optional, default ``true``): when ``false``, skips GitHub's
        "latest" pointer and instead finds the most recent release whose tag contains
        no pre-release indicators (alpha, beta, rc, etc.)

    :param entries: List of GitHub release config entries from config.yaml.
    :param repo_dir: Local APT repository directory.
    :param env: OS release environment variables (from ``platform.freedesktop_os_release()``).
    :return: True if any file was downloaded or updated.
    """
    changed = False
    for entry in entries:
        if entry.get("use_latest_tag", True):
            tag = get_latest_release(entry["repo"])
        else:
            tag = get_latest_stable_release(entry["repo"])
        version = tag.lstrip("v")

        pattern = entry["filename"].format(APP_VERSION=version, **env)
        is_wildcard = "*" in pattern or "?" in pattern
        if is_wildcard:
            assets = get_release_assets(entry["repo"], tag)
            matched = [a for a in assets if fnmatch.fnmatch(a["name"], pattern)]
            if not matched:
                logger.warning(
                    f"No release assets matched pattern {pattern!r} for "
                    f"{entry['repo']} tag {tag}"
                )
                continue
            if len(matched) > 1:
                logger.warning(
                    f"Multiple assets matched {pattern!r} for {entry['repo']} tag {tag}: "
                    f"{[a['name'] for a in matched]!r}, using {matched[0]['name']!r}"
                )
            filename = matched[0]["name"]
            url = matched[0]["browser_download_url"]
        else:
            filename = pattern
            url = f"{entry['repo']}/releases/download/{tag}/{filename}"

        try:
            if fetch_if_modified_since(url, repo_dir / filename):
                changed = True
        except requests.exceptions.HTTPError as e:
            if (
                e.response is not None
                and e.response.status_code == 404
                and not is_wildcard
            ):
                try:
                    assets = get_release_assets(entry["repo"], tag)
                    asset_info = f"  Available assets for tag {tag}: {[a['name'] for a in assets]}"
                except requests.exceptions.HTTPError:
                    asset_info = (
                        f"  Could not fetch asset list for tag {tag!r} — "
                        f"the tag may not exist as a GitHub release."
                    )
                logger.error(
                    f"File not found: {url}\n"
                    f"  The filename '{entry['filename']}' expanded to '{filename}' "
                    f"for {entry['repo']} tag {tag} does not exist.\n"
                    f"{asset_info}\n"
                    f"  Fix the 'filename' value in config.yaml, or use a wildcard "
                    f"pattern (e.g. '*_{{}}.deb') to match automatically."
                )
            else:
                raise

    return changed
