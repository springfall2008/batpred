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
import urllib.request
import shutil
import tempfile


def download_predbat_file_from_github(tag, filename, new_filename):
    """
    Downloads a predbat source file from github and returns the contents
    Now supports files in subdirectories.

    Args:
        tag (str): The tag to download from (e.g. v1.0.0)
        filename (str): The filename to download (e.g. predbat.py or utils/battery_manager.py)
        new_filename (str): The new filename to save the file as
    Returns:
        str: The contents of the file
    """
    # Handle both flat files and files in directories
    url_path = filename.replace(os.sep, "/")  # Ensure forward slashes for URL
    url = "https://raw.githubusercontent.com/springfall2008/batpred/" + tag + "/apps/predbat/{}".format(url_path)
    print("Downloading {}".format(url))
    r = requests.get(url, headers={})
    if r.ok:
        data = r.text
        print("Got data, writing to {}".format(new_filename))
        if new_filename:
            # Create directory if needed
            dir_path = os.path.dirname(new_filename)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path)
            with open(new_filename, "w") as han:
                han.write(data)
        return data
    else:
        print("Error: Failed to download {}".format(url))
        return None


def predbat_update_move(version, backup_path_or_files):
    """
    Move the updated files into place.
    Handles both zip-based (backup_path) and individual file approaches.
    """
    tag_split = version.split(" ")
    if tag_split:
        tag = tag_split[0]
        this_path = os.path.dirname(__file__)

        # Check if we have a backup path (zip method) or file list (individual method)
        if isinstance(backup_path_or_files, str) and os.path.isdir(backup_path_or_files):
            # Zip method - copy from backup directory
            backup_path = backup_path_or_files
            print("Moving files from backup directory: {}".format(backup_path))

            # Copy all files from backup to current directory
            for root, dirs, files in os.walk(backup_path):
                for file in files:
                    source = os.path.join(root, file)
                    # Calculate relative path from backup_path
                    rel_path = os.path.relpath(source, backup_path)
                    dest = os.path.join(this_path, rel_path)

                    # Create destination directory if needed
                    dest_dir = os.path.dirname(dest)
                    if dest_dir and dest_dir != this_path and not os.path.exists(dest_dir):
                        os.makedirs(dest_dir)

                    # Copy the file
                    shutil.copy2(source, dest)
                    print("Copied {} to {}".format(rel_path, dest))

            # Clean up backup directory
            shutil.rmtree(backup_path)
            print("Cleaned up backup directory")

        else:
            # Individual file method (backward compatibility)
            files = backup_path_or_files
            print("Moving individual files with version suffix")

            # Process files, creating directories as needed
            for file in files:
                source = os.path.join(this_path, file + "." + tag)
                dest = os.path.join(this_path, file)

                # Create destination directory if needed
                dest_dir = os.path.dirname(dest)
                if dest_dir and dest_dir != this_path and not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)

                # Move the file
                if os.path.exists(source):
                    os.rename(source, dest)
                    print("Moved {} to {}".format(source, dest))
                else:
                    print("Warning: Source file {} not found".format(source))

        print("Update complete")
        return True
    return False


def get_files_from_predbat(predbat_code):
    files = ["predbat.py"]
    for line in predbat_code.split("\n"):
        if line.startswith("PREDBAT_FILES"):
            files = line.split("=")[1].strip()
            files = files.replace("[", "")
            files = files.replace("]", "")
            files = files.replace('"', "")
            files = files.replace(" ", "")
            files = files.split(",")
            break
    return files


def check_install():
    """
    Check if Predbat is installed correctly
    Now supports files in subdirectories.
    """
    this_path = os.path.dirname(__file__)
    predbat_file = os.path.join(this_path, "predbat.py")
    if os.path.exists(predbat_file):
        with open(predbat_file, "r") as han:
            predbat_code = han.read()
            files = get_files_from_predbat(predbat_code)
            for file in files:
                filepath = os.path.join(this_path, file)
                if not os.path.exists(filepath):
                    print("Error: File {} is missing".format(filepath))
                    return False
                if os.path.getsize(filepath) == 0:
                    print("Error: File {} is zero bytes".format(filepath))
                    return False
        return True
    return False


def predbat_update_download_zip(version):
    """
    Download the defined version of Predbat from Github using zip method (like addon).
    This supports directory structures automatically.
    """
    this_path = os.path.dirname(__file__)
    tag_split = version.split(" ")
    if tag_split:
        tag = tag_split[0]

        print("Downloading Predbat {} using zip method...".format(version))

        # Download entire repository as zip
        download_url = "https://github.com/springfall2008/batpred/archive/refs/tags/{}.zip".format(tag)

        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "predbat_{}.zip".format(tag))

            try:
                print("Downloading {}".format(download_url))
                urllib.request.urlretrieve(download_url, zip_path)
                print("Predbat downloaded successfully")
            except Exception as e:
                print("Error: Unable to download Predbat - {}".format(e))
                return None

            print("Extracting Predbat...")
            extract_path = os.path.join(temp_dir, "extract")
            os.makedirs(extract_path)
            shutil.unpack_archive(zip_path, extract_path)

            # Find the extracted directory (batpred-X.Y.Z format)
            repo_path = os.path.join(extract_path, "batpred-{}".format(tag.replace("v", "")))
            predbat_source = os.path.join(repo_path, "apps", "predbat")

            if not os.path.exists(predbat_source):
                print("Error: Could not find predbat source at {}".format(predbat_source))
                return None

            # Copy only Python files selectively (safe approach)
            backup_path = os.path.join(this_path, "backup_{}".format(tag))
            if os.path.exists(backup_path):
                shutil.rmtree(backup_path)
            os.makedirs(backup_path)

            print("Copying Python files to {}...".format(backup_path))

            # Copy main *.py files from root directory
            for item in os.listdir(predbat_source):
                source_path = os.path.join(predbat_source, item)
                dest_path = os.path.join(backup_path, item)

                if os.path.isfile(source_path) and item.endswith(".py"):
                    # Copy only Python files
                    shutil.copy2(source_path, dest_path)
                    print("  Copied file: {}".format(item))
                elif os.path.isdir(source_path) and item not in ["config", "__pycache__", ".ruff_cache", ".git"]:
                    # Copy subdirectories but only *.py files within them
                    os.makedirs(dest_path)
                    print("  Created directory: {}".format(item))
                    for subitem in os.listdir(source_path):
                        if subitem.endswith(".py"):
                            sub_source = os.path.join(source_path, subitem)
                            sub_dest = os.path.join(dest_path, subitem)
                            shutil.copy2(sub_source, sub_dest)
                            print("    Copied: {}/{}".format(item, subitem))

            # Note: Only *.py files copied, no config or other file types

            print("Download and extraction completed successfully")
            return backup_path

    return None


def predbat_update_download(version):
    """
    Download the defined version of Predbat from Github.
    Uses zip method for better directory support.
    """
    # Use zip method (more reliable with directories)
    backup_path = predbat_update_download_zip(version)
    if backup_path:
        return backup_path

    # Fallback to original method for backward compatibility
    print("Zip method failed, trying individual file download...")
    this_path = os.path.dirname(__file__)
    tag_split = version.split(" ")
    if tag_split:
        tag = tag_split[0]

        # Download predbat.py
        file = "predbat.py"
        predbat_code = download_predbat_file_from_github(tag, file, os.path.join(this_path, file + "." + tag))
        if predbat_code:
            # Get the list of other files to download by searching for PREDBAT_FILES in predbat.py
            files = get_files_from_predbat(predbat_code)

            # Download the remaining files
            if files:
                for file in files:
                    # Download the remaining files
                    if file != "predbat.py":
                        if not download_predbat_file_from_github(tag, file, os.path.join(this_path, file + "." + tag)):
                            return None
            return files
    return None
