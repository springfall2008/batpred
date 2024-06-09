# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
import os
import re
import time
import math
import requests


def download_predbat_file_from_github(tag, filename, new_filename):
    """
    Downloads a predbat source file from github and returns the contents

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


def predbat_update_download(version):
    """
    Download the defined version of Predbat from Github
    """
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
