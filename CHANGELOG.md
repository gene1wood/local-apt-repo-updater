# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0]

### Added

- `install-apt-hook` subcommand: writes an `APT::Update::Pre-Invoke` snippet to
  `/etc/apt/apt.conf.d/` so the local repo is refreshed automatically before every `apt update`.
- `install-sources-list` subcommand: writes a `sources.list.d` entry pointing at the local repo,
  trusted via a `signed-by=` keyring rather than `[trusted=yes]`, so apt actually consumes it.
- The local repo is now GPG-signed: a dedicated, passphrase-less signing key is generated
  automatically (in `/var/lib/local-apt-repo-updater/gnupg`) and used to produce a real
  `InRelease`/`Release.gpg` on every rebuild, with the public half exported to
  `/etc/apt/keyrings/local-apt-repo-updater.gpg` for `install-sources-list`'s `signed-by=` entry.
- `apt_repo_directory` now defaults to `/srv/local-apt-repo` when omitted from config.yaml.
- Colorized (red/yellow) warning/error log output when attached to a terminal; respects `NO_COLOR`.
- Refuses to run, or to install a sources.list entry, if `apt_repo_directory` exists but isn't
  owned by root or is group-/world-writable — anything writable by someone else would get
  legitimately GPG-signed on the next rebuild.

### Changed

- A failed package fetch, or any other error during a normal run, is now logged instead of
  raised: one broken provider entry no longer stops the others, and the process always exits `0`
  so it never aborts `apt update` when running as its Pre-Invoke hook.

## [1.0.0]

### Added

- `github_releases` provider: wildcard filename matching against release assets, and a
  `use_latest_tag` option to skip pre-release "latest" pointers.
- `apache_directory` provider: fetch the newest `.deb` from an Apache directory listing,
  with Debian-version-aware comparison, an optional `version_url` for resolving
  `{APP_VERSION}` from a separate version-listing page, and support for matching
  multiple filename patterns per entry.
- `fixed_urls` provider: `last_modified_local_compare` and `content_length` conditional
  strategies for servers that don't support `If-Modified-Since`.
