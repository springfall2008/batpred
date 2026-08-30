# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests that a debug yaml always carries the 5-minute step arrays.

update_pred() frees load_minutes_step, load_minutes_step10, pv_forecast_minute_step and
pv_forecast_minute10_step at the end of every cycle unless switch.predbat_debug_enable is on, and
only calculate_plan()'s recompute branch ever refills them. A debug yaml downloaded from the web
interface with the switch off therefore shipped them as {}, and replaying such a dump raised
KeyError on minute 0 inside run_prediction() - so the artefact most bug reports carry could not be
replayed at all.
"""

STEP_KEYS = ["load_minutes_step", "load_minutes_step10", "pv_forecast_minute_step", "pv_forecast_minute10_step"]


def test_debug_yaml_step_data(my_predbat):
    """Verify create_debug_yaml() repopulates the step arrays when update_pred() has freed them."""
    failed = False
    print("**** Testing debug yaml step data ****")

    saved = {key: getattr(my_predbat, key, {}) for key in STEP_KEYS}
    # Set by fetch_config_options() on a running install; the test instance has not run a cycle.
    for name, value in (("metric_load_divergence", None), ("load_scaling", 1.0), ("load_scaling10", 1.1)):
        if not hasattr(my_predbat, name):
            setattr(my_predbat, name, value)
    try:
        print("Test: step arrays freed by update_pred are rebuilt for the dump")
        for key in STEP_KEYS:
            setattr(my_predbat, key, {})

        my_predbat.rebuild_debug_step_data()

        for key in STEP_KEYS:
            rebuilt = getattr(my_predbat, key)
            if not rebuilt:
                print("  ERROR: {} is still empty after the rebuild".format(key))
                failed = True
            elif 0 not in rebuilt:
                print("  ERROR: {} was rebuilt without minute 0 - run_prediction indexes it directly and would raise KeyError".format(key))
                failed = True

        print("Test: the dump itself carries them, so it can be replayed")
        for key in STEP_KEYS:
            setattr(my_predbat, key, {})
        debug_text = my_predbat.create_debug_yaml(write_file=False)
        # Checked as text rather than parsed: the dump carries python object tags, so it needs
        # yaml.unsafe_load, and "key: {}" is exactly the shape the broken dumps had.
        for key in STEP_KEYS:
            if "{}: {{}}".format(key) in debug_text:
                print("  ERROR: debug yaml carries {} as an empty dict - a dump taken with debug disabled would not replay".format(key))
                failed = True

        print("Test: a rebuild that cannot run must not cost the user their dump")
        for key in STEP_KEYS:
            setattr(my_predbat, key, {})
        saved_step_data_history = my_predbat.step_data_history

        def _raise(*args, **kwargs):
            raise AttributeError("simulated missing config")

        my_predbat.step_data_history = _raise
        try:
            debug_text = my_predbat.create_debug_yaml(write_file=False)
        except AttributeError:
            print("  ERROR: a failing step-data rebuild aborted the whole debug yaml")
            failed = True
            debug_text = ""
        finally:
            my_predbat.step_data_history = saved_step_data_history
        if debug_text and "soc_max" not in debug_text:
            print("  ERROR: debug yaml came back without its normal content after a failed rebuild")
            failed = True

        print("Test: already-populated step arrays are left alone")
        marker = {0: 1.0, 5: 2.0}
        for key in STEP_KEYS:
            setattr(my_predbat, key, dict(marker))
        my_predbat.rebuild_debug_step_data()
        for key in STEP_KEYS:
            if getattr(my_predbat, key) != marker:
                print("  ERROR: {} was rebuilt when it was already populated - the dump must reflect the plan that ran".format(key))
                failed = True
    finally:
        for key, value in saved.items():
            setattr(my_predbat, key, value)

    return failed
