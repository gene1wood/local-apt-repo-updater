import os
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

logger = logging.getLogger(__name__)


def apply_last_modified(response, dest: Path) -> None:
    """
    If the response contains a Last-Modified header, apply it to the file mtime.
    """
    last_modified = response.headers.get("Last-Modified")
    if not last_modified:
        logger.debug(f"No Last-Modified header for {dest}")
        return

    try:
        dt = parsedate_to_datetime(last_modified)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        mtime = dt.timestamp()
        os.utime(dest, (mtime, mtime))
        logger.debug(f"Set mtime for {dest} to {dt.isoformat()}")
    except Exception as e:
        logger.warning(f"Failed to apply Last-Modified for {dest}: {e}")


def http_date_from_mtime(path: Path) -> str:
    dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return format_datetime(dt, usegmt=True)


def fetch_if_modified_since(url: str, dest: Path) -> bool:
    """
    :param url: URL to fetch.
    :param dest: Local destination path.
    :return: True if the file was fetched (new or updated), False if unchanged.
    """
    headers = {}
    if dest.exists():
        if_modified_since = http_date_from_mtime(dest)
        headers["If-Modified-Since"] = if_modified_since
        logger.debug(f"Using If-Modified-Since for {dest} of {if_modified_since}")

    logger.debug(f"Fetching {url}")
    r = requests.get(url, headers=headers, stream=True)

    if r.status_code == 304:
        logger.debug(f"Not modified: {url}")
        return False

    r.raise_for_status()

    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)

    apply_last_modified(r, dest)
    logger.info(f"Fetched new package {url}")
    return True


def fetch_head_local_compare(url: str, dest: Path) -> bool:
    """
    Compare Last-Modified from a HEAD request against local mtime before fetching.

    This solves for problems like
    https://gist.github.com/gene1wood/5249118ad566eb349f4b85da2f8c9e8f

    :param url: URL to fetch.
    :param dest: Local destination path.
    :return: True if the file was fetched (new or updated), False if unchanged.
    """
    if dest.exists():
        if_modified_since = datetime.fromtimestamp(
            dest.stat().st_mtime, tz=timezone.utc
        )
        r = requests.head(url, allow_redirects=True)
        last_modified = r.headers.get("Last-Modified")
        if last_modified:
            dt = parsedate_to_datetime(last_modified)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt <= if_modified_since:
                logger.debug(f"Not modified: {url}")
                return False
            else:
                logger.debug(
                    f"Fetching {url} because Last-Modified is {dt.isoformat()} "
                    f"and the local file {dest} mtime is {if_modified_since.isoformat()}"
                )
        else:
            logger.debug(f"No Last-Modified header in the response for {url}")

    logger.debug(f"Fetching {url}")
    r = requests.get(url, stream=True)
    r.raise_for_status()

    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)

    apply_last_modified(r, dest)
    logger.info(f"Fetched new package {url}")
    return True


def fetch_with_size_check(url: str, dest: Path) -> bool:
    """
    Skip fetch if Content-Length matches local file size.

    :param url: URL to fetch.
    :param dest: Local destination path.
    :return: True if the file was fetched (new or updated), False if unchanged.
    """
    if dest.exists():
        local_size = dest.stat().st_size
        head = requests.head(url, allow_redirects=True)
        head.raise_for_status()
        remote_size = head.headers.get("Content-Length")

        logger.debug(f"Size check for {url} (local={local_size}, remote={remote_size})")

        if remote_size is not None and int(remote_size) == local_size:
            logger.debug(f"Content length unchanged for {url}")
            return False

    logger.debug(f"Fetching {url}")
    r = requests.get(url, stream=True)
    r.raise_for_status()

    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)

    apply_last_modified(r, dest)
    logger.info(f"Fetched new package {url}")
    return True
