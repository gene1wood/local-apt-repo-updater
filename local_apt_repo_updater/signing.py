import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BATCH_KEY_PARAMS = (
    "%no-protection\n"
    "Key-Type: RSA\n"
    "Key-Length: 4096\n"
    "Name-Real: local-apt-repo-updater\n"
    "Name-Comment: local APT repository signing key\n"
    "Name-Email: local-apt-repo-updater@localhost\n"
    "Expire-Date: 0\n"
    "%commit\n"
)


def _has_secret_key(gnupg_home: Path) -> bool:
    result = subprocess.run(
        ["gpg", "--homedir", str(gnupg_home), "--with-colons", "--list-secret-keys"],
        capture_output=True,
        text=True,
        check=False,
    )
    return any(line.startswith("sec:") for line in result.stdout.splitlines())


def ensure_signing_key(gnupg_home: Path) -> None:
    """
    Generate a dedicated, passphrase-less GPG key for signing the local APT
    repo, unless one already exists in ``gnupg_home``.

    The key never expires and has no passphrase, since it must be usable
    unattended from an APT::Update::Pre-Invoke hook. ``gnupg_home`` is
    expected to hold exactly this one key; its secrecy is enforced purely by
    filesystem permissions (mode 0700), not a passphrase.

    :param gnupg_home: Dedicated GNUPGHOME directory for the signing key.
    """
    gnupg_home.mkdir(parents=True, exist_ok=True)
    gnupg_home.chmod(0o700)

    if _has_secret_key(gnupg_home):
        return

    logger.info(f"Generating a new APT repo signing key in {gnupg_home}")
    subprocess.run(
        ["gpg", "--homedir", str(gnupg_home), "--batch", "--gen-key"],
        input=_BATCH_KEY_PARAMS,
        text=True,
        check=True,
        capture_output=True,
    )


def export_public_key(gnupg_home: Path, dest: Path) -> None:
    """
    Export the signing key's public half as a binary keyring, for use with
    apt's ``signed-by=`` sources.list option.

    :param gnupg_home: Dedicated GNUPGHOME directory holding the signing key.
    :param dest: Path to write the exported public keyring to.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        ["gpg", "--homedir", str(gnupg_home), "--output", str(dest), "--export"]
    )


def sign_release(gnupg_home: Path, repo_dir: Path) -> None:
    """
    GPG-sign a freshly rebuilt Release file: a clear-signed InRelease for
    modern apt, and a detached Release.gpg for older apt versions.

    :param gnupg_home: Dedicated GNUPGHOME directory holding the signing key.
    :param repo_dir: Local APT repository directory containing Release.
    """
    release = repo_dir / "Release"
    subprocess.check_call(
        [
            "gpg",
            "--homedir",
            str(gnupg_home),
            "--batch",
            "--yes",
            "--digest-algo",
            "SHA512",
            "--clearsign",
            "--output",
            str(repo_dir / "InRelease"),
            str(release),
        ]
    )
    subprocess.check_call(
        [
            "gpg",
            "--homedir",
            str(gnupg_home),
            "--batch",
            "--yes",
            "--digest-algo",
            "SHA512",
            "--detach-sign",
            "--output",
            str(repo_dir / "Release.gpg"),
            str(release),
        ]
    )
