# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from unittest.mock import patch


def _run_loop_expecting_failure(my_predbat, loop):
    """Run a run-loop callback that is expected to re-raise the update_pred exception."""
    try:
        loop(None)
    except Exception:
        # The loops deliberately re-raise; we only care about the active flag state afterwards
        pass


def test_active_flag(my_predbat):
    """
    Verify that the predbat.active flag (and therefore the web spinner) is always cleared
    after a run, even when update_pred() raises an exception or returns early. Otherwise the
    "Predbat Active" switch and web UI spinner get stuck on permanently.
    """
    print("*** Running test: Active flag is cleared on exception")
    failed = 0

    # Satisfy the run-loop preconditions
    my_predbat.ha_interface.websocket_active = True

    def boom(scheduled=True):
        # Mimic update_pred turning the active flag on and then failing part way through
        my_predbat.expose_config("active", True)
        raise Exception("simulated update_pred failure")

    # --- Scheduled run via run_time_loop ---
    my_predbat.prediction_started = False
    my_predbat.update_pending = False
    my_predbat.expose_config("active", False)
    with patch.object(my_predbat, "update_pred", side_effect=boom):
        _run_loop_expecting_failure(my_predbat, my_predbat.run_time_loop)

    if my_predbat.get_arg("active", False):
        print("ERROR: active flag stuck ON after run_time_loop raised an exception")
        failed = 1
    else:
        print("OK: active flag cleared after run_time_loop exception")

    # prediction_started must also be reset so future runs are not blocked
    if my_predbat.prediction_started:
        print("ERROR: prediction_started stuck ON after run_time_loop exception")
        failed = 1

    # --- Web/manual update via update_time_loop ---
    my_predbat.prediction_started = False
    my_predbat.update_pending = True
    my_predbat.expose_config("active", False)
    with patch.object(my_predbat, "update_pred", side_effect=boom), patch.object(my_predbat, "load_user_config"), patch.object(my_predbat, "validate_config"), patch.object(my_predbat, "create_entity_list"):
        _run_loop_expecting_failure(my_predbat, my_predbat.update_time_loop)

    if my_predbat.get_arg("active", False):
        print("ERROR: active flag stuck ON after update_time_loop raised an exception")
        failed = 1
    else:
        print("OK: active flag cleared after update_time_loop exception")

    if my_predbat.prediction_started:
        print("ERROR: prediction_started stuck ON after update_time_loop exception")
        failed = 1

    return failed
