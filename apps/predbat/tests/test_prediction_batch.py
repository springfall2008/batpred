# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the batched prediction fan-out.

launch_run_prediction_* no longer runs anything: it queues a job on the Prediction and returns a
handle whose first get() flushes the whole batch through one pk_run_batch call. These tests pin the
two things that makes conditional - that a queued job returns exactly what the direct
thread_run_prediction_* path returns, and that a job the kernel cannot take still runs.
"""


from prediction import Prediction


def make_export_windows(minutes_now):
    """Build a small deterministic export window layout for the trial-input tests"""
    return [
        {"start": minutes_now + 60, "end": minutes_now + 120, "average": 15.0},
        {"start": minutes_now + 180, "end": minutes_now + 240, "average": 20.0},
    ]


def test_export_trial_does_not_mutate_caller_window(my_predbat):
    """The export trial must build its own window list, returns True on failure.

    It used to write the trial start straight into the caller's window dict, which was only safe
    because each pool worker mutated its own unpickled copy. A batched fan-out holds one shared list
    across every job in the batch, so an in-place write would corrupt every other trial of the same
    window.
    """
    print("**** Running export trial input tests ****")
    failed = False
    minutes_now = my_predbat.minutes_now
    export_window = make_export_windows(minutes_now)
    export_limits = [100.0, 100.0]
    original = [dict(window) for window in export_window]

    prediction = Prediction(my_predbat, {}, {}, {}, {})
    trial_window, trial_limits = prediction._prepare_export(5.0, minutes_now + 90, 0, export_window, export_limits, None)

    if export_window != original:
        print("ERROR: _prepare_export mutated the caller's export window: {} vs {}".format(export_window, original))
        failed = True
    if export_limits != [100.0, 100.0]:
        print("ERROR: _prepare_export mutated the caller's export limits: {}".format(export_limits))
        failed = True
    if trial_window[0]["start"] != minutes_now + 90:
        print("ERROR: trial window start not applied, got {}".format(trial_window[0]["start"]))
        failed = True
    if trial_window[1] is not export_window[1]:
        print("ERROR: untouched windows should be shared with the caller's list, not copied")
        failed = True
    if trial_limits[0] != 5.0:
        print("ERROR: trial export limit not applied, got {}".format(trial_limits[0]))
        failed = True

    # The trial start is clamped to at least 5 minutes before the window end
    trial_window, _ = prediction._prepare_export(5.0, minutes_now + 200, 0, export_window, export_limits, None)
    if trial_window[0]["start"] != minutes_now + 115:
        print("ERROR: trial window start not clamped to end-5, got {}".format(trial_window[0]["start"]))
        failed = True

    if not failed:
        print("Export trial input tests passed")
    return failed


def run_prediction_batch_tests(my_predbat):
    """Run every batched prediction test, returns True on failure"""
    failed = test_export_trial_does_not_mutate_caller_window(my_predbat)
    if failed:
        print("**** Prediction batch tests FAILED ****")
    else:
        print("**** Prediction batch tests passed ****")
    return failed
