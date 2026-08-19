# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Tests for userinterface.py's is_new_install() (Bug B from #4397/#4396, also seen
independently as #3259 and #3306).

A restart can catch HA's state store before it has fully warmed back up, so a single
failed predbat.status read is not reliable evidence of a genuinely new install on its
own. Misdetecting an existing install as new resets live config items (e.g. mode) back
to their defaults. predbat_config.json only exists once Predbat has actually saved a
config before, so its presence is used as a second, persistent signal.
"""

import json
import os
import tempfile


def test_new_install_detection(my_predbat):
    """Tests is_new_install() isn't fooled by a transient predbat.status read failure when predbat_config.json proves this is a real install."""
    failed = False
    print("**** test_new_install_detection ****")

    original_load_previous_value_from_ha = my_predbat.load_previous_value_from_ha
    original_config_root = my_predbat.config_root
    original_db_primary = my_predbat.ha_interface.db_primary

    def status_unavailable(entity, attribute=None):
        if entity == my_predbat.prefix + ".status":
            return None
        return original_load_previous_value_from_ha(entity, attribute=attribute)

    try:
        my_predbat.load_previous_value_from_ha = status_unavailable
        my_predbat.ha_interface.db_primary = False

        with tempfile.TemporaryDirectory() as tmp_root:
            my_predbat.config_root = tmp_root

            print("  [no predbat_config.json] genuinely fresh install - predbat.status unavailable and nothing persisted")
            if not my_predbat.is_new_install():
                print("ERROR: expected a new install to be detected when predbat.status is unavailable and no predbat_config.json exists")
                failed = True

            config_path = os.path.join(tmp_root, "predbat_config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"battery_capacity": 10.0}, f)

            print("  [predbat_config.json exists] existing install is not misdetected as new even though predbat.status is unavailable")
            if my_predbat.is_new_install():
                print("ERROR: expected an existing install (predbat_config.json present) to NOT be treated as new, even with predbat.status unavailable")
                failed = True

            print("  [db_primary mode] the JSON-file signal is skipped - DB-primary installs don't use predbat_config.json")
            my_predbat.ha_interface.db_primary = True
            if not my_predbat.is_new_install():
                print("ERROR: expected db_primary mode to fall back to new-install detection, ignoring predbat_config.json")
                failed = True
            my_predbat.ha_interface.db_primary = False

        print("  [predbat.status available] a real status read is still authoritative regardless of predbat_config.json")
        my_predbat.load_previous_value_from_ha = original_load_previous_value_from_ha
        if my_predbat.is_new_install():
            print("ERROR: expected an install with a readable predbat.status to not be treated as new")
            failed = True
    finally:
        my_predbat.load_previous_value_from_ha = original_load_previous_value_from_ha
        my_predbat.config_root = original_config_root
        my_predbat.ha_interface.db_primary = original_db_primary

    print("**** test_new_install_detection PASSED ****" if not failed else "**** test_new_install_detection FAILED ****")
    return failed
