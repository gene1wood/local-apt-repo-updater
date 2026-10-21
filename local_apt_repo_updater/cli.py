import argparse
import logging
import platform
import subprocess
import yaml
from pathlib import Path
from platformdirs import user_config_dir

from local_apt_repo_updater import github_releases, fixed_urls, apache_directory

logger = logging.getLogger(__name__)

# Registry of package origin providers.
# Each module must define:
#   CONFIG_KEY: str
#   fetch_packages(entries: list[dict], repo_dir: Path, env: dict) -> bool
#
# To add a new provider: import its module above and append it to this list.
PROVIDERS = {
    p.CONFIG_KEY: p.fetch_packages
    for p in [github_releases, fixed_urls, apache_directory]
}


def setup_logging(verbosity: int) -> None:
    if verbosity >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )


def xdg_config_path() -> Path:
    return Path(user_config_dir(appname="local-apt-repo-updater")) / "config.yaml"


def load_config() -> dict:
    logger.debug(f"Loading configuration from {xdg_config_path()}")
    with open(xdg_config_path()) as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
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


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    config = load_config()
    repo_dir = Path(config["apt_repo_directory"])
    repo_dir.mkdir(parents=True, exist_ok=True)

    rebuild = False
    env = platform.freedesktop_os_release()

    for key, fetch_fn in PROVIDERS.items():
        entries = config.get(key, [])
        if entries and fetch_fn(entries, repo_dir, env):
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
