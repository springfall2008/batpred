# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import io
import sys
import importlib
import tarfile
import tempfile
import shutil
from unittest.mock import patch
import yaml

# Add parent directory to path for standalone execution
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from download import (
    get_github_directory_listing,
    check_install,
    predbat_update_download,
    compute_data_sha1,
    compute_file_sha1,
    download_predbat_file_from_github,
    download_predbat_release_archive,
    extract_predbat_files_from_archive,
    match_archive_member,
    predbat_update_move,
    resolve_predbat_repository,
    verify_staged_files,
    DEFAULT_PREDBAT_REPOSITORY,
    DOWNLOAD_MAX_ATTEMPTS,
)


def test_download(my_predbat):
    """
    Comprehensive test suite for Predbat download/update functionality.

    Tests all major functionality including:
    - GitHub API directory listing
    - File SHA1 computation
    - Installation validation
    - Download operations
    - File move/update operations
    """

    # Registry of all sub-tests
    sub_tests = [
        ("resolve_repo_default", _test_resolve_predbat_repository_default, "Repository resolver falls back to upstream default"),
        ("resolve_repo_env", _test_resolve_predbat_repository_env_override, "Repository resolver honours PREDBAT_REPOSITORY env var"),
        ("resolve_repo_env_empty", _test_resolve_predbat_repository_env_empty_falls_back_default, "Repository resolver ignores empty/whitespace PREDBAT_REPOSITORY"),
        ("resolve_repo_explicit", _test_resolve_predbat_repository_explicit_overrides_env, "Repository resolver explicit arg overrides env var"),
        ("github_listing_success", _test_get_github_directory_listing_success, "GitHub directory listing success"),
        ("github_listing_failure", _test_get_github_directory_listing_failure, "GitHub API failure (404)"),
        ("github_listing_exception", _test_get_github_directory_listing_exception, "GitHub API exception handling"),
        ("github_listing_env_repo", _test_get_github_directory_listing_uses_env_repository, "GitHub listing uses repository from env var"),
        ("github_listing_explicit_repo", _test_get_github_directory_listing_explicit_repository, "GitHub listing explicit repository overrides env var"),
        ("compute_sha1", _test_compute_file_sha1, "Compute file SHA1"),
        ("compute_sha1_missing", _test_compute_file_sha1_missing_file, "SHA1 computation on missing file"),
        ("check_install_valid", _test_check_install_with_valid_manifest, "Check install with valid manifest"),
        ("check_install_missing", _test_check_install_missing_file, "Check install missing file"),
        ("check_install_zero", _test_check_install_zero_byte_file, "Check install zero byte file"),
        ("check_install_size_mismatch", _test_check_install_size_mismatch, "Check install size mismatch"),
        ("check_install_sha_mismatch", _test_check_install_sha_mismatch, "Check install SHA mismatch"),
        ("check_install_no_manifest", _test_check_install_no_manifest_downloads, "Check install downloads manifest"),
        ("update_download_success", _test_predbat_update_download_success, "Update download success"),
        ("update_download_api_failure", _test_predbat_update_download_api_failure, "Update download API failure"),
        ("update_download_file_failure", _test_predbat_update_download_file_failure, "Update download file failure"),
        ("download_file_success", _test_download_predbat_file_success, "Download file success"),
        ("download_file_binary", _test_download_predbat_file_binary, "Download binary file round-trips byte-for-byte"),
        ("download_file_failure", _test_download_predbat_file_failure, "Download file failure"),
        ("download_file_no_filename", _test_download_predbat_file_no_filename, "Download file no filename"),
        ("update_move_success", _test_predbat_update_move_success, "Move files success"),
        ("update_move_empty", _test_predbat_update_move_empty_files, "Move files empty list"),
        ("update_move_none", _test_predbat_update_move_none_files, "Move files none list"),
        ("update_move_invalid_version", _test_predbat_update_move_invalid_version, "Move files invalid version"),
        ("update_download_skip_matching", _test_predbat_update_download_skip_matching_sha, "Update download skips files with matching SHA"),
        ("update_download_skip_mixed", _test_predbat_update_download_skip_mixed, "Update download skips some files, downloads others"),
        ("compute_data_sha1", _test_compute_data_sha1, "Compute Git blob SHA1 of in-memory data"),
        ("download_verify_match", _test_download_file_verifies_sha, "Download writes file when checksum matches"),
        ("download_verify_mismatch", _test_download_file_sha_mismatch_aborts, "Download aborts and writes nothing when checksum never matches"),
        ("download_verify_retry", _test_download_file_sha_retry_succeeds, "Download retries a checksum mismatch and keeps the good copy"),
        ("archive_download_success", _test_download_release_archive_success, "Release archive streams to a temporary file"),
        ("archive_download_failure", _test_download_release_archive_failure, "Release archive download failure returns None"),
        ("archive_download_too_large", _test_download_release_archive_too_large, "Release archive over the size cap is rejected and cleaned up"),
        ("archive_member_match", _test_match_archive_member, "Archive member matching accepts only apps/predbat files"),
        ("archive_extract_success", _test_extract_archive_success, "Archive extract stages only the listed files"),
        ("archive_extract_apps_yaml", _test_extract_archive_preserves_live_apps_yaml, "Archive extract never overwrites the live apps.yaml"),
        ("archive_extract_mismatch", _test_extract_archive_checksum_mismatch, "Archive extract aborts and cleans up on checksum mismatch"),
        ("archive_extract_size", _test_extract_archive_size_mismatch, "Archive extract aborts on member size mismatch"),
        ("archive_extract_missing", _test_extract_archive_missing_file, "Archive extract aborts when a listed file is absent"),
        ("archive_extract_traversal", _test_extract_archive_rejects_traversal, "Archive extract ignores path traversal members"),
        ("update_download_release_archive", _test_update_download_release_uses_archive, "Release download uses the archive, not per-file requests"),
        ("update_download_any_ref", _test_update_download_archive_tried_for_every_ref, "Archive is tried for any ref, with no hard coded branch name"),
        ("update_download_fresh_listing", _test_update_download_retries_with_fresh_listing, "A mismatch is retried against a freshly fetched listing"),
        ("update_download_archive_fallback", _test_update_download_archive_fetch_failure_falls_back, "No archive available falls back to per-file download"),
        ("update_download_archive_abort", _test_update_download_archive_verify_failure_aborts, "Archive verification failure aborts without falling back"),
        ("update_download_cleanup", _test_update_download_cleans_staged_on_failure, "Failed download removes the files staged so far"),
        ("update_move_verifies", _test_update_move_verifies_staged_files, "Move verifies staged files against the staged manifest"),
        ("update_move_mismatch", _test_update_move_blocks_on_staged_mismatch, "Move installs nothing when a staged file is corrupt"),
        ("update_move_missing", _test_update_move_blocks_on_missing_staged_file, "Move installs nothing when a staged file is missing"),
        ("update_move_no_manifest", _test_update_move_without_manifest_proceeds, "Move proceeds with a warning when no staged manifest exists"),
        ("verify_staged_bad_manifest", _test_verify_staged_files_invalid_manifest, "Staged verification fails on an invalid manifest"),
        ("check_install_empty_manifest", _test_check_install_empty_manifest, "Check install returns a pair when the manifest is empty"),
        ("predbat_main_repo_override", _test_predbat_download_main_uses_configured_repository, "Predbat main update uses configured repository override"),
        ("predbat_tag_repo_upstream", _test_predbat_download_tag_uses_default_repository, "Predbat tagged update always uses upstream repository"),
        ("predbat_startup_pins_upstream", _test_predbat_startup_self_check_uses_default_repository, "Predbat startup self-check pins repository to upstream"),
    ]

    print("\n" + "=" * 70)
    print("PREDBAT DOWNLOAD/UPDATE TEST SUITE")
    print("=" * 70)

    failed = 0
    passed = 0

    for test_name, test_func, test_desc in sub_tests:
        print(f"\n[{test_name}] {test_desc}")
        print("-" * 70)
        try:
            test_result = test_func(my_predbat)
            if test_result:
                print(f"✗ FAILED: {test_name}")
                failed += 1
            else:
                print(f"✓ PASSED: {test_name}")
                passed += 1
        except Exception as e:
            print(f"✗ EXCEPTION in {test_name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("=" * 70)

    return failed


def _import_predbat_module_for_tests():
    """Import predbat safely for unit tests without triggering real update/download side effects."""
    if "predbat" in sys.modules:
        return sys.modules["predbat"]

    with patch("download.check_install", return_value=(True, False)):
        with patch("download.predbat_update_download", return_value=None):
            with patch("download.predbat_update_move", return_value=True):
                with patch("builtins.print"):
                    return importlib.import_module("predbat")


def _test_resolve_predbat_repository_default(my_predbat):
    """Test repository resolution defaults to upstream when no override is present."""
    with patch.dict("download.os.environ", {}, clear=True):
        repository = resolve_predbat_repository()
        assert repository == DEFAULT_PREDBAT_REPOSITORY
    return 0


def _test_resolve_predbat_repository_env_override(my_predbat):
    """Test repository resolution uses PREDBAT_REPOSITORY environment override."""
    env_repo = "example/forked-batpred"
    with patch.dict("download.os.environ", {"PREDBAT_REPOSITORY": env_repo}, clear=True):
        repository = resolve_predbat_repository()
        assert repository == env_repo
    return 0


def _test_resolve_predbat_repository_env_empty_falls_back_default(my_predbat):
    """Test empty/whitespace PREDBAT_REPOSITORY values fall back to upstream default."""
    for env_value in ["", "   ", "\t\n"]:
        with patch.dict("download.os.environ", {"PREDBAT_REPOSITORY": env_value}, clear=True):
            repository = resolve_predbat_repository()
            assert repository == DEFAULT_PREDBAT_REPOSITORY
    return 0


def _test_resolve_predbat_repository_explicit_overrides_env(my_predbat):
    """Test explicit repository argument takes precedence over environment override."""
    env_repo = "example/forked-batpred"
    explicit_repo = "owner/explicit-repo"
    with patch.dict("download.os.environ", {"PREDBAT_REPOSITORY": env_repo}, clear=True):
        repository = resolve_predbat_repository(repository=explicit_repo)
        assert repository == explicit_repo
    return 0


def _test_get_github_directory_listing_success(my_predbat):
    """
    Test successful GitHub API directory listing
    """
    # Mock GitHub API response
    mock_response = [
        {"name": "predbat.py", "path": "apps/predbat/predbat.py", "sha": "abc123", "size": 50000, "type": "file"},
        {"name": "config.py", "path": "apps/predbat/config.py", "sha": "def456", "size": 30000, "type": "file"},
        {"name": "tests", "path": "apps/predbat/tests", "type": "dir"},  # Should be filtered out
    ]

    with patch("requests.get") as mock_get:
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = mock_response

        result = get_github_directory_listing("v8.30.8")

        assert result is not None
        assert len(result) == 2  # Only files, not directories
        assert result[0]["name"] == "predbat.py"
        assert result[0]["sha"] == "abc123"
        assert result[1]["name"] == "config.py"
        mock_get.assert_called_once()
    return 0


def _test_get_github_directory_listing_failure(my_predbat):
    """
    Test GitHub API failure
    """
    with patch("requests.get") as mock_get:
        mock_get.return_value.ok = False
        mock_get.return_value.status_code = 404

        result = get_github_directory_listing("v8.30.8")

        assert result is None
    return 0


def _test_get_github_directory_listing_exception(my_predbat):
    """
    Test GitHub API exception handling
    """
    with patch("requests.get") as mock_get:
        mock_get.side_effect = Exception("Network error")

        result = get_github_directory_listing("v8.30.8")

        assert result is None
    return 0


def _test_get_github_directory_listing_uses_env_repository(my_predbat):
    """Test GitHub listing resolves repository from PREDBAT_REPOSITORY env var."""
    mock_response = [{"name": "predbat.py", "sha": "abc", "size": 1, "type": "file"}]
    env_repo = "example/forked-batpred"

    with patch.dict("download.os.environ", {"PREDBAT_REPOSITORY": env_repo}, clear=True):
        with patch("requests.get") as mock_get:
            mock_get.return_value.ok = True
            mock_get.return_value.json.return_value = mock_response

            result = get_github_directory_listing("v8.30.8")

            assert result is not None
            called_url = mock_get.call_args[0][0]
            assert "/repos/{}/contents/apps/predbat?ref=v8.30.8".format(env_repo) in called_url
    return 0


def _test_get_github_directory_listing_explicit_repository(my_predbat):
    """Test explicit repository for GitHub listing overrides PREDBAT_REPOSITORY env var."""
    mock_response = [{"name": "predbat.py", "sha": "abc", "size": 1, "type": "file"}]
    env_repo = "example/forked-batpred"
    explicit_repo = "springfall2008/batpred"

    with patch.dict("download.os.environ", {"PREDBAT_REPOSITORY": env_repo}, clear=True):
        with patch("requests.get") as mock_get:
            mock_get.return_value.ok = True
            mock_get.return_value.json.return_value = mock_response

            result = get_github_directory_listing("v8.30.8", repository=explicit_repo)

            assert result is not None
            called_url = mock_get.call_args[0][0]
            assert "/repos/{}/contents/apps/predbat?ref=v8.30.8".format(explicit_repo) in called_url
            assert env_repo not in called_url
    return 0


def _test_compute_file_sha1(my_predbat):
    """
    Test Git blob SHA1 hash computation (matches GitHub's SHA)
    """
    # Create a temporary file with known content
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("test content\n")
        temp_path = f.name

    try:
        sha1 = compute_file_sha1(temp_path)
        # Git blob SHA of "test content\n" (computed as: sha1("blob 13\0test content\n"))
        assert sha1 == "d670460b4b4aece5915caf5c68d12f560a9fe3e4"
    finally:
        os.unlink(temp_path)
    return 0


def _test_compute_file_sha1_missing_file(my_predbat):
    """
    Test SHA1 computation on missing file
    """
    sha1 = compute_file_sha1("/nonexistent/file.txt")
    assert sha1 is None
    return 0


def _test_check_install_with_valid_manifest(my_predbat):
    """
    Test check_install with valid manifest and matching files
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create test files
        test_file1 = os.path.join(temp_dir, "test1.py")
        test_file2 = os.path.join(temp_dir, "test2.py")

        with open(test_file1, "w") as f:
            f.write("print('test1')\n")
        with open(test_file2, "w") as f:
            f.write("print('test2')\n")

        # Create manifest
        manifest = [{"name": "test1.py", "size": os.path.getsize(test_file1), "sha": compute_file_sha1(test_file1)}, {"name": "test2.py", "size": os.path.getsize(test_file2), "sha": compute_file_sha1(test_file2)}]

        manifest_file = os.path.join(temp_dir, "manifest.yaml")
        with open(manifest_file, "w") as f:
            yaml.dump(manifest, f)

        # Patch __file__ to point to temp_dir
        with patch("download.os.path.dirname", return_value=temp_dir):
            result, modified = check_install("v8.30.8")
            assert result is True
            assert modified is False

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_check_install_missing_file(my_predbat):
    """
    Test check_install with missing file
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create manifest referencing non-existent file
        manifest = [{"name": "missing.py", "size": 100, "sha": "abc123"}]

        manifest_file = os.path.join(temp_dir, "manifest.yaml")
        with open(manifest_file, "w") as f:
            yaml.dump(manifest, f)

        with patch("download.os.path.dirname", return_value=temp_dir):
            result, modified = check_install("v8.30.8")
            assert result is False
            assert modified is False

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_check_install_zero_byte_file(my_predbat):
    """
    Test check_install with zero-byte file
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create zero-byte file
        test_file = os.path.join(temp_dir, "empty.py")
        with open(test_file, "w") as f:
            pass  # Empty file

        manifest = [{"name": "empty.py", "size": 100, "sha": "abc123"}]

        manifest_file = os.path.join(temp_dir, "manifest.yaml")
        with open(manifest_file, "w") as f:
            yaml.dump(manifest, f)

        with patch("download.os.path.dirname", return_value=temp_dir):
            result, modified = check_install("v8.30.8")
            assert result is False
            assert modified is False

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_check_install_size_mismatch(my_predbat):
    """
    Test check_install warns on size mismatch but doesn't fail
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create test file
        test_file = os.path.join(temp_dir, "test.py")
        with open(test_file, "w") as f:
            f.write("print('test')\n")

        # Manifest with wrong size
        manifest = [{"name": "test.py", "size": 999999, "sha": compute_file_sha1(test_file)}]  # Wrong size

        manifest_file = os.path.join(temp_dir, "manifest.yaml")
        with open(manifest_file, "w") as f:
            yaml.dump(manifest, f)

        with patch("download.os.path.dirname", return_value=temp_dir):
            result, modified = check_install("v8.30.8")
            assert result is True  # Should pass with warning
            assert modified is True

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_check_install_sha_mismatch(my_predbat):
    """
    Test check_install warns on SHA mismatch but doesn't fail
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create test file
        test_file = os.path.join(temp_dir, "test.py")
        with open(test_file, "w") as f:
            f.write("print('test')\n")

        # Manifest with wrong SHA
        manifest = [{"name": "test.py", "size": os.path.getsize(test_file), "sha": "wrongsha123"}]  # Wrong SHA

        manifest_file = os.path.join(temp_dir, "manifest.yaml")
        with open(manifest_file, "w") as f:
            yaml.dump(manifest, f)

        with patch("download.os.path.dirname", return_value=temp_dir):
            result, modified = check_install("v8.30.8")
            assert result is True  # Should pass with warning
            assert modified is True

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_check_install_no_manifest_downloads(my_predbat):
    """
    Test check_install downloads manifest from GitHub if missing
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create test files
        test_file = os.path.join(temp_dir, "test.py")
        with open(test_file, "w") as f:
            f.write("print('test')\n")

        # Mock GitHub API response
        mock_files = [{"name": "test.py", "size": os.path.getsize(test_file), "sha": compute_file_sha1(test_file), "type": "file"}]

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
                result, modified = check_install("v8.30.8")
                assert result is True
                assert modified is False
                # Check manifest was created
                assert os.path.exists(os.path.join(temp_dir, "manifest.yaml"))

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_predbat_update_download_success(my_predbat):
    """
    Test successful download of all files (main branch uses the per-file path)
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Mock GitHub API responses
        mock_files = [{"name": "predbat.py", "size": 1000, "sha": "abc123", "type": "file"}, {"name": "config.py", "size": 500, "sha": "def456", "type": "file"}]

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
                with patch("download.download_predbat_release_archive", return_value=None):
                    with patch("download.download_predbat_file_from_github", return_value="file content"):
                        result = predbat_update_download("v8.30.8")

                    assert result is not None
                    assert "manifest.yaml" in result
                    assert "predbat.py" in result
                    assert "config.py" in result
                    # Check manifest file was created
                    assert os.path.exists(os.path.join(temp_dir, "manifest.yaml.v8.30.8"))

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_predbat_update_download_api_failure(my_predbat):
    """
    Test download aborts when GitHub API fails
    """
    temp_dir = tempfile.mkdtemp()

    try:
        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=None):
                result = predbat_update_download("v8.30.8")
                assert result is None

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_predbat_update_download_file_failure(my_predbat):
    """
    Test download aborts when individual file download fails
    """
    temp_dir = tempfile.mkdtemp()

    try:
        mock_files = [{"name": "predbat.py", "size": 1000, "sha": "abc123", "type": "file"}]

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
                with patch("download.download_predbat_release_archive", return_value=None):
                    with patch("download.download_predbat_file_from_github", return_value=None):
                        result = predbat_update_download("v8.30.8")
                        assert result is None

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_predbat_file_success(my_predbat):
    """
    Test successful download of a file from GitHub
    """
    temp_dir = tempfile.mkdtemp()

    try:
        output_file = os.path.join(temp_dir, "test.py.v8.30.8")

        # Mock successful HTTP response (requests.Response exposes both .text and .content;
        # the downloader must use .content and write in binary mode so binary files such as
        # the prediction kernel .so binaries round-trip byte-for-byte)
        mock_response = type("MockResponse", (), {"ok": True, "content": b'print("test file content")\n'})()

        with patch("download.requests.get", return_value=mock_response):
            result = download_predbat_file_from_github("v8.30.8", "test.py", output_file)

            # Verify file was written byte-for-byte
            assert os.path.exists(output_file)
            with open(output_file, "rb") as f:
                content = f.read()
            assert content == b'print("test file content")\n'
            assert result == b'print("test file content")\n'

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_predbat_file_binary(my_predbat):
    """
    Test downloading a binary file (e.g. the prediction kernel .so) round-trips byte-for-byte
    """
    temp_dir = tempfile.mkdtemp()

    try:
        output_file = os.path.join(temp_dir, "prediction_kernel_lib_x86_64.so.v8.30.8")
        # Bytes that are not valid UTF-8 and include newline-like bytes, to catch any
        # accidental text decoding or newline translation in the download path
        binary_data = bytes([0x7F, 0x45, 0x4C, 0x46, 0x00, 0x0A, 0x0D, 0xFF, 0xFE, 0x80])
        mock_response = type("MockResponse", (), {"ok": True, "content": binary_data})()

        with patch("download.requests.get", return_value=mock_response):
            result = download_predbat_file_from_github("v8.30.8", "prediction_kernel_lib_x86_64.so", output_file)

            assert os.path.exists(output_file)
            with open(output_file, "rb") as f:
                content = f.read()
            assert content == binary_data
            assert result == binary_data

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_predbat_file_failure(my_predbat):
    """
    Test failed download of a file from GitHub
    """
    temp_dir = tempfile.mkdtemp()

    try:
        output_file = os.path.join(temp_dir, "test.py.v8.30.8")

        # Mock failed HTTP response
        mock_response = type("MockResponse", (), {"ok": False, "status_code": 404})()

        with patch("download.requests.get", return_value=mock_response):
            result = download_predbat_file_from_github("v8.30.8", "test.py", output_file)

            # Verify file was not created
            assert not os.path.exists(output_file)
            assert result is None

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_predbat_file_no_filename(my_predbat):
    """
    Test download without saving to file (returns content only)
    """
    # Mock successful HTTP response
    mock_response = type("MockResponse", (), {"ok": True, "content": b'print("test file content")\n'})()

    with patch("download.requests.get", return_value=mock_response):
        result = download_predbat_file_from_github("v8.30.8", "test.py", None)
        assert result == b'print("test file content")\n'
    return 0


def _test_predbat_update_move_success(my_predbat):
    """
    Test successful move of downloaded files into place
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create test files with version tags
        test_files = ["predbat.py", "config.py", "manifest.yaml"]
        tag = "v8.30.8"

        for filename in test_files:
            tagged_file = os.path.join(temp_dir, filename + "." + tag)
            with open(tagged_file, "w") as f:
                f.write("content of {}\n".format(filename))

        # The staged manifest is re-checked before anything is moved, so it has to match
        _write_staged_manifest(temp_dir, tag, ["predbat.py", "config.py"])

        # Mock os.system and os.path.dirname
        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.os.system") as mock_system:
                result = predbat_update_move(tag, test_files)

                assert result is True
                # Verify os.system was called with mv commands
                assert mock_system.called
                call_args = mock_system.call_args[0][0]
                assert "mv -f" in call_args
                assert "predbat.py" in call_args
                assert "config.py" in call_args
                assert "manifest.yaml" in call_args
                assert "echo 'Update complete'" in call_args

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_predbat_update_move_empty_files(my_predbat):
    """
    Test predbat_update_move with empty file list
    """
    result = predbat_update_move("v8.30.8", [])
    assert result is False


def _test_predbat_update_move_none_files(my_predbat):
    """
    Test predbat_update_move with None file list
    """
    result = predbat_update_move("v8.30.8", None)
    assert result is False
    return 0


def _test_predbat_update_move_invalid_version(my_predbat):
    """
    Test predbat_update_move with empty version string still executes
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Even with empty version, the function should still run (just with empty tag)
        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.os.system") as mock_system:
                result = predbat_update_move("", ["test.py"])
                # Should still return True and call os.system
                assert result is True
                assert mock_system.called

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_predbat_update_download_skip_matching_sha(my_predbat):
    """
    Test download skips files when local SHA matches remote SHA
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create local file with known content
        local_file = os.path.join(temp_dir, "predbat.py")
        test_content = "print('existing file')\n"
        with open(local_file, "w") as f:
            f.write(test_content)

        # Compute its SHA
        local_sha = compute_file_sha1(local_file)

        # Mock GitHub API with file that has matching SHA
        mock_files = [{"name": "predbat.py", "size": len(test_content), "sha": local_sha, "type": "file"}]

        download_called = False

        def mock_download(tag, filename, output_path, repository=None, expected_sha=None):
            nonlocal download_called
            download_called = True
            return "downloaded content"

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
                # The per-file path is now reached only when no archive is available
                with patch("download.download_predbat_release_archive", return_value=None), patch("download.download_predbat_file_from_github", side_effect=mock_download):
                    result = predbat_update_download("main")

                    # Verify download was skipped (not called)
                    assert download_called is False, "download_predbat_file_from_github should not be called when SHA matches"

                    # Verify file list still includes the file
                    assert result is not None
                    assert "predbat.py" in result
                    assert "manifest.yaml" in result

                    # Verify staged file exists (copied from local)
                    staged_file = os.path.join(temp_dir, "predbat.py.main")
                    assert os.path.exists(staged_file), "Staged file should exist after copy"
                    with open(staged_file, "r") as f:
                        staged_content = f.read()
                    assert staged_content == test_content, "Staged file should match local file"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_predbat_update_download_skip_mixed(my_predbat):
    """
    Test download skips files with matching SHA but downloads files with mismatched SHA
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Create two local files
        file1 = os.path.join(temp_dir, "predbat.py")
        file1_content = "print('file1')\n"
        with open(file1, "w") as f:
            f.write(file1_content)

        file2 = os.path.join(temp_dir, "config.py")
        file2_content = "print('file2 old')\n"
        with open(file2, "w") as f:
            f.write(file2_content)

        # Compute SHAs
        file1_sha = compute_file_sha1(file1)
        file2_sha_wrong = "abc123different"  # Different SHA to force download

        # Mock GitHub API with two files - one matching, one different
        mock_files = [{"name": "predbat.py", "size": len(file1_content), "sha": file1_sha, "type": "file"}, {"name": "config.py", "size": 100, "sha": file2_sha_wrong, "type": "file"}]

        download_calls = []

        def mock_download(tag, filename, output_path, repository=None, expected_sha=None):
            download_calls.append(filename)
            with open(output_path, "w") as f:
                f.write("downloaded content for {}\n".format(filename))
            return "downloaded content"

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
                # The per-file path is now reached only when no archive is available
                with patch("download.download_predbat_release_archive", return_value=None), patch("download.download_predbat_file_from_github", side_effect=mock_download):
                    result = predbat_update_download("main")

                    # Verify only config.py was downloaded (not predbat.py)
                    assert len(download_calls) == 1, "Should download only 1 file"
                    assert "config.py" in download_calls, "Should download config.py"
                    assert "predbat.py" not in download_calls, "Should NOT download predbat.py (SHA matches)"

                    # Verify both files in result
                    assert result is not None
                    assert "predbat.py" in result
                    assert "config.py" in result

                    # Verify predbat.py was copied (not downloaded)
                    staged_file1 = os.path.join(temp_dir, "predbat.py.main")
                    assert os.path.exists(staged_file1)
                    with open(staged_file1, "r") as f:
                        content = f.read()
                    assert content == file1_content, "Staged predbat.py should be copied from local"

                    # Verify config.py was downloaded (new content)
                    staged_file2 = os.path.join(temp_dir, "config.py.main")
                    assert os.path.exists(staged_file2)
                    with open(staged_file2, "r") as f:
                        content = f.read()
                    assert content == "downloaded content for config.py\n", "Staged config.py should be downloaded"

    finally:
        shutil.rmtree(temp_dir)
    return 0


class _MockStreamResponse:
    """Minimal stand-in for a streamed requests response used by the archive tests."""

    def __init__(self, chunks, ok=True, status_code=200):
        """Store the chunks the fake response will yield."""
        self.chunks = chunks
        self.ok = ok
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size=None):
        """Yield the configured chunks, ignoring the requested chunk size."""
        for chunk in self.chunks:
            yield chunk

    def close(self):
        """Record that the connection was released, as a real streamed response requires."""
        self.closed = True


class _MockUnclosableResponse:
    """A streamed response with no close() at all, standing in for a simpler test mock."""

    def __init__(self, chunks, ok=True, status_code=200):
        """Store the chunks the fake response will yield."""
        self.chunks = chunks
        self.ok = ok
        self.status_code = status_code

    def iter_content(self, chunk_size=None):
        """Yield the configured chunks, ignoring the requested chunk size."""
        for chunk in self.chunks:
            yield chunk


def _build_test_archive(archive_path, prefix, predbat_files, extra_members=None):
    """
    Build a GitHub style source archive for testing.

    Args:
        archive_path (str): Where to write the .tar.gz
        prefix (str): The top level directory name GitHub adds (e.g. batpred-8.47.5)
        predbat_files (dict): Bare filename to bytes, placed in <prefix>/apps/predbat/
        extra_members (dict, optional): Full member path to bytes, added verbatim
    """
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, data in predbat_files.items():
            info = tarfile.TarInfo(name="{}/apps/predbat/{}".format(prefix, name))
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for member_name, data in (extra_members or {}).items():
            info = tarfile.TarInfo(name=member_name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _listing_for(predbat_files):
    """
    Build a GitHub directory listing for the supplied files.

    Args:
        predbat_files (dict): Bare filename to bytes
    Returns:
        list: Listing entries with name/size/sha/type
    """
    return [{"name": name, "size": len(data), "sha": compute_data_sha1(data), "type": "file"} for name, data in predbat_files.items()]


def _write_staged_manifest(this_path, tag, filenames):
    """
    Write a staged manifest describing the staged copies of *filenames*.

    Args:
        this_path (str): Directory holding the staged files
        tag (str): The version tag used as the staged filename suffix
        filenames (list): Bare filenames to describe
    """
    manifest = []
    for filename in filenames:
        staged_path = os.path.join(this_path, filename + "." + tag)
        manifest.append({"name": filename, "size": os.path.getsize(staged_path), "sha": compute_file_sha1(staged_path)})
    with open(os.path.join(this_path, "manifest.yaml." + tag), "w") as han:
        yaml.dump(manifest, han)


def _test_compute_data_sha1(my_predbat):
    """
    Test Git blob SHA1 computation over in-memory data matches the on-disk helper
    """
    data = b"test content\n"
    assert compute_data_sha1(data) == "d670460b4b4aece5915caf5c68d12f560a9fe3e4"

    temp_dir = tempfile.mkdtemp()
    try:
        path = os.path.join(temp_dir, "test.txt")
        with open(path, "wb") as han:
            han.write(data)
        assert compute_file_sha1(path) == compute_data_sha1(data)
    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_file_verifies_sha(my_predbat):
    """
    Test a download whose checksum matches is written to disk
    """
    temp_dir = tempfile.mkdtemp()

    try:
        content = b'print("verified content")\n'
        output_file = os.path.join(temp_dir, "test.py.v8.30.8")
        mock_response = type("MockResponse", (), {"ok": True, "content": content})()

        with patch("download.requests.get", return_value=mock_response) as mock_get:
            result = download_predbat_file_from_github("v8.30.8", "test.py", output_file, expected_sha=compute_data_sha1(content))

            assert result == content
            assert os.path.exists(output_file)
            with open(output_file, "rb") as han:
                assert han.read() == content
            assert mock_get.call_count == 1, "A matching checksum should not be retried"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_file_sha_mismatch_aborts(my_predbat):
    """
    Test a download whose checksum never matches is retried, then abandoned without writing
    """
    temp_dir = tempfile.mkdtemp()

    try:
        output_file = os.path.join(temp_dir, "test.py.v8.30.8")
        mock_response = type("MockResponse", (), {"ok": True, "content": b"corrupted content\n"})()

        with patch("download.requests.get", return_value=mock_response) as mock_get:
            result = download_predbat_file_from_github("v8.30.8", "test.py", output_file, expected_sha=compute_data_sha1(b"the real content\n"))

            assert result is None, "A file that fails verification must not be returned"
            assert not os.path.exists(output_file), "A file that fails verification must never be written"
            assert mock_get.call_count == DOWNLOAD_MAX_ATTEMPTS, "The download should be retried before giving up"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_file_sha_retry_succeeds(my_predbat):
    """
    Test a transient checksum mismatch is retried and the good copy is kept
    """
    temp_dir = tempfile.mkdtemp()

    try:
        good = b'print("the real content")\n'
        output_file = os.path.join(temp_dir, "test.py.v8.30.8")
        responses = [
            type("MockResponse", (), {"ok": True, "content": b"truncated"})(),
            type("MockResponse", (), {"ok": True, "content": good})(),
        ]

        with patch("download.requests.get", side_effect=responses) as mock_get:
            result = download_predbat_file_from_github("v8.30.8", "test.py", output_file, expected_sha=compute_data_sha1(good))

            assert result == good
            assert mock_get.call_count == 2
            with open(output_file, "rb") as han:
                assert han.read() == good, "Only the verified copy should reach disk"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_release_archive_success(my_predbat):
    """
    Test the release archive is streamed to a temporary file
    """
    payload = b"archive-bytes" * 100
    response = _MockStreamResponse([payload[:500], payload[500:]])
    temp_dir = tempfile.mkdtemp()

    try:
        with patch("download.requests.get", return_value=response) as mock_get:
            archive_path = download_predbat_release_archive("v8.30.8", repository="owner/repo", target_dir=temp_dir)

            assert archive_path is not None
            # The archive is written beside the staged files, not into the system temporary
            # directory, which under Home Assistant is a small tmpfs
            assert os.path.dirname(archive_path) == temp_dir
            with open(archive_path, "rb") as han:
                assert han.read() == payload
            called_url = mock_get.call_args[0][0]
            assert called_url == "https://github.com/owner/repo/archive/v8.30.8.tar.gz"
            assert mock_get.call_args.kwargs.get("stream") is True, "The archive must be streamed, not buffered in memory"
            assert response.closed is True, "The streamed response must be closed to release the connection"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_release_archive_failure(my_predbat):
    """
    Test a failed release archive request returns None and releases the connection
    """
    temp_dir = tempfile.mkdtemp()

    try:
        response = _MockStreamResponse([], ok=False, status_code=404)

        with patch("download.requests.get", return_value=response):
            assert download_predbat_release_archive("v8.30.8", target_dir=temp_dir) is None
        assert response.closed is True, "A non-OK response must still be closed, its body is never read"

        with patch("download.requests.get", side_effect=Exception("Network error")):
            assert download_predbat_release_archive("v8.30.8", target_dir=temp_dir) is None

        # A response object that has no close() at all must not break the download
        unclosable = _MockUnclosableResponse([b"archive-bytes"])
        with patch("download.requests.get", return_value=unclosable):
            archive_path = download_predbat_release_archive("v8.30.8", target_dir=temp_dir)
            assert archive_path is not None
            os.remove(archive_path)

        assert os.listdir(temp_dir) == [], "A failed archive download must not leave a partial file behind"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_download_release_archive_too_large(my_predbat):
    """
    Test an oversized release archive is rejected and its temporary file removed
    """
    temp_dir = tempfile.mkdtemp()

    try:
        handle, temp_path = tempfile.mkstemp(prefix="oversized-", suffix=".tar.gz", dir=temp_dir)

        # One chunk larger than the cap, so the very first write trips the limit
        response = _MockStreamResponse([b"x" * 16])

        with patch("download.requests.get", return_value=response):
            with patch("download.tempfile.mkstemp", return_value=(handle, temp_path)):
                with patch("download.MAX_ARCHIVE_BYTES", 8):
                    result = download_predbat_release_archive("v8.30.8", target_dir=temp_dir)

        assert result is None
        assert not os.path.exists(temp_path), "The partial archive should be cleaned up"
        assert response.closed is True, "A response abandoned part way through must still be closed"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_match_archive_member(my_predbat):
    """
    Test archive member matching accepts only files directly inside apps/predbat
    """
    assert match_archive_member("batpred-8.47.5/apps/predbat/predbat.py") == "predbat.py"
    # Subdirectories are not part of the install set
    assert match_archive_member("batpred-8.47.5/apps/predbat/tests/test_download.py") is None
    assert match_archive_member("batpred-8.47.5/apps/predbat/config/inverter.yaml") is None
    # Anything outside apps/predbat is ignored
    assert match_archive_member("batpred-8.47.5/docs/install.md") is None
    assert match_archive_member("batpred-8.47.5/apps/other/predbat.py") is None
    # Traversal attempts never resolve to a bare filename
    assert match_archive_member("batpred-8.47.5/apps/predbat/../../../evil.py") is None
    assert match_archive_member("batpred-8.47.5/apps/predbat/..") is None
    return 0


def _test_extract_archive_success(my_predbat):
    """
    Test extraction stages exactly the listed files and ignores the rest of the archive
    """
    temp_dir = tempfile.mkdtemp()

    try:
        predbat_files = {"predbat.py": b'print("predbat")\n', "config.py": b'print("config")\n'}
        extras = {
            "batpred-8.30.8/apps/predbat/tests/test_download.py": b"tests are not installed\n",
            "batpred-8.30.8/apps/predbat/config/inverter.yaml": b"config: true\n",
            "batpred-8.30.8/docs/install.md": b"# docs are not installed\n",
            "batpred-8.30.8/apps/predbat/unlisted.py": b"not in the listing\n",
        }
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files, extras)

        staged = extract_predbat_files_from_archive(archive_path, "v8.30.8", _listing_for(predbat_files), temp_dir)

        assert staged is not None
        assert sorted(staged) == ["config.py", "predbat.py"]
        for name, data in predbat_files.items():
            with open(os.path.join(temp_dir, name + ".v8.30.8"), "rb") as han:
                assert han.read() == data

        # Nothing outside the listing should have been written anywhere
        written = sorted(os.listdir(temp_dir))
        assert written == ["config.py.v8.30.8", "predbat.py.v8.30.8", "release.tar.gz"], written

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_extract_archive_preserves_live_apps_yaml(my_predbat):
    """
    Test the live apps.yaml is never overwritten by an update

    For the Predbat app the install directory is also the add-on config directory, so the
    user's live apps.yaml sits right next to the installed files. The template ships as
    apps/predbat/config/apps.yaml, inside a subdirectory, so it is neither in the GitHub
    directory listing nor an install candidate, and the user's configuration survives.
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # The user's live configuration, sitting in the install directory
        live_apps_yaml = os.path.join(temp_dir, "apps.yaml")
        live_content = b"pred_bat:\n  my_precious_settings: true\n"
        with open(live_apps_yaml, "wb") as han:
            han.write(live_content)

        predbat_files = {"predbat.py": b'print("predbat")\n'}
        extras = {
            # The shipped template, in the config subdirectory as it is in the real repository
            "batpred-8.30.8/apps/predbat/config/apps.yaml": b"pred_bat:\n  template: true\n",
            "batpred-8.30.8/coverage/apps.yaml": b"pred_bat:\n  test_fixture: true\n",
        }
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files, extras)

        staged = extract_predbat_files_from_archive(archive_path, "v8.30.8", _listing_for(predbat_files), temp_dir)

        assert staged == ["predbat.py"]
        assert not os.path.exists(os.path.join(temp_dir, "apps.yaml.v8.30.8")), "apps.yaml must never be staged for install"
        with open(live_apps_yaml, "rb") as han:
            assert han.read() == live_content, "The live apps.yaml must be left untouched"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_extract_archive_checksum_mismatch(my_predbat):
    """
    Test a tampered archive file aborts extraction and leaves nothing staged
    """
    temp_dir = tempfile.mkdtemp()

    try:
        predbat_files = {"aaa_first.py": b'print("staged before the bad one")\n', "zzz_tampered.py": b'print("original")\n'}
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files)

        listing = _listing_for(predbat_files)
        for entry in listing:
            if entry["name"] == "zzz_tampered.py":
                entry["sha"] = compute_data_sha1(b'print("something else entirely")\n')

        staged = extract_predbat_files_from_archive(archive_path, "v8.30.8", listing, temp_dir)

        assert staged is None, "A checksum mismatch must abort the extraction"
        assert os.listdir(temp_dir) == ["release.tar.gz"], "Files staged before the failure should be cleaned up"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_extract_archive_size_mismatch(my_predbat):
    """
    Test a member whose size disagrees with the listing aborts extraction
    """
    temp_dir = tempfile.mkdtemp()

    try:
        predbat_files = {"predbat.py": b'print("predbat")\n'}
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files)

        listing = _listing_for(predbat_files)
        listing[0]["size"] = 999999

        staged = extract_predbat_files_from_archive(archive_path, "v8.30.8", listing, temp_dir)

        assert staged is None
        assert os.listdir(temp_dir) == ["release.tar.gz"]

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_extract_archive_missing_file(my_predbat):
    """
    Test extraction aborts when a listed file is absent from the archive
    """
    temp_dir = tempfile.mkdtemp()

    try:
        predbat_files = {"predbat.py": b'print("predbat")\n'}
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files)

        listing = _listing_for(predbat_files)
        listing.append({"name": "missing.py", "size": 10, "sha": compute_data_sha1(b"missing\n"), "type": "file"})

        staged = extract_predbat_files_from_archive(archive_path, "v8.30.8", listing, temp_dir)

        assert staged is None, "An incomplete archive must abort the extraction"
        assert os.listdir(temp_dir) == ["release.tar.gz"]

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_extract_archive_rejects_traversal(my_predbat):
    """
    Test a path traversal member cannot write outside the install directory

    Staged paths are built from the listed filename rather than the archive member path,
    so a hostile member has no route out of the install directory even if the listing
    itself names something outside it.
    """
    temp_dir = tempfile.mkdtemp()

    try:
        install_dir = os.path.join(temp_dir, "install")
        os.makedirs(install_dir)

        predbat_files = {"predbat.py": b'print("predbat")\n'}
        evil = b'print("pwned")\n'
        extras = {
            "batpred-8.30.8/apps/predbat/../../../evil.py": evil,
            "../../../../evil_absolute.py": evil,
        }
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files, extras)

        staged = extract_predbat_files_from_archive(archive_path, "v8.30.8", _listing_for(predbat_files), install_dir)

        assert staged == ["predbat.py"]
        assert sorted(os.listdir(install_dir)) == ["predbat.py.v8.30.8"]
        assert sorted(os.listdir(temp_dir)) == ["install", "release.tar.gz"], "Nothing may be written outside the install directory"
        assert not os.path.exists(os.path.join(os.path.dirname(temp_dir), "evil.py"))

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_download_release_uses_archive(my_predbat):
    """
    Test a release version is downloaded via the archive rather than one request per file
    """
    temp_dir = tempfile.mkdtemp()

    try:
        predbat_files = {"predbat.py": b'print("predbat")\n', "config.py": b'print("config")\n'}
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files)

        # download_predbat_release_archive hands ownership of the file to its caller, which
        # deletes it, so give it a throwaway copy
        def fake_archive_download(tag, repository=None, target_dir=None):
            """Hand out a throwaway copy of the test archive, as the real downloader would."""
            copy_path = os.path.join(temp_dir, "release-copy.tar.gz")
            shutil.copyfile(archive_path, copy_path)
            return copy_path

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=_listing_for(predbat_files)):
                with patch("download.download_predbat_release_archive", side_effect=fake_archive_download) as mock_archive:
                    with patch("download.download_predbat_file_from_github") as mock_per_file:
                        result = predbat_update_download("v8.30.8")

                        assert result is not None
                        assert sorted(result) == ["config.py", "manifest.yaml", "predbat.py"]
                        assert mock_archive.call_count == 1
                        assert not mock_per_file.called, "The archive path must not fall back to per-file downloads"
                        assert os.path.exists(os.path.join(temp_dir, "manifest.yaml.v8.30.8"))
                        with open(os.path.join(temp_dir, "predbat.py.v8.30.8"), "rb") as han:
                            assert han.read() == predbat_files["predbat.py"]
                        assert not os.path.exists(os.path.join(temp_dir, "release-copy.tar.gz")), "The temporary archive should be deleted"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_download_archive_tried_for_every_ref(my_predbat):
    """
    Test the archive is tried for every ref, not only for ref names that look like releases

    GitHub builds an archive for any ref it knows about, so branches get the same single
    request treatment as releases and nothing is keyed off a hard coded branch name.
    """
    temp_dir = tempfile.mkdtemp()

    try:
        predbat_files = {"predbat.py": b'print("predbat")\n'}
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-branch", predbat_files)

        for ref in ["main", "master", "some-feature-branch", "v8.30.8"]:
            staged_dir = os.path.join(temp_dir, "install-" + ref)
            os.makedirs(staged_dir)

            def fake_archive_download(tag, repository=None, target_dir=None):
                """Hand out a throwaway copy of the test archive, as the real downloader would."""
                copy_path = os.path.join(target_dir, "release-copy.tar.gz")
                shutil.copyfile(archive_path, copy_path)
                return copy_path

            with patch("download.os.path.dirname", return_value=staged_dir):
                with patch("download.get_github_directory_listing", return_value=_listing_for(predbat_files)):
                    with patch("download.download_predbat_release_archive", side_effect=fake_archive_download) as mock_archive:
                        with patch("download.download_predbat_file_from_github") as mock_per_file:
                            result = predbat_update_download(ref)

                            assert result is not None, ref
                            assert mock_archive.called, "The archive should be tried for ref {}".format(ref)
                            assert mock_archive.call_args[0][0] == ref
                            assert not mock_per_file.called, "No per-file requests are needed when the archive works for ref {}".format(ref)

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_download_retries_with_fresh_listing(my_predbat):
    """
    Test a mismatch is retried against a freshly fetched listing

    Updating from a branch can race with a commit landing part way through, leaving the
    listing describing one commit and the archive another. Re-fetching the listing lets
    the update settle on the newer commit rather than failing.
    """
    temp_dir = tempfile.mkdtemp()

    try:
        moved_files = {"predbat.py": b'print("the commit that landed mid-update")\n'}
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-branch", moved_files)

        # The first listing describes the old commit, so it will not match the archive
        stale_listing = [{"name": "predbat.py", "size": len(moved_files["predbat.py"]), "sha": compute_data_sha1(b'print("the commit we started from")\n'), "type": "file"}]
        listings = [stale_listing, _listing_for(moved_files)]

        def fake_listing(tag, repository=None):
            """Return the stale listing first, then the one matching the archive."""
            return listings.pop(0) if listings else _listing_for(moved_files)

        def fake_archive_download(tag, repository=None, target_dir=None):
            """Hand out a throwaway copy of the test archive, as the real downloader would."""
            copy_path = os.path.join(target_dir, "release-copy.tar.gz")
            shutil.copyfile(archive_path, copy_path)
            return copy_path

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", side_effect=fake_listing) as mock_listing:
                with patch("download.download_predbat_release_archive", side_effect=fake_archive_download):
                    with patch("download.download_predbat_file_from_github") as mock_per_file:
                        result = predbat_update_download("some-branch")

                        assert result is not None, "The update should settle once the listing catches up"
                        assert mock_listing.call_count == 2, "The listing should be re-fetched after a mismatch"
                        assert not mock_per_file.called, "A mismatch must not be resolved by downloading from elsewhere"

                        # The manifest must describe the listing the files were verified
                        # against, or the pre-install check would reject them
                        with open(os.path.join(temp_dir, "manifest.yaml.some-branch"), "r") as han:
                            manifest = yaml.safe_load(han)
                        assert manifest[0]["sha"] == compute_data_sha1(moved_files["predbat.py"])
                        assert verify_staged_files(temp_dir, result, "some-branch") is True

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_download_archive_fetch_failure_falls_back(my_predbat):
    """
    Test an archive that cannot be fetched falls back to downloading files individually
    """
    temp_dir = tempfile.mkdtemp()

    try:
        mock_files = [{"name": "predbat.py", "size": 10, "sha": "abc123", "type": "file"}]

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
                with patch("download.download_predbat_release_archive", return_value=None) as mock_archive:
                    with patch("download.download_predbat_file_from_github", return_value=b"content") as mock_per_file:
                        result = predbat_update_download("v8.30.8")

                        assert result is not None, "A blocked archive host should not break the update"
                        assert mock_archive.call_count == 1, "A fetch failure should fall back rather than retry the archive"
                        assert mock_per_file.called, "The per-file download should be used as the fallback"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_download_archive_verify_failure_aborts(my_predbat):
    """
    Test an archive that fails verification aborts instead of falling back

    Content that does not match the published checksums must not be installed by another
    route, so verification failure is retried and then abandoned.
    """
    temp_dir = tempfile.mkdtemp()

    try:
        predbat_files = {"predbat.py": b'print("predbat")\n'}
        archive_path = os.path.join(temp_dir, "release.tar.gz")
        _build_test_archive(archive_path, "batpred-8.30.8", predbat_files)

        listing = _listing_for(predbat_files)
        listing[0]["sha"] = compute_data_sha1(b'print("not what was published")\n')

        def fake_archive_download(tag, repository=None, target_dir=None):
            """Hand out a throwaway copy of the test archive, as the real downloader would."""
            copy_path = os.path.join(temp_dir, "release-copy.tar.gz")
            shutil.copyfile(archive_path, copy_path)
            return copy_path

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=listing) as mock_listing:
                with patch("download.download_predbat_release_archive", side_effect=fake_archive_download) as mock_archive:
                    with patch("download.download_predbat_file_from_github") as mock_per_file:
                        result = predbat_update_download("v8.30.8")

                        assert result is None, "A tampered archive must abort the update"
                        assert mock_archive.call_count == DOWNLOAD_MAX_ATTEMPTS, "The archive should be retried before giving up"
                        assert mock_listing.call_count == DOWNLOAD_MAX_ATTEMPTS, "Each retry should re-fetch the listing"
                        assert not mock_per_file.called, "Verification failure must not fall back to another download route"
                        assert not os.path.exists(os.path.join(temp_dir, "predbat.py.v8.30.8"))
                        assert not os.path.exists(os.path.join(temp_dir, "manifest.yaml.v8.30.8"))

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_download_cleans_staged_on_failure(my_predbat):
    """
    Test an aborted download removes the files it had already staged
    """
    temp_dir = tempfile.mkdtemp()

    try:
        mock_files = [
            {"name": "first.py", "size": 10, "sha": "aaa111", "type": "file"},
            {"name": "second.py", "size": 10, "sha": "bbb222", "type": "file"},
        ]

        def mock_download(tag, filename, output_path, repository=None, expected_sha=None):
            if filename == "second.py":
                return None
            with open(output_path, "wb") as han:
                han.write(b"staged\n")
            return b"staged\n"

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
                # The per-file path is now reached only when no archive is available
                with patch("download.download_predbat_release_archive", return_value=None), patch("download.download_predbat_file_from_github", side_effect=mock_download):
                    result = predbat_update_download("main")

                    assert result is None
                    assert os.listdir(temp_dir) == [], "A failed update must not leave staged files behind"

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_move_verifies_staged_files(my_predbat):
    """
    Test the move verifies the staged files against the staged manifest before installing
    """
    temp_dir = tempfile.mkdtemp()

    try:
        files = ["predbat.py", "config.py"]
        tag = "v8.30.8"
        for filename in files:
            with open(os.path.join(temp_dir, filename + "." + tag), "wb") as han:
                han.write("content of {}\n".format(filename).encode("utf-8"))
        _write_staged_manifest(temp_dir, tag, files)

        assert verify_staged_files(temp_dir, files, tag) is True

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.os.system") as mock_system:
                assert predbat_update_move(tag, files + ["manifest.yaml"]) is True
                assert mock_system.called

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_move_blocks_on_staged_mismatch(my_predbat):
    """
    Test a staged file corrupted after download is never installed
    """
    temp_dir = tempfile.mkdtemp()

    try:
        files = ["predbat.py", "config.py"]
        tag = "v8.30.8"
        for filename in files:
            with open(os.path.join(temp_dir, filename + "." + tag), "wb") as han:
                han.write("content of {}\n".format(filename).encode("utf-8"))
        _write_staged_manifest(temp_dir, tag, files)

        # Corrupt a staged file after the manifest was written, as a bad write would
        with open(os.path.join(temp_dir, "config.py." + tag), "wb") as han:
            han.write(b"truncated")

        assert verify_staged_files(temp_dir, files, tag) is False

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.os.system") as mock_system:
                assert predbat_update_move(tag, files + ["manifest.yaml"]) is False
                assert not mock_system.called, "Nothing may be installed when a staged file fails verification"

        # The installed files must be untouched
        assert not os.path.exists(os.path.join(temp_dir, "predbat.py"))
        assert not os.path.exists(os.path.join(temp_dir, "config.py"))

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_move_blocks_on_missing_staged_file(my_predbat):
    """
    Test a missing staged file blocks the install
    """
    temp_dir = tempfile.mkdtemp()

    try:
        files = ["predbat.py", "config.py"]
        tag = "v8.30.8"
        for filename in files:
            with open(os.path.join(temp_dir, filename + "." + tag), "wb") as han:
                han.write("content of {}\n".format(filename).encode("utf-8"))
        _write_staged_manifest(temp_dir, tag, files)
        os.remove(os.path.join(temp_dir, "config.py." + tag))

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.os.system") as mock_system:
                assert predbat_update_move(tag, files) is False
                assert not mock_system.called

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_update_move_without_manifest_proceeds(my_predbat):
    """
    Test the move proceeds with a warning when there is no staged manifest to check against
    """
    temp_dir = tempfile.mkdtemp()

    try:
        tag = "v8.30.8"
        with open(os.path.join(temp_dir, "predbat.py." + tag), "wb") as han:
            han.write(b"content\n")

        assert verify_staged_files(temp_dir, ["predbat.py"], tag) is True

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.os.system") as mock_system:
                assert predbat_update_move(tag, ["predbat.py"]) is True
                assert mock_system.called

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_verify_staged_files_invalid_manifest(my_predbat):
    """
    Test an unreadable or malformed staged manifest fails verification
    """
    temp_dir = tempfile.mkdtemp()

    try:
        tag = "v8.30.8"
        with open(os.path.join(temp_dir, "predbat.py." + tag), "wb") as han:
            han.write(b"content\n")

        # Not a list of file entries
        with open(os.path.join(temp_dir, "manifest.yaml." + tag), "w") as han:
            han.write("this is not a manifest\n")
        assert verify_staged_files(temp_dir, ["predbat.py"], tag) is False

        # Empty manifest
        with open(os.path.join(temp_dir, "manifest.yaml." + tag), "w") as han:
            han.write("")
        assert verify_staged_files(temp_dir, ["predbat.py"], tag) is False

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_check_install_empty_manifest(my_predbat):
    """
    Test check_install returns a (passed, modified) pair when the manifest is empty

    Callers unpack two values, so returning a bare bool here used to crash the startup
    self-check rather than reporting a failed install.
    """
    temp_dir = tempfile.mkdtemp()

    try:
        with open(os.path.join(temp_dir, "manifest.yaml"), "w") as han:
            han.write("")

        with patch("download.os.path.dirname", return_value=temp_dir):
            result, modified = check_install("v8.30.8")
            assert result is False
            assert modified is False

    finally:
        shutil.rmtree(temp_dir)
    return 0


def _test_predbat_download_main_uses_configured_repository(my_predbat):
    """Test download_predbat_version('main') uses get_predbat_repository() value."""
    predbat_module = _import_predbat_module_for_tests()

    class DummyPredBat:
        def __init__(self):
            self.stop_thread = False
            self.pool = None

        def log(self, *_args, **_kwargs):
            return None

        def expose_config(self, *_args, **_kwargs):
            return None

        def get_arg(self, *_args, **_kwargs):
            return False

        def get_predbat_repository(self):
            return "example/forked-batpred"

        def call_notify(self, *_args, **_kwargs):
            return None

    dummy = DummyPredBat()
    with patch("predbat.predbat_update_download", return_value=None) as mock_download:
        predbat_module.PredBat.download_predbat_version(dummy, "main")
        assert mock_download.called
        assert mock_download.call_args.kwargs.get("repository") == "example/forked-batpred"
    return 0


def _test_predbat_download_tag_uses_default_repository(my_predbat):
    """Test download_predbat_version('<tag>') ignores override and uses upstream default."""
    predbat_module = _import_predbat_module_for_tests()

    class DummyPredBat:
        def __init__(self):
            self.stop_thread = False
            self.pool = None

        def log(self, *_args, **_kwargs):
            return None

        def expose_config(self, *_args, **_kwargs):
            return None

        def get_arg(self, *_args, **_kwargs):
            return False

        def get_predbat_repository(self):
            return "example/forked-batpred"

        def call_notify(self, *_args, **_kwargs):
            return None

    dummy = DummyPredBat()
    with patch("predbat.predbat_update_download", return_value=None) as mock_download:
        predbat_module.PredBat.download_predbat_version(dummy, "v8.30.8")
        assert mock_download.called
        assert mock_download.call_args.kwargs.get("repository") == DEFAULT_PREDBAT_REPOSITORY
    return 0


def _test_predbat_startup_self_check_uses_default_repository(my_predbat):
    """Test startup self-check/update pins repository to DEFAULT_PREDBAT_REPOSITORY at runtime."""
    with patch("download.check_install", return_value=(False, False)) as mock_check_install:
        with patch("download.predbat_update_download", return_value=["predbat.py"]) as mock_update_download:
            with patch("download.predbat_update_move", return_value=True):
                with patch("builtins.print"):
                    with patch("sys.exit", side_effect=SystemExit):
                        try:
                            if "predbat" in sys.modules:
                                importlib.reload(sys.modules["predbat"])
                            else:
                                importlib.import_module("predbat")
                        except SystemExit:
                            pass

    assert mock_check_install.called
    assert mock_check_install.call_args.kwargs.get("repository") == DEFAULT_PREDBAT_REPOSITORY

    assert mock_update_download.called
    assert mock_update_download.call_args.kwargs.get("repository") == DEFAULT_PREDBAT_REPOSITORY
    return 0
