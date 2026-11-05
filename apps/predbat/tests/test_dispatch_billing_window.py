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
Billing a whole planned dispatch window off-peak, as E.ON Next Drive Smart does (#5521).

Octopus bills a dispatch half hour off-peak only when the car charges in it, and lists it as completed
later; E.ON bills every half hour a planned dispatch was in force, whether the car drew power or not,
and its completed list only has the half hours the car charged in. With intelligent_dispatch_billing
"window" (or "auto" with the Kraken component on provider eon) a half hour seen in a running dispatch
keeps the dispatch rate for the day, and nothing strips the rate from a car that is not charging.
The held half hours are saved to storage under either rule, so a restart does not lose them.
"""
import copy
import shutil
import tempfile
from datetime import timedelta

from const import OCTOPUS_DISPATCH_HOLD_MINUTES
from tests.test_infra import make_test_storage
from tests.test_octopus_dispatch_hold import STATE_FIELDS, _check, _cycle, _day, _dispatch, _held, _low, _reset

EXTRA_FIELDS = ("components", "octopus_slots_held_saved", "octopus_intelligent_consider_full")


class _Kraken:
    """
    Stand-in for the Kraken component: only its provider is read.
    """

    def __init__(self, provider):
        """Remember the provider."""
        self.provider = provider


class _Components:
    """
    Components with an optional Kraken component and an optional storage component.
    """

    def __init__(self, kraken=None, storage=None):
        """Remember the components to return."""
        self.kraken = kraken
        self.storage = storage

    def get_component(self, name):
        """Return the Kraken or storage component, None for everything else."""
        return {"kraken": self.kraken, "storage": self.storage}.get(name)


def _setup(my_predbat, billing="window", provider=None, storage=None):
    """
    The #5520 test car, with the billing rule set and the Kraken component on provider (None: not running).
    """
    _reset(my_predbat)
    my_predbat.octopus_slots_held_saved = {}
    my_predbat.components = _Components(_Kraken(provider) if provider else None, storage)
    if billing is None:
        my_predbat.args.pop("intelligent_dispatch_billing", None)
    else:
        my_predbat.args["intelligent_dispatch_billing"] = billing


def _running(my_predbat, minute, end_minute, kwh=10.0):
    """
    A planned dispatch running at minute, its start advanced to 2 minutes before it as the API components do.
    """
    return [_dispatch(my_predbat, minute - 2, end_minute, kwh=kwh, location=None)]


def _run_billing_choice(my_predbat):
    """
    Which rule applies: the apps.yaml setting, else the Kraken component's provider.
    """
    failed = False
    print("Test 1: the billing rule follows intelligent_dispatch_billing, else the Kraken provider")
    cases = [
        (None, None, False, "no setting, no Kraken component"),
        ("auto", None, False, "auto, no Kraken component"),
        ("auto", "eon", True, "auto, Kraken on E.ON"),
        ("auto", "EON", True, "auto, Kraken on E.ON in capitals"),
        (None, "eon", True, "no setting, Kraken on E.ON"),
        ("auto", "edf", False, "auto, Kraken on EDF"),
        ("octopus", "eon", False, "octopus forced on E.ON"),
        ("window", None, True, "window forced without Kraken"),
        ("window", "edf", True, "window forced on EDF"),
        ("nonsense", "eon", True, "an unknown setting is taken as auto"),
        ("nonsense", None, False, "an unknown setting is taken as auto, no Kraken"),
    ]
    for billing, provider, expected, name in cases:
        _setup(my_predbat, billing=billing, provider=provider)
        result = my_predbat.dispatch_billing_window()
        failed |= _check("t1 {}".format(name), result == expected, "got {}".format(result))
    return failed


def _run_window(my_predbat):
    """
    The elapsed half hours of a dispatch keep its rate for the day under the window rule.
    """
    failed = False
    window_hold = 24 * 60

    print("Test 2: a dispatch open all evening with the car not charging stays cheap all evening (1 Oct)")
    _setup(my_predbat)
    for minute in range(1145, 1440, 30):
        rates = _cycle(my_predbat, minute, planned=_running(my_predbat, minute, 1440))
    rates = _cycle(my_predbat, 1435, planned=_running(my_predbat, 1435, 1440))
    failed |= _check("t2 19:00-24:00 cheap at 23:55", _low(rates, 1140, 1440), "rates {} {}".format(rates[1140], rates[1410]))
    failed |= _check("t2 18:30-19:00 never in a dispatch, day rate", _day(rates, 1110, 1140), "rate {}".format(rates[1110]))
    # And after midnight, as the half hours are yesterday's now
    my_predbat.midnight_utc = my_predbat.midnight_utc + timedelta(days=1)
    rates = _cycle(my_predbat, 10)
    failed |= _check("t2 yesterday evening still cheap after midnight", _low(rates, 1140 - 1440, 0), "rate {}".format(rates[1140 - 1440]))
    failed |= _check("t2 today untouched", _day(rates, 0, 30), "rate {}".format(rates[0]))
    # Back on the original day for the rest of the tests
    my_predbat.midnight_utc = my_predbat.midnight_utc - timedelta(days=1)

    print("Test 3: the same evening under the Octopus rule goes back to the day rate after the hold")
    _setup(my_predbat, billing="octopus")
    for minute in range(1145, 1440, 30):
        rates = _cycle(my_predbat, minute, planned=_running(my_predbat, minute, 1440))
    rates = _cycle(my_predbat, 1435, planned=_running(my_predbat, 1435, 1440))
    failed |= _check("t3 19:00 back to the day rate", _day(rates, 1140, 1170), "rate {}".format(rates[1140]))
    failed |= _check("t3 last {} minutes still held".format(OCTOPUS_DISPATCH_HOLD_MINUTES), _low(rates, 1440 - OCTOPUS_DISPATCH_HOLD_MINUTES - 30, 1440), "rate {}".format(rates[1440 - OCTOPUS_DISPATCH_HOLD_MINUTES - 30]))

    print("Test 4: a dispatch withdrawn minutes after it started keeps its half hour, and only that (6 Oct)")
    _setup(my_predbat)
    _cycle(my_predbat, 1100, planned=_running(my_predbat, 1100, 360 + 1440))
    rates = _cycle(my_predbat, 1105)
    failed |= _check("t4 18:00-18:30 cheap once withdrawn", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    rates = _cycle(my_predbat, 1260)
    failed |= _check("t4 18:00-18:30 still cheap at 21:00", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t4 18:30-19:30 day rate", _day(rates, 1110, 1170), "rate {}".format(rates[1110]))

    print("Test 5: the gap between two dispatches stays at the day rate (3 Oct)")
    _setup(my_predbat)
    for minute in (1025, 1055):
        _cycle(my_predbat, minute, planned=_running(my_predbat, minute, 1080))
    for minute in (1085, 1115, 1145):
        _cycle(my_predbat, minute, planned=[_dispatch(my_predbat, 1170, 1440)])
    for minute in (1175, 1205):
        rates = _cycle(my_predbat, minute, planned=_running(my_predbat, minute, 1440))
    failed |= _check("t5 17:00-18:00 cheap", _low(rates, 1020, 1080), "rate {}".format(rates[1020]))
    failed |= _check("t5 18:00-19:30 day rate", _day(rates, 1080, 1170), "rate {}".format(rates[1080]))
    failed |= _check("t5 19:30-20:30 cheap", _low(rates, 1170, 1230), "rate {}".format(rates[1170]))

    print("Test 6: a completed dispatch of no energy does not take the half hour back")
    _setup(my_predbat)
    _cycle(my_predbat, 1085, planned=_running(my_predbat, 1085, 1200))
    completed = [_dispatch(my_predbat, 1080, 1110, kwh=0.0)]
    rates = _cycle(my_predbat, 1130, planned=_running(my_predbat, 1130, 1200), completed=completed)
    failed |= _check("t6 still cheap with a zero-kWh completed entry", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t6 the half hour is held", len(_held(my_predbat)) == 1, "held {}".format(_held(my_predbat)))
    # One with energy is the record, so the half hour is not counted twice
    completed = [_dispatch(my_predbat, 1080, 1110, kwh=3.0)]
    rates = _cycle(my_predbat, 1135, planned=_running(my_predbat, 1135, 1200), completed=completed)
    failed |= _check("t6 cheap with a completed entry", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t6 nothing held under a completed entry", _held(my_predbat) == [], "held {}".format(_held(my_predbat)))

    print("Test 7: the window hold runs out after a day")
    _setup(my_predbat)
    _cycle(my_predbat, 1085, planned=_running(my_predbat, 1085, 1110))
    rates = _cycle(my_predbat, 1110 + window_hold - 5)
    failed |= _check("t7 held to the end of the hold", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    rates = _cycle(my_predbat, 1110 + window_hold)
    failed |= _check("t7 dropped after the hold", my_predbat.octopus_slots_held.get(0) == {}, "held {}".format(my_predbat.octopus_slots_held))
    return failed


def _run_gates(my_predbat):
    """
    Nothing withholds the dispatch rate for a car that is not charging, or not needing it, under the window rule.
    """
    failed = False
    print("Test 8: slot confirmation does not apply to a car under the window rule")
    _setup(my_predbat)
    my_predbat.car_charging_slots = [[{"start": 1080, "end": 1200, "kwh": 5.0, "octopus": True}]]
    failed |= _check("t8 not active under window", not my_predbat.dynamic_load_car_active(0))
    my_predbat.args["intelligent_dispatch_billing"] = "octopus"
    failed |= _check("t8 still active under octopus", my_predbat.dynamic_load_car_active(0))

    print("Test 9: dispatch minutes no car needs keep the dispatch rate under the window rule")
    _setup(my_predbat)
    my_predbat.octopus_intelligent_consider_full = True
    my_predbat.minutes_now = 1080
    my_predbat.octopus_slots = [[_dispatch(my_predbat, 1200, 1440)]]
    my_predbat.car_charging_slots = [[{"start": 1200, "end": 1230, "kwh": 5.0, "octopus": True}]]
    failed |= _check("t9 no surplus under window", my_predbat.octopus_surplus_minutes() == set(), "surplus {}".format(sorted(my_predbat.octopus_surplus_minutes())[:4]))
    my_predbat.args["intelligent_dispatch_billing"] = "octopus"
    failed |= _check("t9 surplus under octopus", 1260 in my_predbat.octopus_surplus_minutes())
    return failed


def _restart(my_predbat):
    """
    Lose the held half hours, as a restart does, then restore them from storage.
    """
    my_predbat.octopus_slots_held = {}
    my_predbat.octopus_slots_held_saved = {}
    my_predbat.octopus_held_load()


def _run_persistence(my_predbat):
    """
    The held half hours are saved whenever they change and restored after a restart, under either rule.
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

        print("Test 10: a held half hour survives a restart (Octopus rule)")
        _setup(my_predbat, billing="octopus", storage=storage)
        _cycle(my_predbat, 1085, planned=_running(my_predbat, 1085, 1200))
        failed |= _check("t10 saved once", saves == ["octopus_held"], "saves {}".format(saves))
        _cycle(my_predbat, 1090, planned=_running(my_predbat, 1090, 1200))
        failed |= _check("t10 not saved again when unchanged", saves == ["octopus_held"], "saves {}".format(saves))
        _restart(my_predbat)
        rates = _cycle(my_predbat, 1115)
        failed |= _check("t10 18:00-18:30 cheap after the restart", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))

        print("Test 11: a half hour past its hold is not restored")
        _restart(my_predbat)
        my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=1110 + OCTOPUS_DISPATCH_HOLD_MINUTES)
        my_predbat.octopus_slots_held = {}
        my_predbat.octopus_held_load()
        failed |= _check("t11 nothing restored", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))
        # The window rule holds it for the day, so it is restored then
        my_predbat.args["intelligent_dispatch_billing"] = "window"
        my_predbat.octopus_held_load()
        failed |= _check("t11 restored under window", len(my_predbat.octopus_slots_held.get(0, {})) == 1, "held {}".format(my_predbat.octopus_slots_held))

        print("Test 12: a whole evening of held half hours survives a restart (window rule)")
        _setup(my_predbat, storage=storage)
        for minute in range(1145, 1440, 30):
            _cycle(my_predbat, minute, planned=_running(my_predbat, minute, 1440))
        _restart(my_predbat)
        rates = _cycle(my_predbat, 1438)
        failed |= _check("t12 19:00-24:00 cheap after the restart", _low(rates, 1140, 1440), "rate {}".format(rates[1140]))

        print("Test 13: a corrupt store is ignored")
        run_save = storage.backend.save
        from tests.test_infra import run_async

        run_async(run_save("predbat", "octopus_held", {"cars": {"x": {"bad": "worse"}, "0": "nope"}}, format="json"))
        _restart(my_predbat)
        failed |= _check("t13 nothing restored", not any(my_predbat.octopus_slots_held.values()), "held {}".format(my_predbat.octopus_slots_held))

        print("Test 15: a failed write is retried next cycle even though nothing has changed since")
        _setup(my_predbat, storage=storage)
        fail_next = [True]

        async def failing_once_save(module, filename, data, **kwargs):
            """Fail the first write, then count and make the rest."""
            if fail_next[0]:
                fail_next[0] = False
                return False
            return await counting_save(module, filename, data, **kwargs)

        storage.backend.save = failing_once_save
        saves.clear()
        _cycle(my_predbat, 1085, planned=_running(my_predbat, 1085, 1110))
        failed |= _check("t15 first write failed", saves == [], "saves {}".format(saves))
        _cycle(my_predbat, 1115)
        failed |= _check("t15 retried once the store works", saves == ["octopus_held"], "saves {}".format(saves))
        _restart(my_predbat)
        rates = _cycle(my_predbat, 1120)
        failed |= _check("t15 18:00-18:30 cheap after the restart", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
        storage.backend.save = counting_save

        print("Test 14: no storage component, nothing breaks")
        _setup(my_predbat, storage=None)
        _cycle(my_predbat, 1085, planned=_running(my_predbat, 1085, 1200))
        my_predbat.octopus_held_load()
        failed |= _check("t14 still held in memory", len(my_predbat.octopus_slots_held.get(0, {})) == 1, "held {}".format(my_predbat.octopus_slots_held))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failed


def test_dispatch_billing_window(my_predbat):
    """
    Planned dispatch half hours are billed off-peak whole under the window rule (E.ON, #5521), and held half
    hours survive a restart.
    """
    print("*** Running test: dispatch billing window")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_extra = {field: getattr(my_predbat, field, None) for field in EXTRA_FIELDS}
    saved_args = {key: my_predbat.args.get(key) for key in ("octopus_slot_low_rate", "octopus_slot_max", "intelligent_dispatch_billing")}
    try:
        my_predbat.args["octopus_slot_low_rate"] = True
        my_predbat.args["octopus_slot_max"] = 48
        failed = _run_billing_choice(my_predbat)
        failed |= _run_window(my_predbat)
        failed |= _run_gates(my_predbat)
        failed |= _run_persistence(my_predbat)
    finally:
        for key, value in saved_args.items():
            if value is None:
                my_predbat.args.pop(key, None)
            else:
                my_predbat.args[key] = value
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
        for field, value in saved_extra.items():
            setattr(my_predbat, field, value)
    print("*** Dispatch billing window test {}".format("FAILED" if failed else "PASSED"))
    return failed
