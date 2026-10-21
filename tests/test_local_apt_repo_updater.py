import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from local_apt_repo_updater import (
    cli,
    fetchers,
    fixed_urls,
    github_releases,
    apache_directory,
    signing,
)
from local_apt_repo_updater.cli import main, setup_logging
from local_apt_repo_updater.fetchers import (
    apply_last_modified,
    fetch_head_local_compare,
    fetch_if_modified_since,
    fetch_with_size_check,
    http_date_from_mtime,
)
from local_apt_repo_updater.fixed_urls import fetch_packages as fixed_fetch_packages
from local_apt_repo_updater.github_releases import (
    _is_stable_tag,
    fetch_packages as gh_fetch_packages,
    get_latest_release,
    get_latest_stable_release,
    get_release_assets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_response(status_code=200, headers=None, content=b"package data"):
    """Build a minimal mock requests.Response."""
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers if headers is not None else {}
    r.iter_content = MagicMock(return_value=[content])
    return r


# ---------------------------------------------------------------------------
# fetchers.apply_last_modified
# ---------------------------------------------------------------------------


class TestApplyLastModified:
    def test_no_header_leaves_mtime_unchanged(self, tmp_path):
        f = tmp_path / "pkg.deb"
        f.write_bytes(b"data")
        before = f.stat().st_mtime
        apply_last_modified(make_response(headers={}), f)
        assert f.stat().st_mtime == before

    def test_valid_header_sets_mtime(self, tmp_path):
        f = tmp_path / "pkg.deb"
        f.write_bytes(b"data")
        resp = make_response(headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
        apply_last_modified(resp, f)
        expected_mtime = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        assert f.stat().st_mtime == pytest.approx(expected_mtime, abs=1)

    def test_invalid_header_logs_warning_and_does_not_raise(self, tmp_path, caplog):
        f = tmp_path / "pkg.deb"
        f.write_bytes(b"data")
        resp = make_response(headers={"Last-Modified": "not-a-date"})
        with caplog.at_level(logging.WARNING):
            apply_last_modified(resp, f)
        assert any("Failed to apply Last-Modified" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# fetchers.http_date_from_mtime
# ---------------------------------------------------------------------------


class TestHttpDateFromMtime:
    def test_returns_correct_http_date(self, tmp_path):
        f = tmp_path / "pkg.deb"
        f.write_bytes(b"data")
        # 2024-01-01 00:00:00 UTC is a Monday
        known_mtime = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(f, (known_mtime, known_mtime))
        result = http_date_from_mtime(f)
        assert result == "Mon, 01 Jan 2024 00:00:00 GMT"


# ---------------------------------------------------------------------------
# fetchers.fetch_if_modified_since
# ---------------------------------------------------------------------------


class TestFetchIfModifiedSince:
    def test_no_existing_file_sends_no_conditional_header(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        with patch("local_apt_repo_updater.fetchers.requests.get") as mock_get:
            mock_get.return_value = make_response(200)
            fetch_if_modified_since("http://example.com/pkg.deb", dest)
            headers_sent = mock_get.call_args.kwargs["headers"]
            assert "If-Modified-Since" not in headers_sent

    def test_existing_file_sends_if_modified_since(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"old")
        with patch("local_apt_repo_updater.fetchers.requests.get") as mock_get:
            mock_get.return_value = make_response(304)
            fetch_if_modified_since("http://example.com/pkg.deb", dest)
            headers_sent = mock_get.call_args.kwargs["headers"]
            assert "If-Modified-Since" in headers_sent

    def test_304_returns_false(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"old")
        with patch(
            "local_apt_repo_updater.fetchers.requests.get",
            return_value=make_response(304),
        ):
            assert fetch_if_modified_since("http://example.com/pkg.deb", dest) is False

    def test_200_writes_file_and_returns_true(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        with patch(
            "local_apt_repo_updater.fetchers.requests.get",
            return_value=make_response(200, content=b"new content"),
        ):
            result = fetch_if_modified_since("http://example.com/pkg.deb", dest)
        assert result is True
        assert dest.read_bytes() == b"new content"

    def test_200_applies_last_modified_to_written_file(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        resp = make_response(
            200, headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
        )
        with patch("local_apt_repo_updater.fetchers.requests.get", return_value=resp):
            fetch_if_modified_since("http://example.com/pkg.deb", dest)
        expected = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        assert dest.stat().st_mtime == pytest.approx(expected, abs=1)


# ---------------------------------------------------------------------------
# fetchers.fetch_head_local_compare
# ---------------------------------------------------------------------------


class TestFetchHeadLocalCompare:
    def test_no_existing_file_skips_head_and_fetches(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        with (
            patch("local_apt_repo_updater.fetchers.requests.head") as mock_head,
            patch(
                "local_apt_repo_updater.fetchers.requests.get",
                return_value=make_response(200),
            ),
        ):
            result = fetch_head_local_compare("http://example.com/pkg.deb", dest)
        mock_head.assert_not_called()
        assert result is True

    def test_remote_older_than_local_returns_false(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"data")
        # Local file mtime: 2024-06-01
        local_mtime = datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp()
        os.utime(dest, (local_mtime, local_mtime))
        # Remote Last-Modified: 2024-01-01 (older)
        head_resp = make_response(
            headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
        )
        with (
            patch(
                "local_apt_repo_updater.fetchers.requests.head", return_value=head_resp
            ),
            patch("local_apt_repo_updater.fetchers.requests.get") as mock_get,
        ):
            result = fetch_head_local_compare("http://example.com/pkg.deb", dest)
        mock_get.assert_not_called()
        assert result is False

    def test_remote_newer_than_local_fetches_and_returns_true(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"old")
        # Local file mtime: 2024-01-01
        local_mtime = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(dest, (local_mtime, local_mtime))
        # Remote Last-Modified: 2024-06-01 (newer)
        head_resp = make_response(
            headers={"Last-Modified": "Sat, 01 Jun 2024 00:00:00 GMT"}
        )
        with (
            patch(
                "local_apt_repo_updater.fetchers.requests.head", return_value=head_resp
            ),
            patch(
                "local_apt_repo_updater.fetchers.requests.get",
                return_value=make_response(200),
            ),
        ):
            result = fetch_head_local_compare("http://example.com/pkg.deb", dest)
        assert result is True

    def test_no_last_modified_in_head_fetches_and_returns_true(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"old")
        head_resp = make_response(headers={})
        with (
            patch(
                "local_apt_repo_updater.fetchers.requests.head", return_value=head_resp
            ),
            patch(
                "local_apt_repo_updater.fetchers.requests.get",
                return_value=make_response(200),
            ),
        ):
            result = fetch_head_local_compare("http://example.com/pkg.deb", dest)
        assert result is True


# ---------------------------------------------------------------------------
# fetchers.fetch_with_size_check
# ---------------------------------------------------------------------------


class TestFetchWithSizeCheck:
    def test_no_existing_file_fetches_and_returns_true(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        with (
            patch("local_apt_repo_updater.fetchers.requests.head") as mock_head,
            patch(
                "local_apt_repo_updater.fetchers.requests.get",
                return_value=make_response(200),
            ),
        ):
            result = fetch_with_size_check("http://example.com/pkg.deb", dest)
        mock_head.assert_not_called()
        assert result is True

    def test_content_length_matches_local_size_returns_false(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"exactdata")  # 9 bytes
        head_resp = make_response(headers={"Content-Length": "9"})
        with (
            patch(
                "local_apt_repo_updater.fetchers.requests.head", return_value=head_resp
            ),
            patch("local_apt_repo_updater.fetchers.requests.get") as mock_get,
        ):
            result = fetch_with_size_check("http://example.com/pkg.deb", dest)
        mock_get.assert_not_called()
        assert result is False

    def test_content_length_differs_fetches_and_returns_true(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"old")  # 3 bytes
        head_resp = make_response(headers={"Content-Length": "9999"})
        with (
            patch(
                "local_apt_repo_updater.fetchers.requests.head", return_value=head_resp
            ),
            patch(
                "local_apt_repo_updater.fetchers.requests.get",
                return_value=make_response(200),
            ),
        ):
            result = fetch_with_size_check("http://example.com/pkg.deb", dest)
        assert result is True

    def test_no_content_length_header_fetches_and_returns_true(self, tmp_path):
        dest = tmp_path / "pkg.deb"
        dest.write_bytes(b"old")
        head_resp = make_response(headers={})
        with (
            patch(
                "local_apt_repo_updater.fetchers.requests.head", return_value=head_resp
            ),
            patch(
                "local_apt_repo_updater.fetchers.requests.get",
                return_value=make_response(200),
            ),
        ):
            result = fetch_with_size_check("http://example.com/pkg.deb", dest)
        assert result is True


# ---------------------------------------------------------------------------
# github_releases.get_latest_release
# ---------------------------------------------------------------------------


class TestGetLatestRelease:
    def test_extracts_tag_from_location_header(self):
        resp = make_response(
            302,
            headers={"Location": "https://github.com/owner/repo/releases/tag/v1.2.3"},
        )
        with patch(
            "local_apt_repo_updater.github_releases.requests.head", return_value=resp
        ):
            assert get_latest_release("https://github.com/owner/repo") == "v1.2.3"

    def test_trailing_slash_in_location_stripped(self):
        resp = make_response(
            302,
            headers={"Location": "https://github.com/owner/repo/releases/tag/v1.2.3/"},
        )
        with patch(
            "local_apt_repo_updater.github_releases.requests.head", return_value=resp
        ):
            assert get_latest_release("https://github.com/owner/repo") == "v1.2.3"


# ---------------------------------------------------------------------------
# github_releases.get_release_assets
# ---------------------------------------------------------------------------


class TestGetReleaseAssets:
    def test_builds_correct_api_url(self):
        resp = make_response(200)
        resp.json.return_value = {"assets": []}
        with patch(
            "local_apt_repo_updater.github_releases.requests.get", return_value=resp
        ) as mock_get:
            get_release_assets("https://github.com/owner/repo", "v1.0.0")
            called_url = mock_get.call_args.args[0]
            assert (
                called_url
                == "https://api.github.com/repos/owner/repo/releases/tags/v1.0.0"
            )

    def test_returns_assets_list(self):
        assets = [
            {
                "name": "pkg_1.0_amd64.deb",
                "browser_download_url": "https://example.com/pkg.deb",
            }
        ]
        resp = make_response(200)
        resp.json.return_value = {"assets": assets}
        with patch(
            "local_apt_repo_updater.github_releases.requests.get", return_value=resp
        ):
            assert (
                get_release_assets("https://github.com/owner/repo", "v1.0.0") == assets
            )

    def test_returns_empty_list_when_no_assets_key(self):
        resp = make_response(200)
        resp.json.return_value = {}
        with patch(
            "local_apt_repo_updater.github_releases.requests.get", return_value=resp
        ):
            assert get_release_assets("https://github.com/owner/repo", "v1.0.0") == []


# ---------------------------------------------------------------------------
# github_releases._is_stable_tag
# ---------------------------------------------------------------------------


class TestIsStableTag:
    @pytest.mark.parametrize(
        "tag",
        [
            "v1.2.3",
            "1.2.3",
            "v1.2.3.4",
            "v10.0.0",
            "v1.2.3.post1",
        ],
    )
    def test_stable_tags(self, tag):
        assert _is_stable_tag(tag) is True

    @pytest.mark.parametrize(
        "tag",
        [
            "v1.2.3-alpha",
            "v1.2.3-alpha.1",
            "v1.2.3-alpha1",
            "v1.2.3-beta",
            "v1.2.3-beta.2",
            "v1.2.3-rc1",
            "v1.2.3-rc.1",
            "v1.2.3-dev",
            "v1.2.3-pre",
            "v1.2.3-preview",
            "v1.2.3-nightly",
            "v1.2.3a1",
            "v1.2.3b2",
            "1.2.3.beta.1",
        ],
    )
    def test_prerelease_tags(self, tag):
        assert _is_stable_tag(tag) is False

    @pytest.mark.parametrize(
        "tag",
        [
            "nightly-build",
            "latest",
            "main",
            "",
        ],
    )
    def test_non_version_tags_are_not_stable(self, tag):
        assert _is_stable_tag(tag) is False


# ---------------------------------------------------------------------------
# github_releases.get_latest_stable_release
# ---------------------------------------------------------------------------


class TestGetLatestStableRelease:
    API = "https://api.github.com/repos/owner/repo/releases"

    def _mock_pages(self, pages: list[list[dict]]):
        """Return a side_effect list for requests.get that yields pages."""
        responses = []
        for page in pages:
            r = MagicMock()
            r.json.return_value = page
            responses.append(r)
        return responses

    def _release(self, tag, prerelease=False, draft=False):
        return {"tag_name": tag, "prerelease": prerelease, "draft": draft}

    def test_returns_first_stable_tag(self):
        releases = [
            self._release("v2.0.0-beta"),
            self._release("v1.9.0"),
            self._release("v1.8.0"),
        ]
        with patch(
            "local_apt_repo_updater.github_releases.requests.get",
            return_value=self._mock_pages([releases])[0],
        ):
            result = get_latest_stable_release("https://github.com/owner/repo")
        assert result == "v1.9.0"

    def test_skips_github_prerelease_flag(self):
        releases = [
            self._release("v2.0.0", prerelease=True),
            self._release("v1.9.0"),
        ]
        with patch(
            "local_apt_repo_updater.github_releases.requests.get",
            return_value=self._mock_pages([releases])[0],
        ):
            result = get_latest_stable_release("https://github.com/owner/repo")
        assert result == "v1.9.0"

    def test_skips_draft_releases(self):
        releases = [
            self._release("v2.0.0", draft=True),
            self._release("v1.9.0"),
        ]
        with patch(
            "local_apt_repo_updater.github_releases.requests.get",
            return_value=self._mock_pages([releases])[0],
        ):
            result = get_latest_stable_release("https://github.com/owner/repo")
        assert result == "v1.9.0"

    def test_paginates_until_stable_found(self):
        page1 = [self._release("v2.0.0-alpha"), self._release("v1.9.0-beta")]
        page2 = [self._release("v1.8.0")]
        with patch(
            "local_apt_repo_updater.github_releases.requests.get",
            side_effect=self._mock_pages([page1, page2]),
        ):
            result = get_latest_stable_release("https://github.com/owner/repo")
        assert result == "v1.8.0"

    def test_raises_when_no_stable_release_found(self):
        with patch(
            "local_apt_repo_updater.github_releases.requests.get",
            side_effect=self._mock_pages(
                [
                    [self._release("v1.0.0-alpha")],
                    [],  # empty page signals end
                ]
            ),
        ):
            with pytest.raises(ValueError, match="No stable release found"):
                get_latest_stable_release("https://github.com/owner/repo")

    def test_uses_correct_api_url(self):
        releases = [self._release("v1.0.0")]
        with patch(
            "local_apt_repo_updater.github_releases.requests.get",
            return_value=self._mock_pages([releases])[0],
        ) as mock_get:
            get_latest_stable_release("https://github.com/owner/repo")
            called_url = mock_get.call_args.args[0]
            assert called_url == "https://api.github.com/repos/owner/repo/releases"


# ---------------------------------------------------------------------------
# github_releases.fetch_packages
# ---------------------------------------------------------------------------


class TestGitHubFetchPackages:
    BASE_ENTRY = {
        "repo": "https://github.com/owner/repo",
        "filename": "pkg-{APP_VERSION}.deb",
    }

    def _patch_latest(self, tag="v1.2.3"):
        return patch(
            "local_apt_repo_updater.github_releases.get_latest_release",
            return_value=tag,
        )

    def _patch_fetch(self, changed=False):
        return patch(
            "local_apt_repo_updater.github_releases.fetch_if_modified_since",
            return_value=changed,
        )

    def test_v_prefix_stripped_from_app_version(self, tmp_path):
        with self._patch_latest("v2.0.0"), self._patch_fetch(False) as mock_fetch:
            gh_fetch_packages([self.BASE_ENTRY], tmp_path, {})
            dest_name = mock_fetch.call_args.args[1].name
            assert "2.0.0" in dest_name
            assert "v2.0.0" not in dest_name

    def test_env_vars_expanded_in_filename(self, tmp_path):
        entry = {
            "repo": "https://github.com/owner/repo",
            "filename": "pkg_{APP_VERSION}_{VERSION_ID}.deb",
        }
        with self._patch_latest("v1.0.0"), self._patch_fetch() as mock_fetch:
            gh_fetch_packages([entry], tmp_path, {"VERSION_ID": "22.04"})
            dest_name = mock_fetch.call_args.args[1].name
            assert "22.04" in dest_name

    def test_constructed_url_contains_tag_and_filename(self, tmp_path):
        with self._patch_latest("v1.2.3"), self._patch_fetch() as mock_fetch:
            gh_fetch_packages([self.BASE_ENTRY], tmp_path, {})
            called_url = mock_fetch.call_args.args[0]
            assert "v1.2.3" in called_url
            assert "pkg-1.2.3.deb" in called_url

    def test_returns_false_when_nothing_changed(self, tmp_path):
        with self._patch_latest(), self._patch_fetch(False):
            assert gh_fetch_packages([self.BASE_ENTRY], tmp_path, {}) is False

    def test_returns_true_when_file_changed(self, tmp_path):
        with self._patch_latest(), self._patch_fetch(True):
            assert gh_fetch_packages([self.BASE_ENTRY], tmp_path, {}) is True

    def test_multiple_entries_returns_true_if_any_changed(self, tmp_path):
        entries = [
            self.BASE_ENTRY,
            {**self.BASE_ENTRY, "filename": "other-{APP_VERSION}.deb"},
        ]
        with (
            self._patch_latest(),
            patch(
                "local_apt_repo_updater.github_releases.fetch_if_modified_since",
                side_effect=[False, True],
            ),
        ):
            assert gh_fetch_packages(entries, tmp_path, {}) is True

    def test_multiple_entries_returns_false_if_none_changed(self, tmp_path):
        entries = [
            self.BASE_ENTRY,
            {**self.BASE_ENTRY, "filename": "other-{APP_VERSION}.deb"},
        ]
        with self._patch_latest(), self._patch_fetch(False):
            assert gh_fetch_packages(entries, tmp_path, {}) is False

    def test_one_entry_failing_does_not_block_others(self, tmp_path, caplog):
        entries = [
            self.BASE_ENTRY,
            {**self.BASE_ENTRY, "filename": "other-{APP_VERSION}.deb"},
        ]
        with (
            patch(
                "local_apt_repo_updater.github_releases.get_latest_release",
                side_effect=[ConnectionError("boom"), "v1.2.3"],
            ),
            self._patch_fetch(True),
        ):
            with caplog.at_level(logging.ERROR):
                result = gh_fetch_packages(entries, tmp_path, {})
        assert result is True
        assert any("Failed to fetch release" in m for m in caplog.messages)

    def test_use_latest_tag_true_calls_get_latest_release(self, tmp_path):
        entry = {**self.BASE_ENTRY, "use_latest_tag": True}
        with (
            self._patch_latest("v1.0.0") as mock_latest,
            patch(
                "local_apt_repo_updater.github_releases.get_latest_stable_release"
            ) as mock_stable,
            self._patch_fetch(),
        ):
            gh_fetch_packages([entry], tmp_path, {})
        mock_latest.assert_called_once()
        mock_stable.assert_not_called()

    def test_use_latest_tag_false_calls_get_latest_stable_release(self, tmp_path):
        entry = {**self.BASE_ENTRY, "use_latest_tag": False}
        with (
            patch(
                "local_apt_repo_updater.github_releases.get_latest_release"
            ) as mock_latest,
            patch(
                "local_apt_repo_updater.github_releases.get_latest_stable_release",
                return_value="v1.0.0",
            ) as mock_stable,
            self._patch_fetch(),
        ):
            gh_fetch_packages([entry], tmp_path, {})
        mock_stable.assert_called_once()
        mock_latest.assert_not_called()

    def test_use_latest_tag_defaults_to_true(self, tmp_path):
        # Entry with no use_latest_tag key should use get_latest_release
        with (
            self._patch_latest("v1.0.0") as mock_latest,
            patch(
                "local_apt_repo_updater.github_releases.get_latest_stable_release"
            ) as mock_stable,
            self._patch_fetch(),
        ):
            gh_fetch_packages([self.BASE_ENTRY], tmp_path, {})
        mock_latest.assert_called_once()
        mock_stable.assert_not_called()


# ---------------------------------------------------------------------------
# github_releases.fetch_packages — wildcard filename (glob matching)
# ---------------------------------------------------------------------------


class TestGitHubFetchPackagesGlob:
    REPO = "https://github.com/owner/repo"
    ASSETS = [
        {
            "name": "appimagelauncher_3.0.0-beta-2-gha287.96cb937_amd64.deb",
            "browser_download_url": "https://github.com/owner/repo/releases/download/v3.0.0-beta.2/appimagelauncher_3.0.0-beta-2-gha287.96cb937_amd64.deb",
        },
        {
            "name": "appimagelauncher_3.0.0-beta-2-gha287.96cb937_arm64.deb",
            "browser_download_url": "https://github.com/owner/repo/releases/download/v3.0.0-beta.2/appimagelauncher_3.0.0-beta-2-gha287.96cb937_arm64.deb",
        },
    ]

    def _patch_latest(self, tag="v3.0.0-beta.2"):
        return patch(
            "local_apt_repo_updater.github_releases.get_latest_release",
            return_value=tag,
        )

    def _patch_assets(self, assets=None):
        return patch(
            "local_apt_repo_updater.github_releases.get_release_assets",
            return_value=assets if assets is not None else self.ASSETS,
        )

    def _patch_fetch(self, changed=False):
        return patch(
            "local_apt_repo_updater.github_releases.fetch_if_modified_since",
            return_value=changed,
        )

    def test_wildcard_filename_calls_get_release_assets(self, tmp_path):
        entry = {"repo": self.REPO, "filename": "appimagelauncher_*_amd64.deb"}
        with (
            self._patch_latest(),
            self._patch_assets() as mock_assets,
            self._patch_fetch(),
        ):
            gh_fetch_packages([entry], tmp_path, {})
        mock_assets.assert_called_once()

    def test_non_wildcard_filename_does_not_call_get_release_assets(self, tmp_path):
        entry = {"repo": self.REPO, "filename": "pkg-{APP_VERSION}.deb"}
        with (
            self._patch_latest("v1.0.0"),
            self._patch_assets() as mock_assets,
            self._patch_fetch(),
        ):
            gh_fetch_packages([entry], tmp_path, {})
        mock_assets.assert_not_called()

    def test_uses_matched_asset_filename(self, tmp_path):
        entry = {"repo": self.REPO, "filename": "appimagelauncher_*_amd64.deb"}
        with (
            self._patch_latest(),
            self._patch_assets(),
            self._patch_fetch() as mock_fetch,
        ):
            gh_fetch_packages([entry], tmp_path, {})
        dest_name = mock_fetch.call_args.args[1].name
        assert dest_name == "appimagelauncher_3.0.0-beta-2-gha287.96cb937_amd64.deb"

    def test_uses_asset_browser_download_url(self, tmp_path):
        entry = {"repo": self.REPO, "filename": "appimagelauncher_*_amd64.deb"}
        with (
            self._patch_latest(),
            self._patch_assets(),
            self._patch_fetch() as mock_fetch,
        ):
            gh_fetch_packages([entry], tmp_path, {})
        called_url = mock_fetch.call_args.args[0]
        assert called_url == self.ASSETS[0]["browser_download_url"]

    def test_app_version_expanded_before_matching(self, tmp_path):
        assets = [
            {
                "name": "pkg_1.2.3-build42_amd64.deb",
                "browser_download_url": "https://example.com/pkg.deb",
            }
        ]
        entry = {"repo": self.REPO, "filename": "pkg_{APP_VERSION}-*_amd64.deb"}
        with (
            self._patch_latest("v1.2.3"),
            self._patch_assets(assets),
            self._patch_fetch() as mock_fetch,
        ):
            gh_fetch_packages([entry], tmp_path, {})
        dest_name = mock_fetch.call_args.args[1].name
        assert dest_name == "pkg_1.2.3-build42_amd64.deb"

    def test_no_matching_asset_skips_entry_and_warns(self, tmp_path, caplog):
        entry = {"repo": self.REPO, "filename": "appimagelauncher_*_amd64.deb"}
        with (
            self._patch_latest(),
            self._patch_assets([]),
            self._patch_fetch() as mock_fetch,
        ):
            with caplog.at_level(logging.WARNING):
                gh_fetch_packages([entry], tmp_path, {})
        mock_fetch.assert_not_called()
        assert any("No release assets matched" in m for m in caplog.messages)

    def test_multiple_matches_uses_first_and_warns(self, tmp_path, caplog):
        # Both ASSETS entries match "appimagelauncher_*.deb"
        entry = {"repo": self.REPO, "filename": "appimagelauncher_*.deb"}
        with (
            self._patch_latest(),
            self._patch_assets(),
            self._patch_fetch() as mock_fetch,
        ):
            with caplog.at_level(logging.WARNING):
                gh_fetch_packages([entry], tmp_path, {})
        assert mock_fetch.call_count == 1
        assert any("Multiple assets matched" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# fixed_urls.fetch_packages
# ---------------------------------------------------------------------------


class TestFixedUrlsFetchPackages:
    """
    _FETCH_MODES captures function references at import time, so we use
    patch.dict on the dict itself rather than patching individual function names.
    """

    BASE_ENTRY = {"url": "https://example.com/downloads", "filename": "pkg.deb"}

    def test_default_mode_uses_last_modified(self, tmp_path):
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            fixed_fetch_packages([self.BASE_ENTRY], tmp_path, {})
            mock_fn.assert_called_once()

    def test_mode_last_modified(self, tmp_path):
        entry = {**self.BASE_ENTRY, "conditional": "last_modified"}
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            fixed_fetch_packages([entry], tmp_path, {})
            mock_fn.assert_called_once()

    def test_mode_last_modified_local_compare(self, tmp_path):
        entry = {**self.BASE_ENTRY, "conditional": "last_modified_local_compare"}
        mock_fn = MagicMock(return_value=False)
        with patch.dict(
            fixed_urls._FETCH_MODES, {"last_modified_local_compare": mock_fn}
        ):
            fixed_fetch_packages([entry], tmp_path, {})
            mock_fn.assert_called_once()

    def test_mode_content_length(self, tmp_path):
        entry = {**self.BASE_ENTRY, "conditional": "content_length"}
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"content_length": mock_fn}):
            fixed_fetch_packages([entry], tmp_path, {})
            mock_fn.assert_called_once()

    def test_unknown_mode_logs_error_and_does_not_raise(self, tmp_path, caplog):
        entry = {**self.BASE_ENTRY, "conditional": "telepathy"}
        with caplog.at_level(logging.ERROR):
            result = fixed_fetch_packages([entry], tmp_path, {})
        assert result is False
        assert any("Unknown conditional mode" in m for m in caplog.messages)

    def test_env_vars_expanded_in_url_and_filename(self, tmp_path):
        entry = {
            "url": "https://example.com/ubuntu_{VERSION_ID}",
            "filename": "pkg_{VERSION_ID}_amd64.deb",
        }
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            fixed_fetch_packages([entry], tmp_path, {"VERSION_ID": "22.04"})
            called_url, called_dest = mock_fn.call_args.args
            assert "22.04" in called_url
            assert "22.04" in called_dest.name

    def test_url_combines_base_url_and_filename(self, tmp_path):
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            fixed_fetch_packages([self.BASE_ENTRY], tmp_path, {})
            called_url = mock_fn.call_args.args[0]
            assert called_url == "https://example.com/downloads/pkg.deb"

    def test_dest_is_repo_dir_plus_filename(self, tmp_path):
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            fixed_fetch_packages([self.BASE_ENTRY], tmp_path, {})
            called_dest = mock_fn.call_args.args[1]
            assert called_dest == tmp_path / "pkg.deb"

    def test_returns_false_when_nothing_changed(self, tmp_path):
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            assert fixed_fetch_packages([self.BASE_ENTRY], tmp_path, {}) is False

    def test_returns_true_when_file_changed(self, tmp_path):
        mock_fn = MagicMock(return_value=True)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            assert fixed_fetch_packages([self.BASE_ENTRY], tmp_path, {}) is True

    def test_multiple_entries_returns_true_if_any_changed(self, tmp_path):
        entries = [self.BASE_ENTRY, {**self.BASE_ENTRY, "filename": "other.deb"}]
        mock_fn = MagicMock(side_effect=[False, True])
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            assert fixed_fetch_packages(entries, tmp_path, {}) is True

    def test_multiple_entries_returns_false_if_none_changed(self, tmp_path):
        entries = [self.BASE_ENTRY, {**self.BASE_ENTRY, "filename": "other.deb"}]
        mock_fn = MagicMock(return_value=False)
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            assert fixed_fetch_packages(entries, tmp_path, {}) is False

    def test_one_entry_failing_does_not_block_others(self, tmp_path, caplog):
        entries = [self.BASE_ENTRY, {**self.BASE_ENTRY, "filename": "other.deb"}]
        mock_fn = MagicMock(side_effect=[ConnectionError("boom"), True])
        with patch.dict(fixed_urls._FETCH_MODES, {"last_modified": mock_fn}):
            with caplog.at_level(logging.ERROR):
                result = fixed_fetch_packages(entries, tmp_path, {})
        assert result is True
        assert mock_fn.call_count == 2
        assert any("Failed to fetch" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# cli.PROVIDERS registry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    def test_github_releases_key_registered(self):
        assert "github_releases" in cli.PROVIDERS

    def test_fixed_urls_key_registered(self):
        assert "fixed_urls" in cli.PROVIDERS

    def test_provider_values_are_callable(self):
        for key, fn in cli.PROVIDERS.items():
            assert callable(fn), f"PROVIDERS[{key!r}] is not callable"


# ---------------------------------------------------------------------------
# cli.setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def test_verbosity_0_sets_info_level(self):
        with patch("local_apt_repo_updater.cli.logging.basicConfig") as mock_cfg:
            setup_logging(0)
            mock_cfg.assert_called_once()
            assert mock_cfg.call_args.kwargs["level"] == logging.INFO

    def test_verbosity_1_sets_debug_level(self):
        with patch("local_apt_repo_updater.cli.logging.basicConfig") as mock_cfg:
            setup_logging(1)
            assert mock_cfg.call_args.kwargs["level"] == logging.DEBUG

    def test_verbosity_2_sets_debug_level(self):
        with patch("local_apt_repo_updater.cli.logging.basicConfig") as mock_cfg:
            setup_logging(2)
            assert mock_cfg.call_args.kwargs["level"] == logging.DEBUG

    def test_installs_color_formatter_on_handler(self):
        with patch("local_apt_repo_updater.cli.logging.basicConfig") as mock_cfg:
            setup_logging(0)
            handler = mock_cfg.call_args.kwargs["handlers"][0]
            assert isinstance(handler.formatter, cli.ColorFormatter)


# ---------------------------------------------------------------------------
# cli.use_color_output / cli.ColorFormatter
# ---------------------------------------------------------------------------


class TestUseColorOutput:
    def test_true_when_tty_and_no_color_unset(self):
        with (
            patch("local_apt_repo_updater.cli.sys.stderr.isatty", return_value=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("NO_COLOR", None)
            assert cli.use_color_output() is True

    def test_false_when_not_a_tty(self):
        with patch("local_apt_repo_updater.cli.sys.stderr.isatty", return_value=False):
            assert cli.use_color_output() is False

    def test_false_when_no_color_set(self):
        with (
            patch("local_apt_repo_updater.cli.sys.stderr.isatty", return_value=True),
            patch.dict(os.environ, {"NO_COLOR": "1"}),
        ):
            assert cli.use_color_output() is False


class TestColorFormatter:
    def test_error_wrapped_in_red_when_color_enabled(self):
        formatter = cli.ColorFormatter("%(levelname)s: %(message)s", use_color=True)
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "boom", None, None
        )
        result = formatter.format(record)
        assert result.startswith("\x1b[31m")
        assert result.endswith("\x1b[0m")

    def test_warning_wrapped_in_yellow_when_color_enabled(self):
        formatter = cli.ColorFormatter("%(levelname)s: %(message)s", use_color=True)
        record = logging.LogRecord(
            "test", logging.WARNING, __file__, 1, "careful", None, None
        )
        result = formatter.format(record)
        assert result.startswith("\x1b[33m")

    def test_info_not_colored(self):
        formatter = cli.ColorFormatter("%(levelname)s: %(message)s", use_color=True)
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "hello", None, None
        )
        assert formatter.format(record) == "INFO: hello"

    def test_no_escape_codes_when_color_disabled(self):
        formatter = cli.ColorFormatter("%(levelname)s: %(message)s", use_color=False)
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "boom", None, None
        )
        assert formatter.format(record) == "ERROR: boom"


# ---------------------------------------------------------------------------
# cli.parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_default_command_is_none(self):
        with patch("local_apt_repo_updater.cli.sys.argv", ["prog"]):
            args = cli.parse_args()
        assert args.command is None

    def test_install_apt_hook_defaults(self):
        with patch("local_apt_repo_updater.cli.sys.argv", ["prog", "install-apt-hook"]):
            args = cli.parse_args()
        assert args.command == "install-apt-hook"
        assert args.path == cli.DEFAULT_APT_HOOK_PATH
        assert args.executable is None

    def test_install_apt_hook_custom_path_and_executable(self):
        with patch(
            "local_apt_repo_updater.cli.sys.argv",
            [
                "prog",
                "install-apt-hook",
                "--path",
                "/tmp/hook",
                "--executable",
                "/tmp/bin/local-apt-repo-updater",
            ],
        ):
            args = cli.parse_args()
        assert args.path == Path("/tmp/hook")
        assert args.executable == Path("/tmp/bin/local-apt-repo-updater")

    def test_install_sources_list_defaults(self):
        with patch(
            "local_apt_repo_updater.cli.sys.argv",
            ["prog", "install-sources-list"],
        ):
            args = cli.parse_args()
        assert args.command == "install-sources-list"
        assert args.path == cli.DEFAULT_SOURCES_LIST_PATH
        assert args.repo_dir is None
        assert args.keyring == cli.DEFAULT_KEYRING_PATH

    def test_install_sources_list_custom_path_and_repo_dir(self):
        with patch(
            "local_apt_repo_updater.cli.sys.argv",
            [
                "prog",
                "install-sources-list",
                "--path",
                "/tmp/sources.list",
                "--repo-dir",
                "/tmp/repo",
                "--keyring",
                "/tmp/keyring.gpg",
            ],
        ):
            args = cli.parse_args()
        assert args.path == Path("/tmp/sources.list")
        assert args.repo_dir == Path("/tmp/repo")
        assert args.keyring == Path("/tmp/keyring.gpg")


# ---------------------------------------------------------------------------
# cli.detect_executable
# ---------------------------------------------------------------------------


class TestDetectExecutable:
    def test_uses_argv0_when_not_a_python_script(self):
        with patch(
            "local_apt_repo_updater.cli.sys.argv",
            ["/root/.local/bin/local-apt-repo-updater"],
        ):
            assert cli.detect_executable() == Path(
                "/root/.local/bin/local-apt-repo-updater"
            )

    def test_falls_back_to_which_when_argv0_is_a_python_file(self):
        with (
            patch("local_apt_repo_updater.cli.sys.argv", ["cli.py"]),
            patch(
                "local_apt_repo_updater.cli.shutil.which",
                return_value="/usr/local/bin/local-apt-repo-updater",
            ),
        ):
            assert cli.detect_executable() == Path(
                "/usr/local/bin/local-apt-repo-updater"
            )

    def test_falls_back_to_argv0_when_which_finds_nothing(self):
        with (
            patch("local_apt_repo_updater.cli.sys.argv", ["cli.py"]),
            patch("local_apt_repo_updater.cli.shutil.which", return_value=None),
        ):
            assert cli.detect_executable() == Path("cli.py").resolve()


# ---------------------------------------------------------------------------
# cli.render_apt_hook / cli.install_apt_hook
# ---------------------------------------------------------------------------


class TestRenderAptHook:
    def test_includes_pre_invoke_block_and_quoted_path(self):
        content = cli.render_apt_hook(Path("/usr/bin/local-apt-repo-updater"))
        assert "APT::Update::Pre-Invoke {" in content
        assert '"/usr/bin/local-apt-repo-updater";' in content

    def test_escapes_quotes_in_path(self):
        content = cli.render_apt_hook(Path('/opt/weird"path/local-apt-repo-updater'))
        assert '\\"path' in content


class TestInstallAptHook:
    def test_writes_hook_file(self, tmp_path):
        hook_path = tmp_path / "apt.conf.d" / "99local-apt-repo-updater"
        cli.install_apt_hook(hook_path, Path("/usr/bin/local-apt-repo-updater"))
        content = hook_path.read_text()
        assert "/usr/bin/local-apt-repo-updater" in content
        assert "APT::Update::Pre-Invoke" in content

    def test_creates_parent_directories(self, tmp_path):
        hook_path = tmp_path / "nested" / "dir" / "hook"
        cli.install_apt_hook(hook_path, Path("/usr/bin/local-apt-repo-updater"))
        assert hook_path.exists()

    def test_permission_error_raises_systemexit_with_guidance(self, tmp_path):
        hook_path = tmp_path / "hook"
        with patch.object(Path, "write_text", side_effect=PermissionError):
            with pytest.raises(SystemExit, match="sudo local-apt-repo-updater"):
                cli.install_apt_hook(hook_path, Path("/usr/bin/local-apt-repo-updater"))


# ---------------------------------------------------------------------------
# cli.load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    @staticmethod
    def _patch_xdg_config_path(tmp_path):
        return patch(
            "local_apt_repo_updater.cli.xdg_config_path",
            return_value=tmp_path / "config.yaml",
        )

    def test_raises_filenotfounderror_when_nothing_exists(self, tmp_path):
        with self._patch_xdg_config_path(tmp_path):
            with pytest.raises(FileNotFoundError):
                cli.load_config()

    def test_loads_main_config_only(self, tmp_path):
        (tmp_path / "config.yaml").write_text("apt_repo_directory: /data/apt-repo\n")
        with self._patch_xdg_config_path(tmp_path):
            assert cli.load_config() == {"apt_repo_directory": "/data/apt-repo"}

    def test_config_d_without_main_config_works(self, tmp_path):
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "10-example.yaml").write_text(
            "fixed_urls:\n- url: https://example.com\n  filename: example.deb\n"
        )
        with self._patch_xdg_config_path(tmp_path):
            config = cli.load_config()
        assert config == {
            "fixed_urls": [{"url": "https://example.com", "filename": "example.deb"}]
        }

    def test_merges_provider_lists_across_files_without_affecting_each_other(
        self, tmp_path
    ):
        (tmp_path / "config.yaml").write_text(
            "fixed_urls:\n- url: https://main.example.com\n  filename: main.deb\n"
        )
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "10-first.yaml").write_text(
            "fixed_urls:\n- url: https://first.example.com\n  filename: first.deb\n"
        )
        (config_d / "20-second.yaml").write_text(
            "fixed_urls:\n- url: https://second.example.com\n  filename: second.deb\n"
        )
        with self._patch_xdg_config_path(tmp_path):
            config = cli.load_config()
        assert [entry["filename"] for entry in config["fixed_urls"]] == [
            "main.deb",
            "first.deb",
            "second.deb",
        ]

    def test_config_d_loaded_in_sorted_filename_order(self, tmp_path):
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "20-second.yaml").write_text(
            "fixed_urls:\n- url: https://second.example.com\n  filename: second.deb\n"
        )
        (config_d / "10-first.yml").write_text(
            "fixed_urls:\n- url: https://first.example.com\n  filename: first.deb\n"
        )
        with self._patch_xdg_config_path(tmp_path):
            config = cli.load_config()
        assert [entry["filename"] for entry in config["fixed_urls"]] == [
            "first.deb",
            "second.deb",
        ]

    def test_scalar_key_last_loaded_file_wins(self, tmp_path):
        (tmp_path / "config.yaml").write_text("apt_repo_directory: /from/main\n")
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "10-override.yaml").write_text(
            "apt_repo_directory: /from/config.d\n"
        )
        with self._patch_xdg_config_path(tmp_path):
            config = cli.load_config()
        assert config["apt_repo_directory"] == "/from/config.d"

    def test_ignores_non_yaml_files_in_config_d(self, tmp_path):
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "README.md").write_text("not yaml\n")
        (config_d / "10-example.yaml").write_text(
            "fixed_urls:\n- url: https://example.com\n  filename: example.deb\n"
        )
        with self._patch_xdg_config_path(tmp_path):
            config = cli.load_config()
        assert len(config["fixed_urls"]) == 1


# ---------------------------------------------------------------------------
# cli.resolve_apt_repo_dir
# ---------------------------------------------------------------------------


class TestResolveAptRepoDir:
    def test_uses_configured_value(self):
        with patch(
            "local_apt_repo_updater.cli.load_config",
            return_value={"apt_repo_directory": "/data/apt-repo"},
        ):
            assert cli.resolve_apt_repo_dir() == Path("/data/apt-repo")

    def test_falls_back_to_default_when_key_missing(self):
        with patch("local_apt_repo_updater.cli.load_config", return_value={}):
            assert cli.resolve_apt_repo_dir() == cli.DEFAULT_APT_REPO_DIR

    def test_falls_back_to_default_when_config_missing(self):
        with patch(
            "local_apt_repo_updater.cli.load_config", side_effect=FileNotFoundError
        ):
            assert cli.resolve_apt_repo_dir() == cli.DEFAULT_APT_REPO_DIR


# ---------------------------------------------------------------------------
# cli.check_repo_dir_permissions
# ---------------------------------------------------------------------------


class TestCheckRepoDirPermissions:
    @staticmethod
    def _stat(uid, mode):
        return MagicMock(st_uid=uid, st_mode=mode)

    def test_passes_for_root_owned_non_writable_dir(self, tmp_path):
        with patch.object(Path, "stat", return_value=self._stat(0, 0o040755)):
            cli.check_repo_dir_permissions(tmp_path)  # must not raise

    def test_passes_for_root_owned_private_dir(self, tmp_path):
        with patch.object(Path, "stat", return_value=self._stat(0, 0o040700)):
            cli.check_repo_dir_permissions(tmp_path)  # must not raise

    def test_raises_when_not_owned_by_root(self, tmp_path):
        with patch.object(Path, "stat", return_value=self._stat(1000, 0o040755)):
            with pytest.raises(cli.InsecureRepoDirectoryError, match="root"):
                cli.check_repo_dir_permissions(tmp_path)

    def test_raises_when_group_writable(self, tmp_path):
        with patch.object(Path, "stat", return_value=self._stat(0, 0o040775)):
            with pytest.raises(cli.InsecureRepoDirectoryError, match="writable"):
                cli.check_repo_dir_permissions(tmp_path)

    def test_raises_when_world_writable(self, tmp_path):
        with patch.object(Path, "stat", return_value=self._stat(0, 0o040757)):
            with pytest.raises(cli.InsecureRepoDirectoryError, match="writable"):
                cli.check_repo_dir_permissions(tmp_path)


# ---------------------------------------------------------------------------
# cli.render_sources_list / cli.install_sources_list
# ---------------------------------------------------------------------------


class TestRenderSourcesList:
    def test_includes_signed_by_file_uri(self):
        content = cli.render_sources_list(
            Path("/srv/local-apt-repo"), Path("/etc/apt/keyrings/x.gpg")
        )
        assert content == (
            "deb [signed-by=/etc/apt/keyrings/x.gpg] file:///srv/local-apt-repo ./\n"
        )


class TestInstallSourcesList:
    def test_writes_sources_list_file(self, tmp_path):
        list_path = tmp_path / "sources.list.d" / "local-apt-repo-updater.list"
        keyring_path = tmp_path / "keyring.gpg"
        with (
            patch("local_apt_repo_updater.signing.ensure_signing_key"),
            patch("local_apt_repo_updater.signing.export_public_key"),
        ):
            cli.install_sources_list(
                list_path, Path("/srv/local-apt-repo"), keyring_path
            )
        content = list_path.read_text()
        assert "file:///srv/local-apt-repo" in content
        assert f"signed-by={keyring_path}" in content

    def test_creates_parent_directories(self, tmp_path):
        list_path = tmp_path / "nested" / "dir" / "sources.list"
        keyring_path = tmp_path / "keyring.gpg"
        with (
            patch("local_apt_repo_updater.signing.ensure_signing_key"),
            patch("local_apt_repo_updater.signing.export_public_key"),
        ):
            cli.install_sources_list(
                list_path, Path("/srv/local-apt-repo"), keyring_path
            )
        assert list_path.exists()

    def test_ensures_signing_key_and_exports_it(self, tmp_path):
        list_path = tmp_path / "sources.list"
        keyring_path = tmp_path / "keyring.gpg"
        with (
            patch("local_apt_repo_updater.signing.ensure_signing_key") as mock_ensure,
            patch("local_apt_repo_updater.signing.export_public_key") as mock_export,
        ):
            cli.install_sources_list(
                list_path, Path("/srv/local-apt-repo"), keyring_path
            )
        mock_ensure.assert_called_once_with(cli.DEFAULT_GNUPG_HOME)
        mock_export.assert_called_once_with(cli.DEFAULT_GNUPG_HOME, keyring_path)

    def test_permission_error_raises_systemexit_with_guidance(self, tmp_path):
        list_path = tmp_path / "sources.list"
        keyring_path = tmp_path / "keyring.gpg"
        with (
            patch("local_apt_repo_updater.signing.ensure_signing_key"),
            patch("local_apt_repo_updater.signing.export_public_key"),
            patch.object(Path, "write_text", side_effect=PermissionError),
        ):
            with pytest.raises(SystemExit, match="sudo local-apt-repo-updater"):
                cli.install_sources_list(
                    list_path, Path("/srv/local-apt-repo"), keyring_path
                )

    def test_skips_permission_check_when_repo_dir_does_not_exist_yet(self, tmp_path):
        repo_dir = tmp_path / "does-not-exist-yet"
        list_path = tmp_path / "sources.list"
        keyring_path = tmp_path / "keyring.gpg"
        with (
            patch("local_apt_repo_updater.signing.ensure_signing_key"),
            patch("local_apt_repo_updater.signing.export_public_key"),
        ):
            cli.install_sources_list(list_path, repo_dir, keyring_path)  # no raise
        assert list_path.exists()

    def test_refuses_when_existing_repo_dir_is_insecure(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        list_path = tmp_path / "sources.list"
        keyring_path = tmp_path / "keyring.gpg"
        with (
            patch("local_apt_repo_updater.signing.ensure_signing_key"),
            patch("local_apt_repo_updater.signing.export_public_key"),
            patch.object(
                Path, "stat", return_value=MagicMock(st_uid=1000, st_mode=0o040755)
            ),
        ):
            with pytest.raises(SystemExit, match="root"):
                cli.install_sources_list(list_path, repo_dir, keyring_path)
        assert not list_path.exists()


# ---------------------------------------------------------------------------
# signing._has_secret_key
# ---------------------------------------------------------------------------


class TestHasSecretKey:
    def test_true_when_sec_line_present(self, tmp_path):
        with patch(
            "local_apt_repo_updater.signing.subprocess.run",
            return_value=MagicMock(stdout="sec:u:4096:1:ABCDEF0123456789:\n"),
        ):
            assert signing._has_secret_key(tmp_path) is True

    def test_false_when_no_sec_line(self, tmp_path):
        with patch(
            "local_apt_repo_updater.signing.subprocess.run",
            return_value=MagicMock(stdout=""),
        ):
            assert signing._has_secret_key(tmp_path) is False


# ---------------------------------------------------------------------------
# signing.ensure_signing_key
# ---------------------------------------------------------------------------


class TestEnsureSigningKey:
    def test_creates_gnupg_home_with_restrictive_permissions(self, tmp_path):
        gnupg_home = tmp_path / "gnupg"
        with patch("local_apt_repo_updater.signing._has_secret_key", return_value=True):
            signing.ensure_signing_key(gnupg_home)
        assert gnupg_home.is_dir()
        assert (gnupg_home.stat().st_mode & 0o777) == 0o700

    def test_skips_generation_when_key_exists(self, tmp_path):
        gnupg_home = tmp_path / "gnupg"
        with (
            patch("local_apt_repo_updater.signing._has_secret_key", return_value=True),
            patch("local_apt_repo_updater.signing.subprocess.run") as mock_run,
        ):
            signing.ensure_signing_key(gnupg_home)
        mock_run.assert_not_called()

    def test_generates_key_when_missing(self, tmp_path):
        gnupg_home = tmp_path / "gnupg"
        with (
            patch("local_apt_repo_updater.signing._has_secret_key", return_value=False),
            patch("local_apt_repo_updater.signing.subprocess.run") as mock_run,
        ):
            signing.ensure_signing_key(gnupg_home)
        mock_run.assert_called_once()
        called_cmd = mock_run.call_args.args[0]
        assert called_cmd == [
            "gpg",
            "--homedir",
            str(gnupg_home),
            "--batch",
            "--gen-key",
        ]
        assert mock_run.call_args.kwargs["check"] is True
        assert "%no-protection" in mock_run.call_args.kwargs["input"]


# ---------------------------------------------------------------------------
# signing.export_public_key
# ---------------------------------------------------------------------------


class TestExportPublicKey:
    def test_creates_parent_dir_and_calls_gpg_export(self, tmp_path):
        gnupg_home = tmp_path / "gnupg"
        dest = tmp_path / "keyrings" / "repo.gpg"
        with patch("local_apt_repo_updater.signing.subprocess.check_call") as mock_call:
            signing.export_public_key(gnupg_home, dest)
        assert dest.parent.is_dir()
        mock_call.assert_called_once_with(
            ["gpg", "--homedir", str(gnupg_home), "--output", str(dest), "--export"]
        )


# ---------------------------------------------------------------------------
# signing.sign_release
# ---------------------------------------------------------------------------


class TestSignRelease:
    def test_clearsigns_and_detach_signs(self, tmp_path):
        gnupg_home = tmp_path / "gnupg"
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        with patch("local_apt_repo_updater.signing.subprocess.check_call") as mock_call:
            signing.sign_release(gnupg_home, repo_dir)

        commands = [c.args[0] for c in mock_call.call_args_list]
        assert len(commands) == 2

        clearsign_cmd = next(cmd for cmd in commands if "--clearsign" in cmd)
        assert str(repo_dir / "InRelease") in clearsign_cmd
        assert str(repo_dir / "Release") in clearsign_cmd

        detach_cmd = next(cmd for cmd in commands if "--detach-sign" in cmd)
        assert str(repo_dir / "Release.gpg") in detach_cmd
        assert str(repo_dir / "Release") in detach_cmd


# ---------------------------------------------------------------------------
# cli.main
# ---------------------------------------------------------------------------


class TestMain:
    """
    Tests for the main() orchestration function.

    Each test mocks out I/O boundaries: config loading, provider fetch
    functions, subprocess calls, and platform detection.
    """

    def _run_main(self, tmp_path, config_overrides=None, provider_side_effects=None):
        """
        Run main() with a temporary repo dir and controllable provider results.

        :param config_overrides: Extra keys merged into the config dict.
        :param provider_side_effects: Dict mapping provider key → return value.
        :return: The mock for subprocess.check_call.
        """
        config = {"apt_repo_directory": str(tmp_path), **(config_overrides or {})}
        provider_results = {"github_releases": False, "fixed_urls": False}
        provider_results.update(provider_side_effects or {})

        mock_providers = {
            k: MagicMock(return_value=v) for k, v in provider_results.items()
        }
        fake_args = MagicMock(verbose=0)

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch("local_apt_repo_updater.cli.load_config", return_value=config),
            patch(
                "local_apt_repo_updater.cli.platform.freedesktop_os_release",
                return_value={},
            ),
            patch.dict(cli.PROVIDERS, mock_providers, clear=True),
            patch(
                "local_apt_repo_updater.cli.subprocess.check_call"
            ) as mock_check_call,
            patch("local_apt_repo_updater.signing.ensure_signing_key"),
            patch("local_apt_repo_updater.signing.sign_release"),
            patch("local_apt_repo_updater.signing.export_public_key"),
            patch("local_apt_repo_updater.cli.check_repo_dir_permissions"),
        ):
            main()

        return mock_check_call

    def test_creates_repo_dir_if_missing(self, tmp_path):
        repo_dir = tmp_path / "new" / "repo"
        self._run_main(repo_dir)
        assert repo_dir.exists()

    def test_no_rebuild_when_providers_unchanged_and_metadata_exists(self, tmp_path):
        (tmp_path / "Packages").write_text("")
        (tmp_path / "Release").write_text("")
        (tmp_path / "InRelease").write_text("")
        mock_check_call = self._run_main(tmp_path)
        mock_check_call.assert_not_called()

    def test_rebuild_triggered_when_provider_reports_change(self, tmp_path):
        (tmp_path / "Packages").write_text("")
        (tmp_path / "Release").write_text("")
        mock_check_call = self._run_main(
            tmp_path,
            config_overrides={"github_releases": [{"repo": "x", "filename": "y.deb"}]},
            provider_side_effects={"github_releases": True},
        )
        mock_check_call.assert_called()

    def test_rebuild_triggered_when_packages_file_missing(self, tmp_path):
        (tmp_path / "Release").write_text("")
        # Packages does not exist
        mock_check_call = self._run_main(tmp_path)
        mock_check_call.assert_called()

    def test_rebuild_triggered_when_release_file_missing(self, tmp_path):
        (tmp_path / "Packages").write_text("")
        # Release does not exist
        mock_check_call = self._run_main(tmp_path)
        mock_check_call.assert_called()

    def test_rebuild_triggered_when_inrelease_file_missing(self, tmp_path):
        (tmp_path / "Packages").write_text("")
        (tmp_path / "Release").write_text("")
        # InRelease does not exist
        mock_check_call = self._run_main(tmp_path)
        mock_check_call.assert_called()

    def test_rebuild_signs_release(self, tmp_path):
        fake_args = MagicMock(verbose=0, command=None)
        mock_gh_fetch = MagicMock(return_value=True)

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch(
                "local_apt_repo_updater.cli.load_config",
                return_value={"apt_repo_directory": str(tmp_path)},
            ),
            patch(
                "local_apt_repo_updater.cli.platform.freedesktop_os_release",
                return_value={},
            ),
            patch.dict(cli.PROVIDERS, {"github_releases": mock_gh_fetch}, clear=True),
            patch("local_apt_repo_updater.cli.subprocess.check_call"),
            patch("local_apt_repo_updater.signing.ensure_signing_key") as mock_ensure,
            patch("local_apt_repo_updater.signing.sign_release") as mock_sign,
            patch("local_apt_repo_updater.signing.export_public_key") as mock_export,
            patch("local_apt_repo_updater.cli.check_repo_dir_permissions"),
        ):
            main()

        mock_ensure.assert_called_once_with(cli.DEFAULT_GNUPG_HOME)
        mock_sign.assert_called_once_with(cli.DEFAULT_GNUPG_HOME, tmp_path)
        mock_export.assert_called_once_with(
            cli.DEFAULT_GNUPG_HOME, cli.DEFAULT_KEYRING_PATH
        )

    def test_refuses_and_does_nothing_when_repo_dir_is_insecure(self, tmp_path):
        (tmp_path / "Packages").write_text("")
        with (
            patch.object(
                Path, "stat", return_value=MagicMock(st_uid=1000, st_mode=0o040755)
            ),
            patch("local_apt_repo_updater.cli.subprocess.check_call") as mock_call,
            patch("local_apt_repo_updater.signing.sign_release") as mock_sign,
        ):
            with pytest.raises(cli.InsecureRepoDirectoryError, match="root"):
                cli.run_update({"apt_repo_directory": str(tmp_path)})
        mock_call.assert_not_called()
        mock_sign.assert_not_called()

    def test_insecure_repo_dir_is_logged_and_does_not_crash_main(
        self, tmp_path, caplog
    ):
        fake_args = MagicMock(verbose=0, command=None)

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch(
                "local_apt_repo_updater.cli.load_config",
                return_value={"apt_repo_directory": str(tmp_path)},
            ),
            patch.object(
                Path, "stat", return_value=MagicMock(st_uid=1000, st_mode=0o040755)
            ),
        ):
            with caplog.at_level(logging.ERROR):
                main()  # must not raise

        assert any("local-apt-repo-updater failed" in m for m in caplog.messages)

    def test_freshly_created_repo_dir_gets_safe_mode_regardless_of_umask(
        self, tmp_path
    ):
        new_dir = tmp_path / "new-repo"
        old_umask = os.umask(0)
        try:
            with (
                patch("local_apt_repo_updater.cli.check_repo_dir_permissions"),
                patch.dict(cli.PROVIDERS, {}, clear=True),
                patch("local_apt_repo_updater.cli.subprocess.check_call"),
                patch("local_apt_repo_updater.signing.ensure_signing_key"),
                patch("local_apt_repo_updater.signing.sign_release"),
                patch("local_apt_repo_updater.signing.export_public_key"),
                patch(
                    "local_apt_repo_updater.cli.platform.freedesktop_os_release",
                    return_value={},
                ),
            ):
                cli.run_update({"apt_repo_directory": str(new_dir)})
        finally:
            os.umask(old_umask)

        assert (new_dir.stat().st_mode & 0o777) == 0o755

    def test_rebuild_calls_apt_ftparchive_packages(self, tmp_path):
        mock_check_call = self._run_main(
            tmp_path,
            provider_side_effects={"github_releases": True},
        )
        commands = [c.args[0] for c in mock_check_call.call_args_list]
        assert any(
            cmd[0] == "apt-ftparchive" and cmd[1] == "packages" for cmd in commands
        )

    def test_rebuild_calls_apt_ftparchive_release(self, tmp_path):
        mock_check_call = self._run_main(
            tmp_path,
            provider_side_effects={"github_releases": True},
        )
        commands = [c.args[0] for c in mock_check_call.call_args_list]
        assert any(
            cmd[0] == "apt-ftparchive" and cmd[1] == "release" for cmd in commands
        )

    def test_providers_receive_config_entries_and_env(self, tmp_path):
        (tmp_path / "Packages").write_text("")
        (tmp_path / "Release").write_text("")
        (tmp_path / "InRelease").write_text("")
        entries = [{"repo": "https://github.com/owner/repo", "filename": "pkg.deb"}]
        fake_env = {"VERSION_ID": "22.04"}
        fake_args = MagicMock(verbose=0)
        mock_gh_fetch = MagicMock(return_value=False)

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch(
                "local_apt_repo_updater.cli.load_config",
                return_value={
                    "apt_repo_directory": str(tmp_path),
                    "github_releases": entries,
                },
            ),
            patch(
                "local_apt_repo_updater.cli.platform.freedesktop_os_release",
                return_value=fake_env,
            ),
            patch.dict(
                cli.PROVIDERS,
                {
                    "github_releases": mock_gh_fetch,
                    "fixed_urls": MagicMock(return_value=False),
                },
                clear=True,
            ),
            patch("local_apt_repo_updater.cli.subprocess.check_call"),
            patch("local_apt_repo_updater.cli.check_repo_dir_permissions"),
        ):
            main()

        mock_gh_fetch.assert_called_once_with(entries, tmp_path, fake_env)

    def test_provider_skipped_when_config_has_no_entries(self, tmp_path):
        (tmp_path / "Packages").write_text("")
        (tmp_path / "Release").write_text("")
        (tmp_path / "InRelease").write_text("")
        fake_args = MagicMock(verbose=0)
        mock_gh_fetch = MagicMock(return_value=False)

        # Config has no github_releases key
        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch(
                "local_apt_repo_updater.cli.load_config",
                return_value={"apt_repo_directory": str(tmp_path)},
            ),
            patch(
                "local_apt_repo_updater.cli.platform.freedesktop_os_release",
                return_value={},
            ),
            patch.dict(cli.PROVIDERS, {"github_releases": mock_gh_fetch}, clear=True),
            patch("local_apt_repo_updater.cli.subprocess.check_call"),
            patch("local_apt_repo_updater.cli.check_repo_dir_permissions"),
        ):
            main()

        mock_gh_fetch.assert_not_called()

    def test_install_apt_hook_command_skips_update_and_writes_hook(self, tmp_path):
        hook_path = tmp_path / "hook"
        fake_args = MagicMock(
            verbose=0, command="install-apt-hook", path=hook_path, executable=None
        )

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch(
                "local_apt_repo_updater.cli.detect_executable",
                return_value=Path("/usr/bin/local-apt-repo-updater"),
            ),
            patch("local_apt_repo_updater.cli.load_config") as mock_load_config,
        ):
            main()

        mock_load_config.assert_not_called()
        assert "/usr/bin/local-apt-repo-updater" in hook_path.read_text()

    def test_install_sources_list_command_skips_update_and_writes_file(self, tmp_path):
        list_path = tmp_path / "sources.list"
        keyring_path = tmp_path / "keyring.gpg"
        fake_args = MagicMock(
            verbose=0,
            command="install-sources-list",
            path=list_path,
            repo_dir=Path("/srv/local-apt-repo"),
            keyring=keyring_path,
        )

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch("local_apt_repo_updater.cli.load_config") as mock_load_config,
            patch(
                "local_apt_repo_updater.cli.subprocess.check_call"
            ) as mock_check_call,
            patch("local_apt_repo_updater.signing.ensure_signing_key") as mock_ensure,
            patch("local_apt_repo_updater.signing.export_public_key") as mock_export,
        ):
            main()

        mock_load_config.assert_not_called()
        mock_check_call.assert_not_called()
        mock_ensure.assert_called_once_with(cli.DEFAULT_GNUPG_HOME)
        mock_export.assert_called_once_with(cli.DEFAULT_GNUPG_HOME, keyring_path)
        assert "file:///srv/local-apt-repo" in list_path.read_text()
        assert f"signed-by={keyring_path}" in list_path.read_text()

    def test_missing_apt_repo_directory_falls_back_to_default(self, tmp_path):
        fake_args = MagicMock(verbose=0, command=None)
        default_dir = tmp_path / "default-repo"

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch("local_apt_repo_updater.cli.load_config", return_value={}),
            patch("local_apt_repo_updater.cli.DEFAULT_APT_REPO_DIR", default_dir),
            patch(
                "local_apt_repo_updater.cli.platform.freedesktop_os_release",
                return_value={},
            ),
            patch.dict(
                cli.PROVIDERS,
                {"github_releases": MagicMock(return_value=False)},
                clear=True,
            ),
            patch("local_apt_repo_updater.cli.subprocess.check_call"),
            patch("local_apt_repo_updater.signing.ensure_signing_key"),
            patch("local_apt_repo_updater.signing.sign_release"),
            patch("local_apt_repo_updater.signing.export_public_key"),
            patch("local_apt_repo_updater.cli.check_repo_dir_permissions"),
        ):
            main()

        assert default_dir.exists()

    def test_unexpected_failure_is_logged_and_does_not_raise(self, caplog):
        fake_args = MagicMock(verbose=0, command=None)

        with (
            patch("local_apt_repo_updater.cli.parse_args", return_value=fake_args),
            patch("local_apt_repo_updater.cli.setup_logging"),
            patch(
                "local_apt_repo_updater.cli.load_config",
                side_effect=RuntimeError("network is down"),
            ),
        ):
            with caplog.at_level(logging.ERROR):
                main()  # must not raise

        assert any("local-apt-repo-updater failed" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# apache_directory._compare_version_part
# ---------------------------------------------------------------------------


class TestCompareVersionPart:
    def cmp(self, a, b):
        from local_apt_repo_updater.apache_directory import _compare_version_part

        return _compare_version_part(a, b)

    def test_equal_versions(self):
        assert self.cmp("1.0.3", "1.0.3") == 0

    def test_greater_patch(self):
        assert self.cmp("1.0.4", "1.0.3") > 0

    def test_lesser_patch(self):
        assert self.cmp("1.0.3", "1.0.4") < 0

    def test_multi_digit_numeric_comparison(self):
        # 1.10 > 1.9 numerically
        assert self.cmp("1.10", "1.9") > 0
        assert self.cmp("4.1.10", "4.1.9") > 0

    def test_leading_zeros_ignored(self):
        assert self.cmp("1.000.3", "1.0.3") == 0
        assert self.cmp("1.010", "1.10") == 0

    def test_tilde_sorts_before_empty(self):
        # tilde is less than empty string (Debian pre-release marker)
        assert self.cmp("4.0.0~b2", "4.0.0") < 0

    def test_tilde_sorts_before_any_character(self):
        assert self.cmp("1.0~rc1", "1.0") < 0

    def test_empty_vs_digit(self):
        # empty string (absent revision) < "2"
        assert self.cmp("", "2") < 0

    def test_letters_sort_after_digits(self):
        # non-digit non-tilde characters sort after empty/digits
        assert self.cmp("1.0a", "1.0") > 0


# ---------------------------------------------------------------------------
# apache_directory._compare_deb_versions
# ---------------------------------------------------------------------------


class TestCompareDébVersions:
    def cmp(self, a, b):
        from local_apt_repo_updater.apache_directory import _compare_deb_versions

        return _compare_deb_versions(a, b)

    def test_equal(self):
        assert self.cmp("1.0.3", "1.0.3") == 0

    def test_simple_greater(self):
        assert self.cmp("1.0.4", "1.0.3") > 0

    def test_debian_revision_greater(self):
        # 1.0.3-3 > 1.0.3-2
        assert self.cmp("1.0.3-3", "1.0.3-2") > 0

    def test_debian_revision_vs_no_revision(self):
        # 1.0.3-2 > 1.0.3 (no revision treated as revision 0)
        assert self.cmp("1.0.3-2", "1.0.3") > 0

    def test_epoch_dominates(self):
        assert self.cmp("2:1.0.0", "1:9.9.9") > 0

    def test_tilde_prerelease_less_than_release(self):
        assert self.cmp("4.0.0~b2", "4.0.0") < 0

    def test_multi_digit_upstream(self):
        assert self.cmp("1.10.0", "1.9.0") > 0

    @pytest.mark.parametrize(
        "newer,older",
        [
            ("1.0.8", "1.0.6"),
            ("1.0.3-3", "1.0.3-2"),
            ("1.0.4", "1.0.3"),
            ("1.0.3", "1.0.3~pre1"),
            ("4.15.3", "4.1.10"),
            ("5.0.1", "0.48.0"),
        ],
    )
    def test_newer_greater_than_older(self, newer, older):
        assert self.cmp(newer, older) > 0
        assert self.cmp(older, newer) < 0


# ---------------------------------------------------------------------------
# apache_directory._extract_deb_version
# ---------------------------------------------------------------------------


class TestExtractDebVersion:
    def extract(self, filename):
        from local_apt_repo_updater.apache_directory import _extract_deb_version

        return _extract_deb_version(filename)

    def test_simple_version(self):
        assert self.extract("proton-vpn-cli_1.0.1_all.deb") == "1.0.1"

    def test_version_with_debian_revision(self):
        assert self.extract("protonvpn-stable-release_1.0.3-2_all.deb") == "1.0.3-2"

    def test_tilde_version(self):
        assert self.extract("proton-vpn-gtk-app_4.0.0~b2_all.deb") == "4.0.0~b2"

    def test_amd64_arch(self):
        assert self.extract("pkg_2.1.0_amd64.deb") == "2.1.0"

    def test_no_match_returns_none(self):
        from local_apt_repo_updater.apache_directory import _extract_deb_version

        assert _extract_deb_version("Packages") is None
        assert _extract_deb_version("no_version.deb") is None


# ---------------------------------------------------------------------------
# apache_directory.list_deb_files
# ---------------------------------------------------------------------------

SAMPLE_APACHE_LISTING = """\
<!DOCTYPE HTML>
<html><head><title>Index of /test/</title></head>
<body><h1>Index of /test/</h1><hr><pre>
<a href="../">../</a>
<a href="Packages">Packages</a>                         01-Jan-2024 00:00    1234
<a href="pkg-a_1.0.0_all.deb">pkg-a_1.0.0_all.deb</a>              01-Jan-2024 00:00    5000
<a href="pkg-a_1.0.1_all.deb">pkg-a_1.0.1_all.deb</a>              02-Jan-2024 00:00    5100
<a href="pkg-b_2.0.0_amd64.deb">pkg-b_2.0.0_amd64.deb</a>            03-Jan-2024 00:00    6000
</pre><hr></body></html>
"""


class TestListDebFiles:
    def test_returns_only_deb_filenames(self):
        from local_apt_repo_updater.apache_directory import list_deb_files

        resp = MagicMock()
        resp.status_code = 200
        resp.text = SAMPLE_APACHE_LISTING
        with patch(
            "local_apt_repo_updater.apache_directory.requests.get", return_value=resp
        ):
            result = list_deb_files("https://example.com/test/")
        assert result == [
            "pkg-a_1.0.0_all.deb",
            "pkg-a_1.0.1_all.deb",
            "pkg-b_2.0.0_amd64.deb",
        ]

    def test_excludes_parent_dir_and_non_deb(self):
        from local_apt_repo_updater.apache_directory import list_deb_files

        resp = MagicMock()
        resp.status_code = 200
        resp.text = SAMPLE_APACHE_LISTING
        with patch(
            "local_apt_repo_updater.apache_directory.requests.get", return_value=resp
        ):
            result = list_deb_files("https://example.com/test/")
        assert "../" not in result
        assert "Packages" not in result

    def test_raises_on_http_error(self):
        import requests as req
        from local_apt_repo_updater.apache_directory import list_deb_files

        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = req.exceptions.HTTPError("404")
        with patch(
            "local_apt_repo_updater.apache_directory.requests.get", return_value=resp
        ):
            with pytest.raises(req.exceptions.HTTPError):
                list_deb_files("https://example.com/test/")


# ---------------------------------------------------------------------------
# apache_directory.find_newest_deb
# ---------------------------------------------------------------------------


class TestFindNewestDeb:
    def _mock_list(self, filenames):
        return patch(
            "local_apt_repo_updater.apache_directory.list_deb_files",
            return_value=filenames,
        )

    def test_finds_newest_by_version(self):
        from local_apt_repo_updater.apache_directory import find_newest_deb

        files = ["pkg-a_1.0.0_all.deb", "pkg-a_1.0.1_all.deb", "pkg-a_0.9.0_all.deb"]
        with self._mock_list(files):
            assert (
                find_newest_deb("https://example.com/", "pkg-a_*_all.deb")
                == "pkg-a_1.0.1_all.deb"
            )

    def test_returns_none_when_no_match(self):
        from local_apt_repo_updater.apache_directory import find_newest_deb

        with self._mock_list(["pkg-b_1.0.0_all.deb"]):
            assert find_newest_deb("https://example.com/", "pkg-a_*_all.deb") is None

    def test_single_match_returned(self):
        from local_apt_repo_updater.apache_directory import find_newest_deb

        with self._mock_list(["pkg-a_1.0.0_all.deb", "pkg-b_2.0.0_all.deb"]):
            assert (
                find_newest_deb("https://example.com/", "pkg-a_*_all.deb")
                == "pkg-a_1.0.0_all.deb"
            )

    def test_debian_revision_compared_correctly(self):
        from local_apt_repo_updater.apache_directory import find_newest_deb

        files = ["pkg_1.0.3_all.deb", "pkg_1.0.3-2_all.deb", "pkg_1.0.3-3_all.deb"]
        with self._mock_list(files):
            assert (
                find_newest_deb("https://example.com/", "pkg_*_all.deb")
                == "pkg_1.0.3-3_all.deb"
            )

    def test_tilde_prerelease_not_selected_over_release(self):
        from local_apt_repo_updater.apache_directory import find_newest_deb

        files = ["pkg_4.0.0~b2_all.deb", "pkg_4.0.0_all.deb", "pkg_3.9.9_all.deb"]
        with self._mock_list(files):
            assert (
                find_newest_deb("https://example.com/", "pkg_*_all.deb")
                == "pkg_4.0.0_all.deb"
            )

    def test_multi_digit_versions_ordered_numerically(self):
        from local_apt_repo_updater.apache_directory import find_newest_deb

        files = ["pkg_4.9.7_all.deb", "pkg_4.15.3_all.deb", "pkg_4.1.10_all.deb"]
        with self._mock_list(files):
            assert (
                find_newest_deb("https://example.com/", "pkg_*_all.deb")
                == "pkg_4.15.3_all.deb"
            )

    def test_env_var_substituted_in_pattern(self):
        from local_apt_repo_updater.apache_directory import find_newest_deb

        files = ["pkg_1.0.0_amd64.deb", "pkg_1.0.1_arm64.deb"]
        with self._mock_list(files):
            result = find_newest_deb("https://example.com/", "pkg_*_amd64.deb")
        assert result == "pkg_1.0.0_amd64.deb"


# ---------------------------------------------------------------------------
# apache_directory.fetch_packages
# ---------------------------------------------------------------------------


class TestApacheDirectoryFetchPackages:
    BASE_ENTRY = {
        "url": "https://example.com/debian/dists/stable/main/binary-all/",
        "filename": "pkg_*_all.deb",
    }

    def _patch_newest(self, filename="pkg_1.0.1_all.deb"):
        return patch(
            "local_apt_repo_updater.apache_directory.find_newest_deb",
            return_value=filename,
        )

    def _patch_fetch(self, changed=False):
        return patch(
            "local_apt_repo_updater.apache_directory.fetch_if_modified_since",
            return_value=changed,
        )

    def test_fetches_newest_file(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        with (
            self._patch_newest("pkg_1.0.1_all.deb"),
            self._patch_fetch(True) as mock_fetch,
        ):
            fetch_packages([self.BASE_ENTRY], tmp_path, {})
        called_url, called_dest = mock_fetch.call_args.args
        assert "pkg_1.0.1_all.deb" in called_url
        assert called_dest == tmp_path / "pkg_1.0.1_all.deb"

    def test_url_constructed_correctly(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        with self._patch_newest("pkg_1.0.1_all.deb"), self._patch_fetch() as mock_fetch:
            fetch_packages([self.BASE_ENTRY], tmp_path, {})
        called_url = mock_fetch.call_args.args[0]
        assert (
            called_url
            == "https://example.com/debian/dists/stable/main/binary-all/pkg_1.0.1_all.deb"
        )

    def test_trailing_slash_not_doubled(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        entry = {**self.BASE_ENTRY, "url": "https://example.com/test/"}
        with self._patch_newest("pkg_1.0.0_all.deb"), self._patch_fetch() as mock_fetch:
            fetch_packages([entry], tmp_path, {})
        called_url = mock_fetch.call_args.args[0]
        assert "//" not in called_url.split("://", 1)[1]

    def test_returns_true_when_file_changed(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        with self._patch_newest(), self._patch_fetch(True):
            assert fetch_packages([self.BASE_ENTRY], tmp_path, {}) is True

    def test_returns_false_when_unchanged(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        with self._patch_newest(), self._patch_fetch(False):
            assert fetch_packages([self.BASE_ENTRY], tmp_path, {}) is False

    def test_warns_and_skips_when_no_match(self, tmp_path, caplog):
        from local_apt_repo_updater.apache_directory import fetch_packages

        with (
            patch(
                "local_apt_repo_updater.apache_directory.find_newest_deb",
                return_value=None,
            ),
            patch(
                "local_apt_repo_updater.apache_directory.fetch_if_modified_since"
            ) as mock_fetch,
        ):
            with caplog.at_level(logging.WARNING):
                result = fetch_packages([self.BASE_ENTRY], tmp_path, {})
        mock_fetch.assert_not_called()
        assert result is False
        assert any("No .deb files matching" in m for m in caplog.messages)

    def test_env_vars_expanded_in_url_and_filename(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        entry = {
            "url": "https://example.com/binary-{VERSION_CODENAME}/",
            "filename": "pkg_*_{VERSION_ID}_amd64.deb",
        }
        with (
            patch(
                "local_apt_repo_updater.apache_directory.find_newest_deb",
                return_value="pkg_1.0_noble_amd64.deb",
            ) as mock_newest,
            self._patch_fetch(),
        ):
            fetch_packages(
                [entry], tmp_path, {"VERSION_CODENAME": "noble", "VERSION_ID": "24.04"}
            )
        called_url, called_pattern = mock_newest.call_args.args
        assert "noble" in called_url
        assert "24.04" in called_pattern

    def test_multiple_entries_returns_true_if_any_changed(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        entries = [self.BASE_ENTRY, {**self.BASE_ENTRY, "filename": "other_*_all.deb"}]
        with (
            self._patch_newest(),
            patch(
                "local_apt_repo_updater.apache_directory.fetch_if_modified_since",
                side_effect=[False, True],
            ),
        ):
            assert fetch_packages(entries, tmp_path, {}) is True

    def test_one_entry_failing_does_not_block_others(self, tmp_path, caplog):
        from local_apt_repo_updater.apache_directory import fetch_packages

        entries = [self.BASE_ENTRY, {**self.BASE_ENTRY, "filename": "other_*_all.deb"}]
        with (
            patch(
                "local_apt_repo_updater.apache_directory.find_newest_deb",
                side_effect=[ConnectionError("boom"), "other_1.0.0_all.deb"],
            ),
            self._patch_fetch(True),
        ):
            with caplog.at_level(logging.ERROR):
                result = fetch_packages(entries, tmp_path, {})
        assert result is True
        assert any("Failed to process" in m for m in caplog.messages)

    def test_filename_list_fetches_each_pattern(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        entry = {**self.BASE_ENTRY, "filename": ["pkg-a_*_all.deb", "pkg-b_*_all.deb"]}
        newest_side_effect = ["pkg-a_1.0.0_all.deb", "pkg-b_2.0.0_all.deb"]
        with (
            patch(
                "local_apt_repo_updater.apache_directory.find_newest_deb",
                side_effect=newest_side_effect,
            ) as mock_newest,
            self._patch_fetch(),
        ):
            fetch_packages([entry], tmp_path, {})
        assert mock_newest.call_count == 2
        patterns_called = [c.args[1] for c in mock_newest.call_args_list]
        assert patterns_called == ["pkg-a_*_all.deb", "pkg-b_*_all.deb"]

    def test_filename_list_returns_true_if_any_changed(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        entry = {**self.BASE_ENTRY, "filename": ["pkg-a_*_all.deb", "pkg-b_*_all.deb"]}
        with (
            patch(
                "local_apt_repo_updater.apache_directory.find_newest_deb",
                side_effect=["pkg-a_1.0.0_all.deb", "pkg-b_2.0.0_all.deb"],
            ),
            patch(
                "local_apt_repo_updater.apache_directory.fetch_if_modified_since",
                side_effect=[False, True],
            ),
        ):
            assert fetch_packages([entry], tmp_path, {}) is True

    def test_filename_string_behaves_like_single_item_list(self, tmp_path):
        from local_apt_repo_updater.apache_directory import fetch_packages

        with (
            patch(
                "local_apt_repo_updater.apache_directory.find_newest_deb",
                return_value="pkg_1.0.0_all.deb",
            ) as mock_newest,
            self._patch_fetch(),
        ):
            fetch_packages([self.BASE_ENTRY], tmp_path, {})
        assert mock_newest.call_count == 1


# ---------------------------------------------------------------------------
# cli.PROVIDERS registry — apache_directory registered
# ---------------------------------------------------------------------------


class TestProviderRegistryApache:
    def test_apache_directory_key_registered(self):
        assert "apache_directory" in cli.PROVIDERS

    def test_apache_directory_value_is_callable(self):
        assert callable(cli.PROVIDERS["apache_directory"])
