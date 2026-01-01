# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import sys
import tempfile
import shutil
from unittest.mock import patch
import yaml

# Add parent directory to path for standalone execution
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from download import get_github_directory_listing, check_install, predbat_update_download, compute_file_sha1, download_predbat_file_from_github, predbat_update_move


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
        ("github_listing_success", _test_get_github_directory_listing_success, "GitHub directory listing success"),
        ("github_listing_failure", _test_get_github_directory_listing_failure, "GitHub API failure (404)"),
        ("github_listing_exception", _test_get_github_directory_listing_exception, "GitHub API exception handling"),
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
        ("download_file_failure", _test_download_predbat_file_failure, "Download file failure"),
        ("download_file_no_filename", _test_download_predbat_file_no_filename, "Download file no filename"),
        ("update_move_success", _test_predbat_update_move_success, "Move files success"),
        ("update_move_empty", _test_predbat_update_move_empty_files, "Move files empty list"),
        ("update_move_none", _test_predbat_update_move_none_files, "Move files none list"),
        ("update_move_invalid_version", _test_predbat_update_move_invalid_version, "Move files invalid version"),
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
    Test successful download of all files
    """
    temp_dir = tempfile.mkdtemp()

    try:
        # Mock GitHub API responses
        mock_files = [{"name": "predbat.py", "size": 1000, "sha": "abc123", "type": "file"}, {"name": "config.py", "size": 500, "sha": "def456", "type": "file"}]

        with patch("download.os.path.dirname", return_value=temp_dir):
            with patch("download.get_github_directory_listing", return_value=mock_files):
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

        # Mock successful HTTP response
        mock_response = type("MockResponse", (), {"ok": True, "text": 'print("test file content")\n'})()

        with patch("download.requests.get", return_value=mock_response):
            result = download_predbat_file_from_github("v8.30.8", "test.py", output_file)

            # Verify file was written
            assert os.path.exists(output_file)
            with open(output_file, "r") as f:
                content = f.read()
            assert content == 'print("test file content")\n'
            assert result == 'print("test file content")\n'

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
    mock_response = type("MockResponse", (), {"ok": True, "text": 'print("test file content")\n'})()

    with patch("download.requests.get", return_value=mock_response):
        result = download_predbat_file_from_github("v8.30.8", "test.py", None)
        assert result == 'print("test file content")\n'
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
