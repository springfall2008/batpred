# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Reproduction of a reported plan that fails to fill the battery during a negative import window.

The reported plan (Agile-style day) has import rates that go negative from 10:00 to 16:00 while
export stays flat at 12p. Importing is therefore paid for twice - you are paid ~4p/kWh to take the
energy in and paid another 12p/kWh to give it back - so the battery should be filled to 100% and
held/exported for the 16:00-19:00 peak. The reported plan only reached ~54% SoC and split the window
into alternating charge/export slots.

The scenario is rebuilt from the published plan table (import rate, export rate, PV and load per
30-minute slot) rather than from a debug YAML, so it is self-contained and stays readable.
"""

from tests.test_infra import reset_inverter
from tests.test_random_scenarios import expand_rates, expand_load_minutes, expand_pv_forecast

MINUTES_PER_DAY = 24 * 60
SLOT_MINUTES = 30
SLOTS_PER_DAY = MINUTES_PER_DAY // SLOT_MINUTES

# Import rate p/kWh per 30 minute slot from 00:00. Slots 00:00-19:00 are transcribed from the reported
# plan; 19:30 onwards is a plausible Agile evening decay (the reported plan screenshot stops at 19:00).
RATE_IMPORT_SLOTS = [
    24.72, 23.84,  # 00:00 00:30
    23.13, 23.37,  # 01:00 01:30
    23.10, 21.98,  # 02:00 02:30
    21.48, 20.52,  # 03:00 03:30
    22.31, 21.49,  # 04:00 04:30
    23.73, 23.68,  # 05:00 05:30
    21.93, 23.59,  # 06:00 06:30
    22.29, 22.54,  # 07:00 07:30
    17.78, 16.96,  # 08:00 08:30
    10.22, 5.66,   # 09:00 09:30
    -1.03, -3.04,  # 10:00 10:30
    -3.32, -3.61,  # 11:00 11:30
    -3.91, -3.91,  # 12:00 12:30
    -4.06, -4.03,  # 13:00 13:30
    -4.02, -3.91,  # 14:00 14:30
    -3.91, -3.91,  # 15:00 15:30
    13.04, 18.93,  # 16:00 16:30
    28.16, 30.41,  # 17:00 17:30
    33.40, 36.16,  # 18:00 18:30
    25.09, 22.50,  # 19:00 19:30 (19:30 onwards extrapolated)
    21.00, 20.00,  # 20:00 20:30
    19.00, 18.50,  # 21:00 21:30
    17.50, 16.50,  # 22:00 22:30
    15.50, 15.00,  # 23:00 23:30
]

# Export is a flat 12p/kWh fixed tariff all day in the reported plan.
RATE_EXPORT_FLAT = 12.0

# PV forecast kWh per 30 minute slot, transcribed from the reported plan's "PV kWh" column.
PV_SLOTS_KWH = [
    0.01, 0.01,  # 00:00 00:30
    0.01, 0.01,  # 01:00 01:30
    0.01, 0.01,  # 02:00 02:30
    0.01, 0.01,  # 03:00 03:30
    0.01, 0.01,  # 04:00 04:30
    0.01, 0.01,  # 05:00 05:30
    0.01, 0.08,  # 06:00 06:30
    0.15, 0.23,  # 07:00 07:30
    0.34, 0.44,  # 08:00 08:30
    0.52, 0.67,  # 09:00 09:30
    1.16, 1.05,  # 10:00 10:30
    1.15, 0.85,  # 11:00 11:30
    1.27, 1.68,  # 12:00 12:30
    2.07, 1.93,  # 13:00 13:30
    1.70, 1.70,  # 14:00 14:30
    1.37, 1.28,  # 15:00 15:30
    0.99, 1.01,  # 16:00 16:30
    0.80, 0.51,  # 17:00 17:30
    0.30, 0.16,  # 18:00 18:30
    0.09, 0.00,  # 19:00 19:30
    0.00, 0.00,  # 20:00 20:30
    0.00, 0.00,  # 21:00 21:30
    0.00, 0.00,  # 22:00 22:30
    0.00, 0.00,  # 23:00 23:30
]

# House load kWh per 30 minute slot, transcribed from the reported plan's "Load kWh" column.
# 19:30 onwards is a plausible evening/overnight tail (the screenshot stops at 19:00).
LOAD_SLOTS_KWH = [
    0.33, 0.23,  # 00:00 00:30
    0.23, 0.23,  # 01:00 01:30
    0.24, 0.27,  # 02:00 02:30
    0.19, 0.30,  # 03:00 03:30
    0.20, 0.30,  # 04:00 04:30
    0.30, 0.22,  # 05:00 05:30
    0.18, 0.20,  # 06:00 06:30
    0.25, 0.21,  # 07:00 07:30
    0.36, 0.42,  # 08:00 08:30
    0.87, 0.54,  # 09:00 09:30
    0.77, 0.16,  # 10:00 10:30
    0.18, 0.71,  # 11:00 11:30
    0.79, 0.17,  # 12:00 12:30
    0.24, 0.45,  # 13:00 13:30
    0.30, 0.28,  # 14:00 14:30
    0.94, 0.39,  # 15:00 15:30
    0.55, 0.39,  # 16:00 16:30
    0.22, 0.27,  # 17:00 17:30
    0.32, 0.36,  # 18:00 18:30
    0.30, 0.35,  # 19:00 19:30 (19:30 onwards extrapolated)
    0.40, 0.40,  # 20:00 20:30
    0.35, 0.30,  # 21:00 21:30
    0.30, 0.30,  # 22:00 22:30
    0.30, 0.30,  # 23:00 23:30
]

# Battery/inverter shape derived from the reported plan's own energy balance: in the evening slots the
# cost divided by the import rate gives ~1.3-1.7kWh imported per 30 minutes for a 16-17% SoC step, so
# the pack is ~8.5-9.5kWh charging at ~3kW. The 14:00 export slot needs ~5.8kW out, so a 6kW inverter.
BATTERY_SIZE_KWH = 9.5
BATTERY_START_SOC_PERCENT = 35.0
BATTERY_RATE_KW = 3.0
INVERTER_LIMIT_KW = 6.0

# Slot indices of the negative-rate window (10:00 through 15:30 inclusive).
NEGATIVE_WINDOW_START_SLOT = 20
NEGATIVE_WINDOW_END_SLOT = 32

# Slot indices of the evening peak (17:00 through 19:00 inclusive), where import is 25-36p.
PEAK_WINDOW_START_SLOT = 34
PEAK_WINDOW_END_SLOT = 38


def _slots_to_minutes(slot_values, per_minute_divisor):
    """Expand a per-30-minute-slot profile into a 1440 entry per-minute profile.

    Args:
        slot_values: list of SLOTS_PER_DAY values, one per 30 minute slot
        per_minute_divisor: divide each slot value by this to get the per-minute value

    Returns:
        list[float] of MINUTES_PER_DAY per-minute values
    """
    profile = []
    for value in slot_values:
        profile.extend([value / per_minute_divisor] * SLOT_MINUTES)
    return profile


def _build_rate_day(slot_values):
    """Expand a per-30-minute-slot rate profile into a 1440 entry per-minute rate profile.

    Args:
        slot_values: list of SLOTS_PER_DAY rates in p/kWh

    Returns:
        list[float] of MINUTES_PER_DAY per-minute rates
    """
    return _slots_to_minutes(slot_values, 1.0)


def apply_negative_rate_scenario(my_predbat):
    """Configure predbat with the reported negative-import-rate day.

    Args:
        my_predbat: PredBat instance to configure in place
    """
    reset_inverter(my_predbat)

    # The reported plan starts at midnight, so run the plan from midnight over the whole day.
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = MINUTES_PER_DAY
    my_predbat.forecast_plan_hours = 24
    my_predbat.forecast_days = 1

    # --- Battery / inverter ---
    my_predbat.soc_max = BATTERY_SIZE_KWH
    my_predbat.soc_kw = BATTERY_SIZE_KWH * BATTERY_START_SOC_PERCENT / 100.0
    my_predbat.battery_rate_max_charge = BATTERY_RATE_KW / 60.0
    my_predbat.battery_rate_max_charge_dc = BATTERY_RATE_KW / 60.0
    my_predbat.battery_rate_max_discharge = BATTERY_RATE_KW / 60.0
    my_predbat.charge_rate_now = BATTERY_RATE_KW / 60.0
    my_predbat.discharge_rate_now = BATTERY_RATE_KW / 60.0
    my_predbat.inverter_limit = INVERTER_LIMIT_KW / 60.0
    my_predbat.export_limit = INVERTER_LIMIT_KW / 60.0
    my_predbat.inverter_hybrid = False
    my_predbat.battery_loss = 0.97
    my_predbat.battery_loss_discharge = 0.97
    my_predbat.inverter_loss = 0.97
    my_predbat.reserve = 0.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_min = 0.0

    # --- Planning options ---
    my_predbat.calculate_best = True
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.calculate_second_pass = False
    my_predbat.calculate_savings = False
    my_predbat.args["threads"] = 0
    my_predbat.plan_valid = False
    my_predbat.debug_enable = False

    # --- Rates ---
    minutes_now = my_predbat.minutes_now
    end_minute = minutes_now + my_predbat.forecast_minutes
    rate_import_day = _build_rate_day(RATE_IMPORT_SLOTS)
    rate_export_day = [RATE_EXPORT_FLAT] * MINUTES_PER_DAY
    my_predbat.rate_import = expand_rates(rate_import_day, minutes_now, end_minute=end_minute)
    my_predbat.rate_export = expand_rates(rate_export_day, minutes_now, end_minute=end_minute)

    # --- Load (historical, read back one day) ---
    load_day_kwh = _slots_to_minutes(LOAD_SLOTS_KWH, float(SLOT_MINUTES))
    my_predbat.load_minutes = expand_load_minutes(load_day_kwh, minutes_now=minutes_now)
    my_predbat.load_minutes_age = 14
    my_predbat.days_previous = [1]
    my_predbat.days_previous_weight = [1.0]
    my_predbat.load_forecast_only = False
    my_predbat.load_forecast = {}
    my_predbat.load_scaling = 1.0
    my_predbat.load_scaling10 = 1.0
    my_predbat.load_inday_adjustment = 1.0
    my_predbat.dynamic_load_baseline = {}

    # --- PV forecast ---
    pv_day_kw = _slots_to_minutes(PV_SLOTS_KWH, 0.5)  # kWh per 30 mins -> kW
    pv_normal, pv10 = expand_pv_forecast(pv_day_kw, my_predbat.forecast_minutes)
    my_predbat.pv_forecast_minute = pv_normal
    my_predbat.pv_forecast_minute10 = pv10

    # --- Rate thresholds and charge/export window discovery ---
    # rate_scan/rate_scan_export must run first: they set rate_min/rate_max/rate_average and
    # rate_min_forward, which set_rate_thresholds and the plan's battery residual value depend on.
    my_predbat.rate_min_forward = {}
    my_predbat.rate_scan(my_predbat.rate_import)
    my_predbat.rate_min_base = my_predbat.rate_min
    my_predbat.rate_max_base = my_predbat.rate_max
    my_predbat.rate_scan_export(my_predbat.rate_export)
    my_predbat.set_rate_thresholds()
    my_predbat.high_export_rates, export_lowest, _export_highest = my_predbat.rate_scan_window(
        my_predbat.rate_export, 5, my_predbat.rate_export_cost_threshold, True, alt_rates=my_predbat.rate_import
    )
    if my_predbat.rate_high_threshold == 0 and export_lowest <= my_predbat.rate_export_max:
        my_predbat.rate_export_cost_threshold = export_lowest
    my_predbat.low_rates, _lowest, highest = my_predbat.rate_scan_window(
        my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False, alt_rates=my_predbat.rate_export
    )
    if my_predbat.rate_low_threshold == 0 and highest >= my_predbat.rate_min:
        my_predbat.rate_import_cost_threshold = highest


def _predicted_soc(my_predbat, minute):
    """Return the planned SoC in kWh at the given minute offset from now.

    Args:
        my_predbat: PredBat instance with a computed plan
        minute: minutes from now

    Returns:
        float SoC in kWh, or 0.0 if the plan has no entry at or just before that minute
    """
    predict_soc = my_predbat.prediction.predict_soc
    # predict_soc is stored on PREDICT_STEP boundaries, walk back to the nearest one that exists
    for offset in range(0, SLOT_MINUTES):
        if (minute - offset) in predict_soc:
            return predict_soc[minute - offset]
    return 0.0


def run_negative_rate_charge_tests(my_predbat):
    """Plan the reported negative-import-rate day and check the battery is filled while rates are negative.

    Args:
        my_predbat: PredBat instance from the shared fixture (unused, a throwaway is built instead)

    Returns:
        True if the test failed
    """
    print("**** Running negative import rate charge tests ****")
    failed = False

    # This scenario replaces the rates, load history, PV forecast, horizon and clock wholesale, so work on
    # a throwaway instance rather than leaving the shared fixture holding this day for later tests
    from unit_test import create_predbat

    my_predbat = create_predbat()

    apply_negative_rate_scenario(my_predbat)
    my_predbat.calculate_plan(recompute=True, publish=False)

    cost, _import_kwh_battery, _import_kwh_house, export_kwh, _soc_min, soc, _soc_min_minute, _battery_cycle, _metric_keep, _final_iboost, _final_carbon_g = my_predbat.run_prediction(
        my_predbat.charge_limit_best,
        my_predbat.charge_window_best,
        my_predbat.export_window_best,
        my_predbat.export_limits_best,
        False,
        end_record=my_predbat.end_record,
        save="best",
    )

    # Report the planned SoC through the negative rate window
    print("Planned SoC through the negative import window:")
    peak_soc_percent = 0.0
    for slot in range(NEGATIVE_WINDOW_START_SLOT, NEGATIVE_WINDOW_END_SLOT + 1):
        minute = slot * SLOT_MINUTES
        soc_percent = _predicted_soc(my_predbat, minute) / my_predbat.soc_max * 100.0
        peak_soc_percent = max(peak_soc_percent, soc_percent)
        print("  {:02d}:{:02d} import {:6.2f}p  SoC {:5.1f}%".format(minute // 60, minute % 60, RATE_IMPORT_SLOTS[slot], soc_percent))
    print("Plan cost {:.1f}p export {:.2f}kWh final SoC {:.2f}kWh peak SoC {:.1f}%".format(cost, export_kwh, soc, peak_soc_percent))

    # Importing at a negative rate and exporting at a flat 12p is profitable on every kWh, so the plan
    # must fill the battery while the rate is negative rather than stopping part way up.
    if peak_soc_percent < 99.0:
        print("ERROR: plan only reached {:.1f}% SoC during the negative import window, expected a full battery".format(peak_soc_percent))
        failed = True

    # The evening peak is 25-36p import against a 12p export, so grid charging there always loses money.
    # The battery may still gain whatever the PV leaves over after the house load, and no more.
    peak_start_minute = PEAK_WINDOW_START_SLOT * SLOT_MINUTES
    peak_end_minute = (PEAK_WINDOW_END_SLOT + 1) * SLOT_MINUTES
    peak_soc_gain = _predicted_soc(my_predbat, peak_end_minute) - _predicted_soc(my_predbat, peak_start_minute)
    peak_pv_surplus = sum(PV_SLOTS_KWH[slot] - LOAD_SLOTS_KWH[slot] for slot in range(PEAK_WINDOW_START_SLOT, PEAK_WINDOW_END_SLOT + 1))
    print("Evening peak {:02d}:{:02d}-{:02d}:{:02d} SoC gain {:.2f}kWh against a PV surplus of {:.2f}kWh".format(peak_start_minute // 60, peak_start_minute % 60, peak_end_minute // 60, peak_end_minute % 60, peak_soc_gain, peak_pv_surplus))
    if peak_soc_gain > max(peak_pv_surplus, 0.0) + 0.1:
        print("ERROR: plan charged the battery by {:.2f}kWh across the evening peak but only {:.2f}kWh of PV was spare - the rest came from the grid at 25-36p".format(peak_soc_gain, peak_pv_surplus))
        failed = True

    # Always write the plan out: this scenario is a reproduction case, so the plan itself is the thing
    # worth reading whether or not the assertions above hold
    html_plan, _raw_plan = my_predbat.publish_html_plan(
        my_predbat.pv_forecast_minute_step,
        my_predbat.pv_forecast_minute10_step,
        my_predbat.load_minutes_step,
        my_predbat.load_minutes_step10,
        my_predbat.end_record,
        publish=False,
    )
    if failed:
        open("plan_negative_rate.html", "w").write("<html><head><meta charset='utf-8'><title>Negative import rate test plan</title></head><body>" + html_plan + "</body></html>")
        print("Wrote plan to plan_negative_rate.html")

    return failed
