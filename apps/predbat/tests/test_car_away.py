# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for manual_car_away, the per-slot override saying the car will not be plugged in.

car_charging_planned tells Predbat whether the car is plugged in *now*, so a plan made this morning
happily schedules an afternoon charge for a car that will be out, and only finds out when the
afternoon comes. This override is how the user says so in advance.

A slot the car is away for is skipped outright rather than shortened or repriced: if the car is not
there, nothing can go into it from any source.
"""

from tests.test_infra import reset_rates, update_rates_import


def ready_time_str(my_predbat, minutes_ahead):
    """An HH:MM:SS ready time the given number of minutes after the test clock."""
    target = (my_predbat.minutes_now + minutes_ahead) % (24 * 60)
    return "{:02d}:{:02d}:00".format(target // 60, target % 60)


def setup_car(my_predbat, car_kwh=10.0, ready_ahead=720, rate=7.0):
    """A single car needing car_kwh within ready_ahead minutes, with no away slots."""
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
    my_predbat.manual_car_away_times = []


def build_low_rates(my_predbat, count=24):
    """Half-hour import windows, all the same price so slot choice is by time, not price."""
    return [{"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": 5.0} for n in range(count)]


def test_away_slots_are_skipped(my_predbat):
    """A slot marked away yields no charging, and the charge moves to slots the car is present for."""
    print("  - test_away_slots_are_skipped")
    failed = False
    setup_car(my_predbat)
    reset_rates(my_predbat, 5.0, 1.0)
    low_rates = build_low_rates(my_predbat)
    update_rates_import(my_predbat, low_rates)

    baseline = my_predbat.plan_car_charging(0, low_rates)
    if not baseline:
        print("ERROR: expected a baseline plan with no away slots")
        return True
    baseline_kwh = sum(slot["kwh"] for slot in baseline)

    # Mark the first four half-hours away
    away = [my_predbat.minutes_now + 30 * n for n in range(4)]
    my_predbat.manual_car_away_times = away
    plan = my_predbat.plan_car_charging(0, low_rates)

    for slot in plan:
        for marker in away:
            if slot["start"] < marker + my_predbat.plan_interval_minutes and slot["end"] > marker:
                print("ERROR: slot {}-{} overlaps an away marker at {}".format(slot["start"], slot["end"], marker))
                failed = True

    # The car still needs the same energy, it just has to come from later slots
    if abs(sum(slot["kwh"] for slot in plan) - baseline_kwh) > 0.2:
        print("ERROR: the charge should move, not vanish - baseline {} got {}".format(baseline_kwh, sum(slot["kwh"] for slot in plan)))
        failed = True

    return failed


def test_away_does_not_end_the_search(my_predbat):
    """An away slot must not stop the loop, or every later slot is lost with it.

    The have-enough test breaks out of the loop; an away slot has to be skipped before reaching it,
    or the first away marker silently truncates the whole plan.
    """
    print("  - test_away_does_not_end_the_search")
    failed = False
    setup_car(my_predbat)
    reset_rates(my_predbat, 5.0, 1.0)
    low_rates = build_low_rates(my_predbat)
    update_rates_import(my_predbat, low_rates)

    # Away for the very first slot only - everything after it must still be planned
    my_predbat.manual_car_away_times = [my_predbat.minutes_now]
    plan = my_predbat.plan_car_charging(0, low_rates)

    if not plan:
        print("ERROR: an away marker on the first slot wiped out the whole plan")
        return True
    if sum(slot["kwh"] for slot in plan) < (my_predbat.car_charging_limit[0] - 0.5):
        print("ERROR: the car should still reach its limit from later slots, got {}".format(sum(slot["kwh"] for slot in plan)))
        failed = True
    return failed


def test_away_matches_on_overlap(my_predbat):
    """Suppression is by overlap, not by an exact start match."""
    print("  - test_away_matches_on_overlap")
    failed = False
    setup_car(my_predbat)

    interval = my_predbat.plan_interval_minutes
    marker = my_predbat.minutes_now + 60
    my_predbat.manual_car_away_times = [marker]

    # A window starting inside the away slot overlaps it
    if not my_predbat.car_slot_is_away(marker + 5, marker + interval + 5):
        print("ERROR: a window starting inside an away slot should be suppressed")
        failed = True
    # One ending inside it also overlaps
    if not my_predbat.car_slot_is_away(marker - 5, marker + 5):
        print("ERROR: a window ending inside an away slot should be suppressed")
        failed = True
    # One entirely before it does not
    if my_predbat.car_slot_is_away(marker - interval, marker):
        print("ERROR: a window ending exactly as the away slot starts should not be suppressed")
        failed = True
    # Nor one entirely after
    if my_predbat.car_slot_is_away(marker + interval, marker + interval * 2):
        print("ERROR: a window starting after the away slot ends should not be suppressed")
        failed = True

    # With nothing configured, nothing is ever away
    my_predbat.manual_car_away_times = []
    if my_predbat.car_slot_is_away(marker, marker + interval):
        print("ERROR: with no away times configured nothing should be suppressed")
        failed = True
    return failed


def test_away_survives_an_unset_config_value(my_predbat):
    """A registered-but-unset selection must decode to nothing rather than crashing.

    manual_car_away is gated behind an "enable", unlike the other manual selects, so before the UI
    has published it the stored value reads back as None rather than "". manual_times() called
    .replace() on that directly, which took fetch_config_options() down with an AttributeError -
    seen as the headless annual bootstrap failing to start at all.
    """
    print("  - test_away_survives_an_unset_config_value")
    failed = False
    item = my_predbat.config_index.get("manual_car_away")
    if item is None:
        print("ERROR: manual_car_away is not registered")
        return True
    saved = item.get("value", "")
    try:
        item["value"] = None
        try:
            result = my_predbat.manual_times("manual_car_away", update=False)
        except Exception as e:
            print("ERROR: an unset value raised {}: {}".format(type(e).__name__, e))
            return True
        if result:
            print("ERROR: an unset value should decode to no slots, got {}".format(result))
            failed = True
        # And the same for an empty string, which is what a cleared selection looks like
        item["value"] = ""
        if my_predbat.manual_times("manual_car_away", update=False):
            print("ERROR: an empty value should decode to no slots")
            failed = True
    finally:
        item["value"] = saved
    return failed


def test_away_is_reachable_from_the_plan_ui(my_predbat):
    """The plan table must offer Car Away, send it, and clear it again.

    The config item and the planner logic are not enough on their own: the plan's time cells build
    their dropdown from an explicit list of actions in web_helper.py and post them to a handler in
    web.py that matches on the action string. Miss either and the setting exists but there is no way
    to reach it from the plan, which is where it is actually useful - the first version of this
    feature shipped exactly that way.

    Reads the sources rather than driving a browser, which is what the surrounding web tests do.
    """
    print("  - test_away_is_reachable_from_the_plan_ui")
    import os

    failed = False
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    helper = open(os.path.join(here, "web_helper.py")).read()
    web = open(os.path.join(here, "web.py")).read()

    if "'Car Away'" not in helper:
        print("ERROR: the plan time-cell dropdown does not offer a Car Away action")
        failed = True
    if "manual_car_away_times" not in helper:
        print("ERROR: the plan renderer never reads manual_car_away_times, so a set slot cannot show as set")
        failed = True
    if helper.count("manual_car_away_times") < 4:
        print("ERROR: expected the away times in the action union, both highlight blocks and the menu test, got {}".format(helper.count("manual_car_away_times")))
        failed = True

    if 'action == "Car Away"' not in web:
        print("ERROR: the plan_override handler does not accept the Car Away action")
        failed = True
    if web.count('"manual_car_away_times": manual_car_away_times') < 2:
        print("ERROR: both plan payloads must carry the away times or one of the two views cannot show them")
        failed = True

    # Clear has to reach this select too, or a marker set from the plan can never be removed there
    clear_block = web.split('if action == "Clear":')[1].split("else:")[0] if 'if action == "Clear":' in web else ""
    if "manual_car_away" not in clear_block:
        print("ERROR: Clear does not clear manual_car_away, so an away marker cannot be undone from the plan")
        failed = True

    return failed


def run_car_away_tests(my_predbat):
    """Run every manual_car_away test."""
    print("**** Running car away tests ****\n")
    carried = (
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
        "manual_car_away_times",
    )
    saved = {name: getattr(my_predbat, name, None) for name in carried}
    try:
        failed = test_away_slots_are_skipped(my_predbat)
        failed |= test_away_does_not_end_the_search(my_predbat)
        failed |= test_away_matches_on_overlap(my_predbat)
        failed |= test_away_survives_an_unset_config_value(my_predbat)
        failed |= test_away_is_reachable_from_the_plan_ui(my_predbat)
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed
