# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def _run_guard(my_predbat, loop):
    """Run a run-loop callback that is expected to refuse to run, returning the exception it raised."""
    try:
        loop(None)
    except Exception as e:
        return e
    return None


def test_time_loop_guard(my_predbat):
    """
    Verify the run-loop HA interface guards report the missing interface and set fatal_error when
    ha_interface is None. update_time_loop's log line used to read ha_interface.db_primary
    unconditionally, so the one case the guard exists for raised AttributeError before
    fatal_error could be set, leaving Predbat running with no way to stop it (#5135).
    """
    print("*** Running test: Run-loop guard with no HA interface")
    failed = 0

    for loop_name in ("update_time_loop", "run_time_loop"):
        # Each test gets its own PredBat, so clearing the interface here cannot reach another test
        my_predbat.ha_interface = None
        my_predbat.fatal_error = False

        error = _run_guard(my_predbat, getattr(my_predbat, loop_name))

        if isinstance(error, AttributeError):
            print("ERROR: {} raised AttributeError ({}) rather than reporting the missing HA interface".format(loop_name, error))
            failed = 1
        elif error is None:
            print("ERROR: {} did not raise when ha_interface is None".format(loop_name))
            failed = 1
        elif "HA interface not active" not in str(error):
            print("ERROR: {} raised an unexpected exception: {}".format(loop_name, error))
            failed = 1
        else:
            print("OK: {} reported the missing HA interface".format(loop_name))

        if not my_predbat.fatal_error:
            print("ERROR: {} did not set fatal_error when ha_interface is None".format(loop_name))
            failed = 1
        else:
            print("OK: {} set fatal_error when ha_interface is None".format(loop_name))

    return failed
