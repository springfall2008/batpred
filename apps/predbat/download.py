# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import requests
import yaml
import hashlib


def get_github_directory_listing(tag):
    """
    Get the list of files in the apps/predbat directory from GitHub

    Args:
        tag (str): The tag to query (e.g. v1.0.0)
    Returns:
        list: List of file metadata dicts from GitHub API, or None on failure
    """
    url = "https://api.github.com/repos/springfall2008/batpred/contents/apps/predbat?ref={}".format(tag)
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


def compute_file_sha1(filepath):
    """
    Compute Git blob SHA1 hash of a file (matches GitHub's SHA)
    Git computes SHA as: sha1("blob " + filesize + "\0" + contents)

    Args:
        filepath (str): Path to the file
    Returns:
        str: Git blob SHA1 hash as hex string, or None on error
    """
    try:
        sha1 = hashlib.sha1()
        with open(filepath, "rb") as f:
            data = f.read()

        # Compute Git blob SHA: sha1("blob " + size + "\0" + contents)
        header = "blob {}\0".format(len(data)).encode("utf-8")
        sha1.update(header + data)
        return sha1.hexdigest()
    except Exception as e:
        print("Error: Failed to compute SHA1 for {}: {}".format(filepath, e))
        return None


def download_predbat_file_from_github(tag, filename, new_filename):
    """
    Downloads a Predbat source file from GitHub and returns the contents

    Args:
        tag (str): The tag to download from (e.g. v1.0.0)
        filename (str): The filename to download (e.g. predbat.py)
        new_filename (str): The new filename to save the file as
    Returns:
        str: The contents of the file
    """
    url = "https://raw.githubusercontent.com/springfall2008/batpred/" + tag + "/apps/predbat/{}".format(filename)
    print("Downloading {}".format(url))
    r = requests.get(url, headers={})
    if r.ok:
        data = r.text
        print("Got data, writing to {}".format(new_filename))
        if new_filename:
            with open(new_filename, "w") as han:
                han.write(data)
        return data
    else:
        print("Error: Failed to download {}".format(url))
        return None


def predbat_update_move(version, files):
    """
    Move the updated files into place
    """
    if not files:
        return False
    tag_split = version.split(" ")
    if tag_split:
        tag = tag_split[0]
        this_path = os.path.dirname(__file__)
        cmd = ""
        for file in files:
            cmd += "mv -f {} {} && ".format(os.path.join(this_path, file + "." + tag), os.path.join(this_path, file))
        cmd += "echo 'Update complete'"
        os.system(cmd)
        return True
    return False


def check_install(version):
    """
    Check if Predbat is installed correctly

    Args:
        version (str): The version string (e.g. v8.30.8)
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
            file_list = get_github_directory_listing(tag)
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
                    return True  # Continue without manifest
            else:
                print("Warn: Failed to download manifest from GitHub")
                return True  # Continue without manifest
        else:
            return True  # Continue without manifest

    # Load and validate against manifest
    try:
        with open(manifest_file, "r") as f:
            files = yaml.safe_load(f)

        if not files:
            print("Error: Manifest is empty")
            return False

        validation_passed = True

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

            # Warn on SHA mismatch but don't fail
            if expected_sha:
                actual_sha = compute_file_sha1(filepath)
                if actual_sha and actual_sha != expected_sha:
                    print("Warn: File {} SHA mismatch: expected {}, got {}".format(filepath, expected_sha, actual_sha))

        return validation_passed

    except Exception as e:
        print("Error: Failed to load manifest: {}".format(e))
        return False


def predbat_update_download(version):
    """
    Download the defined version of Predbat from GitHub
    """
    this_path = os.path.dirname(__file__)
    tag_split = version.split(" ")
    if tag_split:
        tag = tag_split[0]

        # Get the list of files from GitHub API
        file_list = get_github_directory_listing(tag)
        if not file_list:
            print("Error: Failed to get file list from GitHub")
            return None

        # Download all files
        downloaded_files = []
        for file_info in file_list:
            filename = file_info["name"]
            if not download_predbat_file_from_github(tag, filename, os.path.join(this_path, filename + "." + tag)):
                print("Error: Failed to download {}".format(filename))
                return None
            downloaded_files.append(filename)

        # Sort files alphabetically
        file_list_sorted = sorted(file_list, key=lambda x: x["name"])

        # Generate manifest.yaml (just the sorted file list from GitHub API)
        manifest_filename = os.path.join(this_path, "manifest.yaml." + tag)
        try:
            with open(manifest_filename, "w") as f:
                yaml.dump(file_list_sorted, f, default_flow_style=False, sort_keys=False)
            print("Generated manifest: {}".format(manifest_filename))
        except Exception as e:
            print("Error: Failed to write manifest: {}".format(e))
            return None

        # Return list of files including manifest
        downloaded_files.append("manifest.yaml")
        return downloaded_files
    return None


def main():
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
        result = check_install(args.check)
        if result:
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
