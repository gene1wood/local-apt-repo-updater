import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
import yaml
from pathlib import Path
from typing import ClassVar

from platformdirs import user_config_dir

from local_apt_repo_updater import (
    github_releases,
    fixed_urls,
    apache_directory,
    signing,
)

logger = logging.getLogger(__name__)

DEFAULT_APT_HOOK_PATH = Path("/etc/apt/apt.conf.d/99local-apt-repo-updater")
DEFAULT_SOURCES_LIST_PATH = Path("/etc/apt/sources.list.d/local-apt-repo-updater.list")
DEFAULT_APT_REPO_DIR = Path("/srv/local-apt-repo")
DEFAULT_GNUPG_HOME = Path("/var/lib/local-apt-repo-updater/gnupg")
DEFAULT_KEYRING_PATH = Path("/etc/apt/keyrings/local-apt-repo-updater.gpg")

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


class ColorFormatter(logging.Formatter):
    """
    Log formatter that highlights warnings and errors in color, matching the
    red/yellow apt itself uses to flag problems, so they stand out in
    ``apt update``'s console output when this tool runs as a Pre-Invoke hook.
    """

    _COLORS: ClassVar[dict[int, str]] = {
        logging.WARNING: "\x1b[33m",
        logging.ERROR: "\x1b[31m",
        logging.CRITICAL: "\x1b[31m",
    }
    _RESET = "\x1b[0m"

    def __init__(self, *args, use_color: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = self._COLORS.get(record.levelno)
        return f"{color}{message}{self._RESET}" if self.use_color and color else message


def use_color_output() -> bool:
    """
    :returns: True if log output should be colorized, i.e. stderr is a
        terminal and the user hasn't opted out via ``NO_COLOR``.
    """
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def setup_logging(verbosity: int) -> None:
    level = logging.DEBUG if verbosity >= 1 else logging.INFO

    handler = logging.StreamHandler()
    handler.setFormatter(
        ColorFormatter("%(levelname)s: %(message)s", use_color=use_color_output())
    )
    logging.basicConfig(level=level, handlers=[handler])


def xdg_config_path() -> Path:
    return Path(user_config_dir(appname="local-apt-repo-updater")) / "config.yaml"


def config_d_path() -> Path:
    """
    :returns: The ``config.d`` drop-in directory alongside
        :func:`xdg_config_path`, following the ``name.d/`` convention used by
        ``sudoers.d``, ``cron.d``, and similar.
    """
    return xdg_config_path().parent / "config.d"


def _merge_config_fragment(config: dict, fragment: dict) -> None:
    """
    Merge ``fragment`` into ``config`` in place.

    Provider keys (``github_releases``, ``fixed_urls``, ``apache_directory``)
    are list-valued, so each file's entries are appended to the accumulated
    list rather than replacing it - this is what lets separate config.d files
    each add their own packages without affecting one another. Any other key
    (e.g. ``apt_repo_directory``) is a plain scalar override, last file wins.

    :param config: Accumulated configuration, updated in place.
    :param fragment: Parsed contents of one config.yaml or config.d file.
    """
    for key, value in fragment.items():
        if key in PROVIDERS:
            config.setdefault(key, []).extend(value)
        else:
            config[key] = value


def load_config() -> dict:
    """
    Load config.yaml and merge in any config.d/*.yaml drop-ins, in sorted
    filename order, on top of it.

    :returns: Merged configuration.
    :raises FileNotFoundError: If neither config.yaml nor config.d exists.
    """
    config: dict = {}
    found = False

    main_path = xdg_config_path()
    if main_path.exists():
        logger.debug(f"Loading configuration from {main_path}")
        with open(main_path) as f:
            _merge_config_fragment(config, yaml.safe_load(f) or {})
        found = True

    d_path = config_d_path()
    if d_path.is_dir():
        fragment_paths = sorted(
            p for p in d_path.iterdir() if p.is_file() and p.suffix in (".yaml", ".yml")
        )
        for fragment_path in fragment_paths:
            logger.debug(f"Loading configuration fragment from {fragment_path}")
            with open(fragment_path) as f:
                _merge_config_fragment(config, yaml.safe_load(f) or {})
            found = True

    if not found:
        raise FileNotFoundError(f"No configuration found at {main_path} or {d_path}")

    return config


def detect_executable() -> Path:
    """
    Determine the path to the running ``local-apt-repo-updater`` executable,
    for use inside the generated apt.conf.d hook.

    :returns: Absolute path to the executable.
    :rtype: Path
    """
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.suffix != ".py":
        return argv0

    found = shutil.which("local-apt-repo-updater")
    return Path(found).resolve() if found else argv0


def render_apt_hook(executable: Path) -> str:
    """
    Render the contents of an apt.conf.d snippet that runs ``executable``
    as an ``APT::Update::Pre-Invoke`` hook.

    :param executable: Path to the local-apt-repo-updater executable.
    :returns: File contents for the apt.conf.d snippet.
    :rtype: str
    """
    escaped = str(executable).replace("\\", "\\\\").replace('"', '\\"')
    return (
        "// Managed by `local-apt-repo-updater install-apt-hook`\n"
        "APT::Update::Pre-Invoke {\n"
        f'  "{escaped}";\n'
        "};\n"
    )


def install_apt_hook(hook_path: Path, executable: Path) -> None:
    """
    Write an APT::Update::Pre-Invoke hook that runs ``executable`` before
    every ``apt update``.

    :param hook_path: Destination apt.conf.d file to write.
    :param executable: Path to the local-apt-repo-updater executable.
    """
    content = render_apt_hook(executable)
    try:
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(content)
    except PermissionError:
        raise SystemExit(
            f"Permission denied writing {hook_path}. Re-run as root, e.g.:\n"
            f"  sudo local-apt-repo-updater install-apt-hook"
        )

    print(f"Installed APT Pre-Invoke hook at {hook_path}:\n\n{content}")


class InsecureRepoDirectoryError(Exception):
    """Raised when apt_repo_directory is writable by anyone other than root."""


def check_repo_dir_permissions(repo_dir: Path) -> None:
    """
    Refuse to trust repo_dir if anyone other than root can write to it.

    Anything placed here gets picked up by apt-ftparchive and re-signed with
    this tool's own key on every rebuild, so write access to this directory
    is equivalent to write access to the signed repo itself. This only
    inspects repo_dir's own owner and mode, not its ancestor directories or
    any POSIX ACLs.

    :param repo_dir: Local APT repository directory.
    :raises InsecureRepoDirectoryError: If the directory isn't owned by root,
        or is group- or world-writable.
    """
    st = repo_dir.stat()
    if st.st_uid != 0:
        raise InsecureRepoDirectoryError(
            f"{repo_dir} is owned by uid {st.st_uid}, not root: refusing to "
            f"trust it. Fix with: sudo chown root {repo_dir}"
        )
    if st.st_mode & 0o022:
        raise InsecureRepoDirectoryError(
            f"{repo_dir} is group- or world-writable (mode "
            f"{oct(st.st_mode & 0o777)}): refusing to trust it. Fix with: "
            f"sudo chmod go-w {repo_dir}"
        )


def resolve_apt_repo_dir() -> Path:
    """
    Determine the configured local APT repo directory without requiring
    config.yaml to exist, for use by install commands that only need this
    one setting.

    :returns: ``apt_repo_directory`` from config.yaml if present, otherwise
        :data:`DEFAULT_APT_REPO_DIR`.
    """
    try:
        config = load_config()
    except FileNotFoundError:
        return DEFAULT_APT_REPO_DIR
    return Path(config.get("apt_repo_directory") or DEFAULT_APT_REPO_DIR)


def render_sources_list(repo_dir: Path, keyring_path: Path) -> str:
    """
    Render an apt sources.list.d entry pointing at the local APT repo, trusted
    via its dedicated signing key rather than ``[trusted=yes]``.

    :param repo_dir: Local APT repository directory.
    :param keyring_path: Path to the repo's exported public signing key.
    :returns: File contents for the sources.list.d entry.
    :rtype: str
    """
    return f"deb [signed-by={keyring_path}] file://{repo_dir} ./\n"


def install_sources_list(list_path: Path, repo_dir: Path, keyring_path: Path) -> None:
    """
    Write a sources.list.d entry pointing apt at the local APT repo, creating
    the repo's signing key if it doesn't exist yet and exporting its public
    half to ``keyring_path`` so apt can verify the repo via ``signed-by=``.

    :param list_path: Destination sources.list.d file to write.
    :param repo_dir: Local APT repository directory.
    :param keyring_path: Path to export the repo's public signing key to.
    """
    if repo_dir.exists():
        try:
            check_repo_dir_permissions(repo_dir)
        except InsecureRepoDirectoryError as e:
            raise SystemExit(str(e))

    signing.ensure_signing_key(DEFAULT_GNUPG_HOME)
    signing.export_public_key(DEFAULT_GNUPG_HOME, keyring_path)

    content = render_sources_list(repo_dir, keyring_path)
    try:
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text(content)
    except PermissionError:
        raise SystemExit(
            f"Permission denied writing {list_path}. Re-run as root, e.g.:\n"
            f"  sudo local-apt-repo-updater install-sources-list"
        )

    print(f"Installed APT sources.list.d entry at {list_path}:\n\n{content}")


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

    subparsers = parser.add_subparsers(dest="command")
    install_parser = subparsers.add_parser(
        "install-apt-hook",
        help="Install an APT::Update::Pre-Invoke hook that runs this tool "
        "before every `apt update`",
    )
    install_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_APT_HOOK_PATH,
        help=f"apt.conf.d file to write (default: {DEFAULT_APT_HOOK_PATH})",
    )
    install_parser.add_argument(
        "--executable",
        type=Path,
        default=None,
        help="Path to the local-apt-repo-updater executable to invoke from "
        "the hook (default: auto-detected)",
    )

    sources_parser = subparsers.add_parser(
        "install-sources-list",
        help="Install an apt sources.list.d entry pointing at the local APT repo",
    )
    sources_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_SOURCES_LIST_PATH,
        help=f"sources.list.d file to write (default: {DEFAULT_SOURCES_LIST_PATH})",
    )
    sources_parser.add_argument(
        "--repo-dir",
        type=Path,
        default=None,
        help="Local APT repo directory to point at (default: this machine's "
        "configured apt_repo_directory, or "
        f"{DEFAULT_APT_REPO_DIR} if unset)",
    )
    sources_parser.add_argument(
        "--keyring",
        type=Path,
        default=DEFAULT_KEYRING_PATH,
        help="Path to export the repo's public signing key to "
        f"(default: {DEFAULT_KEYRING_PATH})",
    )

    return parser.parse_args()


def run_update(config: dict) -> None:
    """
    Fetch packages from every configured provider and rebuild the local APT
    repository's metadata if anything changed.

    :param config: Parsed config.yaml contents.
    """
    repo_dir = Path(config.get("apt_repo_directory") or DEFAULT_APT_REPO_DIR)
    just_created = not repo_dir.exists()
    repo_dir.mkdir(parents=True, exist_ok=True)
    if just_created:
        repo_dir.chmod(0o755)  # don't inherit a hostile umask on first creation

    check_repo_dir_permissions(repo_dir)

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
        or not (repo_dir / "InRelease").exists()
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

        signing.ensure_signing_key(DEFAULT_GNUPG_HOME)
        signing.sign_release(DEFAULT_GNUPG_HOME, repo_dir)
        signing.export_public_key(DEFAULT_GNUPG_HOME, DEFAULT_KEYRING_PATH)

        logger.info(f"Local APT repo {repo_dir} rebuilt and signed")


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    if args.command == "install-apt-hook":
        install_apt_hook(args.path, args.executable or detect_executable())
        return

    if args.command == "install-sources-list":
        install_sources_list(
            args.path, args.repo_dir or resolve_apt_repo_dir(), args.keyring
        )
        return

    # Runs as an APT::Update::Pre-Invoke hook, so a non-zero exit here would
    # abort the whole `apt update`. Every failure below is logged instead of
    # raised, so a problem with one package source never blocks apt.
    try:
        run_update(load_config())
    except Exception:
        logger.exception(
            "local-apt-repo-updater failed; leaving the local APT repo as-is"
        )


if __name__ == "__main__":
    main()
