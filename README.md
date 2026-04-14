# local-apt-repo-updater

`local-apt-repo-updater` is a local APT repository updater.
It conditionally downloads .deb packages from upstream sources, maintains a local APT repository,
and only rebuilds repository metadata when packages actually change.

---

## Features

- Conditional downloads using If-Modified-Since
- Size-based comparison fallback for servers without Last-Modified headers
- Declarative YAML configuration
- Rebuilds APT metadata only when required

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

### Example configuration

```yaml
apt_repo_directory: /path/to/apt/repo
github_releases:
- repo: https://github.com/example/project
  filename: example-{APP_VERSION}.deb
fixed_urls:
- url: https://example.com/downloads
  filename: example.deb
  conditional: last_modified
- url: https://no-lastmodified-headers.example.net/linux/repo/ubuntu_{VERSION_ID}/amd64
  filename: example_{VERSION_ID}_amd64.deb
  conditional: content_length
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

# TODO

```shell
https://prod.download.desktop.kiro.dev/stable/metadata-linux-x64-deb-stable.json
https://prod.download.desktop.kiro.dev/releases/stable/linux-x64/signed/0.9.2/deb/kiro-ide-0.9.2-stable-linux-x64.deb

```