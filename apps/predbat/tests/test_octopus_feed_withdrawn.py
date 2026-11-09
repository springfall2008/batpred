# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""
Removing the discount the Octopus Energy integration's rate feed keeps for a withdrawn dispatch (GH#5663).

The feed lowers the price of dispatch minutes and marks them is_intelligent_adjusted, and was seen to keep doing
so for half an hour after Octopus withdrew the dispatch. A minute whose half hour a dispatch covered at an earlier
cycle, and none covers now, goes back to the day rate; one no dispatch has ever covered is left alone. The half
hours seen covered are saved to storage, so a restart while the feed is still stale does not bring the discount back.
"""
import copy
import shutil
import tempfile
from datetime import timedelta

from tests.test_infra import MockStorageComponents, make_test_storage, run_async

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
RATE_LOW = 7.6
RATE_DAY = 35.6

STATE_FIELDS = (
    "num_cars",
    "minutes_now",
    "midnight_utc",
    "forecast_minutes",
    "rate_max_base",
    "io_adjusted",
    "octopus_slots",
    "octopus_feed_covered",
    "octopus_feed_stripped",
    "octopus_feed_covered_saved",
    "now_utc",
    "octopus_intelligent_dynamic",
    "car_charging_now",
)


def _check(name, condition, detail=""):
    """
    Print the outcome of one check, returning True when it failed.
    """
    if condition:
        return False
    print("ERROR: {} {}".format(name, detail))
    return True


def _dispatch(my_predbat, start_minute, end_minute, kwh=3.55, source="smart-charge", location="AT_HOME", held=False):
    """
    A raw Octopus dispatch entry covering start_minute to end_minute past midnight.
    """
    start = my_predbat.midnight_utc + timedelta(minutes=start_minute)
    end = my_predbat.midnight_utc + timedelta(minutes=end_minute)
    slot = {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": kwh, "source": source, "location": location}
    if held:
        slot["held"] = True
        slot.pop("charge_in_kwh")
    return slot


def _reset(my_predbat):
    """
    One Octopus Intelligent car, nothing seen covered yet.
    """
    my_predbat.num_cars = 1
    my_predbat.forecast_minutes = 2 * 24 * 60
    my_predbat.rate_max_base = RATE_DAY
    my_predbat.octopus_slots = [[]]
    my_predbat.octopus_feed_covered = set()
    my_predbat.octopus_feed_stripped = set()
    my_predbat.octopus_feed_covered_saved = []
    my_predbat.components = MockStorageComponents(None)
    my_predbat.octopus_intelligent_dynamic = True
    my_predbat.car_charging_now = [False]
    my_predbat.args.pop("intelligent_dispatch_billing", None)


def _cycle(my_predbat, minute, feed, slots=None, save=True, car_slots=None):
    """
    One cycle at minute past midnight: the feed discounts the minutes in each (start, end) of feed, car 0's
    dispatches are slots, or each car's are car_slots. Returns the rates after the strip.
    """
    my_predbat.minutes_now = minute
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=minute)
    my_predbat.octopus_slots = [list(car) for car in car_slots] if car_slots is not None else [list(slots or [])]
    rates = {m: RATE_DAY for m in range(-24 * 60, 2 * 24 * 60)}
    my_predbat.io_adjusted = {}
    for start_minute, end_minute in feed:
        for m in range(start_minute, end_minute):
            rates[m] = RATE_LOW
            my_predbat.io_adjusted[m] = True
    return my_predbat.octopus_strip_withdrawn_feed_rates(rates, save=save)


def _low(rates, start_minute, end_minute):
    """
    Whether every minute from start_minute up to end_minute keeps the feed's discount.
    """
    return all(rates[m] == RATE_LOW for m in range(start_minute, end_minute))


def _day(rates, start_minute, end_minute):
    """
    Whether every minute from start_minute up to end_minute is at the day rate.
    """
    return all(rates[m] == RATE_DAY for m in range(start_minute, end_minute))


def _run(my_predbat):
    """
    Run the scenarios, returning True when any check failed.
    """
    failed = False

    print("Test 1: a dispatch withdrawn while the feed still discounts it goes back to the day rate (the #5663 report)")
    _reset(my_predbat)
    # 16:35: the 16:30-17:00 dispatch is listed and the feed discounts it
    rates = _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
    failed |= _check("t1 discounted while listed", _low(rates, 990, 1020), "rate {}".format(rates[995]))
    # 16:40: Octopus has withdrawn it, the feed still discounts 16:40-17:00
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t1 day rate once withdrawn", _day(rates, 1000, 1020), "rates {} {}".format(rates[1000], rates[1019]))
    failed |= _check("t1 marker removed", not any(my_predbat.io_adjusted.get(m) for m in range(1000, 1020)), "io_adjusted {}".format(sorted(my_predbat.io_adjusted)[:3]))

    print("Test 2: a discount no dispatch has ever covered is left alone")
    # The feed can be ahead of the dispatching sensor, or there may be no dispatching sensor at all
    _reset(my_predbat)
    rates = _cycle(my_predbat, 1000, [(1100, 1130)])
    failed |= _check("t2 kept", _low(rates, 1100, 1130), "rate {}".format(rates[1100]))
    rates = _cycle(my_predbat, 1005, [(1100, 1130)], [_dispatch(my_predbat, 1100, 1130)])
    failed |= _check("t2 kept once listed", _low(rates, 1100, 1130), "rate {}".format(rates[1100]))

    print("Test 3: a dispatch moved later loses the old half hour and keeps the new one")
    _reset(my_predbat)
    _cycle(my_predbat, 960, [(1260, 1290)], [_dispatch(my_predbat, 1260, 1290)])
    rates = _cycle(my_predbat, 965, [(1260, 1320)], [_dispatch(my_predbat, 1290, 1320)])
    failed |= _check("t3 old half hour at the day rate", _day(rates, 1260, 1290), "rate {}".format(rates[1260]))
    failed |= _check("t3 new half hour discounted", _low(rates, 1290, 1320), "rate {}".format(rates[1290]))

    print("Test 4: a half hour covered by a completed or held dispatch keeps the discount")
    for name, slot in (("completed", _dispatch(my_predbat, 990, 1005, kwh=1.2)), ("held", _dispatch(my_predbat, 990, 1000, held=True))):
        _reset(my_predbat)
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        rates = _cycle(my_predbat, 1000, [(1000, 1020)], [slot])
        failed |= _check("t4 {} keeps the half hour".format(name), _low(rates, 1000, 1020), "rate {}".format(rates[1000]))

    print("Test 5: elapsed minutes and the fixed overnight window are never touched")
    _reset(my_predbat)
    _cycle(my_predbat, 995, [(990, 1020), (1410, 1440)], [_dispatch(my_predbat, 990, 1020), _dispatch(my_predbat, 1410, 1440)])
    rates = _cycle(my_predbat, 1000, [(990, 1020), (1410, 1440)])
    failed |= _check("t5 elapsed minutes kept", _low(rates, 990, 1000), "rate {}".format(rates[990]))
    failed |= _check("t5 rest of the half hour at the day rate", _day(rates, 1000, 1020), "rate {}".format(rates[1000]))
    failed |= _check("t5 23:30-00:00 kept", _low(rates, 1410, 1440), "rate {}".format(rates[1410]))

    print("Test 6: under the window billing rule the discount stays")
    _reset(my_predbat)
    my_predbat.args["intelligent_dispatch_billing"] = "window"
    _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t6 kept", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))
    failed |= _check("t6 nothing recorded", not my_predbat.octopus_feed_covered, "covered {}".format(my_predbat.octopus_feed_covered))
    my_predbat.args.pop("intelligent_dispatch_billing", None)

    print("Test 7: a compare run (save False) records nothing, and a restart starts with nothing recorded")
    _reset(my_predbat)
    _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)], save=False)
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t7 nothing recorded by a compare run", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))
    _reset(my_predbat)
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t7 nothing stripped after a restart with no storage", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))

    print("Test 8: half hours seen covered are kept by absolute time across midnight, and dropped once past")
    _reset(my_predbat)
    # 06:00-06:30 tomorrow, outside the fixed window
    _cycle(my_predbat, 1430, [(1800, 1830)], [_dispatch(my_predbat, 1800, 1830)])
    saved_midnight = my_predbat.midnight_utc
    try:
        my_predbat.midnight_utc = saved_midnight + timedelta(days=1)
        rates = _cycle(my_predbat, 5, [(360, 390)])
        failed |= _check("t8 withdrawn after midnight at the day rate", _day(rates, 360, 390), "rate {}".format(rates[360]))
        _cycle(my_predbat, 400, [])
        failed |= _check("t8 past half hours dropped", not my_predbat.octopus_feed_covered, "covered {}".format(my_predbat.octopus_feed_covered))
    finally:
        my_predbat.midnight_utc = saved_midnight

    print("Test 9: logged when the stripped count changes, not every cycle")
    _reset(my_predbat)
    logged = []
    original_log = my_predbat.log
    my_predbat.log = lambda msg, *args, **kwargs: logged.append(msg)
    try:
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        _cycle(my_predbat, 1000, [(1000, 1020)])
        # Fewer minutes as time passes is not a new strip
        _cycle(my_predbat, 1005, [(1005, 1020)])
        _cycle(my_predbat, 1010, [(1010, 1020)])
    finally:
        my_predbat.log = original_log
    messages = [msg for msg in logged if "rate feed still carries" in msg]
    failed |= _check("t9 logged once", len(messages) == 1, "logged {}".format(messages))

    print("Test 10: with the dispatch check off, the half hour in progress is kept, later ones are not")
    # Only the check's 15 second poll holds a half hour a dispatch started and was withdrawn in between two cycles
    _reset(my_predbat)
    my_predbat.octopus_intelligent_dynamic = False
    _cycle(my_predbat, 995, [(990, 1050)], [_dispatch(my_predbat, 990, 1050)])
    rates = _cycle(my_predbat, 1000, [(1000, 1050)])
    failed |= _check("t10 half hour in progress kept", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))
    failed |= _check("t10 next half hour at the day rate", _day(rates, 1020, 1050), "rate {}".format(rates[1020]))

    print("Test 11: while a car reads charging nothing is stripped, as Octopus re-planning a running session empties the list for minutes")
    _reset(my_predbat)
    _cycle(my_predbat, 995, [(990, 1050)], [_dispatch(my_predbat, 990, 1050)])
    my_predbat.car_charging_now = [True]
    rates = _cycle(my_predbat, 1000, [(1000, 1050)])
    failed |= _check("t11 all kept", _low(rates, 1000, 1050), "rates {} {}".format(rates[1000], rates[1020]))
    # Once the car has stopped and the dispatch is still gone, it is stripped
    my_predbat.car_charging_now = [False]
    rates = _cycle(my_predbat, 1005, [(1005, 1050)])
    failed |= _check("t11 stripped once the car has stopped", _day(rates, 1005, 1050), "rate {}".format(rates[1005]))

    print("Test 12: a half hour another car's dispatch still covers keeps the discount")
    _reset(my_predbat)
    my_predbat.num_cars = 2
    my_predbat.car_charging_now = [False, False]
    _cycle(my_predbat, 995, [(990, 1080)], car_slots=[[_dispatch(my_predbat, 990, 1080)], []])
    rates = _cycle(my_predbat, 1000, [(1000, 1080)], car_slots=[[], [_dispatch(my_predbat, 1020, 1050)]])
    failed |= _check("t12 car 1's half hour kept", _low(rates, 1020, 1050), "rate {}".format(rates[1020]))
    failed |= _check("t12 others at the day rate", _day(rates, 1000, 1020) and _day(rates, 1050, 1080), "rates {} {}".format(rates[1000], rates[1050]))

    return failed


def _restart(my_predbat):
    """
    Lose the half hours seen covered, as a restart does, then restore them from storage.
    """
    my_predbat.octopus_feed_covered = set()
    my_predbat.octopus_feed_stripped = set()
    my_predbat.octopus_feed_covered_saved = []
    my_predbat.octopus_feed_load()


def _run_persistence(my_predbat):
    """
    The half hours seen covered are saved whenever they change and restored after a restart.
    """
    failed = False
    tmpdir = tempfile.mkdtemp()
    try:
        storage = make_test_storage(my_predbat, tmpdir)
        saves = []
        backend_save = storage.backend.save

        async def counting_save(module, filename, data, **kwargs):
            """Count the writes, then make them."""
            saves.append(filename)
            return await backend_save(module, filename, data, **kwargs)

        storage.backend.save = counting_save

        print("Test 13: a restart while the feed is still stale strips the withdrawn dispatch (the #5663 report)")
        _reset(my_predbat)
        my_predbat.components = MockStorageComponents(storage)
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        failed |= _check("t13 saved once", saves == ["octopus_feed_covered"], "saves {}".format(saves))
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        failed |= _check("t13 not saved again when unchanged", saves == ["octopus_feed_covered"], "saves {}".format(saves))
        my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=1000)
        _restart(my_predbat)
        rates = _cycle(my_predbat, 1000, [(1000, 1020)])
        failed |= _check("t13 day rate after the restart", _day(rates, 1000, 1020), "rate {}".format(rates[1000]))

        print("Test 14: a half hour that is over is not restored")
        my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=1020)
        _restart(my_predbat)
        failed |= _check("t14 nothing restored", not my_predbat.octopus_feed_covered, "covered {}".format(my_predbat.octopus_feed_covered))

        print("Test 15: a corrupt store is ignored")
        run_async(backend_save("predbat", "octopus_feed_covered", {"periods": ["bad", 5, None]}, format="json"))
        my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=1000)
        _restart(my_predbat)
        failed |= _check("t15 nothing restored from bad entries", not my_predbat.octopus_feed_covered, "covered {}".format(my_predbat.octopus_feed_covered))
        run_async(backend_save("predbat", "octopus_feed_covered", ["not", "a", "dict"], format="json"))
        _restart(my_predbat)
        failed |= _check("t15 nothing restored from a bad shape", not my_predbat.octopus_feed_covered, "covered {}".format(my_predbat.octopus_feed_covered))

        print("Test 16: a failed write is retried next cycle even though nothing has changed since")
        _reset(my_predbat)
        my_predbat.components = MockStorageComponents(storage)
        fail_next = [True]

        async def failing_once_save(module, filename, data, **kwargs):
            """Fail the first write, then count and make the rest."""
            if fail_next[0]:
                fail_next[0] = False
                return False
            return await counting_save(module, filename, data, **kwargs)

        storage.backend.save = failing_once_save
        saves.clear()
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        failed |= _check("t16 first write failed", saves == [], "saves {}".format(saves))
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        failed |= _check("t16 retried once the store works", saves == ["octopus_feed_covered"], "saves {}".format(saves))
        storage.backend.save = counting_save

        print("Test 17: a compare run (save False) writes nothing")
        _reset(my_predbat)
        my_predbat.components = MockStorageComponents(storage)
        saves.clear()
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)], save=False)
        failed |= _check("t17 nothing written", saves == [], "saves {}".format(saves))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failed


class _StopFetch(Exception):
    """Raised by the test's hook to stop fetch_sensor_data() once it has handed the import rates on."""


def _free_half_hour(my_predbat):
    """
    The first half hour after the one in progress that, with the one after it, lies outside the fixed window.
    """
    period = (my_predbat.minutes_now // 30 + 1) * 30
    while my_predbat.in_iog_fixed_window(period) or my_predbat.in_iog_fixed_window(period + 30):
        period += 30
    return period


def _run_fetch(my_predbat):
    """
    The real fetch_sensor_data() strips the withdrawn discount after the car's dispatches are merged and before
    rate_replicate() reads the io_adjusted markers.
    """
    failed = False
    print("Test 18: fetch_sensor_data() strips a withdrawn dispatch's discount and keeps one still listed")
    feed_id = "sensor.test_feed_withdrawn_rates"
    slot_id = "binary_sensor.test_feed_withdrawn_dispatching"
    # fetch_sensor_data() is stopped part way, so put back everything it touched before returning
    saved_state = dict(my_predbat.__dict__)
    saved_args = copy.deepcopy(my_predbat.args)
    dummy_items = my_predbat.ha_interface.dummy_items
    saved_items = {entity_id: dummy_items.get(entity_id) for entity_id in (feed_id, slot_id)}
    seen = {}
    original_replicate = my_predbat.rate_replicate

    def hook(rates, rate_io=None, is_import=True, is_gas=False, use_future=False):
        """Record the import rates and markers as rate_replicate() is handed them, then stop the fetch."""
        # Only the import replication is handed the live markers; rate_base_min_max() replicates a copy first
        if rate_io is not my_predbat.io_adjusted:
            return original_replicate(rates, rate_io, is_import=is_import, is_gas=is_gas, use_future=use_future)
        seen["rates"] = dict(rates)
        seen["io_adjusted"] = dict(rate_io)
        raise _StopFetch()

    try:
        _reset(my_predbat)
        withdrawn = _free_half_hour(my_predbat)
        listed = withdrawn + 30
        feed = []
        for period in range(0, 3 * 24 * 60, 30):
            discounted = period in (withdrawn, listed)
            feed.append(
                {
                    "value_inc_vat": RATE_LOW if discounted else RATE_DAY,
                    "valid_from": (my_predbat.midnight_utc + timedelta(minutes=period)).strftime(TIME_FORMAT),
                    "valid_to": (my_predbat.midnight_utc + timedelta(minutes=period + 30)).strftime(TIME_FORMAT),
                    "is_intelligent_adjusted": discounted,
                }
            )
        dummy_items[feed_id] = {"state": str(RATE_DAY), "rates": feed}
        dummy_items[slot_id] = {"state": "off", "planned_dispatches": [_dispatch(my_predbat, listed, listed + 30)], "completed_dispatches": []}
        my_predbat.args["metric_octopus_import"] = feed_id
        my_predbat.args["octopus_intelligent_slot"] = slot_id
        my_predbat.octopus_intelligent_ignore_unplugged = False
        # Both half hours were seen covered at an earlier cycle; only the later one is still listed
        my_predbat.octopus_feed_covered = {my_predbat.midnight_utc + timedelta(minutes=withdrawn), my_predbat.midnight_utc + timedelta(minutes=listed)}
        my_predbat.rate_replicate = hook
        # The shared fixture's sensor history can't feed the load and energy reads ahead of the rates, and they
        # play no part in them
        my_predbat.minute_data_load = lambda *args, **kwargs: ({}, 0)
        my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
        # Nor do free electricity sessions, and the fixture's octopus_free_url would otherwise fetch the live page
        my_predbat.download_octopus_free = lambda url: []
        try:
            my_predbat.fetch_sensor_data(save=False)
            failed |= _check("t18 fetch reached rate_replicate()", False, "returned without importing rates")
        except _StopFetch:
            rates = seen["rates"]
            io_adjusted = seen["io_adjusted"]
            failed |= _check("t18 withdrawn half hour at the day rate", _day(rates, withdrawn, withdrawn + 30), "rate {}".format(rates.get(withdrawn)))
            failed |= _check("t18 withdrawn markers gone before rate_replicate()", not any(io_adjusted.get(m) for m in range(withdrawn, withdrawn + 30)), "io_adjusted {}".format(io_adjusted.get(withdrawn)))
            failed |= _check("t18 listed half hour keeps the discount", _low(rates, listed, listed + 30), "rate {}".format(rates.get(listed)))
            failed |= _check("t18 listed markers kept", all(io_adjusted.get(m) for m in range(listed, listed + 30)), "io_adjusted {}".format(io_adjusted.get(listed)))
    finally:
        for name in [name for name in my_predbat.__dict__ if name not in saved_state]:
            delattr(my_predbat, name)
        my_predbat.__dict__.update(saved_state)
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        for entity_id, item in saved_items.items():
            if item is None:
                dummy_items.pop(entity_id, None)
            else:
                dummy_items[entity_id] = item
    return failed


def test_octopus_feed_withdrawn(my_predbat):
    """
    The rate feed's discount for a withdrawn Octopus Intelligent dispatch goes back to the day rate.
    """
    print("*** Running test: Octopus feed discount for a withdrawn dispatch")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_components = my_predbat.components
    saved_args = {key: my_predbat.args.get(key) for key in ("intelligent_dispatch_billing",)}
    try:
        failed = _run(my_predbat)
        failed |= _run_persistence(my_predbat)
        failed |= _run_fetch(my_predbat)
    finally:
        my_predbat.components = saved_components
        for key, value in saved_args.items():
            if value is None:
                my_predbat.args.pop(key, None)
            else:
                my_predbat.args[key] = value
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
    print("*** Octopus feed discount for a withdrawn dispatch test {}".format("FAILED" if failed else "PASSED"))
    return failed
