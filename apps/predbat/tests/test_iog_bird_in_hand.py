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
Octopus Intelligent "bird in hand" credit.

A car's dispatch only gives the house its cheap rate if the car really charges in it, and the car can be
unplugged at any time, so the half hour it is charging in now is the one certain part of the run. When
charging the battery in the dispatch scored the same as waiting for the fixed off-peak window the planner
used to wait, and a house that looked like it would reach 23:30 could run out first.

While a car is in a dispatch the dispatch check has not cancelled (the same decision that shows a "?" on a
cancelled slot), io_confirmed_credit_update() marks the run with a small score-only credit per kWh stored,
largest for the current half hour and tapering after it, so a tie goes to the energy already in hand. Cost
is never touched, so a genuinely cheaper slot still wins.
"""
import copy
from datetime import timedelta

from const import PREDBAT_MAX_CARS
from plan import IO_CONFIRMED_CREDIT
from prediction import Prediction
from tests.test_infra import reset_inverter
from tests.test_iog_charge_skew import run_iog_skew_scenario, _snapshot_state, _restore_state

# Attributes these tests set on top of those the IOG skew harness snapshots
STATE_FIELDS = (
    "num_cars",
    "now_utc_real",
    "car_charging_slots",
    "octopus_intelligent_dynamic",
    "dynamic_load_car_effective",
    "io_confirmed_credit",
    "io_confirmed_active",
    "io_confirmed_tiebreak",
    "plan_valid",
    "pv_metric10_weight",
    "pv_metric90_weight",
)

LOW_RATE = 7.0
IOG_SLOTS = 9  # 19:00 - 23:30
FIRM_SLOTS = 12  # 23:30 - 05:30
LOAD_KW = 1.0
LOAD_SCALING = (1.05, 1.1, 0.7)  # the old load_scaling, load_scaling10 and load_scaling90 defaults
LOSSLESS = (1.0, 1.0, 1.0)
LOSSY = (0.96, 0.97, 0.97)  # the default inverter_loss, battery_loss and battery_loss_discharge


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if condition:
        print("  OK: {}".format(name))
        return False
    print("  ERROR: {} {}".format(name, detail))
    return True


def _at(my_predbat, minute):
    """
    Move the clock to minute past midnight, as update_time() would: minutes_now is floored to the 5 minute
    PREDICT_STEP while now_utc_real stays exact.
    """
    my_predbat.now_utc_real = my_predbat.midnight_utc + timedelta(minutes=minute)
    my_predbat.minutes_now = int(minute / 5) * 5


def _dispatch(my_predbat, start, end, dynamic=True, cancelled=False, octopus=True):
    """
    One car with an Intelligent dispatch from start to end. dynamic is octopus_intelligent_dynamic (the
    dispatch check on); cancelled is the check's decision this cycle, as dynamic_load_car_check() leaves it.
    """
    my_predbat.num_cars = 1
    my_predbat.car_charging_slots = [[{"start": start, "end": end, "kwh": 10.0, "octopus": octopus}]] + [[] for _ in range(PREDBAT_MAX_CARS - 1)]
    my_predbat.octopus_intelligent_dynamic = dynamic
    my_predbat.dynamic_load_car_effective = {0: cancelled}


def _expected_credit(my_predbat, run_end):
    """
    The credit map a boosted run from now to run_end should produce: IO_CONFIRMED_CREDIT for the current
    half hour, halved for the next, a third for the one after and so on.
    """
    period_start = (my_predbat.minutes_now // 30) * 30
    return {minute: IO_CONFIRMED_CREDIT / (1 + (minute - period_start) // 30) for minute in range(my_predbat.minutes_now, run_end)}


def run_credit_map_tests(my_predbat):
    """
    io_confirmed_credit_update() decides which minutes carry the credit, and asks for a fresh plan when a run
    first becomes boosted - leaving the incumbent for should_replace_plan() to judge.
    """
    print("\n**** IOG bird-in-hand credit map ****")
    failed = False
    now = 1145  # 19:05
    _at(my_predbat, now)
    run_end = 1140 + 30 * IOG_SLOTS
    my_predbat.io_adjusted = {minute: True for minute in range(1140, run_end)}
    my_predbat.io_confirmed_active = False
    my_predbat.io_confirmed_credit = {}

    # In a dispatch that is not cancelled: the run from now to the end of the dispatch-made-cheap minutes, tapering per half hour
    _dispatch(my_predbat, 1140, run_end)
    my_predbat.plan_valid = True
    changed = my_predbat.io_confirmed_credit_update()
    expected = _expected_credit(my_predbat, run_end)
    failed |= _check("b1 dispatch run gets the credit", my_predbat.io_confirmed_credit == expected, "got {} entries, first {}".format(len(my_predbat.io_confirmed_credit), sorted(my_predbat.io_confirmed_credit.items())[:2]))
    failed |= _check("b1 current half hour full credit", my_predbat.io_confirmed_credit.get(now) == IO_CONFIRMED_CREDIT)
    failed |= _check("b1 next half hour half credit", abs(my_predbat.io_confirmed_credit.get(1170, 0) - IO_CONFIRMED_CREDIT / 2) < 1e-9)
    failed |= _check("b1 nothing after the run", run_end not in my_predbat.io_confirmed_credit)
    failed |= _check("b1 newly boosted asks for a replan", changed is True, "returned {}".format(changed))
    failed |= _check("b1 the incumbent plan is kept for should_replace_plan() to judge", my_predbat.plan_valid is True)
    failed |= _check("b1 newly boosted arms the one-off tie-break", my_predbat.io_confirmed_tiebreak is True)

    # Later cycles of the same dispatch, including across half-hour boundaries: still boosted, but no fresh replan
    # and the tie-break is not re-armed once a replan has spent it, so the plan is not up for replacement every cycle
    for minute in [1150, 1170, 1171, 1175, 1200, 1201]:
        _at(my_predbat, minute)
        _dispatch(my_predbat, 1140, run_end)
        my_predbat.plan_valid = True
        my_predbat.io_confirmed_tiebreak = False
        changed = my_predbat.io_confirmed_credit_update()
        failed |= _check(
            "b2 at {} still boosted, no replan or tie-break".format(my_predbat.time_abs_str(minute)),
            my_predbat.io_confirmed_credit == _expected_credit(my_predbat, run_end) and changed is False and my_predbat.plan_valid is True and my_predbat.io_confirmed_tiebreak is False,
            "entries {} returned {} plan_valid {} tiebreak {}".format(len(my_predbat.io_confirmed_credit), changed, my_predbat.plan_valid, my_predbat.io_confirmed_tiebreak),
        )
    _at(my_predbat, now)

    # Cancelled by the dispatch check: no credit, and resuming asks for a replan again
    _dispatch(my_predbat, 1140, run_end, cancelled=True)
    changed = my_predbat.io_confirmed_credit_update()
    failed |= _check("b3 cancelled dispatch has no credit", my_predbat.io_confirmed_credit == {} and changed is False)
    _dispatch(my_predbat, 1140, run_end)
    changed = my_predbat.io_confirmed_credit_update()
    failed |= _check("b3 resumed dispatch asks for a replan", changed is True)

    # The dispatch has ended (the house rate may still be cheap for the rest of the half hour)
    my_predbat.io_confirmed_active = False
    _dispatch(my_predbat, 1080, 1140)
    failed |= _check("b4 ended dispatch has no credit", not my_predbat.io_confirmed_credit_update() and my_predbat.io_confirmed_credit == {})

    # The dispatch check off: the dispatch having started is enough
    _dispatch(my_predbat, 1140, run_end, dynamic=False)
    changed = my_predbat.io_confirmed_credit_update()
    failed |= _check("b5 check off, started dispatch gets the credit", my_predbat.io_confirmed_credit == _expected_credit(my_predbat, run_end) and changed is True)

    # The dispatch has not started yet
    my_predbat.io_confirmed_active = False
    _dispatch(my_predbat, 1200, run_end)
    failed |= _check("b6 dispatch not started has no credit", not my_predbat.io_confirmed_credit_update() and my_predbat.io_confirmed_credit == {})

    # Not an Octopus dispatch at all
    _dispatch(my_predbat, 1140, run_end, octopus=False)
    failed |= _check("b7 Predbat-led slot has no credit", not my_predbat.io_confirmed_credit_update() and my_predbat.io_confirmed_credit == {})

    # The dispatch sits inside the fixed off-peak window, so it made nothing cheaper and holds no risk
    _dispatch(my_predbat, 1140, run_end)
    my_predbat.io_adjusted = {minute: True for minute in range(1200, run_end)}
    failed |= _check("b8 current half hour not made cheap by the dispatch has no credit", not my_predbat.io_confirmed_credit_update() and my_predbat.io_confirmed_credit == {})
    my_predbat.io_adjusted = {minute: True for minute in range(1140, run_end)}

    # A compare.py run (save False) works the credit out but neither remembers it nor arms the tie-break
    my_predbat.io_confirmed_active = False
    my_predbat.plan_valid = True
    my_predbat.io_confirmed_tiebreak = False
    _dispatch(my_predbat, 1140, run_end)
    changed = my_predbat.io_confirmed_credit_update(save=False)
    failed |= _check("b9 save False still builds the credit", my_predbat.io_confirmed_credit == _expected_credit(my_predbat, run_end))
    failed |= _check("b9 save False keeps the state", changed is False and my_predbat.io_confirmed_active is False and my_predbat.plan_valid is True and my_predbat.io_confirmed_tiebreak is False)
    return failed


def _scenario(my_predbat, name, battery_soc, losses=LOSSLESS, boosted=True, firm_rate=LOW_RATE, model_car=False, load_kw=LOAD_KW):
    """
    The motivating case: the dispatch runs from now (19:00) to 23:30, the fixed off-peak window from 23:30 to
    05:30, the house draws about 1 kW in between and nothing else is cheap. With boosted the car is in its
    dispatch and it is not cancelled; without, no car dispatch is known. Returns the optimised charge limits.

    The car's dispatch decides the credit, but the prediction models the house alone unless model_car is set:
    a modelled car adds its own load and holds the battery while it charges, which is a different evening.

    The optimisation runs on the C++ kernel: on the Python engine these scenarios took over 30 seconds.
    """
    now = my_predbat.minutes_now
    _at(my_predbat, now)
    windows = []
    for n in range(IOG_SLOTS + FIRM_SLOTS):
        start = now + 30 * n
        windows.append({"start": start, "end": start + 30, "average": LOW_RATE if n < IOG_SLOTS else firm_rate})
    run_end = now + 30 * IOG_SLOTS

    def prepare():
        """Set up the car's dispatch (or none) once the harness has stamped io_adjusted, before the Prediction is built."""
        _dispatch(my_predbat, now, run_end)
        if not boosted:
            my_predbat.num_cars = 0
            my_predbat.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
        my_predbat.io_confirmed_active = False
        my_predbat.io_confirmed_credit_update(save=False)
        if not model_car:
            my_predbat.num_cars = 0
            my_predbat.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]

    limits, _ = run_iog_skew_scenario(
        name,
        my_predbat,
        windows,
        io_adjusted_starts=range(now, run_end),
        battery_size=10.0,
        battery_soc=battery_soc,
        charge_rate_kw=3.0,
        load_amount=load_kw,
        load_scaling=LOAD_SCALING,
        losses=losses,
        prepare=prepare,
        kernel=True,
    )
    print("     charge limits: {}".format(limits))
    return limits


def run_planner_tests(my_predbat):
    """
    Plan-level checks against the motivating case, with the default scenario weights.
    """
    print("\n**** IOG bird-in-hand planner ****")
    failed = False
    reset_inverter(my_predbat)
    # The fixture discharges at 1 kW, below the house load, which would leave the grid topping up whatever the battery holds
    my_predbat.battery_rate_max_discharge = 3.0 / 60.0
    my_predbat.discharge_rate_now = 3.0 / 60.0
    my_predbat.pv_metric10_weight = 0.15
    my_predbat.pv_metric90_weight = 0.15

    # Without the credit, once the battery covers even the PV10 evening (4.95 kWh) both plans score the same and
    # the planner waits for 23:30 - the case this credit exists for
    limits = _scenario(my_predbat, "no boost soc=5.0", 5.0, boosted=False)
    failed |= _check("p1 without the boost the plan waits for the off-peak window", limits[0] <= 5.0, "limit {}".format(limits[0]))

    # Boosted: charge in the dispatch slot already in hand, across starting charge and with or without losses
    for losses, socs in [(LOSSLESS, [3.0, 4.0, 4.75, 5.0, 5.5, 6.0, 7.0]), (LOSSY, [4.0, 5.0, 6.0, 7.0])]:
        for soc in socs:
            limits = _scenario(my_predbat, "boosted soc={} losses={}".format(soc, losses), soc, losses=losses)
            failed |= _check("p2 soc={} losses={} charges in the current slot".format(soc, losses), limits[0] > soc, "limit {}".format(limits[0]))

    # The taper puts the run's charge at its front, the half hours least likely to vanish
    limits = _scenario(my_predbat, "boosted front-loaded soc=5.0", 5.0)
    charged = [n for n in range(IOG_SLOTS) if limits[n] > 5.0]
    failed |= _check("p3 run charge is front-loaded", charged == list(range(len(charged))), "charged run slots {}".format(charged))

    # A genuinely cheaper off-peak window still wins: the credit only settles a tie
    limits = _scenario(my_predbat, "boosted cheaper firm soc=6.0", 6.0, firm_rate=6.5)
    failed |= _check("p4 cheaper later slot still wins", all(limit <= 6.0 for limit in limits[:IOG_SLOTS]) and any(limit > 6.0 for limit in limits[IOG_SLOTS:]), "limits {}".format(limits))

    # Nearly full with losses: topping up now and holding it costs about 0.2p more than not, more than the
    # credit is worth, so the plan holds - the credit settles ties, it does not override a real cost
    limits = _scenario(my_predbat, "boosted lossy soc=9.0", 9.0, losses=LOSSY)
    failed |= _check("p4 credit does not outweigh the cost of losses", limits[0] <= 9.0, "limit {}".format(limits[0]))

    # A full battery with no load to draw it down has nothing to take, so the credit changes nothing
    limits_off = _scenario(my_predbat, "no boost full", 10.0, boosted=False, load_kw=0.0)
    limits_on = _scenario(my_predbat, "boosted full", 10.0, load_kw=0.0)
    failed |= _check("p5 full battery plan unchanged", limits_on == limits_off, "with {} without {}".format(limits_on, limits_off))

    # With the car modelled too (its own load, and the battery held while it charges) the slot in hand is still used
    limits = _scenario(my_predbat, "boosted car modelled soc=5.0", 5.0, model_car=True)
    failed |= _check("p6 with the car modelled charges in the current slot", limits[0] > 5.0, "limit {}".format(limits[0]))
    return failed


def run_prediction_tests(my_predbat):
    """
    The credit is score only: it lowers metric_keep by the kWh stored in the run times its credit, and never
    touches the cost. Holding the battery stores nothing, so earns nothing.
    """
    print("\n**** IOG bird-in-hand prediction ****")
    failed = False
    reset_inverter(my_predbat)
    my_predbat.battery_rate_max_discharge = 3.0 / 60.0
    my_predbat.discharge_rate_now = 3.0 / 60.0
    _scenario(my_predbat, "prediction setup", 5.0)
    windows = [dict(window) for window in my_predbat.charge_window_best]
    credit = copy.deepcopy(my_predbat.io_confirmed_credit)

    def predict(charge_limit, with_credit, kernel):
        """Run the nominal scenario for charge_limit with or without the credit map, on the C++ kernel or the Python engine."""
        my_predbat.io_confirmed_credit = credit if with_credit else {}
        my_predbat.prediction_kernel_enable = kernel
        my_predbat.prediction = Prediction(
            my_predbat,
            my_predbat.pv_forecast_minute_step,
            my_predbat.pv_forecast_minute10_step,
            my_predbat.load_minutes_step,
            my_predbat.load_minutes_step10,
            my_predbat.pv_forecast_minute90_step,
            my_predbat.load_minutes_step90,
        )
        result = my_predbat.run_prediction(charge_limit, windows, [], [], False, end_record=my_predbat.forecast_minutes)
        return result[0], result[8]

    for kernel in (False, True):
        engine = "kernel" if kernel else "python"
        # Charge the current half hour to full: 30 minutes at 3 kW stores 1.5 kWh, all at the full credit
        charge_now = [10.0] + [0] * (len(windows) - 1)
        cost_off, keep_off = predict(charge_now, False, kernel)
        cost_on, keep_on = predict(charge_now, True, kernel)
        failed |= _check("c1 {} cost unchanged by the credit".format(engine), cost_on == cost_off, "{} vs {}".format(cost_on, cost_off))
        failed |= _check("c1 {} metric_keep lowered by stored kWh x credit".format(engine), abs((keep_off - keep_on) - 1.5 * IO_CONFIRMED_CREDIT) < 1e-3, "keep {} vs {}".format(keep_on, keep_off))

        # Hold at the current charge: nothing stored, nothing credited
        hold_now = [5.0] + [0] * (len(windows) - 1)
        cost_off, keep_off = predict(hold_now, False, kernel)
        cost_on, keep_on = predict(hold_now, True, kernel)
        failed |= _check("c2 {} hold earns no credit".format(engine), cost_on == cost_off and keep_on == keep_off, "cost {} vs {} keep {} vs {}".format(cost_on, cost_off, keep_on, keep_off))
    return failed


def run_iog_bird_in_hand_tests(my_predbat):
    """
    Tests for the Octopus Intelligent bird-in-hand credit.
    """
    saved = _snapshot_state(my_predbat)
    saved_extra = {field: copy.deepcopy(getattr(my_predbat, field)) for field in STATE_FIELDS if hasattr(my_predbat, field)}
    failed = False
    try:
        failed |= run_credit_map_tests(my_predbat)
        failed |= run_prediction_tests(my_predbat)
        failed |= run_planner_tests(my_predbat)
    finally:
        _restore_state(my_predbat, saved)
        for field, value in saved_extra.items():
            setattr(my_predbat, field, value)
    if failed:
        print("\n**** iog_bird_in_hand tests: FAILED ****")
    else:
        print("\n**** iog_bird_in_hand tests: PASSED ****")
    return 1 if failed else 0
