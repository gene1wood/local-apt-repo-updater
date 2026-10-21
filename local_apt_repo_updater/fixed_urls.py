import logging
from pathlib import Path

from .fetchers import (
    fetch_if_modified_since,
    fetch_head_local_compare,
    fetch_with_size_check,
)

logger = logging.getLogger(__name__)

CONFIG_KEY = "fixed_urls"

_FETCH_MODES = {
    "last_modified": fetch_if_modified_since,
    "last_modified_local_compare": fetch_head_local_compare,
    "content_length": fetch_with_size_check,
}


def fetch_packages(entries: list[dict], repo_dir: Path, env: dict) -> bool:
    """
    Fetch .deb files from fixed (non-release) URLs.

    Each entry requires:
      - ``url``: Base URL template supporting OS release vars
      - ``filename``: Filename template supporting OS release vars
      - ``conditional`` (optional): Staleness check strategy — one of
        ``last_modified`` (default), ``last_modified_local_compare``,
        or ``content_length``

    :param entries: List of fixed URL config entries from config.yaml.
    :param repo_dir: Local APT repository directory.
    :param env: OS release environment variables (from ``platform.freedesktop_os_release()``).
    :return: True if any file was downloaded or updated.
    """
    changed = False
    for entry in entries:
        try:
            base_url = entry["url"].format(**env)
            filename = entry["filename"].format(**env)
            url = f"{base_url}/{filename}"
            dest = repo_dir / filename

            mode = entry.get("conditional", "last_modified")
            fetch_fn = _FETCH_MODES.get(mode)
            if fetch_fn is None:
                raise ValueError(
                    f"Unknown conditional mode: {mode!r}. Valid modes: {list(_FETCH_MODES)}"
                )

            logger.debug(f"Processing {url} (mode={mode})")
            if fetch_fn(url, dest):
                changed = True
        except Exception as e:
            logger.error(f"Failed to fetch {entry.get('url', entry)!r}: {e}")

    return changed
