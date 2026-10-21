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

## Requirements

- Python 3.10+
- apt-ftparchive in PATH
- Python packages:
  - requests
  - PyYAML
  - platformdirs

---

## Design notes

- Uses HTTP HEAD and conditional GETs to minimize bandwidth
