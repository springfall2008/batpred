# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""PredBat self-update utilities.

Downloads PredBat source files from GitHub releases, validates installed
file integrity via SHA1 hashing, and manages the update/rollback process.

Every downloaded file is verified against the Git blob SHA published by the
GitHub directory listing before it is written to disk, and the staged files are
re-verified immediately before being installed. A file that fails verification
is never installed and the update is aborted instead.
"""

import os
import requests
import yaml
import hashlib
import tarfile
import tempfile

DEFAULT_PREDBAT_REPOSITORY = "springfall2008/batpred"

# Number of attempts made for a download that fails integrity verification
DOWNLOAD_MAX_ATTEMPTS = 3

# Hard cap on the size of the downloaded release archive, insurance against a corrupt
# or hostile response exhausting memory or disk. The real archive is around 7MB.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024

# Chunk size used when streaming the release archive to disk
ARCHIVE_CHUNK_BYTES = 256 * 1024


def resolve_predbat_repository(repository=None):
    """Resolve the GitHub repository used for Predbat self-update operations.

    Resolution order:
    1) Explicit *repository* parameter
    2) PREDBAT_REPOSITORY environment variable
    3) Default upstream repository
    """
    if repository is not None:
        repository = repository.strip()
        if repository:
            return repository

    env_repository = os.environ.get("PREDBAT_REPOSITORY")
    if env_repository is not None:
        env_repository = env_repository.strip()
        if env_repository:
            return env_repository

    return DEFAULT_PREDBAT_REPOSITORY


def get_github_directory_listing(tag, repository=None):
    """
    Get the list of files in the apps/predbat directory from GitHub.

    Args:
        tag (str): The tag to query (e.g. v1.0.0).
        repository (str, optional): GitHub repository override in "owner/repo"
            format (e.g. "springfall2008/batpred"). If not provided, the
            value is resolved via ``resolve_predbat_repository()``.

    Returns:
        list: List of file metadata dicts from GitHub API, or None on failure.
    """
    repository = resolve_predbat_repository(repository)
    url = "https://api.github.com/repos/{}/contents/apps/predbat?ref={}".format(repository, tag)
    print("Fetching directory listing from {}".format(url))
    try:
        r = requests.get(url, headers={})
        if r.ok:
            data = r.json()
            # Filter out directories, keep only files with full metadata
            files = []
            for item in data:
                if item.get("type") == "file":
                    files.append(item)
            print("Found {} files in directory".format(len(files)))
            return files
        else:
            print("Error: Failed to fetch directory listing, status code: {}".format(r.status_code))
            return None
    except Exception as e:
        print("Error: Exception while fetching directory listing: {}".format(e))
        return None


def compute_data_sha1(data):
    """
    Compute the Git blob SHA1 hash of in-memory data (matches GitHub's SHA)
    Git computes SHA as: sha1("blob " + filesize + "\0" + contents)

    Args:
        data (bytes): The raw file contents
    Returns:
        str: Git blob SHA1 hash as hex string
    """
    header = "blob {}\0".format(len(data)).encode("utf-8")
    return hashlib.sha1(header + data).hexdigest()


def compute_file_sha1(filepath):
    """
    Compute Git blob SHA1 hash of a file (matches GitHub's SHA)

    Args:
        filepath (str): Path to the file
    Returns:
        str: Git blob SHA1 hash as hex string, or None on error
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        return compute_data_sha1(data)
    except Exception as e:
        print("Error: Failed to compute SHA1 for {}: {}".format(filepath, e))
        return None


def remove_file_quietly(filepath):
    """
    Delete a file, reporting but not raising on failure.

    Used to clean up temporary downloads and staged files after a failed update.

    Args:
        filepath (str): Path to the file to remove
    """
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        print("Warn: Failed to remove {}: {}".format(filepath, e))


def remove_staged_files(this_path, files, tag):
    """
    Remove staged files, used to clean up after an aborted update.

    Staged files are the downloaded copies named "<filename>.<tag>" which sit alongside
    the installed files until the update is committed by ``predbat_update_move()``.

    Args:
        this_path (str): The Predbat install directory
        files (list): Bare filenames whose staged copies should be removed
        tag (str): The version tag used as the staged filename suffix
    """
    for filename in files:
        remove_file_quietly(os.path.join(this_path, filename + "." + tag))


def download_predbat_file_from_github(tag, filename, new_filename, repository=None, expected_sha=None, attempts=DOWNLOAD_MAX_ATTEMPTS):
    """
    Download a Predbat source file from GitHub and return the contents.

    When *expected_sha* is supplied the downloaded data is verified against it before
    anything is written, so a corrupt or truncated download never reaches disk. A
    mismatch is retried up to *attempts* times before the download is abandoned.

    Args:
        tag (str): The tag to download from (e.g. v1.0.0).
        filename (str): The filename to download (e.g. predbat.py).
        new_filename (str): The local path to save the downloaded file.
        repository (str, optional): GitHub repository override in "owner/repo"
            format (e.g. "springfall2008/batpred"). If not provided, the
            value is resolved via ``resolve_predbat_repository()``.
        expected_sha (str, optional): Expected Git blob SHA of the file contents. When
            omitted the download is not verified.
        attempts (int, optional): Number of attempts made when verification fails.

    Returns:
        bytes: The raw contents of the file, or None on failure.
    """
    repository = resolve_predbat_repository(repository)
    url = "https://raw.githubusercontent.com/{}/".format(repository) + tag + "/apps/predbat/{}".format(filename)

    for attempt in range(1, attempts + 1):
        if attempt == 1:
            print("Downloading {}".format(url))
        else:
            print("Downloading {} (attempt {} of {})".format(url, attempt, attempts))

        try:
            r = requests.get(url, headers={})
        except Exception as e:
            print("Warn: Exception while downloading {}: {}".format(url, e))
            continue

        if not r.ok:
            print("Error: Failed to download {}".format(url))
            return None

        # Use raw bytes (not r.text, which decodes through a guessed text encoding and would
        # corrupt binary files such as the prediction kernel .so binaries) and write in binary
        # mode so both source files and binaries round-trip byte-for-byte.
        data = r.content

        if expected_sha:
            actual_sha = compute_data_sha1(data)
            if actual_sha != expected_sha:
                print("Warn: Checksum mismatch for {}: expected {}, got {}".format(filename, expected_sha, actual_sha))
                continue

        print("Got data, writing to {}".format(new_filename))
        if new_filename:
            with open(new_filename, "wb") as han:
                han.write(data)
        return data

    print("Error: Failed to download {} after {} attempts".format(url, attempts))
    return None


def download_predbat_release_archive(tag, repository=None, target_dir=None):
    """
    Download the GitHub source archive for a tag to a temporary file.

    The archive is streamed to disk rather than held in memory, and is capped at
    ``MAX_ARCHIVE_BYTES`` as insurance against a corrupt or hostile response.

    The temporary file is written into the install directory rather than the system
    temporary directory, because under Home Assistant /tmp is a small tmpfs that the
    add-on may not have room in. Writing it alongside the staged files means anywhere
    with space for the update has space for the archive.

    Args:
        tag (str): The tag to download (e.g. v1.0.0).
        repository (str, optional): GitHub repository override in "owner/repo" format.
            If not provided, the value is resolved via ``resolve_predbat_repository()``.
        target_dir (str, optional): Directory to write the temporary file into. Defaults
            to the Predbat install directory.

    Returns:
        str: Path to the downloaded temporary file, which the caller is responsible for
            deleting, or None if the archive could not be fetched.
    """
    repository = resolve_predbat_repository(repository)
    if target_dir is None:
        target_dir = os.path.dirname(__file__)
    url = "https://github.com/{}/archive/{}.tar.gz".format(repository, tag)
    print("Downloading release archive {}".format(url))

    temp_path = None
    response = None
    try:
        response = requests.get(url, headers={}, stream=True)
        if not response.ok:
            # Not an error in itself, the caller downloads the files individually instead
            print("Warn: No release archive at {}, status code: {}".format(url, getattr(response, "status_code", "unknown")))
            return None

        handle, temp_path = tempfile.mkstemp(prefix="predbat-archive-", suffix=".tar.gz", dir=target_dir)
        total = 0
        with os.fdopen(handle, "wb") as archive:
            for chunk in response.iter_content(chunk_size=ARCHIVE_CHUNK_BYTES):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("Release archive exceeds the maximum allowed size of {} bytes".format(MAX_ARCHIVE_BYTES))
                archive.write(chunk)

        print("Downloaded release archive, {} bytes".format(total))
        return temp_path
    except Exception as e:
        print("Error: Exception while downloading release archive {}: {}".format(url, e))
        remove_file_quietly(temp_path)
        return None
    finally:
        # A streamed response only returns its connection to the pool once the body has
        # been consumed, so the paths that bail out early (a non-OK status, or the size cap
        # tripping part way through) would hold the socket open until garbage collection.
        # Those are exactly the paths an update retries, so close on every exit instead.
        if response is not None and hasattr(response, "close"):
            try:
                response.close()
            except Exception as e:
                print("Warn: Failed to close the release archive response: {}".format(e))


def match_archive_member(member_name):
    """
    Match a release archive member against the apps/predbat install directory.

    Archive members are named "<repo>-<ref>/apps/predbat/<filename>". Only files sitting
    directly inside apps/predbat are install candidates, so subdirectories such as tests/
    and config/, and anything elsewhere in the repository, return None.

    Args:
        member_name (str): The member path from the archive

    Returns:
        str: The bare filename, or None if the member is not an install candidate
    """
    parts = member_name.split("/")
    if len(parts) != 4:
        return None
    if parts[1] != "apps" or parts[2] != "predbat":
        return None
    filename = parts[3]
    if not filename or filename in (".", ".."):
        return None
    return filename


def extract_predbat_files_from_archive(archive_path, tag, file_list, this_path):
    """
    Extract and verify the Predbat install files from a downloaded release archive.

    Only the files named in *file_list* (the GitHub directory listing) are extracted;
    everything else in the archive is ignored, so documentation, tests and the rest of
    the repository are never installed. Each file is checked against its expected size
    and Git blob SHA before being written, and the staged path is built from the listed
    filename rather than the archive member path so a hostile member cannot escape the
    install directory.

    Args:
        archive_path (str): Path to the downloaded .tar.gz archive
        tag (str): The version tag used as the staged filename suffix
        file_list (list): GitHub directory listing entries with name/size/sha
        this_path (str): The Predbat install directory

    Returns:
        list: Bare filenames of the staged files, or None if verification failed.
    """
    wanted = {}
    for file_info in file_list:
        name = file_info.get("name")
        if name:
            wanted[name] = file_info

    staged_files = []
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            # Single forward pass over the archive, members are matched against the directory
            # listing rather than extracted wholesale, so seeking back through the compressed
            # stream is never needed and only the wanted files are ever read
            for member in archive:
                filename = match_archive_member(member.name)
                if filename is None or filename not in wanted or filename in staged_files:
                    continue

                file_info = wanted[filename]
                if not member.isfile():
                    raise ValueError("Archive member {} is not a regular file".format(member.name))

                expected_size = file_info.get("size")
                if expected_size is not None and member.size != expected_size:
                    raise ValueError("Archive member {} size mismatch: expected {}, got {}".format(filename, expected_size, member.size))

                handle = archive.extractfile(member)
                if handle is None:
                    raise ValueError("Failed to read archive member {}".format(filename))
                data = handle.read()

                expected_sha = file_info.get("sha")
                if expected_sha:
                    actual_sha = compute_data_sha1(data)
                    if actual_sha != expected_sha:
                        raise ValueError("Checksum mismatch for {}: expected {}, got {}".format(filename, expected_sha, actual_sha))

                with open(os.path.join(this_path, filename + "." + tag), "wb") as staged:
                    staged.write(data)
                staged_files.append(filename)

        missing = sorted(set(wanted) - set(staged_files))
        if missing:
            raise ValueError("Release archive is missing {} expected file(s): {}".format(len(missing), ", ".join(missing)))
    except Exception as e:
        print("Error: Failed to extract release archive: {}".format(e))
        remove_staged_files(this_path, staged_files, tag)
        return None

    print("Extracted and verified {} file(s) from the release archive".format(len(staged_files)))
    return staged_files


def download_predbat_files_from_archive(tag, file_list, this_path, repository=None):
    """
    Download and stage the Predbat files for a ref using the GitHub source archive.

    A single archive download replaces one request per file, which is around 25 times
    faster for a full update. GitHub builds an archive for any ref it knows about, so this
    is tried for every version rather than for particular ref names, and the caller falls
    back to the per-file download only if no archive comes back.

    Args:
        tag (str): The version tag or branch to download
        file_list (list): GitHub directory listing entries with name/size/sha
        this_path (str): The Predbat install directory
        repository (str, optional): GitHub repository override in "owner/repo" format

    Returns:
        tuple: (staged filenames or None, bool indicating whether an archive was obtained).
            The second element distinguishes "there is no archive to use here" from "the
            archive did not match the directory listing", which the caller handles
            differently.
    """
    archive_path = download_predbat_release_archive(tag, repository=repository, target_dir=this_path)
    if not archive_path:
        return None, False

    try:
        return extract_predbat_files_from_archive(archive_path, tag, file_list, this_path), True
    finally:
        remove_file_quietly(archive_path)


def download_predbat_files_individually(tag, file_list, this_path, repository=None):
    """
    Download and stage the Predbat files one at a time from raw.githubusercontent.com.

    Files whose installed copy already matches the expected SHA are copied rather than
    re-downloaded, which keeps development updates from the main branch cheap. Every
    downloaded file is verified against its expected Git blob SHA before being written,
    and any failure aborts the update and removes the files staged so far.

    Args:
        tag (str): The version tag to download
        file_list (list): GitHub directory listing entries with name/size/sha
        this_path (str): The Predbat install directory
        repository (str, optional): GitHub repository override in "owner/repo" format

    Returns:
        list: Bare filenames of the staged files, or None on failure.
    """
    staged_files = []
    skipped_files = []

    for file_info in file_list:
        filename = file_info["name"]
        expected_sha = file_info.get("sha")
        local_filepath = os.path.join(this_path, filename)
        download_filepath = os.path.join(this_path, filename + "." + tag)

        # Check if local file exists and has matching SHA
        skip_download = False
        if expected_sha and os.path.exists(local_filepath):
            local_sha = compute_file_sha1(local_filepath)
            if local_sha == expected_sha:
                print("Skipping {}: local file matches remote SHA ({})".format(filename, expected_sha[:8]))
                # Copy local file to download location so move operation works
                try:
                    with open(local_filepath, "rb") as src:
                        with open(download_filepath, "wb") as dst:
                            dst.write(src.read())
                    skip_download = True
                    skipped_files.append(filename)
                except Exception as e:
                    print("Warn: Failed to copy local file {}: {}".format(filename, e))
                    skip_download = False

        if not skip_download:
            if not download_predbat_file_from_github(tag, filename, download_filepath, repository=repository, expected_sha=expected_sha):
                print("Error: Failed to download {}".format(filename))
                print("Info: When updating from a branch rather than a release, a checksum mismatch can happen if a commit lands part way through the download, please retry the update")
                remove_file_quietly(download_filepath)
                remove_staged_files(this_path, staged_files, tag)
                return None

        staged_files.append(filename)

    if skipped_files:
        print("\nSkipped downloading {} file(s) (already up to date): {}".format(len(skipped_files), ", ".join(skipped_files)))

    return staged_files


def verify_staged_files(this_path, files, tag):
    """
    Verify the staged files against the staged manifest before they are installed.

    This is the final gate before the update is committed, catching a file that was
    corrupted between download and install, for example by a failed write or a full disk.

    The manifest itself is not verified because it is generated locally and is the source
    of the expected checksums. If no staged manifest is present the files cannot be
    checked, which is reported but not treated as a failure since the files were already
    verified at download time.

    Args:
        this_path (str): The Predbat install directory
        files (list): Bare filenames whose staged copies should be verified
        tag (str): The version tag used as the staged filename suffix

    Returns:
        bool: True if every staged file is present and matches, False otherwise
    """
    manifest_file = os.path.join(this_path, "manifest.yaml." + tag)
    if not os.path.exists(manifest_file):
        print("Warn: Staged manifest {} is missing, unable to verify the staged files".format(manifest_file))
        return True

    try:
        with open(manifest_file, "r") as han:
            manifest = yaml.safe_load(han)
    except Exception as e:
        print("Error: Failed to load staged manifest {}: {}".format(manifest_file, e))
        return False

    if not manifest or not isinstance(manifest, list):
        print("Error: Staged manifest {} is not valid".format(manifest_file))
        return False

    expected = {}
    for file_info in manifest:
        if isinstance(file_info, dict) and file_info.get("name"):
            expected[file_info["name"]] = file_info

    verified = True
    for filename in files:
        staged_path = os.path.join(this_path, filename + "." + tag)
        if not os.path.exists(staged_path):
            print("Error: Staged file {} is missing".format(staged_path))
            verified = False
            continue

        file_info = expected.get(filename)
        if not file_info:
            # The manifest has no entry for itself as it is generated locally
            continue

        expected_sha = file_info.get("sha")
        if not expected_sha:
            continue

        actual_sha = compute_file_sha1(staged_path)
        if actual_sha != expected_sha:
            print("Error: Staged file {} checksum mismatch: expected {}, got {}".format(staged_path, expected_sha, actual_sha))
            verified = False

    return verified


def predbat_update_move(version, files):
    """
    Move the updated files into place

    The staged files are re-verified against the staged manifest first, and nothing is
    moved unless every one of them matches, so a corrupted file is never installed.
    """
    if not files:
        return False
    tag_split = version.split(" ")
    if tag_split:
        tag = tag_split[0]
        this_path = os.path.dirname(__file__)

        if not verify_staged_files(this_path, files, tag):
            print("Error: Staged files failed verification, no files have been installed")
            return False

        cmd = ""
        for file in files:
            cmd += "mv -f {} {} && ".format(os.path.join(this_path, file + "." + tag), os.path.join(this_path, file))
        cmd += "echo 'Update complete'"
        os.system(cmd)
        return True
    return False


def check_install(version, repository=None):
    """
    Check if Predbat is installed correctly.

    Args:
        version (str): The version string (e.g. v8.30.8).
        repository (str, optional): GitHub repository override in "owner/repo"
            format (e.g. "springfall2008/batpred"). Used when downloading a
            missing manifest. If not provided, the value is resolved via
            ``resolve_predbat_repository()``.
    """
    this_path = os.path.dirname(__file__)
    manifest_file = os.path.join(this_path, "manifest.yaml")

    # Check if manifest exists
    if not os.path.exists(manifest_file):
        print("Warn: Manifest file {} is missing, bypassing checks...".format(manifest_file))
        # Try to download manifest from GitHub
        tag_split = version.split(" ")
        if tag_split:
            tag = tag_split[0]
            file_list = get_github_directory_listing(tag, repository=repository)
            if file_list:
                # Sort files alphabetically
                file_list_sorted = sorted(file_list, key=lambda x: x["name"])
                # Create manifest
                try:
                    with open(manifest_file, "w") as f:
                        yaml.dump(file_list_sorted, f, default_flow_style=False, sort_keys=False)
                    print("Downloaded and created manifest file")
                except Exception as e:
                    print("Error: Failed to write manifest: {}".format(e))
                    return True, False  # Continue without manifest
            else:
                print("Warn: Failed to download manifest from GitHub")
                return True, False  # Continue without manifest
        else:
            return True, False  # Continue without manifest

    # Load and validate against manifest
    try:
        with open(manifest_file, "r") as f:
            files = yaml.safe_load(f)

        if not files:
            print("Error: Manifest is empty")
            return False, False

        validation_passed = True
        validation_modified = False

        for file_info in files:
            filename = file_info.get("name")
            expected_size = file_info.get("size", 0)
            expected_sha = file_info.get("sha")
            filepath = os.path.join(this_path, filename)

            # Check file exists
            if not os.path.exists(filepath):
                print("Error: File {} is missing".format(filepath))
                validation_passed = False
                continue

            # Check file is not zero bytes
            actual_size = os.path.getsize(filepath)
            if actual_size == 0:
                print("Error: File {} is zero bytes".format(filepath))
                validation_passed = False
                continue

            # Warn on size mismatch but don't fail
            if actual_size != expected_size:
                print("Warn: File {} size mismatch: expected {}, got {}".format(filepath, expected_size, actual_size))
                validation_modified = True
            elif expected_sha:
                # Warn on SHA mismatch but don't fail
                actual_sha = compute_file_sha1(filepath)
                if actual_sha and actual_sha != expected_sha:
                    print("Warn: File {} SHA mismatch: expected {}, got {}".format(filepath, expected_sha, actual_sha))
                    validation_modified = True

        return validation_passed, validation_modified

    except Exception as e:
        print("Error: Failed to load manifest: {}".format(e))
        return False, False


def predbat_update_download(version, repository=None):
    """
    Download the defined version of Predbat from GitHub.

    The GitHub source archive is used whenever one is available, which is one request
    instead of one per file and around 25 times faster for a full update. GitHub builds an
    archive for any ref it knows about, so no assumption is made about which versions have
    one; if none comes back the files are downloaded individually instead, which also lets
    unchanged files be skipped.

    Every file is verified against the Git blob SHA published in the GitHub directory
    listing, and a file that fails verification aborts the whole update so nothing is
    installed. A mismatch is retried against a freshly fetched listing, because updating
    from a branch rather than a release can race with a commit landing part way through.

    Args:
        version (str): The version string (e.g. v8.30.8).
        repository (str, optional): GitHub repository override in "owner/repo"
            format (e.g. "springfall2008/batpred"). If not provided, the
            value is resolved via ``resolve_predbat_repository()``.

    Returns:
        list: Bare filenames staged for install including the manifest, or None on failure.
    """
    this_path = os.path.dirname(__file__)
    tag_split = version.split(" ")
    if not tag_split:
        return None
    tag = tag_split[0]

    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        # The listing is the authority for both which files are installed and what each of
        # them must hash to. It is fetched again on each attempt so that a ref which moved
        # under us is picked up rather than repeatedly checked against a stale listing.
        file_list = get_github_directory_listing(tag, repository=repository)
        if not file_list:
            print("Error: Failed to get file list from GitHub")
            return None

        downloaded_files, archive_available = download_predbat_files_from_archive(tag, file_list, this_path, repository=repository)

        if downloaded_files is None:
            if not archive_available:
                # No archive came back for this ref, so download the files one at a time
                print("Warn: No release archive available, downloading the files individually")
                downloaded_files = download_predbat_files_individually(tag, file_list, this_path, repository=repository)
                if downloaded_files is None:
                    return None
            else:
                # The archive was fetched but did not match the listing. Rather than install
                # it by another route, try again with a fresh listing, which also resolves a
                # branch that moved between the listing and the archive being fetched.
                print("Warn: Downloaded files did not match the directory listing (attempt {} of {})".format(attempt, DOWNLOAD_MAX_ATTEMPTS))
                continue

        # Sort files alphabetically
        file_list_sorted = sorted(file_list, key=lambda x: x["name"])

        # Generate manifest.yaml (the sorted file list the downloaded files were verified
        # against, so the pre-install check uses the same checksums)
        manifest_filename = os.path.join(this_path, "manifest.yaml." + tag)
        try:
            with open(manifest_filename, "w") as f:
                yaml.dump(file_list_sorted, f, default_flow_style=False, sort_keys=False)
            print("Generated manifest: {}".format(manifest_filename))
        except Exception as e:
            print("Error: Failed to write manifest: {}".format(e))
            remove_staged_files(this_path, downloaded_files, tag)
            return None

        # Return list of files including manifest
        downloaded_files.append("manifest.yaml")
        return downloaded_files

    print("Error: Downloaded files failed verification after {} attempts, aborting update".format(DOWNLOAD_MAX_ATTEMPTS))
    return None


def main():  # pragma: no cover
    """
    Main function for standalone testing of download functionality
    """
    import argparse
    import sys

    # Add parent directory to path so we can import download module
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    parser = argparse.ArgumentParser(description="Test Predbat download functionality")
    parser.add_argument("--check", metavar="VERSION", help="Check if Predbat is installed correctly for given version (e.g. v8.30.8)")
    parser.add_argument("--download", metavar="VERSION", help="Download Predbat version from GitHub (e.g. v8.30.8)")

    args = parser.parse_args()

    if args.check:
        print("=" * 60)
        print("Checking Predbat installation for version: {}".format(args.check))
        print("=" * 60)
        result, modified = check_install(args.check)
        if result:
            if modified:
                print("Warn: Installation check PASSED with modifications")
            else:
                print("\n✓ Installation check PASSED")
            sys.exit(0)
        else:
            print("\n✗ Installation check FAILED")
            sys.exit(1)

    elif args.download:
        print("=" * 60)
        print("Downloading Predbat version: {}".format(args.download))
        print("=" * 60)
        files = predbat_update_download(args.download)
        if files:
            print("\n✓ Download successful!")
            print("Files downloaded: {}".format(", ".join(files)))
            predbat_update_move(args.download, files)
            print("Files moved into place.")
            sys.exit(0)
        else:
            print("\n✗ Download FAILED")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
