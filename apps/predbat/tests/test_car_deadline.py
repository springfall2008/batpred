# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for manual_car_deadline, the one-off "have the car at this level by this time".

The everyday guarantee is car_charging_plan_min_soc by car_charging_plan_time. That covers a routine,
not an event: a trip at 15:00 that needs 80% does not fit a 07:30 ready time, and editing the standing
settings for it means remembering to put them back. The deadline is set from the plan page instead and
expires on its own once its slot has passed.

It is a guarantee rather than a hope, in the same way the everyday minimum is. Free sun that lands
before the deadline is counted first, and whatever it leaves is bought before the deadline passes - from
any import slot, cheapest first, and regardless of car_charging_plan_max_price, because the user has
said the charge is needed.
"""

from datetime import timedelta

from tests.test_car_solar import set_pv, setup_car
from tests.test_infra import reset_rates, update_rates_import


def setup_deadline_car(my_predbat, soc=10.0, limit=40.0, min_soc=0, ready_ahead=720):
    """A 50kWh car at soc kWh with a limit of limit kWh, and no standing minimum unless asked for.

    min_soc defaults to 0 so the everyday guarantee asks for nothing, which isolates what the deadline
    itself causes: anything bought in these tests is bought because of it.
    """
    setup_car(my_predbat, car_kwh=limit, ready_ahead=ready_ahead)
    my_predbat.car_charging_soc = [soc]
    my_predbat.car_charging_plan_min_soc = min_soc
    my_predbat.manual_car_away_times = []
    my_predbat.manual_car_deadline_keep = {}
    my_predbat.car_charging_solar_battery_soc = 0
    reset_rates(my_predbat, 30.0, 5.0)


def set_deadline(my_predbat, minutes_ahead, percent):
    """Promise percent by the slot minutes_ahead from now, stored the way manual_rates() decodes it."""
    start = int((my_predbat.minutes_now + minutes_ahead) / my_predbat.plan_interval_minutes) * my_predbat.plan_interval_minutes
    my_predbat.manual_car_deadline_keep = {minute: percent for minute in range(start, start + my_predbat.plan_interval_minutes)}
    return start


def bought_by(plan, minute):
    """kWh bought from the grid in slots that finish by minute."""
    return sum(slot["kwh"] for slot in plan if not slot.get("solar") and slot["end"] <= minute)


def solar_by(plan, minute):
    """kWh taken from the sun in slots that finish by minute."""
    return sum(slot["kwh"] for slot in plan if slot.get("solar") and slot["end"] <= minute)


def test_deadline_buys_before_it(my_predbat):
    """With no sun and no standing minimum, the deadline's shortfall is bought before it passes."""
    print("  - test_deadline_buys_before_it")
    failed = False
    setup_deadline_car(my_predbat)

    if my_predbat.plan_car_charging(0, []):
        print("ERROR: with no deadline and no minimum nothing should be planned, so this test proves nothing")
        return True

    # 10kWh in a 50kWh car, 60% promised = 30kWh, so 20kWh has to be bought within four hours
    deadline = set_deadline(my_predbat, 240, 60)
    plan = my_predbat.plan_car_charging(0, [])
    bought = bought_by(plan, deadline)
    if abs(bought - 20.0) > 0.1:
        print("ERROR: expected 20kWh bought before the deadline, got {} from {}".format(bought, plan))
        failed = True
    if any(slot["end"] > deadline for slot in plan):
        print("ERROR: with no sun, nothing should be planned after the deadline: {}".format(plan))
        failed = True
    return failed


def test_deadline_ignores_the_price_cap(my_predbat):
    """car_charging_plan_max_price still binds the everyday minimum, but not a one-off deadline."""
    print("  - test_deadline_ignores_the_price_cap")
    failed = False
    # Every slot costs 30p against a 10p cap
    setup_deadline_car(my_predbat, min_soc=60, ready_ahead=240)
    my_predbat.car_charging_plan_max_price = [10.0]
    low_rates = [{"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": 30.0} for n in range(8)]

    everyday = my_predbat.plan_car_charging(0, low_rates)
    if everyday:
        print("ERROR: the everyday minimum should still respect the price cap, got {}".format(everyday))
        failed = True

    deadline = set_deadline(my_predbat, 240, 60)
    plan = my_predbat.plan_car_charging(0, low_rates)
    if bought_by(plan, deadline) < 19.9:
        print("ERROR: a one-off deadline is an explicit need and should buy past the price cap, got {}".format(plan))
        failed = True
    return failed


def test_deadline_buys_outside_the_cheap_band(my_predbat):
    """A deadline may buy any slot before it, not only the ones in low_rates.

    low_rates holds the windows under the import threshold. On a tariff whose cheap band is overnight, a
    midday deadline would otherwise find nothing it is allowed to buy and fail silently.
    """
    print("  - test_deadline_buys_outside_the_cheap_band")
    failed = False
    setup_deadline_car(my_predbat)
    # The only cheap windows start after the deadline
    low_rates = [{"start": my_predbat.minutes_now + 300 + 30 * n, "end": my_predbat.minutes_now + 330 + 30 * n, "average": 5.0} for n in range(8)]
    update_rates_import(my_predbat, low_rates)

    deadline = set_deadline(my_predbat, 240, 60)
    plan = my_predbat.plan_car_charging(0, low_rates)
    if bought_by(plan, deadline) < 19.9:
        print("ERROR: the deadline should buy at the day rate when the cheap band is too late, got {}".format(plan))
        failed = True
    return failed


def test_deadline_buys_the_cheapest_slots(my_predbat):
    """In smart mode the deadline's purchases are the cheapest slots before it, not the soonest."""
    print("  - test_deadline_buys_the_cheapest_slots")
    failed = False
    setup_deadline_car(my_predbat)
    # Two expensive slots up front, then six cheap ones: 6 x 3.7kWh = 22.2kWh covers the 20kWh needed
    windows = [{"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": 50.0 if n < 2 else 10.0} for n in range(8)]
    update_rates_import(my_predbat, windows)

    deadline = set_deadline(my_predbat, 240, 60)
    plan = my_predbat.plan_car_charging(0, [])
    bought = [slot for slot in plan if not slot.get("solar") and slot["end"] <= deadline]
    if not bought:
        print("ERROR: nothing was bought before the deadline")
        failed = True
    elif any(slot["average"] != 10.0 for slot in bought):
        print("ERROR: expected only the 10p slots to be bought, got {}".format(bought))
        failed = True
    return failed


def test_deadline_uses_sun_first(my_predbat):
    """Sun that lands before the deadline is used before anything is bought."""
    print("  - test_deadline_uses_sun_first")
    failed = False
    setup_deadline_car(my_predbat)
    my_predbat.car_charging_solar = True
    # 7kW for three hours ending an hour before the deadline: 21kWh, more than the 20kWh needed
    set_pv(my_predbat, 7.0, start_offset=60, length=180)

    deadline = set_deadline(my_predbat, 300, 60)
    plan = my_predbat.plan_car_charging(0, [])
    if bought_by(plan, deadline) > 0.1:
        print("ERROR: the sun before the deadline covers it, so nothing should be bought: {}".format(plan))
        failed = True
    if solar_by(plan, deadline) < 19.9:
        print("ERROR: expected the deadline to be met from the sun, got {}".format(plan))
        failed = True
    return failed


def test_deadline_does_not_count_later_sun(my_predbat):
    """Sun after the deadline cannot meet it, so the shortfall is bought - and the sun still tops up after.

    This is the failure that left a car short overnight against its everyday minimum: sun arriving after
    the deadline was counted towards it, and the purchase that should have covered it was never made.
    """
    print("  - test_deadline_does_not_count_later_sun")
    failed = False
    setup_deadline_car(my_predbat)
    my_predbat.car_charging_solar = True
    set_pv(my_predbat, 7.0, start_offset=300, length=300)

    deadline = set_deadline(my_predbat, 240, 60)
    plan = my_predbat.plan_car_charging(0, [])
    if bought_by(plan, deadline) < 19.9:
        print("ERROR: the sun comes after the deadline, so its shortfall must be bought, got {}".format(plan))
        failed = True
    if not any(slot.get("solar") and slot["start"] >= deadline for slot in plan):
        print("ERROR: the later sun should still top the car up towards its limit: {}".format(plan))
        failed = True
    return failed


def test_deadline_is_capped_at_the_limit(my_predbat):
    """A level above the car's charge limit plans to the limit - the car will not take more."""
    print("  - test_deadline_is_capped_at_the_limit")
    failed = False
    setup_deadline_car(my_predbat, limit=40.0)
    deadline = set_deadline(my_predbat, 360, 100)

    promised = my_predbat.car_one_off_deadlines(0)
    if len(promised) != 1 or abs(promised[0]["kwh"] - 40.0) > 0.001:
        print("ERROR: a 100% deadline on a 40kWh limit should be capped to 40kWh, got {}".format(promised))
        failed = True
    total = sum(slot["kwh"] for slot in my_predbat.plan_car_charging(0, []))
    if total > 30.0 + 0.1:
        print("ERROR: planned {}kWh into a car with only 30kWh of room below its limit".format(total))
        failed = True
    if bought_by(my_predbat.plan_car_charging(0, []), deadline) < 29.9:
        print("ERROR: the capped level should still be bought in full before the deadline")
        failed = True
    return failed


def test_both_deadlines_are_kept(my_predbat):
    """The everyday minimum and a later one-off deadline are each met by their own time."""
    print("  - test_both_deadlines_are_kept")
    failed = False
    # Everyday: 40% (20kWh) in two hours. One-off: 80% (40kWh) in six.
    setup_deadline_car(my_predbat, soc=10.0, limit=40.0, min_soc=40, ready_ahead=120)
    low_rates = [{"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": 5.0} for n in range(24)]
    update_rates_import(my_predbat, low_rates)
    ready = my_predbat.minutes_now + 120

    deadline = set_deadline(my_predbat, 360, 80)
    plan = my_predbat.plan_car_charging(0, low_rates)
    if bought_by(plan, ready) < 9.9:
        print("ERROR: the everyday minimum needs 10kWh by its ready time, got {} from {}".format(bought_by(plan, ready), plan))
        failed = True
    if bought_by(plan, deadline) < 29.9:
        print("ERROR: the one-off deadline needs 30kWh by its time, got {} from {}".format(bought_by(plan, deadline), plan))
        failed = True
    return failed


def test_deadline_claims_sun_ahead_of_the_battery(my_predbat):
    """A battery-priority hold must not bank the sun a deadline is counting on.

    With the battery first, the morning surplus would fill the pack while the car's deadline bought from
    the grid - paying for energy the sun was providing for free, to spare a battery that has the rest of
    the day to fill. The deadline claims the sun before it, exactly as away time claims the sun the car
    will be present for.
    """
    print("  - test_deadline_claims_sun_ahead_of_the_battery")
    failed = False
    setup_deadline_car(my_predbat)
    my_predbat.car_charging_solar = True
    my_predbat.car_charging_solar_battery_soc = 100
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 0.0
    my_predbat.battery_rate_max_charge = 5.0 / 60.0
    my_predbat.battery_rate_max_scaling = 1.0
    # 21kWh of sun ending an hour before the deadline; the empty 10kWh pack would take the first 4 slots
    set_pv(my_predbat, 7.0, start_offset=60, length=180)
    load_step = my_predbat.car_solar_load_forecast()

    if my_predbat.car_solar_reserved_for_car(load_step) != 0.0:
        print("ERROR: with no deadline and no away time nothing should be reserved for the car")
        failed = True

    deadline = set_deadline(my_predbat, 300, 60)
    reserved = my_predbat.car_solar_reserved_for_car(load_step)
    if abs(reserved - 20.0) > 0.1:
        print("ERROR: the deadline needs 20kWh and the sun before it can deliver that, so 20kWh should be reserved, got {}".format(reserved))
        failed = True

    plan = my_predbat.plan_car_charging(0, [])
    if bought_by(plan, deadline) > 0.1:
        print("ERROR: the sun before the deadline should go to the car rather than the battery, but {}kWh was bought: {}".format(bought_by(plan, deadline), plan))
        failed = True
    return failed


def mark_away(my_predbat, start_ahead, end_ahead):
    """Mark the car away for the slots between start_ahead and end_ahead minutes from now."""
    base = my_predbat.minutes_now
    my_predbat.manual_car_away_times = list(range(base + start_ahead, base + end_ahead, my_predbat.plan_interval_minutes))


def overlaps_away(my_predbat, plan):
    """Planned slots that touch a slot the car is marked away for."""
    return [slot for slot in plan if my_predbat.car_slot_is_away(slot["start"], slot["end"])]


def test_deadline_buys_around_away_time(my_predbat):
    """Away slots before a deadline are skipped, so its purchases move to slots the car is there for.

    The cheapest slots here are exactly the ones the car is away for. Buying them would plan a charge into
    a car that is not plugged in, and the deadline would be missed for real.
    """
    print("  - test_deadline_buys_around_away_time")
    failed = False
    setup_deadline_car(my_predbat)
    windows = [{"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": 5.0 if n < 4 else 30.0} for n in range(10)]
    update_rates_import(my_predbat, windows)
    mark_away(my_predbat, 0, 120)

    deadline = set_deadline(my_predbat, 300, 60)
    plan = my_predbat.plan_car_charging(0, [])
    if overlaps_away(my_predbat, plan):
        print("ERROR: charge planned while the car is away: {}".format(overlaps_away(my_predbat, plan)))
        failed = True
    if bought_by(plan, deadline) < 19.9:
        print("ERROR: the six present slots before the deadline can deliver the 20kWh, got {} from {}".format(bought_by(plan, deadline), plan))
        failed = True
    return failed


def test_deadline_ready_for_a_trip(my_predbat):
    """Ready by the time you leave, away from then on: the whole promise lands before departure.

    The usual shape of a trip. The sun after departure is no use to a car that is not there, so none of the
    promise may be left to it and nothing may be planned after the deadline at all.
    """
    print("  - test_deadline_ready_for_a_trip")
    failed = False
    setup_deadline_car(my_predbat)
    my_predbat.car_charging_solar = True
    set_pv(my_predbat, 7.0, start_offset=300, length=300)
    mark_away(my_predbat, 240, 720)

    deadline = set_deadline(my_predbat, 240, 60)
    plan = my_predbat.plan_car_charging(0, [])
    if bought_by(plan, deadline) < 19.9:
        print("ERROR: the car leaves at the deadline, so the promise has to be bought before it, got {}".format(plan))
        failed = True
    if overlaps_away(my_predbat, plan):
        print("ERROR: the sun after departure was planned into a car that is away: {}".format(overlaps_away(my_predbat, plan)))
        failed = True
    return failed


def test_deadline_does_not_claim_sun_while_away(my_predbat):
    """The battery hold only releases sun the car is present for; sun while it is away stays with the battery."""
    print("  - test_deadline_does_not_claim_sun_while_away")
    failed = False
    setup_deadline_car(my_predbat)
    my_predbat.car_charging_solar = True
    my_predbat.car_charging_solar_battery_soc = 100
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 0.0
    my_predbat.battery_rate_max_charge = 5.0 / 60.0
    my_predbat.battery_rate_max_scaling = 1.0
    # Six sunny slots before the deadline; the car is away for the first four of them
    set_pv(my_predbat, 7.0, start_offset=60, length=180)
    mark_away(my_predbat, 60, 180)
    load_step = my_predbat.car_solar_load_forecast()

    deadline = set_deadline(my_predbat, 300, 60)
    reserved = my_predbat.car_solar_reserved_for_car(load_step)
    if abs(reserved - 7.0) > 0.1:
        print("ERROR: only the two present sunny slots (7kWh) can be claimed for the car, got {}".format(reserved))
        failed = True

    plan = my_predbat.plan_car_charging(0, [])
    if overlaps_away(my_predbat, plan):
        print("ERROR: charge planned while the car is away: {}".format(overlaps_away(my_predbat, plan)))
        failed = True
    if bought_by(plan, deadline) + solar_by(plan, deadline) < 19.9:
        print("ERROR: sun and purchases together should still meet the deadline, got {}".format(plan))
        failed = True
    return failed


def test_deadline_warns_when_away_leaves_too_little(my_predbat):
    """A promise that cannot be kept is said out loud, and what can be delivered still is."""
    print("  - test_deadline_warns_when_away_leaves_too_little")
    failed = False
    setup_deadline_car(my_predbat)
    # Away for six of the eight slots before the deadline: two slots at 3.7kWh cannot deliver 20kWh
    mark_away(my_predbat, 0, 180)
    deadline = set_deadline(my_predbat, 240, 60)

    messages = []
    original_log = my_predbat.log
    my_predbat.log = lambda message, *args, **kwargs: messages.append(message)
    try:
        plan = my_predbat.plan_car_charging(0, [])
    finally:
        my_predbat.log = original_log

    if not any("can only reach" in message for message in messages):
        print("ERROR: an unreachable deadline should be warned about, logged {}".format(messages))
        failed = True
    if abs(bought_by(plan, deadline) - 7.4) > 0.1:
        print("ERROR: the two present slots should still be bought in full, got {} from {}".format(bought_by(plan, deadline), plan))
        failed = True
    return failed


def test_deadline_applies_to_the_first_car_only(my_predbat):
    """The level is a percentage of one car's battery, so it is not applied to every car."""
    print("  - test_deadline_applies_to_the_first_car_only")
    failed = False
    setup_deadline_car(my_predbat)
    my_predbat.num_cars = 2
    my_predbat.car_charging_soc = [10.0, 10.0]
    my_predbat.car_charging_limit = [40.0, 40.0]
    my_predbat.car_charging_battery_size = [50.0, 50.0]
    set_deadline(my_predbat, 240, 60)
    if not my_predbat.car_one_off_deadlines(0):
        print("ERROR: the first car should carry the deadline")
        failed = True
    if my_predbat.car_one_off_deadlines(1):
        print("ERROR: the second car should not carry the first car's deadline")
        failed = True
    return failed


def test_deadline_survives_an_unset_config_value(my_predbat):
    """A gated valued select reads back None before it is enabled, and must decode to nothing.

    manual_car_deadline is gated on num_cars. manual_rates() called .replace() on the stored value
    directly - the same shape that took headless startup down through manual_times() for manual_car_away.
    """
    print("  - test_deadline_survives_an_unset_config_value")
    failed = False
    item = my_predbat.config_index.get("manual_car_deadline")
    if item is None:
        print("ERROR: manual_car_deadline is not registered")
        return True
    saved = item.get("value", "")
    try:
        item["value"] = None
        try:
            result = my_predbat.manual_rates("manual_car_deadline", update=False)
        except Exception as e:
            print("ERROR: an unset value raised {}: {}".format(type(e).__name__, e))
            return True
        if result:
            print("ERROR: an unset value should decode to no deadline, got {}".format(result))
            failed = True
    finally:
        item["value"] = saved
    return failed


def test_deadline_select_takes_its_own_default(my_predbat):
    """A slot picked without a level takes manual_car_deadline_value, and an explicit level is kept.

    Valued selects are routed on a substring of their name, and an unrecognised one is dropped with a
    warning - so without its own branch the deadline could not be set at all.
    """
    print("  - test_deadline_select_takes_its_own_default")
    failed = False
    item = my_predbat.config_index.get("manual_car_deadline")
    saved = item.get("value", "")
    slot = int(my_predbat.minutes_now / my_predbat.plan_interval_minutes) * my_predbat.plan_interval_minutes + 120
    label = (my_predbat.midnight_utc + timedelta(minutes=slot)).strftime("%a %H:%M")
    default = my_predbat.get_arg("manual_car_deadline_value")
    try:
        my_predbat.manual_select("manual_car_deadline", "off")
        my_predbat.manual_select("manual_car_deadline", label)
        decoded = my_predbat.manual_rates("manual_car_deadline", default_rate=default, update=False)
        if decoded.get(slot) != default:
            print("ERROR: a slot with no level should take the deadline default {}, got {}".format(default, decoded.get(slot)))
            failed = True

        my_predbat.manual_select("manual_car_deadline", "off")
        my_predbat.manual_select("manual_car_deadline", "{}=70".format(label))
        decoded = my_predbat.manual_rates("manual_car_deadline", default_rate=default, update=False)
        if decoded.get(slot) != 70:
            print("ERROR: an explicit level of 70 should be kept, got {}".format(decoded.get(slot)))
            failed = True
    finally:
        my_predbat.manual_select("manual_car_deadline", "off")
        item["value"] = saved
    return failed


def test_deadline_is_reachable_from_the_plan_ui(my_predbat):
    """The plan's time cells must offer the deadline, send it, show it, and clear it.

    Car Away first shipped with its config and planner logic but no way to reach it from the plan, which
    is where it is useful. Reads the sources rather than driving a browser, as the web tests around it do.
    """
    print("  - test_deadline_is_reachable_from_the_plan_ui")
    import os

    failed = False
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    helper = open(os.path.join(here, "web_helper.py")).read()
    web = open(os.path.join(here, "web.py")).read()

    if "handleCarDeadline(" not in helper:
        print("ERROR: the plan time-cell dropdown does not offer a car ready-by action")
        failed = True
    if "overrides.manual_car_deadline" not in helper:
        print("ERROR: the plan renderer never reads the deadline, so a set one cannot show as set")
        failed = True
    if "override-car-deadline" not in helper:
        print("ERROR: a slot carrying the deadline is not highlighted")
        failed = True
    for action in ("'Set Car Deadline'", "'Clear Car Deadline'"):
        if action not in helper:
            print("ERROR: the plan page never sends {}".format(action))
            failed = True
    if 'action == "Set Car Deadline"' not in web or 'action == "Clear Car Deadline"' not in web:
        print("ERROR: the rate_override handler does not accept the car deadline actions")
        failed = True
    if web.count('"manual_car_deadline": manual_car_deadline_list') < 2:
        print("ERROR: both plan payloads must carry the deadline or one of the two views cannot show it")
        failed = True

    # Only one deadline at a time: setting a new one has to clear the old before adding
    set_block = web.split('action == "Set Car Deadline":')[1].split("elif action")[0] if 'action == "Set Car Deadline":' in web else ""
    if 'async_manual_select("manual_car_deadline", "off")' not in set_block:
        print("ERROR: setting a deadline must replace any existing one rather than add a second")
        failed = True
    return failed


def run_car_deadline_tests(my_predbat):
    """Run every manual_car_deadline test.

    The car, battery, PV and load settings live on the shared my_predbat instance, so everything these
    tests change is snapshotted and put back - leaking a deadline or a lowered minimum into a later test
    only shows up in some orderings (#5079).
    """
    print("**** Running car deadline tests ****\n")
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
        "pv_forecast_minute",
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
        "car_charging_solar_battery_soc",
        "car_charging_rate_threshold_export",
        "car_charging_plan_min_soc",
        "manual_car_away_times",
        "manual_car_deadline_keep",
        "battery_rate_max_charge",
        "battery_rate_max_scaling",
        "soc_kw",
        "soc_max",
    )
    saved = {name: getattr(my_predbat, name, None) for name in carried}
    try:
        failed = test_deadline_buys_before_it(my_predbat)
        failed |= test_deadline_ignores_the_price_cap(my_predbat)
        failed |= test_deadline_buys_outside_the_cheap_band(my_predbat)
        failed |= test_deadline_buys_the_cheapest_slots(my_predbat)
        failed |= test_deadline_uses_sun_first(my_predbat)
        failed |= test_deadline_does_not_count_later_sun(my_predbat)
        failed |= test_deadline_is_capped_at_the_limit(my_predbat)
        failed |= test_both_deadlines_are_kept(my_predbat)
        failed |= test_deadline_claims_sun_ahead_of_the_battery(my_predbat)
        failed |= test_deadline_buys_around_away_time(my_predbat)
        failed |= test_deadline_ready_for_a_trip(my_predbat)
        failed |= test_deadline_does_not_claim_sun_while_away(my_predbat)
        failed |= test_deadline_warns_when_away_leaves_too_little(my_predbat)
        failed |= test_deadline_applies_to_the_first_car_only(my_predbat)
        failed |= test_deadline_survives_an_unset_config_value(my_predbat)
        failed |= test_deadline_select_takes_its_own_default(my_predbat)
        failed |= test_deadline_is_reachable_from_the_plan_ui(my_predbat)
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed
