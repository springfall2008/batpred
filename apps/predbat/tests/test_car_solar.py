# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for solar-aware car charging and the battery-versus-export trade-off.

Two separate things are covered here. car_charging_solar places car slots on forecast sunshine so the
price-based pass only has to cover the shortfall. The trade-off between exporting the battery and
saving it for the car needs no new machinery - it already falls out of the cost model whenever
car_charging_from_battery is on - so the tests here pin that behaviour rather than add to it.

Solar slots are sized from the forecast surplus (PV less house load) rather than the charger's rated
power, because a charge-on-solar charger modulates to whatever is spare. set_house_load below is what
makes that testable: it drives the load forecast directly instead of through the fixture's history.
"""

from const import PREDICT_STEP
from utils import dp2
from tests.test_infra import reset_inverter, reset_rates, update_rates_import, update_rates_export
from prediction import Prediction


def ready_time_str(my_predbat, minutes_ahead):
    """Return an HH:MM:SS ready time the given number of minutes after the test clock.

    The ready time is absolute wall-clock in the config, so a fixed string like "23:30:00" makes a test
    pass or fail depending on the hour it runs at. Deriving it from minutes_now keeps these deterministic.
    """
    target = (my_predbat.minutes_now + minutes_ahead) % (24 * 60)
    return "{:02d}:{:02d}:00".format(target // 60, target % 60)


def set_house_load(my_predbat, kw):
    """Force the house load forecast to a flat kw, whatever history the shared fixture is carrying.

    step_data_history normally builds load from days_previous, which no test can predict. Turning on
    load_forecast_only zeroes that historical term, and dynamic_load_baseline then sets each bucket's
    floor - so with everything else neutralised each bucket comes out at exactly the requested power.
    """
    my_predbat.load_forecast_only = True
    my_predbat.load_forecast = {}
    my_predbat.load_scaling = 1.0
    my_predbat.load_inday_adjustment = 1.0
    my_predbat.load_scaling_dynamic = {}
    my_predbat.manual_load_adjust = {}
    my_predbat.metric_load_divergence = None
    kwh_per_step = kw * PREDICT_STEP / 60.0
    my_predbat.dynamic_load_baseline = {my_predbat.minutes_now + offset: kwh_per_step for offset in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes, PREDICT_STEP)}


def setup_car(my_predbat, car_kwh=8.0, ready_ahead=720, rate=7.4, house_kw=0.0):
    """Configure a single car needing car_kwh within ready_ahead minutes, and clear any existing plan.

    minutes_now is snapped down onto the plan-interval grid first. Solar windows sit on that grid while
    set_pv places the sunny block at an offset from minutes_now, so unless the two are aligned the first
    and last windows only partly overlap the sun and legitimately carry less than the full surplus - which
    made the per-slot power assertions depend on the wall-clock minute the suite happened to start at.
    """
    my_predbat.minutes_now = int(my_predbat.minutes_now / my_predbat.plan_interval_minutes) * my_predbat.plan_interval_minutes
    set_house_load(my_predbat, house_kw)
    my_predbat.num_cars = 1
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_limit = [car_kwh]
    my_predbat.car_charging_battery_size = [50.0]
    my_predbat.car_charging_rate = [rate]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_slots = [[]]
    my_predbat.car_charging_plan_time = [ready_time_str(my_predbat, ready_ahead)]
    my_predbat.car_charging_plan_smart = [True]
    my_predbat.car_charging_plan_max_price = [0.0]
    my_predbat.car_charging_now = [False]
    my_predbat.car_charging_solar = False
    my_predbat.car_charging_solar_excess = 1.0
    my_predbat.car_charging_rate_threshold_export = 99
    my_predbat.car_charging_plan_min_soc = 100


def set_pv(my_predbat, midday_kw, start_offset=240, length=240):
    """Publish a flat block of forecast PV of midday_kw starting start_offset minutes from now."""
    my_predbat.pv_forecast_minute = {}
    for minute in range(my_predbat.minutes_now, my_predbat.minutes_now + my_predbat.forecast_minutes):
        in_sun = start_offset <= (minute - my_predbat.minutes_now) < (start_offset + length)
        my_predbat.pv_forecast_minute[minute] = (midday_kw / 60.0) if in_sun else 0.0


def build_low_rates(my_predbat, count=48):
    """Build half-hour import windows, cheap overnight and expensive by day."""
    low_rates = []
    for n in range(0, count):
        price = 5.0 if (n % 24) > 12 else 30.0
        low_rates.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
    return low_rates


def test_solar_windows_selection(my_predbat):
    """plan_car_charging_solar_windows gates on forecast power and on the export rate."""
    print("  - test_solar_windows_selection")
    failed = False
    setup_car(my_predbat)
    reset_rates(my_predbat, 30.0, 5.0)

    # Off by default: no solar windows at all, whatever the sunshine
    set_pv(my_predbat, 5.0)
    if my_predbat.plan_car_charging_solar_windows():
        print("ERROR: no solar windows should be produced while car_charging_solar is off")
        failed = True

    # On, with plenty of sun and a low export rate
    my_predbat.car_charging_solar = True
    windows = my_predbat.plan_car_charging_solar_windows()
    if not windows:
        print("ERROR: expected solar windows with 5kW of forecast PV")
        failed = True
    for window in windows:
        if not window.get("solar"):
            print("ERROR: solar windows must be marked solar=True")
            failed = True
        # Windows sit on the plan-interval grid, so one may straddle the edge of the sunny block;
        # what matters is that every window overlaps it rather than starting inside it
        start_offset = window["start"] - my_predbat.minutes_now
        end_offset = window["end"] - my_predbat.minutes_now
        if not (start_offset < 480 and end_offset > 240):
            print("ERROR: solar window at offset {}-{} does not overlap the sunny block".format(start_offset, end_offset))
            failed = True

    # Sun below the excess threshold produces nothing
    set_pv(my_predbat, 0.5)
    if my_predbat.plan_car_charging_solar_windows():
        print("ERROR: 0.5kW is below the 1.0kW threshold and should produce no windows")
        failed = True

    # Plenty of sun, but export pays better than the threshold allows
    set_pv(my_predbat, 5.0)
    reset_rates(my_predbat, 30.0, 40.0)
    my_predbat.car_charging_rate_threshold_export = 20
    if my_predbat.plan_car_charging_solar_windows():
        print("ERROR: an export rate of 40 above the threshold of 20 should suppress solar charging")
        failed = True

    return failed


def test_solar_reduces_paid_import(my_predbat):
    """With solar available the price pass only tops up the shortfall, so less energy is bought."""
    print("  - test_solar_reduces_paid_import")
    failed = False
    setup_car(my_predbat, car_kwh=8.0, ready_ahead=720)
    reset_rates(my_predbat, 30.0, 5.0)
    low_rates = build_low_rates(my_predbat)
    update_rates_import(my_predbat, low_rates)
    set_pv(my_predbat, 7.0)

    # Solar off: the whole 8kWh is bought
    plan_off = my_predbat.plan_car_charging(0, low_rates)
    bought_off = sum(slot["kwh"] for slot in plan_off if not slot.get("solar"))
    if any(slot.get("solar") for slot in plan_off):
        print("ERROR: no slot should be marked solar while car_charging_solar is off")
        failed = True

    # Solar on: sunshine covers part of it, so strictly less has to be bought
    my_predbat.car_charging_solar = True
    plan_on = my_predbat.plan_car_charging(0, low_rates)
    solar_kwh = sum(slot["kwh"] for slot in plan_on if slot.get("solar"))
    bought_on = sum(slot["kwh"] for slot in plan_on if not slot.get("solar"))

    if solar_kwh <= 0:
        print("ERROR: expected some of the car charge to come from solar, plan {}".format(plan_on))
        failed = True
    if bought_on >= (bought_off - 0.1):
        print("ERROR: solar should reduce paid import, bought off={} on={}".format(bought_off, bought_on))
        failed = True

    # Both plans must still meet the car's requirement
    for name, plan in (("off", plan_off), ("on", plan_on)):
        total = sum(slot["kwh"] for slot in plan)
        if total < (my_predbat.car_charging_limit[0] - 0.2):
            print("ERROR: plan {} only delivers {} of {}kWh".format(name, total, my_predbat.car_charging_limit[0]))
            failed = True

    return failed


def test_solar_slots_do_not_overlap(my_predbat):
    """A solar window and a cheap-import window covering the same time yield only one slot."""
    print("  - test_solar_slots_do_not_overlap")
    failed = False
    setup_car(my_predbat, car_kwh=20.0, ready_ahead=720)
    reset_rates(my_predbat, 5.0, 1.0)
    low_rates = build_low_rates(my_predbat)
    update_rates_import(my_predbat, low_rates)
    set_pv(my_predbat, 7.0)
    my_predbat.car_charging_solar = True

    plan = my_predbat.plan_car_charging(0, low_rates)
    if not any(slot.get("solar") for slot in plan):
        print("ERROR: expected at least one solar slot in the plan to make this test meaningful")
        failed = True
    for first in range(len(plan)):
        for second in range(first + 1, len(plan)):
            if (plan[first]["start"] < plan[second]["end"]) and (plan[first]["end"] > plan[second]["start"]):
                print("ERROR: overlapping car slots {} and {}".format(plan[first], plan[second]))
                failed = True
    return failed


def test_min_soc_splits_bought_from_solar(my_predbat):
    """Bought slots stop at the guaranteed minimum; solar carries on to the full limit."""
    print("  - test_min_soc_splits_bought_from_solar")
    failed = False
    # 71kWh car, limit 80% (56.8kWh), minimum 30% (21.3kWh) - the Tesla sun-slider arrangement
    setup_car(my_predbat, car_kwh=56.8, ready_ahead=720)
    my_predbat.car_charging_battery_size = [71.0]
    reset_rates(my_predbat, 30.0, 5.0)
    low_rates = build_low_rates(my_predbat)
    update_rates_import(my_predbat, low_rates)
    set_pv(my_predbat, 7.0)
    my_predbat.car_charging_solar = True

    # Default (100%): everything solar does not cover is bought, up to the full limit
    plan_full = my_predbat.plan_car_charging(0, low_rates)
    bought_full = sum(slot["kwh"] for slot in plan_full if not slot.get("solar"))

    # With a 30% minimum, bought energy stops there and solar keeps going
    my_predbat.car_charging_plan_min_soc = 30
    plan_split = my_predbat.plan_car_charging(0, low_rates)
    bought_split = sum(slot["kwh"] for slot in plan_split if not slot.get("solar"))
    solar_split = sum(slot["kwh"] for slot in plan_split if slot.get("solar"))

    if bought_split >= bought_full:
        print("ERROR: a 30% minimum should buy less ({}) than the default ({})".format(bought_split, bought_full))
        failed = True
    # Solar already delivers past 30%, so nothing should need buying here
    if solar_split > 21.3 and bought_split > 0.1:
        print("ERROR: solar covers the minimum, so nothing should be bought, got {}".format(bought_split))
        failed = True
    if solar_split <= 21.3:
        print("ERROR: expected solar to carry past the 21.3kWh minimum, got {}".format(solar_split))
        failed = True

    # No sun: the minimum must still be met from the grid, and no more
    set_pv(my_predbat, 0.0)
    plan_dark = my_predbat.plan_car_charging(0, low_rates)
    bought_dark = sum(slot["kwh"] for slot in plan_dark if not slot.get("solar"))
    if any(slot.get("solar") for slot in plan_dark):
        print("ERROR: no solar slots should be planned with no sun")
        failed = True
    if not (20.0 <= bought_dark <= 23.0):
        print("ERROR: with no sun expected roughly the 21.3kWh minimum bought, got {}".format(bought_dark))
        failed = True

    return failed


def solar_kw_in_slot(slot):
    """Average power a planned slot draws, in kW."""
    return slot["kwh"] * 60.0 / (slot["end"] - slot["start"])


def test_solar_slot_size_follows_surplus(my_predbat):
    """A solar slot is sized from PV minus house load, not from the charger's rated power."""
    print("  - test_solar_slot_size_follows_surplus")
    failed = False
    # 7kW of sun against a 3kW house leaves 4kW for a 7kW charger
    setup_car(my_predbat, car_kwh=50.0, ready_ahead=720, rate=7.0, house_kw=3.0)
    reset_rates(my_predbat, 30.0, 5.0)
    set_pv(my_predbat, 7.0)
    my_predbat.car_charging_solar = True

    plan = my_predbat.plan_car_charging(0, [])
    solar_slots = [slot for slot in plan if slot.get("solar")]
    if not solar_slots:
        print("ERROR: expected solar slots with 7kW of sun against a 3kW house")
        return True

    for slot in solar_slots:
        power = solar_kw_in_slot(slot)
        if abs(power - 4.0) > 0.3:
            print("ERROR: slot should draw the 4kW surplus, got {}kW ({})".format(dp2(power), slot))
            failed = True

    # The old behaviour was to assume the full charger rate regardless, so pin that it is gone
    if any(solar_kw_in_slot(slot) > 6.0 for slot in solar_slots):
        print("ERROR: a solar slot is still being sized at the charger's rated power")
        failed = True

    # Raising the house load must reduce what the car is predicted to take, from the same sunshine
    total_at_3kw = sum(slot["kwh"] for slot in solar_slots)
    set_house_load(my_predbat, 5.0)
    total_at_5kw = sum(slot["kwh"] for slot in my_predbat.plan_car_charging(0, []) if slot.get("solar"))
    if not (total_at_5kw < total_at_3kw):
        print("ERROR: a bigger house load should leave the car less, got {} vs {}".format(total_at_5kw, total_at_3kw))
        failed = True

    return failed


def test_solar_slot_capped_by_charger(my_predbat):
    """Surplus beyond what the charger can take is not credited to the car."""
    print("  - test_solar_slot_capped_by_charger")
    failed = False
    # 12kW of sun and a 1kW house leaves 11kW spare, but the charger tops out at 7kW
    setup_car(my_predbat, car_kwh=50.0, ready_ahead=720, rate=7.0, house_kw=1.0)
    reset_rates(my_predbat, 30.0, 5.0)
    set_pv(my_predbat, 12.0)
    my_predbat.car_charging_solar = True

    plan = my_predbat.plan_car_charging(0, [])
    solar_slots = [slot for slot in plan if slot.get("solar")]
    if not solar_slots:
        print("ERROR: expected solar slots with 12kW of sun")
        return True
    for slot in solar_slots:
        power = solar_kw_in_slot(slot)
        if power > 7.05:
            print("ERROR: slot draws {}kW, above the 7kW charger limit ({})".format(dp2(power), slot))
            failed = True
        if abs(power - 7.0) > 0.3:
            print("ERROR: with 11kW spare the charger should run flat out at 7kW, got {}kW".format(dp2(power)))
            failed = True
    return failed


def test_solar_window_needs_real_surplus(my_predbat):
    """The excess threshold is tested against surplus, so sun the house is already eating does not qualify."""
    print("  - test_solar_window_needs_real_surplus")
    failed = False
    setup_car(my_predbat, rate=7.0, house_kw=0.0)
    reset_rates(my_predbat, 30.0, 5.0)
    my_predbat.car_charging_solar = True
    my_predbat.car_charging_solar_excess = 1.0
    set_pv(my_predbat, 4.0)

    # Sanity: with no house load 4kW of sun is 4kW of surplus and qualifies
    if not my_predbat.plan_car_charging_solar_windows():
        print("ERROR: 4kW of sun against an idle house should qualify")
        failed = True

    # The same 4kW of sun against a 4kW house leaves nothing, and must not qualify
    set_house_load(my_predbat, 4.0)
    windows = my_predbat.plan_car_charging_solar_windows()
    if windows:
        print("ERROR: 4kW of sun fully consumed by a 4kW house should produce no windows, got {}".format(windows[:3]))
        failed = True

    # A house drawing 3.5kW leaves 0.5kW, still under the 1kW threshold
    set_house_load(my_predbat, 3.5)
    if my_predbat.plan_car_charging_solar_windows():
        print("ERROR: a 0.5kW surplus is below the 1kW threshold and should produce no windows")
        failed = True

    # Drop the house to 2kW and the remaining 2kW surplus clears it
    set_house_load(my_predbat, 2.0)
    if not my_predbat.plan_car_charging_solar_windows():
        print("ERROR: a 2kW surplus should clear the 1kW threshold")
        failed = True
    return failed


def test_solar_surplus_floored_per_bucket(my_predbat):
    """Surplus is floored at zero per bucket, so a dark half slot cannot cancel a sunny one."""
    print("  - test_solar_surplus_floored_per_bucket")
    failed = False
    setup_car(my_predbat, rate=7.0, house_kw=2.0)
    reset_rates(my_predbat, 30.0, 5.0)

    # An hour of sun starting two hours out, against a house that draws 2kW around the clock
    set_pv(my_predbat, 6.0, start_offset=120, length=60)
    load_step = my_predbat.car_solar_load_forecast()

    now = my_predbat.minutes_now
    # The sunny hour on its own: 6kW of sun less 2kW of house is 4kW, so 4kWh over the hour
    sunny = my_predbat.car_solar_surplus_kwh(now + 120, now + 180, load_step)
    if abs(sunny - 4.0) > 0.2:
        print("ERROR: expected 4kWh of surplus over the sunny hour, got {}".format(dp2(sunny)))
        failed = True

    # A dark hour is a 2kW deficit, but a deficit is not a negative surplus - it is simply nothing
    dark = my_predbat.car_solar_surplus_kwh(now + 240, now + 300, load_step)
    if abs(dark) > 0.01:
        print("ERROR: a dark hour should yield no surplus, got {}".format(dp2(dark)))
        failed = True

    # Spanning both, the dark half must not eat into the sunny half's 4kWh
    spanning = my_predbat.car_solar_surplus_kwh(now + 120, now + 300, load_step)
    if abs(spanning - 4.0) > 0.2:
        print("ERROR: the dark hours should not cancel the sunny one, expected 4kWh got {}".format(dp2(spanning)))
        failed = True
    return failed


def test_solar_windows_ignore_ready_time(my_predbat):
    """Solar windows run to the forecast horizon, so a morning ready time does not exclude daylight."""
    print("  - test_solar_windows_ignore_ready_time")
    failed = False
    setup_car(my_predbat)
    reset_rates(my_predbat, 30.0, 5.0)
    my_predbat.car_charging_solar = True
    # Sun starts 4 hours out; a ready time only 1 hour out must not suppress it
    set_pv(my_predbat, 5.0, start_offset=240)

    windows = my_predbat.plan_car_charging_solar_windows()
    if not windows:
        print("ERROR: solar windows should be offered regardless of the ready time")
        failed = True
    if not any((window["start"] - my_predbat.minutes_now) >= 240 for window in windows):
        print("ERROR: expected solar windows beyond the sunny block start, got {}".format(windows[:3]))
        failed = True
    return failed


def run_car_export_tradeoff(my_predbat, export_rate, from_battery, car_kwh=8.0):
    """Plan a battery against a fixed evening car slot and one export window at the given rate."""
    end_record = my_predbat.forecast_minutes
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = 20.0
    my_predbat.soc_kw = 18.0
    my_predbat.reserve = 0.5
    my_predbat.set_charge_freeze = True
    my_predbat.set_export_freeze = True
    my_predbat.best_soc_keep = 0.0
    my_predbat.debug_enable = False
    my_predbat.battery_charging_from_grid = True
    my_predbat.car_charging_from_battery = from_battery
    my_predbat.car_energy_reported_load = True
    my_predbat.num_cars = 1
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_limit = [car_kwh]
    my_predbat.car_charging_battery_size = [50.0]
    my_predbat.car_charging_loss = 1.0

    now = my_predbat.minutes_now
    my_predbat.car_charging_slots = [[{"start": now + 120, "end": now + 240, "kwh": car_kwh, "average": 30.0, "octopus": False}]]

    charge_window_best = [{"start": now + 30 * n, "end": now + 30 * (n + 1), "average": 30.0} for n in range(0, 48)]
    export_window_best = [{"start": now, "end": now + 60, "average": export_rate}]

    reset_rates(my_predbat, 30.0, export_rate)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
    load_step = {minute: 0.2 / (60 / 5) for minute in range(0, my_predbat.forecast_minutes, 5)}
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    charge_limit_best = [0 for _ in range(len(charge_window_best))]
    export_limits_best = [100 for _ in range(len(export_window_best))]
    result = my_predbat.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record)

    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best
    my_predbat.optimise_all_windows(result[0], result[8])

    final = my_predbat.run_prediction(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=end_record, save="best")
    exported = bool(my_predbat.export_limits_best) and my_predbat.export_limits_best[0] < 100
    return exported, final[1] + final[2]


def test_car_export_tradeoff(my_predbat):
    """Exporting versus saving the battery for the car is already decided by the cost model.

    This needs no dedicated mechanism: the car's load is part of the prediction, so an export that
    drains the battery is priced against the import the car then needs. The test pins that, because
    the behaviour is easy to break and not obvious from reading either module alone.
    """
    print("  - test_car_export_tradeoff")
    failed = False
    reset_inverter(my_predbat)

    # Export pays far less than the import the car would otherwise need: keep the charge
    exported_cheap, import_cheap = run_car_export_tradeoff(my_predbat, export_rate=5.0, from_battery=True)
    if exported_cheap:
        print("ERROR: should not export at 5p when the car will need import at 30p")
        failed = True

    # Export pays far more than that import: sell it and buy the car's energy back
    exported_rich, _ = run_car_export_tradeoff(my_predbat, export_rate=60.0, from_battery=True)
    if not exported_rich:
        print("ERROR: should export at 60p even though the car will then import at 30p")
        failed = True

    # Letting the battery serve the car reduces what is bought from the grid
    _, import_blocked = run_car_export_tradeoff(my_predbat, export_rate=5.0, from_battery=False)
    if import_cheap >= import_blocked:
        print("ERROR: car_charging_from_battery on should import less ({}) than off ({})".format(import_cheap, import_blocked))
        failed = True

    return failed


def run_car_solar_tests(my_predbat):
    """Run every solar car-charging and car/export trade-off test.

    The car settings live on the shared my_predbat instance, so they are snapshotted and put back
    afterwards. Without this a lowered car_charging_plan_min_soc leaks into whichever test runs next
    and silently halves its expected charge - which only shows up in some test orderings. The load
    forecast inputs set_house_load overwrites are carried for the same reason: leaving
    load_forecast_only on would zero the historical load of every test that follows.
    """
    print("**** Running car solar tests ****\n")
    carried = (
        "minutes_now",
        "load_forecast_only",
        "load_forecast",
        "load_scaling",
        "load_inday_adjustment",
        "load_scaling_dynamic",
        "manual_load_adjust",
        "metric_load_divergence",
        "dynamic_load_baseline",
        "num_cars",
        "car_charging_soc",
        "car_charging_limit",
        "car_charging_battery_size",
        "car_charging_rate",
        "car_charging_loss",
        "car_charging_slots",
        "car_charging_plan_time",
        "car_charging_plan_smart",
        "car_charging_plan_max_price",
        "car_charging_now",
        "car_charging_solar",
        "car_charging_solar_excess",
        "car_charging_rate_threshold_export",
        "car_charging_plan_min_soc",
        "car_charging_from_battery",
    )
    saved = {name: getattr(my_predbat, name, None) for name in carried}

    try:
        failed = test_solar_windows_selection(my_predbat)
        failed |= test_solar_reduces_paid_import(my_predbat)
        failed |= test_solar_slots_do_not_overlap(my_predbat)
        failed |= test_solar_windows_ignore_ready_time(my_predbat)
        failed |= test_min_soc_splits_bought_from_solar(my_predbat)
        failed |= test_solar_surplus_floored_per_bucket(my_predbat)
        failed |= test_solar_window_needs_real_surplus(my_predbat)
        failed |= test_solar_slot_size_follows_surplus(my_predbat)
        failed |= test_solar_slot_capped_by_charger(my_predbat)
        if failed:
            return failed
        failed |= test_car_export_tradeoff(my_predbat)
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed
