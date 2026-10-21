# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0]

### Added

- `github_releases` provider: wildcard filename matching against release assets, and a
  `use_latest_tag` option to skip pre-release "latest" pointers.
- `apache_directory` provider: fetch the newest `.deb` from an Apache directory listing,
  with Debian-version-aware comparison, an optional `version_url` for resolving
  `{APP_VERSION}` from a separate version-listing page, and support for matching
  multiple filename patterns per entry.
- `fixed_urls` provider: `last_modified_local_compare` and `content_length` conditional
  strategies for servers that don't support `If-Modified-Since`.
