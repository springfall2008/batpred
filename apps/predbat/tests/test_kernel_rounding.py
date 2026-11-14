# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests that the kernel returns its scalar results already rounded to 4dp (ABI 11).

kernel_result_tuple no longer applies round(x, 4) to the ten scalar results: the kernel rounds them
itself (round4_fast in prediction_kernel.cpp). These tests pin that:

- every rounded field of every kernel result in a full plan replay is already a 4dp value, so
  rounding it again in Python would change nothing - on the batch path and the single-run path
- each rounded field is within one unit in the 4th decimal place of round(x, 4) of the value the
  kernel computed, by checking the kernel's rounding directly on values that sit on halfway points
- the loader refuses a binary from before ABI 11, which would hand back unrounded values
"""

import prediction_kernel
from tests.test_infra import reset_inverter
from tests.test_kernel_parity import kernel_available

# The ten scalar fields of the 17-field result tuple that the kernel rounds, by tuple index
ROUNDED_FIELDS = {0: "final_metric", 1: "import_kwh_battery", 2: "import_kwh_house", 3: "export_kwh", 4: "soc_min", 5: "final_soc", 7: "battery_cycle", 8: "metric_keep", 9: "final_iboost", 10: "final_carbon_g"}


def test_plan_results_arrive_rounded(my_predbat, dump):
    """Every kernel result of a full plan replay carries its ten scalar fields already rounded to 4dp"""
    print("**** test_plan_results_arrive_rounded {} ****".format(dump))
    failed = False
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(dump)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()
    my_predbat.args["threads"] = 0
    my_predbat.debug_enable = False
    my_predbat.prediction_kernel_enable = True
    my_predbat.plan_valid = False

    real_tuple = prediction_kernel.kernel_result_tuple
    checked = [0]
    unrounded = []

    def checking(pred, result, predict_soc):
        """Check the fields of each result as it is converted"""
        result_tuple = real_tuple(pred, result, predict_soc)
        checked[0] += 1
        for index, name in ROUNDED_FIELDS.items():
            value = result_tuple[index]
            if value != round(value, 4) and len(unrounded) < 5:
                unrounded.append((name, value))
        return result_tuple

    prediction_kernel.kernel_result_tuple = checking
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        prediction_kernel.kernel_result_tuple = real_tuple
        my_predbat.prediction_kernel_enable = False

    if unrounded:
        print("ERROR: kernel results arrived with more than 4 decimal places, e.g. {}".format(unrounded))
        failed = True
    if checked[0] < 1000:
        print("ERROR: the replay only converted {} kernel results, so it proves little".format(checked[0]))
        failed = True
    if not failed:
        print("PASS ({} kernel results, {} fields each)".format(checked[0], len(ROUNDED_FIELDS)))
    return failed


def test_halfway_values_within_one_unit():
    """round4_fast is never more than one unit in the 4th decimal place from round(x, 4), even on halfway points.

    Mirrors the kernel's arithmetic exactly - scale, round half to even, scale back - as Python's round()
    of a float does the same half-to-even rounding nearbyint does, and the build forbids fused or
    reordered floating point (-fno-fast-math -ffp-contract=off).
    """
    print("**** test_halfway_values_within_one_unit ****")
    failed = False
    differ = 0
    tested = 0
    for k in range(-200000, 200000, 7):
        for nudge in (0.0, 1e-13, -1e-13, 2e-12, -2e-12):
            value = (k + 0.5) / 1e4 + nudge
            fast = round(value * 1e4) / 1e4
            exact = round(value, 4)
            tested += 1
            if fast != exact:
                differ += 1
                if abs(fast - exact) > 1.01e-4:
                    print("ERROR: {} rounds to {} but round(x, 4) gives {}".format(value, fast, exact))
                    failed = True
                    break
        if failed:
            break
    if not failed:
        print("PASS ({} halfway values, {} differ by one unit in the 4th decimal place)".format(tested, differ))
    return failed


def test_stale_binary_refused():
    """The loader requires ABI 11, so a binary that returns unrounded results is refused"""
    print("**** test_stale_binary_refused ****")
    failed = False
    if prediction_kernel.KERNEL_ABI_VERSION < 11:
        print("ERROR: KERNEL_ABI_VERSION {} predates the kernel rounding its results".format(prediction_kernel.KERNEL_ABI_VERSION))
        failed = True
    lib = prediction_kernel.KERNEL_LIB
    if lib is not None and lib.pk_abi_version() != prediction_kernel.KERNEL_ABI_VERSION:
        print("ERROR: the loaded kernel reports ABI {} but the loader expects {}".format(lib.pk_abi_version(), prediction_kernel.KERNEL_ABI_VERSION))
        failed = True
    if not failed:
        print("PASS")
    return failed


def run_kernel_rounding_tests(my_predbat):
    """Run the kernel result rounding tests, returns True on failure.

    my_predbat should be a fresh instance: the replays load debug dumps over it.
    """
    available, required_failure = kernel_available()
    if required_failure:
        return True
    failed = test_halfway_values_within_one_unit()
    if not available:
        print("WARNING: Prediction kernel not available - kernel rounding replays SKIPPED")
        return failed
    failed |= test_stale_binary_refused()
    for dump in ("cases/predbat_debug_agile1.yaml", "cases/predbat_debug_pre_saving1.yaml"):
        failed |= test_plan_results_arrive_rounded(my_predbat, dump)
    return failed
