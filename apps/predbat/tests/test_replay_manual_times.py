# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests that manual_all_times is always derived from the per-type manual time lists.

The optimiser leaves every window in manual_all_times alone. It used to be stored, rebuilt by fetch each cycle,
and a debug dump saved that copy: replaying a dump with a manual override cleared from its list still held the
override's slots back from the optimiser, as the stored copy was never rebuilt (seen replaying the #5943 dump).
"""

import os
import tempfile

import yaml

from tests.test_infra import clear_manual_times
from tests.test_single_debug import load_debug_dump

MANUAL_TIME_LISTS = ("manual_charge_times", "manual_export_times", "manual_demand_times", "manual_freeze_charge_times", "manual_freeze_export_times")


def _write_dump(directory, name, manual):
    """Write a dump holding only the given manual time keys (and the config list every dump carries) to directory, returning its path."""
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        yaml.safe_dump(dict(manual, CONFIG_ITEMS=[]), handle)
    return path


def run_replay_manual_times_tests(my_predbat):
    """Run the manual_all_times derivation tests. Returns True on failure."""
    print("**** Running manual_all_times derivation tests ****")
    failed = False

    # Derived from all five lists, and following any change to one of them
    my_predbat.manual_charge_times = [600]
    my_predbat.manual_export_times = [1020]
    my_predbat.manual_demand_times = [1050]
    my_predbat.manual_freeze_charge_times = [660]
    my_predbat.manual_freeze_export_times = [1080]
    if sorted(my_predbat.manual_all_times) != [600, 660, 1020, 1050, 1080]:
        print("ERROR: manual_all_times should hold every per-type manual slot, got {}".format(my_predbat.manual_all_times))
        failed = True
    my_predbat.manual_demand_times = []
    if 1050 in my_predbat.manual_all_times:
        print("ERROR: manual_all_times should follow a cleared list, got {}".format(my_predbat.manual_all_times))
        failed = True
    clear_manual_times(my_predbat)
    if my_predbat.manual_all_times:
        print("ERROR: manual_all_times should be empty once every list is, got {}".format(my_predbat.manual_all_times))
        failed = True

    # Read-only, so nothing can set it out of step with the lists
    try:
        my_predbat.manual_all_times = [1020]
        print("ERROR: manual_all_times should not be assignable")
        failed = True
    except AttributeError:
        pass

    from unit_test import create_predbat

    all_lists_empty = {name: [] for name in MANUAL_TIME_LISTS}
    cases = [
        # A demand override cleared from the dump's list is cleared from the optimiser too, whatever copy the dump saved
        ("edited", dict(all_lists_empty, manual_all_times=[1020, 1050]), []),
        # An unedited dump replays as captured
        ("unedited", dict(all_lists_empty, manual_demand_times=[1020, 1050], manual_all_times=[1020, 1050]), [1020, 1050]),
    ]
    with tempfile.TemporaryDirectory() as directory:
        for name, manual, expected in cases:
            replay = create_predbat()
            logged = []
            original_log = replay.log
            replay.log = lambda message, *args, **kwargs: logged.append(message)
            try:
                load_debug_dump(replay, _write_dump(directory, name + ".yaml", manual))
            finally:
                replay.log = original_log
            # Flagged when the saved copy disagrees with what the rest of the dump derives, and only then
            warned = [message for message in logged if "saved manual_all_times" in message]
            if bool(warned) != (name == "edited"):
                print("ERROR: replaying the {} dump should {}warn that its saved manual_all_times differs, logged {}".format(name, "" if name == "edited" else "not ", warned))
                failed = True
            if sorted(replay.manual_all_times) != expected:
                print("ERROR: replaying the {} dump should leave manual_all_times {}, got {}".format(name, expected, replay.manual_all_times))
                failed = True
            # The dump's saved copy is not restored, so nothing walking the instance's attributes can find it
            if "manual_all_times" in replay.__dict__:
                print("ERROR: replaying the {} dump restored its saved manual_all_times {}".format(name, replay.__dict__["manual_all_times"]))
                failed = True
            # A new dump still writes it, derived, for an older Predbat replaying the dump to read
            dumped = yaml.load(replay.create_debug_yaml(write_file=False), Loader=yaml.Loader)
            if sorted(dumped.get("manual_all_times", [None])) != expected:
                print("ERROR: a dump written after replaying the {} dump should carry manual_all_times {}, got {}".format(name, expected, dumped.get("manual_all_times")))
                failed = True

    if not failed:
        print("PASSED: manual_all_times derivation tests")
    return failed
