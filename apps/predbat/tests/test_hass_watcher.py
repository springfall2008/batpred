# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Tests for hass.py's standalone-mode file watcher (collect_watch_files/resolve_apps_yaml_path).

Covers #4397/#4396: the watcher used to match ANY file named "apps.yaml" anywhere in the
watched directory tree, so a tool (e.g. the Annual prediction tool) writing its own
apps.yaml-shaped scratch file elsewhere under the same tree would be indistinguishable from
the real config file, forcing an unwanted restart of the live instance whenever it ran.
"""

import os
import tempfile

from hass import collect_watch_files, resolve_apps_yaml_path


def test_hass_watcher(my_predbat):
    """Tests collect_watch_files() only matches the one real apps.yaml, plus .py files."""
    failed = False
    print("**** test_hass_watcher ****")

    with tempfile.TemporaryDirectory() as root:
        # Real apps.yaml at the top level, plus a .py file
        real_apps_yaml = os.path.join(root, "apps.yaml")
        with open(real_apps_yaml, "w", encoding="utf-8") as f:
            f.write("pred_bat:\n  timezone: Europe/London\n")

        source_py = os.path.join(root, "predbat.py")
        with open(source_py, "w", encoding="utf-8") as f:
            f.write("# source file\n")

        # A scratch subdirectory (mirroring the Annual tool's work_dir) with its own
        # unrelated apps.yaml-shaped file, plus a hidden dotfile that should never be watched
        scratch_dir = os.path.join(root, "annual_work")
        os.makedirs(scratch_dir)
        scratch_apps_yaml = os.path.join(scratch_dir, "apps.yaml")
        with open(scratch_apps_yaml, "w", encoding="utf-8") as f:
            f.write("pred_bat:\n  timezone: Europe/London\n")

        hidden_py = os.path.join(root, ".hidden.py")
        with open(hidden_py, "w", encoding="utf-8") as f:
            f.write("# should never be watched\n")

        apps_file_path = os.path.abspath(real_apps_yaml)
        watched = collect_watch_files([root], apps_file_path)
        watched_abs = set(os.path.abspath(p) for p in watched)

        print("  [real apps.yaml] the actual configured apps.yaml is watched")
        if os.path.abspath(real_apps_yaml) not in watched_abs:
            print(f"ERROR: real apps.yaml {real_apps_yaml} should be watched, got {watched_abs}")
            failed = True

        print("  [source .py file] source files are still watched")
        if os.path.abspath(source_py) not in watched_abs:
            print(f"ERROR: source file {source_py} should be watched, got {watched_abs}")
            failed = True

        print("  [scratch apps.yaml] a same-named file elsewhere in the tree is NOT watched (#4397/#4396)")
        if os.path.abspath(scratch_apps_yaml) in watched_abs:
            print(f"ERROR: scratch apps.yaml {scratch_apps_yaml} should NOT be watched, got {watched_abs}")
            failed = True

        print("  [hidden dotfile] dotfiles are never watched, even if they end in .py")
        if os.path.abspath(hidden_py) in watched_abs:
            print(f"ERROR: hidden file {hidden_py} should NOT be watched, got {watched_abs}")
            failed = True

    print("  [apps.yaml outside the walked roots] still watched explicitly (Copilot review on #4401)")
    with tempfile.TemporaryDirectory() as walked_root, tempfile.TemporaryDirectory() as outside_root:
        outside_apps_yaml = os.path.join(outside_root, "apps.yaml")
        with open(outside_apps_yaml, "w", encoding="utf-8") as f:
            f.write("pred_bat:\n  timezone: Europe/London\n")

        watched = collect_watch_files([walked_root], outside_apps_yaml)
        watched_abs = set(os.path.abspath(p) for p in watched)
        if os.path.abspath(outside_apps_yaml) not in watched_abs:
            print(f"ERROR: apps.yaml outside the walked roots should still be watched, got {watched_abs}")
            failed = True

    print("  [apps.yaml that does not exist] not added if the configured path is missing entirely")
    with tempfile.TemporaryDirectory() as walked_root:
        missing_apps_yaml = os.path.join(walked_root, "does_not_exist", "apps.yaml")
        watched = collect_watch_files([walked_root], missing_apps_yaml)
        watched_abs = set(os.path.abspath(p) for p in watched)
        if os.path.abspath(missing_apps_yaml) in watched_abs:
            print(f"ERROR: a non-existent apps.yaml path should not be added, got {watched_abs}")
            failed = True

    print("  [resolve_apps_yaml_path] defaults to 'apps.yaml' in the working directory when PREDBAT_APPS_FILE is unset")
    saved_env = os.environ.pop("PREDBAT_APPS_FILE", None)
    try:
        resolved = resolve_apps_yaml_path()
        expected = os.path.abspath("apps.yaml")
        if resolved != expected:
            print(f"ERROR: expected {expected}, got {resolved}")
            failed = True
    finally:
        if saved_env is not None:
            os.environ["PREDBAT_APPS_FILE"] = saved_env

    print("  [resolve_apps_yaml_path] honours PREDBAT_APPS_FILE when set (e.g. by the Annual tool's headless instance)")
    saved_env = os.environ.get("PREDBAT_APPS_FILE")
    try:
        os.environ["PREDBAT_APPS_FILE"] = "/tmp/some_other_dir/apps.yaml"
        resolved = resolve_apps_yaml_path()
        expected = os.path.abspath("/tmp/some_other_dir/apps.yaml")
        if resolved != expected:
            print(f"ERROR: expected {expected}, got {resolved}")
            failed = True
    finally:
        if saved_env is None:
            os.environ.pop("PREDBAT_APPS_FILE", None)
        else:
            os.environ["PREDBAT_APPS_FILE"] = saved_env

    print("**** test_hass_watcher PASSED ****" if not failed else "**** test_hass_watcher FAILED ****")
    return failed
