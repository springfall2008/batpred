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

from download import get_github_directory_listing, check_install, predbat_update_download, compute_file_sha1


def test_get_github_directory_listing_success(my_predbat):
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


def test_get_github_directory_listing_failure(my_predbat):
    """
    Test GitHub API failure
    """
    with patch("requests.get") as mock_get:
        mock_get.return_value.ok = False
        mock_get.return_value.status_code = 404

        result = get_github_directory_listing("v8.30.8")

        assert result is None


def test_get_github_directory_listing_exception(my_predbat):
    """
    Test GitHub API exception handling
    """
    with patch("requests.get") as mock_get:
        mock_get.side_effect = Exception("Network error")

        result = get_github_directory_listing("v8.30.8")

        assert result is None


def test_compute_file_sha1(my_predbat):
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


def test_compute_file_sha1_missing_file(my_predbat):
    """
    Test SHA1 computation on missing file
    """
    sha1 = compute_file_sha1("/nonexistent/file.txt")
    assert sha1 is None


def test_check_install_with_valid_manifest(my_predbat):
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
            result = check_install("v8.30.8")
            assert result is True

    finally:
        shutil.rmtree(temp_dir)


def test_check_install_missing_file(my_predbat):
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
            result = check_install("v8.30.8")
            assert result is False

    finally:
        shutil.rmtree(temp_dir)


def test_check_install_zero_byte_file(my_predbat):
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
            result = check_install("v8.30.8")
            assert result is False

    finally:
        shutil.rmtree(temp_dir)


def test_check_install_size_mismatch(my_predbat):
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
            result = check_install("v8.30.8")
            assert result is True  # Should pass with warning

    finally:
        shutil.rmtree(temp_dir)


def test_check_install_sha_mismatch(my_predbat):
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
            result = check_install("v8.30.8")
            assert result is True  # Should pass with warning

    finally:
        shutil.rmtree(temp_dir)


def test_check_install_no_manifest_downloads(my_predbat):
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
                result = check_install("v8.30.8")
                assert result is True
                # Check manifest was created
                assert os.path.exists(os.path.join(temp_dir, "manifest.yaml"))

    finally:
        shutil.rmtree(temp_dir)


def test_predbat_update_download_success(my_predbat):
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


def test_predbat_update_download_api_failure(my_predbat):
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


def test_predbat_update_download_file_failure(my_predbat):
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


# Test registry for the test runner
TEST_FUNCTIONS = [
    test_get_github_directory_listing_success,
    test_get_github_directory_listing_failure,
    test_get_github_directory_listing_exception,
    test_compute_file_sha1,
    test_compute_file_sha1_missing_file,
    test_check_install_with_valid_manifest,
    test_check_install_missing_file,
    test_check_install_zero_byte_file,
    test_check_install_size_mismatch,
    test_check_install_sha_mismatch,
    test_check_install_no_manifest_downloads,
    test_predbat_update_download_success,
    test_predbat_update_download_api_failure,
    test_predbat_update_download_file_failure,
]
