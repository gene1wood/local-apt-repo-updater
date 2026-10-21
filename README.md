# local-apt-repo-updater

`local-apt-repo-updater` is a local APT repository updater.
It conditionally downloads .deb packages from upstream sources, maintains a local APT repository,
and only rebuilds repository metadata when packages actually change.

---

## Features

- Three package sources: GitHub release assets, fixed URLs, and Apache-style directory listings
- Conditional downloads using If-Modified-Since, with a Last-Modified/local-mtime comparison and a
  Content-Length comparison as fallbacks for servers that don't support it
- Debian-version-aware selection of the newest `.deb` from a directory listing
- Declarative YAML configuration
- Rebuilds APT metadata only when a package actually changed

---

## Installation

`pip install local-apt-repo-updater`

---

## Configuration

### Location

The configuration file must be located at:

`$XDG_CONFIG_HOME/local-apt-repo-updater/config.yaml`

If XDG_CONFIG_HOME is not set:

`~/.config/local-apt-repo-updater/config.yaml`

### Drop-in files

Package entries can also be split across separate files in a `config.d` directory next to
`config.yaml`, e.g. `~/.config/local-apt-repo-updater/config.d/`, following the same `name.d/`
convention as `sudoers.d` or `cron.d`. `config.yaml` itself is optional once `config.d` is in use.

Every `*.yaml`/`*.yml` file directly inside `config.d` is loaded, in sorted filename order, after
`config.yaml`. Each file is a normal config document using the same `github_releases`,
`fixed_urls`, and `apache_directory` keys as `config.yaml`. Entries from every file are appended
together, so one file's packages never affect another's. This makes it easy to keep, say, each
package's configuration in its own file (`10-example.yaml`, `20-cinc.yaml`, ...), add or remove one
without touching the rest, or drop a file in from a package's own install step. Scalar settings
like `apt_repo_directory` are plain overrides instead: if set in more than one file, the
last one loaded (by sorted filename) wins.

---

### Template variables

The `filename` (and `url` for `fixed_urls`) fields are Python format strings. The following variables are available:

**`github_releases`**

| Variable | Description | Example |
|---|---|---|
| `{APP_VERSION}` | Release version, with any leading `v` stripped | `1.2.3` |
| + all OS variables below | | |

When the expanded `filename` value contains `*` or `?` wildcards, the release asset
list is fetched from the GitHub API and the pattern is matched against it to discover
the real filename at runtime.  This handles releases where part of the filename is
unpredictable (e.g. a build counter).  Example:
`project_{APP_VERSION}-*_amd64.deb`

**`github_releases` options**

| Field | Default | Description |
|---|---|---|
| `use_latest_tag` | `true` | When `false`, ignores GitHub's "latest" pointer and instead scans releases to find the most recent one whose tag contains no pre-release indicators (alpha, beta, rc, dev, etc.). Useful when a repo habitually marks pre-releases as "latest". |

**`apache_directory`**

| Variable | Description |
|---|---|
| `{APP_VERSION}` | Only available when `version_url` is set. See below. |
| + all OS variables below | |

Fetches the newest version of a package from an Apache directory listing. The `filename` field is
a glob pattern, or a list of glob patterns, matched against the `.deb` files found at `url`; for
each pattern, all matching files are compared using proper Debian version ordering and the newest
one is downloaded. Epoch, upstream version, Debian revision, and tilde pre-release markers (`~`)
are all handled correctly.

**`apache_directory` options**

| Field | Description |
|---|---|
| `version_url` | Apache directory listing URL whose subdirectory names are version strings (e.g. a listing of per-release download directories). The newest version found there is resolved and substituted as `{APP_VERSION}` into `url` and `filename` before fetching. |

**`fixed_urls`**

| Variable | Description |
|---|---|
| + all OS variables below | |

**`fixed_urls` options**

| Field | Default | Description |
|---|---|---|
| `conditional` | `last_modified` | Strategy used to decide whether to re-download the file. See table below. |

| `conditional` value | Strategy |
|---|---|
| `last_modified` | Sends an `If-Modified-Since` header with the GET request. The download is skipped if the server responds with `304 Not Modified`. Requires the server to honour conditional requests. |
| `last_modified_local_compare` | Issues a HEAD request first and compares the `Last-Modified` response header against the local file's mtime. The download is skipped if the remote file is not newer. Use this when the server returns a `Last-Modified` header but does not honour `If-Modified-Since` (e.g. some CDNs always return `200`). |
| `content_length` | Issues a HEAD request first and compares the `Content-Length` response header against the local file size. The download is skipped if the sizes match. Use this when the server provides neither `304` support nor a reliable `Last-Modified` header. |

**OS variables** (all keys from [`platform.freedesktop_os_release()`](https://docs.python.org/3/library/platform.html#platform.freedesktop_os_release))

| Variable | Description | Ubuntu example |
|---|---|---|
| `{VERSION_ID}` | OS version number | `24.04` |
| `{VERSION_CODENAME}` | OS release codename | `noble` |
| `{ID}` | OS identifier | `ubuntu` |
| `{NAME}` | OS name | `Ubuntu` |
| `{PRETTY_NAME}` | Full OS name and version | `Ubuntu 24.04 LTS` |

Other keys from `os-release` (e.g. `ID_LIKE`, `UBUNTU_CODENAME`) are also available if present on the system.

---

### Example configuration

`apt_repo_directory` defaults to `/srv/local-apt-repo` if omitted.

```yaml
apt_repo_directory: /path/to/apt/repo
github_releases:
- repo: https://github.com/example/project
  filename: example-{APP_VERSION}.deb
- repo: https://github.com/example/unpredictable-filename-project
  filename: "project_{APP_VERSION}-*_amd64.deb"
fixed_urls:
- url: https://example.com/downloads
  filename: example.deb
  conditional: last_modified
- url: https://no-lastmodified-headers.example.net/linux/repo/ubuntu_{VERSION_ID}/amd64
  filename: example_{VERSION_ID}_amd64.deb
  conditional: content_length
apache_directory:
- url: https://repo.protonvpn.com/debian/dists/stable/main/binary-all/
  filename: "protonvpn-stable-release_*_all.deb"
```

---

## Usage

`local-apt-repo-updater`

or

`local-apt-repo-updater --verbose`

---

### Running automatically before `apt update`

`local-apt-repo-updater` can install itself as an [`APT::Update::Pre-Invoke`](https://manpages.ubuntu.com/manpages/noble/man5/apt.conf.5.html)
hook so that it runs (and the local repo's metadata is refreshed) every time `apt update` runs,
and can also add the `sources.list.d` entry apt needs to actually consume that repo:

```bash
sudo local-apt-repo-updater install-apt-hook
sudo local-apt-repo-updater install-sources-list
```

`install-apt-hook` writes a snippet like the following to
`/etc/apt/apt.conf.d/99local-apt-repo-updater`:

```
// Managed by `local-apt-repo-updater install-apt-hook`
APT::Update::Pre-Invoke {
  "/root/.local/bin/local-apt-repo-updater";
};
```

The executable path is auto-detected; pass `--executable /path/to/local-apt-repo-updater` to
override it, or `--path` to write the snippet somewhere other than the default apt.conf.d filename.

`install-sources-list` writes a snippet like the following to
`/etc/apt/sources.list.d/local-apt-repo-updater.list`:

```
deb [signed-by=/etc/apt/keyrings/local-apt-repo-updater.gpg] file:///srv/local-apt-repo ./
```

It points at `apt_repo_directory` from config.yaml if that's set, otherwise at the same
`/srv/local-apt-repo` default the updater itself falls back to; pass `--repo-dir` to override it,
`--keyring` to export the signing key somewhere other than the default, or `--path` to write the
entry somewhere other than the default filename. Running it doesn't require config.yaml to exist
yet.

Both commands must be run as root (e.g. via `sudo`), since `/etc/apt/` is not writable otherwise.

A few things to check for this to work correctly:

- **Config file location**: `apt update` normally runs as root, and `sudo` resets `$HOME` to
  `/root` by default, so the config (and any `config.d` drop-ins) must live under
  `/root/.config/local-apt-repo-updater/` (or wherever `$XDG_CONFIG_HOME` resolves to for root),
  not the invoking user's own config directory.
- **`apt-ftparchive`/`gpg` on root's `PATH`**: the hook runs with whatever `PATH` the `apt update`
  invocation has, which may be more restrictive than an interactive login shell's `PATH` (e.g. a
  `sudoers` `secure_path`, or a cron/unattended-upgrades environment). Confirm `apt-ftparchive` and
  `gpg` are both reachable from there, not just from your own shell.
- **Signed, not `[trusted=yes]`, repository**: this tool generates a dedicated, passphrase-less
  GPG key the first time it's needed (in `/var/lib/local-apt-repo-updater/gnupg`, mode `0700`, with
  no passphrase since it must sign unattended from the Pre-Invoke hook; its secrecy comes entirely
  from that directory's permissions, not a passphrase) and uses it to produce a real `InRelease` /
  `Release.gpg` on every rebuild. `install-sources-list` exports the matching public key and points
  the sources.list entry at it via `signed-by=`, so `apt update` cryptographically verifies the
  repo instead of trusting it unconditionally. This protects against corruption or tampering of the
  repo directory by anything that doesn't hold that key; it does **not** verify that the upstream
  `.deb` a provider downloaded is what the vendor actually published (see "Upstream authenticity"
  below for why). If `/var/lib/local-apt-repo-updater/gnupg` is ever lost, a new key is generated and
  re-exported automatically on the next rebuild (or the next `install-sources-list` run); no manual
  re-trust step needed, since `signed-by=` just reads whatever key is currently at that path.
- **`apt_repo_directory` must only be writable by root**: since this tool re-signs whatever
  `.deb` files it finds there on every rebuild, write access to that directory is equivalent to
  write access to the signed repo. Anyone who can drop a file in gets it legitimately signed on
  the next run. Both the normal run and `install-sources-list` refuse to proceed if the directory
  (once it exists) isn't owned by root or is group-/world-writable, raising a clear error instead
  of silently trusting it. This only checks the directory itself, not its ancestors or POSIX ACLs,
  so it's still worth confirming nothing above it in the path is writable by anyone untrusted. A
  directory this tool creates itself on first run is always `0755` root-owned regardless of your
  umask.
- **A failed fetch never aborts `apt update`**: when run as the Pre-Invoke hook, this tool always
  exits `0`, even if a provider couldn't reach its upstream (network blip, GitHub rate limiting,
  etc.) or `config.yaml` itself is missing or invalid. Problems are logged as errors instead of
  raised, so one broken package source never blocks `apt update` for everything else, and
  previously-fetched packages are left in place rather than removed. On an interactive terminal
  those error/warning lines print in red/yellow so they stand out in `apt update`'s own console
  output; set `NO_COLOR=1` to disable that. Run `local-apt-repo-updater -v` directly (or check
  wherever `apt update`'s output is logged, e.g. for unattended-upgrades) to see what actually
  failed.

#### Upstream authenticity

Signing the local repo (above) only proves the `Packages`/`Release`/`.deb` files apt reads weren't
changed after this tool wrote them. It says nothing about whether the bytes a provider downloaded
match what the upstream vendor actually published, because none of the three sources this tool
pulls from give it anything to verify that against:

- **`github_releases`**: GitHub doesn't sign release assets. Some API responses include a
  GitHub-computed `sha256` digest, which only proves the file wasn't corrupted in transit, not that
  it's what the maintainer intended to publish. A handful of projects additionally publish a
  detached `.asc`/`.sig` or checksum file as a separate release asset, but that's per-project, not
  something this tool can check generically.
- **`fixed_urls`**: entirely vendor-dependent; assume nothing is verifiable unless you know
  otherwise for your specific source.
- **`apache_directory`**: just an HTML directory listing, no signatures at all. If an entry here
  is actually mirroring a real signed apt repo (as opposed to a bare `.deb` dump), the better fix
  is to skip this tool for that source and add the vendor's real apt repo (with their real signing
  key) directly instead.

All fetches go over HTTPS with normal TLS verification (nothing in this codebase disables it), so
each download is at least protected against being altered in transit from the host it names.
Beyond that, upstream authenticity isn't verified today.

---

## Requirements

- Python 3.10+
- apt-ftparchive in PATH
- gpg (GnuPG) in PATH
- Python packages:
  - requests
  - PyYAML
  - platformdirs

---

## Design notes

- Uses HTTP HEAD and conditional GETs to minimize bandwidth
- Signs its own repository metadata with a dedicated, auto-generated GPG key rather than relying
  on `[trusted=yes]`; see "Running automatically before `apt update`" above
