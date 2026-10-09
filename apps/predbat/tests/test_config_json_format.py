# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Tests that predbat_config.json is written one setting per line, so it can be diffed and kept in git, and that it
still loads back to the same settings.
"""

import json
import os
import tempfile


def test_config_json_format(my_predbat):
    """save_current_config() writes indented JSON ending in a newline, and load_current_config() reads it back."""
    failed = False
    print("**** test_config_json_format ****")

    original_config_root = my_predbat.config_root
    original_config_root_p = my_predbat.config_root_p
    original_db_primary = my_predbat.ha_interface.db_primary
    item = my_predbat.config_index["load_scaling"]
    original_value = item["value"]

    try:
        with tempfile.TemporaryDirectory() as tmp_root:
            my_predbat.config_root = tmp_root
            my_predbat.config_root_p = tmp_root
            my_predbat.ha_interface.db_primary = False
            item["value"] = 1.23

            my_predbat.save_current_config()
            path = os.path.join(tmp_root, "predbat_config.json")
            with open(path) as handle:
                text = handle.read()
            settings = json.loads(text)

            if text != json.dumps(settings, indent=2) + "\n":
                print("ERROR: expected indented JSON ending in a newline, got {!r}".format(text[:120]))
                failed = True
            if '  "load_scaling": 1.23,' not in text.splitlines() and '  "load_scaling": 1.23' not in text.splitlines():
                print("ERROR: expected load_scaling on its own indented line")
                failed = True

            # The indented file loads back to the same value
            item["value"] = 9.99
            my_predbat.load_current_config()
            if item["value"] != 1.23:
                print("ERROR: the indented file should load back load_scaling 1.23, got {}".format(item["value"]))
                failed = True
    finally:
        my_predbat.config_root = original_config_root
        my_predbat.config_root_p = original_config_root_p
        my_predbat.ha_interface.db_primary = original_db_primary
        item["value"] = original_value

    return failed
