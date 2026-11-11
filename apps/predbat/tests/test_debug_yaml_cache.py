# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""Tests for the debug dump parse cache the unit test runner switches on (userinterface.DEBUG_YAML_PARSE_CACHE)."""

import os
import tempfile

import yaml

import userinterface

DUMP = {"minutes_now": 5, "cache_probe_list": [1, 2], "CONFIG_ITEMS": []}


def _write(path, data):
    """Write data to path as a debug dump."""
    with open(path, "w") as file:
        yaml.dump(data, file)


def test_debug_yaml_cache(my_predbat):
    """A cached parse is reused, re-read when the file changes, never shared with a replay, and off unless switched on."""
    failed = False
    print("**** Testing the debug dump parse cache ****")
    saved_cache = userinterface.DEBUG_YAML_PARSE_CACHE
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "predbat_debug_cache.yaml")
        _write(path, DUMP)
        try:
            userinterface.DEBUG_YAML_PARSE_CACHE = None
            if userinterface.load_debug_yaml_file(path) is userinterface.load_debug_yaml_file(path):
                print("ERROR: with the cache off, each load should parse afresh")
                failed = True

            userinterface.DEBUG_YAML_PARSE_CACHE = {}
            first = userinterface.load_debug_yaml_file(path)
            if userinterface.load_debug_yaml_file(path) is not first:
                print("ERROR: with the cache on, a second load should reuse the first parse")
                failed = True

            # A replay copies what it takes, so changing the replayed instance leaves the cached parse alone
            my_predbat.read_debug_yaml(path)
            my_predbat.cache_probe_list.append(3)
            my_predbat.read_debug_yaml(path)
            if my_predbat.cache_probe_list != [1, 2] or first["cache_probe_list"] != [1, 2]:
                print("ERROR: a replay changed the cached parse: instance {} cache {}".format(my_predbat.cache_probe_list, first["cache_probe_list"]))
                failed = True

            # A rewritten file is parsed again (the size differs, so this holds on a coarse-grained clock too)
            _write(path, dict(DUMP, cache_probe_list=[1, 2, 3, 4]))
            if userinterface.load_debug_yaml_file(path)["cache_probe_list"] != [1, 2, 3, 4]:
                print("ERROR: a changed file should be parsed again, not served from the cache")
                failed = True
        finally:
            userinterface.DEBUG_YAML_PARSE_CACHE = saved_cache
    return failed
