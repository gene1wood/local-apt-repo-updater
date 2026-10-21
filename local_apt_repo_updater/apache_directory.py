import fnmatch
import logging
import re
from functools import cmp_to_key
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .fetchers import fetch_if_modified_since

logger = logging.getLogger(__name__)

CONFIG_KEY = "apache_directory"


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for attr, value in attrs:
                if attr == "href" and value:
                    self.links.append(value)


def _deb_char_order(c: str) -> int:
    """Debian character ordering for version comparison."""
    if not c or c.isdigit():
        return 0
    if c == "~":
        return -1
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _compare_version_part(a: str, b: str) -> int:
    """
    Compare two Debian version string parts (upstream_version or debian_revision)
    using the dpkg algorithm.
    """
    ia, ib = 0, 0
    while ia < len(a) or ib < len(b):
        first_diff = 0

        # Compare non-digit characters one at a time using Debian char ordering.
        while (ia < len(a) and not a[ia].isdigit()) or (
            ib < len(b) and not b[ib].isdigit()
        ):
            ca = a[ia] if ia < len(a) else ""
            cb = b[ib] if ib < len(b) else ""
            oa = _deb_char_order(ca)
            ob = _deb_char_order(cb)
            if oa != ob:
                return oa - ob
            if ia < len(a):
                ia += 1
            if ib < len(b):
                ib += 1

        # Skip leading zeros before comparing numeric substrings.
        while ia < len(a) and a[ia] == "0":
            ia += 1
        while ib < len(b) and b[ib] == "0":
            ib += 1

        # Walk both digit sequences together, recording the first digit difference.
        while ia < len(a) and a[ia].isdigit() and ib < len(b) and b[ib].isdigit():
            if not first_diff:
                first_diff = ord(a[ia]) - ord(b[ib])
            ia += 1
            ib += 1

        # A longer digit sequence represents a larger integer.
        if ia < len(a) and a[ia].isdigit():
            return 1
        if ib < len(b) and b[ib].isdigit():
            return -1
        if first_diff:
            return first_diff

    return 0


def _parse_deb_version(version: str) -> tuple[int, str, str]:
    """Split a Debian version into (epoch, upstream_version, debian_revision)."""
    if ":" in version:
        epoch_str, rest = version.split(":", 1)
        epoch = int(epoch_str)
    else:
        epoch = 0
        rest = version
    last_dash = rest.rfind("-")
    if last_dash >= 0:
        return epoch, rest[:last_dash], rest[last_dash + 1 :]
    return epoch, rest, ""


def _compare_deb_versions(v1: str, v2: str) -> int:
    """Compare two Debian version strings. Returns negative, 0, or positive."""
    e1, u1, r1 = _parse_deb_version(v1)
    e2, u2, r2 = _parse_deb_version(v2)
    if e1 != e2:
        return e1 - e2
    cmp = _compare_version_part(u1, u2)
    if cmp != 0:
        return cmp
    return _compare_version_part(r1, r2)


def _extract_deb_version(filename: str) -> str | None:
    """
    Extract the version string from a standard Debian package filename.

    Standard format: ``package_version_arch.deb``
    """
    m = re.match(r"^[^_]+_([^_]+)_[^_.]+\.deb$", filename)
    return m.group(1) if m else None


def list_deb_files(url: str) -> list[str]:
    """Fetch an Apache directory listing and return all .deb filenames found."""
    r = requests.get(url)
    r.raise_for_status()
    parser = _LinkParser()
    parser.feed(r.text)
    return [
        href
        for href in parser.links
        if href.endswith(".deb") and "/" not in href and not href.startswith(("?", "#"))
    ]


def list_version_dirs(url: str) -> list[str]:
    """Fetch an Apache directory listing and return subdirectory names (without trailing slash)."""
    r = requests.get(url)
    r.raise_for_status()
    parser = _LinkParser()
    parser.feed(r.text)
    base_path = urlparse(url).path.rstrip("/") + "/"
    results = []
    for href in parser.links:
        if not href.endswith("/") or href.startswith(("?", "#")):
            continue
        # Resolve against base URL so that relative, root-relative, and absolute hrefs
        # are all normalised. Then skip anything that isn't a direct child of base_path
        # (parent directory links, navigation links to other hosts, etc.).
        resolved_path = urlparse(urljoin(url, href)).path
        if not resolved_path.startswith(base_path) or resolved_path == base_path:
            continue
        segment = resolved_path.rstrip("/").rsplit("/", 1)[-1]
        if segment:
            results.append(segment)
    return results


def find_newest_version(url: str) -> str | None:
    """
    Find the newest version from an Apache directory listing of version subdirectories.

    :param url: Apache directory listing URL containing version subdirectories.
    :return: The newest version string, or None if no subdirectories found.
    """
    versions = list_version_dirs(url)
    if not versions:
        return None
    return max(versions, key=cmp_to_key(_compare_deb_versions))


def find_newest_deb(url: str, pattern: str) -> str | None:
    """
    Find the filename with the newest Debian version from an Apache directory listing.

    :param url: Apache directory listing URL.
    :param pattern: Glob pattern (e.g. ``package_*_amd64.deb``) to filter filenames.
    :return: Filename of the newest matching package, or None if no match.
    """
    filenames = list_deb_files(url)
    matched = [f for f in filenames if fnmatch.fnmatch(f, pattern)]
    if not matched:
        return None

    def version_cmp(a: str, b: str) -> int:
        va = _extract_deb_version(a) or ""
        vb = _extract_deb_version(b) or ""
        return _compare_deb_versions(va, vb)

    return max(matched, key=cmp_to_key(version_cmp))


def fetch_packages(entries: list[dict], repo_dir: Path, env: dict) -> bool:
    """
    Fetch the newest .deb from an Apache directory listing.

    Each entry requires:
      - ``url``: Apache directory listing URL, supporting OS release vars and ``{APP_VERSION}``.
      - ``filename``: One glob pattern or a list of glob patterns to match against the listing,
        supporting OS release vars and ``{APP_VERSION}``. Use ``*`` as a wildcard
        (e.g. ``package_*_amd64.deb``). Each pattern is resolved independently; the newest
        matching file for each is downloaded.

    Optional:
      - ``version_url``: URL of an Apache directory listing whose subdirectory names are version
        strings. The newest version is resolved and substituted as ``{APP_VERSION}`` into ``url``
        and ``filename`` before processing.

    :param entries: List of apache_directory config entries from config.yaml.
    :param repo_dir: Local APT repository directory.
    :param env: OS release environment variables (from ``platform.freedesktop_os_release()``).
    :return: True if any file was downloaded or updated.
    """
    changed = False
    for entry in entries:
        entry_env = dict(env)

        if "version_url" in entry:
            version_url = entry["version_url"].format(**entry_env)
            app_version = find_newest_version(version_url)
            if app_version is None:
                logger.warning(f"No version subdirectories found at {version_url}")
                continue
            logger.debug(f"Newest version at {version_url}: {app_version}")
            entry_env["APP_VERSION"] = app_version

        url = entry["url"].format(**entry_env)
        raw = entry["filename"]
        patterns = [
            p.format(**entry_env) for p in (raw if isinstance(raw, list) else [raw])
        ]

        for pattern in patterns:
            filename = find_newest_deb(url, pattern)
            if filename is None:
                logger.warning(f"No .deb files matching {pattern!r} found at {url}")
                continue

            file_url = f"{url.rstrip('/')}/{filename}"
            dest = repo_dir / filename
            logger.debug(f"Newest match for {pattern!r} at {url}: {filename}")
            if fetch_if_modified_since(file_url, dest):
                changed = True

    return changed
