import os
import argparse
import logging
import platform
import subprocess
import requests
import yaml
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from platformdirs import user_config_dir

logger = logging.getLogger(__name__)


def setup_logging(verbosity: int) -> None:
    if verbosity >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )


def xdg_config_path():
    return Path(user_config_dir(appname="local-apt-repo-updater")) / "config.yaml"


def load_config():
    logger.debug(f"Loading configuration from {xdg_config_path()}")
    with open(xdg_config_path()) as f:
        return yaml.safe_load(f)


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


def http_date_from_mtime(path):
    dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return format_datetime(dt, usegmt=True)


def fetch_if_modified_since(url, dest):
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


def fetch_head_local_compare(url, dest):
    # This solves for problems like
    # https://gist.github.com/gene1wood/5249118ad566eb349f4b85da2f8c9e8f
    if dest.exists():
        if_modified_since = datetime.fromtimestamp(dest.stat().st_mtime, tz=timezone.utc)
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
                logger.debug(f"Fetching {url} because Last-Modified is {dt.isoformat()} and the local file {dest} mtime is {if_modified_since.isoformat()}")
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


def fetch_with_size_check(url, dest):
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


def get_latest_github_release(repo):
    logger.debug(f"Checking latest GitHub release for {repo}")
    r = requests.head(f"{repo}/releases/latest", allow_redirects=False)
    r.raise_for_status()
    tag = r.headers["Location"].rstrip("/").split("/")[-1]
    logger.debug(f"Latest tag for {repo} is {tag}")
    return tag


def parse_args():
    parser = argparse.ArgumentParser(
        description="Update a local APT repository from remote sources"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (use -vv for debug output)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)

    config = load_config()
    repo_dir = Path(config["apt_repo_directory"])
    repo_dir.mkdir(parents=True, exist_ok=True)

    rebuild = False
    env = platform.freedesktop_os_release()

    for gh in config.get("github_releases", []):
        tag = get_latest_github_release(gh["repo"])
        version = tag.lstrip("v")
        filename = gh["filename"].format(APP_VERSION=version, **env)
        url = f'{gh["repo"]}/releases/download/{tag}/{filename}'

        if fetch_if_modified_since(url, repo_dir / filename):
            rebuild = True

    for fx in config.get("fixed_urls", []):
        base_url = fx["url"].format(**env)
        filename = fx["filename"].format(**env)
        url = f"{base_url}/{filename}"
        dest = repo_dir / filename

        mode = fx.get("conditional", "last_modified")
        logger.debug(f"Processing {url} (mode={mode})")

        if mode == "last_modified":
            changed = fetch_if_modified_since(url, dest)
        elif mode == "last_modified_local_compare":
            changed = fetch_head_local_compare(url, dest)
        elif mode == "content_length":
            changed = fetch_with_size_check(url, dest)
        else:
            raise ValueError(f"Unknown conditional mode: {mode}")

        if changed:
            rebuild = True

    if (
        rebuild
        or not (repo_dir / "Packages").exists()
        or not (repo_dir / "Release").exists()
    ):
        logger.debug(f"Rebuilding APT repository metadata in {repo_dir}")

        with open(repo_dir / "Packages", "w") as packages_file:
            subprocess.check_call(
                ["apt-ftparchive", "packages", "."],
                cwd=repo_dir,
                stdout=packages_file,
            )

        with open(repo_dir / "Release", "w") as release_file:
            subprocess.check_call(
                ["apt-ftparchive", "release", "."],
                cwd=repo_dir,
                stdout=release_file,
            )

        logger.info(f"Local APT repo {repo_dir} rebuilt")


if __name__ == "__main__":
    main()
