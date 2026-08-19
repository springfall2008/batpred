# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import math
import json
import os
import random
import datetime
import time
import cProfile
import pstats
import io
import traceback

import yaml

from tests.test_infra import reset_inverter
from const import PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MINUTES_PER_DAY = 1440
# Car charging. Drawn from a separate RNG stream (see generate_random_scenario) so adding cars leaves
# every pre-existing scenario parameter bit-identical and old benchmark baselines stay comparable.
CAR_SEED_SALT = 0x5CA12          # keeps the car draws off the main rng sequence
CAR_PROBABILITY = 0.5            # fraction of scenarios that get at least one car
CAR_MAX_COUNT = 2
CAR_SLOTS_MIN = 4                # a plugged-in EV typically has a handful of planned slots
CAR_SLOTS_MAX = 20
CAR_BATTERY_KWH_OPTIONS = [40.0, 60.0, 77.0, 100.0]
# Feature flags. Drawn from their own RNG stream for the same reason as cars, so adding them leaves
# every pre-existing scenario parameter bit-identical. Until these existed the template pinned all
# three for every scenario - low power charge on, iboost and low power export off - so the
# byte-identical benchmark could not see any change to the code paths they guard.
FEATURE_SEED_SALT = 0xFEA7        # keeps the feature draws off the main rng sequence
IBOOST_PROBABILITY = 0.4          # fraction of scenarios running an immersion boost
# Load scaling for the three simulated futures, again on its own rng stream. The template pinned
# load_scaling 0.5 / load_scaling10 0.6 and left load_scaling90 at its 0.7 default, which the planner
# detects as inverted and clamps back to 0.5 - so PV90 carried exactly the central case's load in
# every scenario and the pv90 column could not measure anything the nominal column did not.
LOAD_SCALING_SEED_SALT = 0x10AD5
LOAD_SCALING_MIN = 0.2
LOAD_SCALING_MAX = 2.0
# Octopus Intelligent (IOG) planned-dispatch windows, again on their own rng stream. Without these no
# generated scenario ever sets io_adjusted, so the IOG charge-skew code in sort_window_by_price_combined
# (see plan.py) has no random-suite coverage - every scenario takes the same path whether it is present
# or not. One contiguous run per enabled scenario is enough to exercise both ends of the skew: the
# earliest slots (discounted) and the latest (penalised).
IOG_SEED_SALT = 0x10641           # keeps the IOG draws off the main rng sequence
IOG_PROBABILITY = 0.5             # fraction of scenarios that get an IOG dispatch run
IOG_RUN_SLOTS_MIN = 2             # shortest dispatch run: 1 hour
IOG_RUN_SLOTS_MAX = 12            # longest dispatch run: 6 hours
CLOCK_STEP_MINUTES = 5            # predbat runs on a 5 minute cadence, so start times are multiples of 5
RATE_HISTORY_DAYS = 3             # days of past + future rates to generate
RATE_FUTURE_DAYS = 2

PV10_FRACTION = 0.6               # pv10 = pv * this fraction
# pv90 = pv * this factor. Deliberately NOT the mirror of PV10_FRACTION (which would be 1.4):
# solar upside is bounded by clear-sky while the downside is not, so a real p90 sits much closer
# to the central forecast than the p10 does. 1.25 is in the range Solcast typically produces.
PV90_FACTOR = 1.25                # pv90 = pv * this factor
GAUSSIAN_PV_SIGMA_MINUTES = 180   # 3-hour std dev for PV bell curve
PV_SUNRISE_MINUTE_MIN = 240       # 04:00 earliest sunrise
PV_SUNRISE_MINUTE_MAX = 480       # 08:00 latest sunrise
PV_SUNSET_MINUTE_MIN = 960        # 16:00 earliest sunset
PV_SUNSET_MINUTE_MAX = 1320       # 22:00 latest sunset

LOAD_GAUSSIAN_SIGMA_MINUTES = 60  # 1-hour std dev for load peaks

BATTERY_SOC_OPTIONS_KWH = [2.4, 4.8, 7.0, 9.5, 13.5, 20.0]
PV_PEAK_OPTIONS_KW = [0.0, 1.0, 2.0, 4.0, 6.0, 10.0]

RATE_TYPES = ["single", "dual", "triple", "halfhourly"]
IMPORT_RATE_TYPES = RATE_TYPES + ["negative_halfhourly"]

# ---------------------------------------------------------------------------
# Daily profile generators  (return list[float] with exactly 1440 elements)
# ---------------------------------------------------------------------------


def _smooth_rate_profile(control_points, allow_negative=False):
    """Cosine-interpolate between control points to produce a 1440-minute rate profile.

    Adjacent control points are connected with a smooth S-curve (raised cosine), so the
    profile ramps gradually between each peak/trough rather than jumping abruptly.

    Args:
        control_points: list of (minute, rate) pairs covering at least minute 0 and 1439
        allow_negative: if False, clamp all output values to >= 0

    Returns:
        list[float] of 1440 values
    """
    pts = sorted(control_points, key=lambda x: x[0])
    profile = []
    idx = 0
    n = len(pts)
    for m in range(MINUTES_PER_DAY):
        while idx + 1 < n and pts[idx + 1][0] <= m:
            idx += 1
        t0, v0 = pts[idx]
        if idx + 1 >= n:
            val = v0
        else:
            t1, v1 = pts[idx + 1]
            span = t1 - t0
            mu = (m - t0) / span if span > 0 else 1.0
            val = v0 + (v1 - v0) * (1.0 - math.cos(mu * math.pi)) / 2.0
        if not allow_negative:
            val = max(0.0, val)
        profile.append(val)
    return profile


def generate_rates_day(rate_type, params, seed):
    """Generate a single-day (1440-minute) per-minute rate profile in p/kWh.

    Args:
        rate_type: One of "single", "dual", "triple", "halfhourly", "negative_halfhourly"
        params: dict of rate parameters (see per-type docs below)
        seed: integer seed for halfhourly/negative_halfhourly random generation

    Returns:
        list[float] of 1440 values
    """
    profile = [0.0] * MINUTES_PER_DAY

    if rate_type == "single":
        rate = float(params["rate"])
        for m in range(MINUTES_PER_DAY):
            profile[m] = rate

    elif rate_type == "dual":
        day_rate = float(params["day_rate"])
        night_rate = float(params["night_rate"])
        night_start = _hhmm_to_minute(params["night_start_hhmm"])
        night_end = _hhmm_to_minute(params["night_end_hhmm"])
        for m in range(MINUTES_PER_DAY):
            if _in_window(m, night_start, night_end):
                profile[m] = night_rate
            else:
                profile[m] = day_rate

    elif rate_type == "triple":
        # cheap overnight, shoulder daytime, expensive peak evening
        night_rate = float(params["night_rate"])
        shoulder_rate = float(params["shoulder_rate"])
        peak_rate = float(params["peak_rate"])
        night_start = _hhmm_to_minute(params["night_start_hhmm"])
        night_end = _hhmm_to_minute(params["night_end_hhmm"])
        peak_start = _hhmm_to_minute(params["peak_start_hhmm"])
        peak_end = _hhmm_to_minute(params["peak_end_hhmm"])
        for m in range(MINUTES_PER_DAY):
            if _in_window(m, night_start, night_end):
                profile[m] = night_rate
            elif _in_window(m, peak_start, peak_end):
                profile[m] = peak_rate
            else:
                profile[m] = shoulder_rate

    elif rate_type in ("halfhourly", "negative_halfhourly"):
        # Smooth profile: cosine-interpolate between randomly placed peaks and troughs
        allow_negative = rate_type == "negative_halfhourly"
        rng = random.Random(seed)
        base = float(params.get("base_rate", 15.0))
        low = float(params.get("low_rate", 5.0))
        high = float(params.get("high_rate", 30.0))
        num_highs = int(params.get("num_highs", 2))
        num_lows = int(params.get("num_lows", 2))
        control_points = [(0, base), (MINUTES_PER_DAY - 1, base)]
        for _ in range(num_highs):
            control_points.append((rng.randint(0, MINUTES_PER_DAY - 1), high))
        for _ in range(num_lows):
            control_points.append((rng.randint(0, MINUTES_PER_DAY - 1), low))
        profile = _smooth_rate_profile(control_points, allow_negative)

    return profile


def generate_load_day(daily_kwh, load_type, base_kw, morning_peak_kw, evening_peak_kw, morning_peak_minute=450, evening_peak_minute=1080):
    """Generate a single-day (1440-minute) per-minute load profile in kWh.

    The kWh values are energy consumed in each 1-minute period (i.e. kW * 1/60).
    Values are scaled so that sum(profile) == daily_kwh.

    Args:
        daily_kwh: total energy consumed in the day
        load_type: "flat" or "residential"
        base_kw: base constant load in kW
        morning_peak_kw: height of first Gaussian peak in kW
        evening_peak_kw: height of second Gaussian peak in kW
        morning_peak_minute: minute-of-day for first peak centre (default 450 = 07:30)
        evening_peak_minute: minute-of-day for second peak centre (default 1080 = 18:00)

    Returns:
        list[float] of 1440 values (kWh per minute)
    """
    profile = [0.0] * MINUTES_PER_DAY

    if load_type == "flat":
        per_min = daily_kwh / MINUTES_PER_DAY
        for m in range(MINUTES_PER_DAY):
            profile[m] = per_min
    else:
        # Residential: base load + two Gaussian peaks at randomised times
        for m in range(MINUTES_PER_DAY):
            power_kw = base_kw
            power_kw += morning_peak_kw * math.exp(-0.5 * ((m - morning_peak_minute) / LOAD_GAUSSIAN_SIGMA_MINUTES) ** 2)
            power_kw += evening_peak_kw * math.exp(-0.5 * ((m - evening_peak_minute) / LOAD_GAUSSIAN_SIGMA_MINUTES) ** 2)
            profile[m] = power_kw / 60.0  # kW -> kWh per minute

        # Rescale so the total matches daily_kwh
        total = sum(profile)
        if total > 0:
            scale = daily_kwh / total
            profile = [v * scale for v in profile]

    return profile


def generate_pv_day(peak_kw, peak_hour, sunrise_minute=None, sunset_minute=None):
    """Generate a single-day (1440-minute) per-minute PV power profile in kW.

    Args:
        peak_kw: peak PV power in kW (0 = no solar)
        peak_hour: hour of peak (e.g. 13.0 = 13:00)
        sunrise_minute: minute-of-day when generation starts (default: midpoint of allowed range)
        sunset_minute: minute-of-day when generation ends (default: midpoint of allowed range)

    Returns:
        list[float] of 1440 values (kW)
    """
    if sunrise_minute is None:
        sunrise_minute = (PV_SUNRISE_MINUTE_MIN + PV_SUNRISE_MINUTE_MAX) // 2
    if sunset_minute is None:
        sunset_minute = (PV_SUNSET_MINUTE_MIN + PV_SUNSET_MINUTE_MAX) // 2

    profile = [0.0] * MINUTES_PER_DAY
    if peak_kw <= 0:
        return profile

    peak_minute = int(peak_hour * 60)
    for m in range(MINUTES_PER_DAY):
        if m < sunrise_minute or m > sunset_minute:
            profile[m] = 0.0
        else:
            profile[m] = peak_kw * math.exp(-0.5 * ((m - peak_minute) / GAUSSIAN_PV_SIGMA_MINUTES) ** 2)

    return profile


# ---------------------------------------------------------------------------
# Scenario generation
# ---------------------------------------------------------------------------


def generate_random_scenario(scenario_id, seed):
    """Generate a single random scenario dict from a seed.

    The scenario stores both randomised scalar params AND the complete
    generated time-series data (as compact 1440-value daily profiles) so that
    stored scenarios never change even if the generator code changes later.

    Args:
        scenario_id: integer id for this scenario
        seed: integer random seed

    Returns:
        dict with keys: id, seed, params, data
    """
    rng = random.Random(seed)

    # --- Battery ---
    soc_max_kwh = rng.choice(BATTERY_SOC_OPTIONS_KWH)
    initial_soc_percent = rng.randint(0, 100)
    rate_max_charge_kw = round(rng.uniform(1.0, 5.0), 2)
    rate_max_discharge_kw = round(rng.uniform(1.0, 5.0), 2)

    # --- Inverter ---
    hybrid = rng.choice([True, False])
    inverter_limit_kw = round(rng.uniform(2.5, 10.0), 2)
    export_limit_kw = round(rng.uniform(0.0, inverter_limit_kw), 2)

    # --- Load ---
    daily_kwh = round(rng.uniform(5.0, 50.0), 2)
    load_type = rng.choice(["flat", "residential"])
    base_kw = round(rng.uniform(0.1, 0.5), 3)
    morning_peak_kw = round(rng.uniform(0.5, 3.0), 2)
    evening_peak_kw = round(rng.uniform(0.5, 3.0), 2)
    morning_peak_minute = rng.randint(0, MINUTES_PER_DAY - 1)
    evening_peak_minute = rng.randint(0, MINUTES_PER_DAY - 1)

    # --- PV ---
    peak_kw = rng.choice(PV_PEAK_OPTIONS_KW)
    peak_hour = round(rng.uniform(12.0, 14.0), 2)
    sunrise_minute = rng.randint(PV_SUNRISE_MINUTE_MIN, PV_SUNRISE_MINUTE_MAX)
    sunset_minute = rng.randint(PV_SUNSET_MINUTE_MIN, PV_SUNSET_MINUTE_MAX)

    # --- Import rate ---
    import_rate_type = rng.choice(IMPORT_RATE_TYPES)
    import_rate_params = _sample_rate_params(rng, import_rate_type, cheap=True)
    import_rate_seed = rng.randint(0, 2**31)

    # --- Export rate ---
    export_rate_type = rng.choice(RATE_TYPES)
    export_rate_params = _sample_rate_params(rng, export_rate_type, cheap=False)
    export_rate_seed = rng.randint(0, 2**31)

    # --- Generate daily data profiles ---
    rate_import_day = generate_rates_day(import_rate_type, import_rate_params, import_rate_seed)
    rate_export_day = generate_rates_day(export_rate_type, export_rate_params, export_rate_seed)
    load_day_kwh = generate_load_day(daily_kwh, load_type, base_kw, morning_peak_kw, evening_peak_kw, morning_peak_minute, evening_peak_minute)
    pv_day_kw = generate_pv_day(peak_kw, peak_hour, sunrise_minute, sunset_minute)

    # --- Clock ---
    # Drawn last so that re-generating an existing seed leaves every other parameter unchanged.
    # A random minute-of-day (on predbat's 5 minute run cadence) means scenarios start at every point
    # of the tariff and solar day - overnight cheap windows, the evening peak, mid-generation - and
    # land part way through a plan slot, which is the only way partially-elapsed (in-progress) slots
    # get exercised.
    clock_minutes_now = rng.randrange(0, MINUTES_PER_DAY, CLOCK_STEP_MINUTES)

    # --- Cars ---
    # Deliberately drawn from a SEPARATE rng seeded off the same scenario seed, not from rng above.
    # Taking these from the main stream would shift every subsequent draw and silently rewrite every
    # existing scenario, making stored benchmark baselines incomparable. This way re-generating an
    # existing seed reproduces all of the above exactly and only adds the car block.
    #
    # Cars matter to the benchmark because hit_car_window() is called millions of times per plan and
    # returns on its first line when num_cars is 0 - so a car-less suite cannot see the cost of the
    # code it guards, nor of the car load itself in the prediction.
    car_rng = random.Random(seed ^ CAR_SEED_SALT)
    num_cars = car_rng.randint(1, CAR_MAX_COUNT) if car_rng.random() < CAR_PROBABILITY else 0
    car_battery_kwh = []
    car_slots = []
    for _car_n in range(num_cars):
        car_battery_kwh.append(car_rng.choice(CAR_BATTERY_KWH_OPTIONS))
        slot_count = car_rng.randint(CAR_SLOTS_MIN, CAR_SLOTS_MAX)
        # Contiguous half hour slots from a random start, which is the shape predbat's own planners
        # produce, wrapped inside the day so the slots stay within the profile.
        slot_start = car_rng.randrange(0, MINUTES_PER_DAY, CLOCK_STEP_MINUTES)
        slots = []
        for slot_n in range(slot_count):
            start = (slot_start + slot_n * 30) % MINUTES_PER_DAY
            slots.append({"start": start, "end": start + 30, "kwh": round(car_rng.uniform(0.0, 3.5), 3)})
        car_slots.append(slots)

    # --- Octopus Intelligent (IOG) dispatch windows ---
    # Own rng stream, see the car block above - regenerating an existing seed leaves every other field
    # bit-identical and only adds this block. Without it no generated scenario ever sets io_adjusted,
    # so the IOG charge-skew code in sort_window_by_price_combined (plan.py) has no random-suite
    # coverage at all - every scenario takes the same path whether that code exists or not.
    iog_rng = random.Random(seed ^ IOG_SEED_SALT)
    iog_enabled = iog_rng.random() < IOG_PROBABILITY
    iog_run_start_minute = 0
    iog_run_slots = 0
    if iog_enabled:
        # One contiguous run of half-hour dispatch slots, wrapped inside the day - long enough to
        # exercise both ends of the skew gradient (earliest slots discounted, latest penalised).
        # start_minute + run_slots is all expand_io_adjusted needs to reconstruct the run, so unlike
        # the day-profile arrays above nothing further is stored in "data".
        iog_run_slots = iog_rng.randint(IOG_RUN_SLOTS_MIN, IOG_RUN_SLOTS_MAX)
        iog_run_start_minute = iog_rng.randrange(0, MINUTES_PER_DAY, CLOCK_STEP_MINUTES)

    # --- Load scaling ---
    # Own rng stream, see the car block above. Sorted so that load_scaling90 <= load_scaling <=
    # load_scaling10, which is the order the planner requires: PV90 is the sunny, light load future
    # and PV10 the cloudy, heavy one. Out of order it clamps them and warns, which is what the
    # template was doing.
    scaling_rng = random.Random(seed ^ LOAD_SCALING_SEED_SALT)
    scaling_pv90, scaling_nominal, scaling_pv10 = sorted(round(scaling_rng.uniform(LOAD_SCALING_MIN, LOAD_SCALING_MAX), 3) for _ in range(3))

    # --- Feature flags ---
    # Separate rng, see the car block above. iboost_smart is what makes fetch build an iboost_plan,
    # and iboost_on_export decides whether an export window colliding with that plan is rejected -
    # the two together are what exercise the optimiser's iboost path rather than just the prediction's.
    feature_rng = random.Random(seed ^ FEATURE_SEED_SALT)
    features = {
        "set_export_low_power": feature_rng.choice([True, False]),
        "iboost_enable": feature_rng.random() < IBOOST_PROBABILITY,
    }
    if features["iboost_enable"]:
        features.update(
            {
                "iboost_solar": feature_rng.choice([True, False]),
                "iboost_smart": feature_rng.choice([True, False]),
                "iboost_charging": feature_rng.choice([True, False]),
                "iboost_on_export": feature_rng.choice([True, False]),
                "iboost_prevent_discharge": feature_rng.choice([True, False]),
                "iboost_max_energy": round(feature_rng.uniform(1.0, 10.0), 2),
                "iboost_max_power_kw": round(feature_rng.uniform(1.0, 3.0), 2),
                "iboost_today": round(feature_rng.uniform(0.0, 3.0), 2),
            }
        )

    return {
        "id": scenario_id,
        "seed": seed,
        "params": {
            "clock": {
                "minutes_now": clock_minutes_now,
            },
            "features": features,
            "load_scaling": {
                "nominal": scaling_nominal,
                "pv10": scaling_pv10,
                "pv90": scaling_pv90,
            },
            "cars": {
                "num_cars": num_cars,
                "battery_kwh": car_battery_kwh,
                "slots": car_slots,
            },
            "iog": {
                "enabled": iog_enabled,
                "run_start_minute": iog_run_start_minute,
                "run_slots": iog_run_slots,
            },
            "battery": {
                "soc_max_kwh": soc_max_kwh,
                "initial_soc_percent": initial_soc_percent,
                "rate_max_charge_kw": rate_max_charge_kw,
                "rate_max_discharge_kw": rate_max_discharge_kw,
            },
            "inverter": {
                "hybrid": hybrid,
                "inverter_limit_kw": inverter_limit_kw,
                "export_limit_kw": export_limit_kw,
            },
            "load": {
                "daily_kwh": daily_kwh,
                "load_type": load_type,
                "base_kw": base_kw,
                "morning_peak_kw": morning_peak_kw,
                "evening_peak_kw": evening_peak_kw,
                "morning_peak_minute": morning_peak_minute,
                "evening_peak_minute": evening_peak_minute,
            },
            "pv": {
                "peak_kw": peak_kw,
                "peak_hour": peak_hour,
                "sunrise_minute": sunrise_minute,
                "sunset_minute": sunset_minute,
            },
            "import_rates": {
                "type": import_rate_type,
                "params": import_rate_params,
            },
            "export_rates": {
                "type": export_rate_type,
                "params": export_rate_params,
            },
        },
        "data": {
            "rate_import_day": rate_import_day,
            "rate_export_day": rate_export_day,
            "load_day_kwh": load_day_kwh,
            "pv_day_kw": pv_day_kw,
        },
    }


def generate_scenarios(count, start_seed=0):
    """Generate a list of random scenarios.

    Args:
        count: number of scenarios to generate
        start_seed: seed for the first scenario; each subsequent scenario uses seed+1

    Returns:
        list of scenario dicts
    """
    return [generate_random_scenario(i, start_seed + i) for i in range(count)]


def save_scenarios(scenarios, filepath):
    """Save a list of scenarios to a YAML file.

    Args:
        scenarios: list of scenario dicts (from generate_scenarios)
        filepath: path to write the YAML file
    """
    data = {"scenarios": scenarios}
    with open(filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    print("Wrote {} scenario(s) to {}".format(len(scenarios), filepath))


def load_scenarios(filepath):
    """Load scenarios from a YAML file.

    Args:
        filepath: path to the YAML file written by save_scenarios

    Returns:
        list of scenario dicts
    """
    with open(filepath, "r") as f:
        data = yaml.safe_load(f)
    return data["scenarios"]


# ---------------------------------------------------------------------------
# Expansion helpers  (daily profile -> full predbat-format dicts)
# ---------------------------------------------------------------------------


def expand_rates(day_profile, minutes_now=0, end_minute=None):
    """Expand a 1440-value daily rate profile into a full predbat rate dict.

    The dict is keyed by minute offset from today's midnight and covers
    RATE_HISTORY_DAYS days in the past and at least RATE_FUTURE_DAYS days in
    the future (or up to end_minute if that is further ahead, which is needed
    when minutes_now is non-zero and the forecast extends beyond midnight of
    the second future day).

    Args:
        day_profile: list[float] of exactly 1440 per-minute rate values (p/kWh)
        minutes_now: current minutes since midnight (used to anchor today)
        end_minute: minimum end minute to cover; defaults to RATE_FUTURE_DAYS * MINUTES_PER_DAY

    Returns:
        dict[int, float]
    """
    result = {}
    start = -RATE_HISTORY_DAYS * MINUTES_PER_DAY
    end = max(RATE_FUTURE_DAYS * MINUTES_PER_DAY, end_minute if end_minute is not None else 0)
    for minute in range(start, end + 1):
        idx = minute % MINUTES_PER_DAY
        result[minute] = day_profile[idx]
    return result


def expand_io_adjusted(run_start_minute, run_slots, minutes_now=0, end_minute=None):
    """Expand one daily-recurring IOG dispatch run into a predbat io_adjusted dict.

    Mirrors expand_rates, but sparse and built from the run's (start, slot count) rather than a
    precomputed profile: self.io_adjusted only ever carries True entries for Octopus Intelligent
    planned-dispatch minutes (see apps/predbat/fetch.py and apps/predbat/octopus.py), and callers
    use io_adjusted.get(minute, False), so minutes that are not dispatched are simply absent here.
    The run recurs at the same clock time every day, the same way the rate/PV/load day profiles do.

    Args:
        run_start_minute: minute-of-day (0-1439) the dispatch run starts
        run_slots: number of contiguous half-hour dispatch slots in the run (0 = no run)
        minutes_now: current minutes since midnight (used to anchor today)
        end_minute: minimum end minute to cover; defaults to RATE_FUTURE_DAYS * MINUTES_PER_DAY

    Returns:
        dict[int, bool]
    """
    if run_slots <= 0:
        return {}

    day_minutes = set()
    for slot_n in range(run_slots):
        slot_start = (run_start_minute + slot_n * 30) % MINUTES_PER_DAY
        for offset in range(30):
            day_minutes.add((slot_start + offset) % MINUTES_PER_DAY)

    result = {}
    start = -RATE_HISTORY_DAYS * MINUTES_PER_DAY
    end = max(RATE_FUTURE_DAYS * MINUTES_PER_DAY, end_minute if end_minute is not None else 0)
    for minute in range(start, end + 1):
        if (minute % MINUTES_PER_DAY) in day_minutes:
            result[minute] = True
    return result


def expand_load_minutes(day_kwh_profile, minutes_now=0, history_days=14):
    """Build a predbat load_minutes cumulative-kWh dict from a daily profile.

    load_minutes uses positive integer keys where key i means "i minutes ago".
    get_from_incrementing(data, i) = data[i] - data[i+1] gives the energy
    consumed i minutes ago. Larger keys = further in the past = more cumulative
    energy stored.

    get_historical(data, minute) looks up data[1440 - minute] for days_previous=[1],
    so data must be populated at those positive indices.

    We align the daily profile to clock time using minutes_now: the energy
    at index i (= i minutes ago) corresponds to minute-of-day
    (minutes_now - i) % 1440 in the daily profile.

    Args:
        day_kwh_profile: list[float] of 1440 per-minute kWh values
        minutes_now: current minute-of-day
        history_days: how many days of history to generate

    Returns:
        dict[int, float]
    """
    result = {}
    max_i = history_days * MINUTES_PER_DAY
    # Build from the far past (max_i) to now (0).
    # result[i] = result[i+1] + profile[(minutes_now - i) % 1440]
    # so that result[i] - result[i+1] = profile[(minutes_now - i) % 1440]
    cumulative = 0.0
    result[max_i + 1] = cumulative
    for i in range(max_i, -1, -1):
        minute_of_day = (minutes_now - i) % MINUTES_PER_DAY
        cumulative += day_kwh_profile[minute_of_day]
        result[i] = cumulative
    return result


def expand_pv_forecast(day_kw_profile, forecast_minutes, minutes_now=0, pv10_fraction=PV10_FRACTION, pv90_factor=PV90_FACTOR):
    """Build predbat pv_forecast_minute, pv_forecast_minute10 and pv_forecast_minute90 from a daily profile.

    The p90 series matters because without one the PV90 upside scenario falls back to a copy of the p50
    and becomes inert - identical to nominal apart from load scaling - so a benchmark run against these
    scenarios could not tell whether PV90 helps or hurts.

    Args:
        day_kw_profile: list[float] of 1440 per-minute kW values
        forecast_minutes: how many minutes forward from now to cover (e.g. 2880 = 48h)
        minutes_now: current minute-of-day. The series is keyed by absolute minutes from midnight
            because step_data_history reads forward data at (minute + minutes_now), so it has to run
            to minutes_now + forecast_minutes or the tail of the horizon silently has no PV.
        pv10_fraction: fraction of normal forecast used for the P10 (pessimistic) forecast
        pv90_factor: multiple of normal forecast used for the P90 (optimistic) forecast

    Returns:
        tuple(dict[int, float], dict[int, float], dict[int, float]) =
            (pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90)
    """
    pv_normal = {}
    pv10 = {}
    pv90 = {}
    for minute in range(0, minutes_now + forecast_minutes + 1):
        idx = minute % MINUTES_PER_DAY
        kw = day_kw_profile[idx]
        pv_normal[minute] = kw / 60.0         # kW -> kWh per minute (predbat format)
        pv10[minute] = kw * pv10_fraction / 60.0
        pv90[minute] = kw * pv90_factor / 60.0
    return pv_normal, pv10, pv90


# ---------------------------------------------------------------------------
# Scenario application
# ---------------------------------------------------------------------------


def apply_scenario_to_predbat(my_predbat, scenario):
    """Apply a scenario's randomised fields to an already-initialised predbat instance.

    The template has already been loaded via read_debug_yaml; this function only
    overwrites the fields that are randomised. Rebuilds step data and rate
    thresholds so the plan is computed from the new data.

    Args:
        my_predbat: a PredBat instance with template already loaded
        scenario: scenario dict (from load_scenarios or generate_random_scenario)
    """
    params = scenario["params"]
    data = scenario["data"]

    # --- Battery scalars ---
    bat = params["battery"]
    my_predbat.soc_max = bat["soc_max_kwh"]
    my_predbat.soc_kw = bat["soc_max_kwh"] * bat["initial_soc_percent"] / 100.0
    my_predbat.battery_rate_max_charge = bat["rate_max_charge_kw"] / 60.0
    my_predbat.battery_rate_max_discharge = bat["rate_max_discharge_kw"] / 60.0

    # --- Inverter scalars ---
    inv = params["inverter"]
    my_predbat.inverter_hybrid = inv["hybrid"]
    my_predbat.inverter_limit = inv["inverter_limit_kw"] / 60.0
    my_predbat.export_limit = inv["export_limit_kw"] / 60.0

    # --- Cars ---
    # Scenarios written before cars existed carry no "cars" entry; those run car-less exactly as before.
    cars = params.get("cars", {"num_cars": 0, "battery_kwh": [], "slots": []})
    num_cars = cars["num_cars"]
    my_predbat.num_cars = num_cars
    # Every per-car attribute predbat indexes by car_n has to be sized to num_cars, not just the ones
    # this scenario varies - set_rate_thresholds takes max(car_charging_plan_max_price[:num_cars]) and
    # raises on an empty slice, and the prediction indexes the rest per car per minute.
    my_predbat.car_charging_slots = [list(slots) for slots in cars["slots"]] + [[] for _ in range(8 - num_cars)]
    my_predbat.car_charging_battery_size = list(cars["battery_kwh"]) or [100.0]
    my_predbat.car_charging_limit = [size for size in cars["battery_kwh"]] or [100.0]
    my_predbat.car_charging_soc = [0.0 for _ in range(num_cars)]
    my_predbat.car_charging_soc_next = [None for _ in range(num_cars)]
    my_predbat.car_charging_rate = [7.4 for _ in range(max(num_cars, 1))]
    my_predbat.car_charging_planned = [False for _ in range(num_cars)]
    my_predbat.car_charging_now = [False for _ in range(num_cars)]
    my_predbat.car_charging_plan_smart = [False for _ in range(num_cars)]
    my_predbat.car_charging_plan_max_price = [0.0 for _ in range(num_cars)]
    my_predbat.car_charging_plan_time = ["07:00:00" for _ in range(num_cars)]
    my_predbat.car_charging_manual_soc = [False for _ in range(num_cars)]
    my_predbat.car_charging_exclusive = [False for _ in range(num_cars)]

    # --- Fix forecast horizon so all scenarios are comparable ---
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.forecast_plan_hours = 24
    my_predbat.forecast_days = 1

    # --- Clock: each scenario has its own time of day ---
    # Scenario files written before this existed carry no "clock" entry; those keep the template's
    # own minutes_now so previously recorded benchmark results stay comparable. The value is absolute
    # (minutes from midnight), so applying scenarios in a loop cannot accumulate.
    clock = params.get("clock")
    if clock and "minutes_now" in clock:
        my_predbat.minutes_now = clock["minutes_now"]
        my_predbat.now_utc = my_predbat.midnight_utc + datetime.timedelta(minutes=my_predbat.minutes_now)

    # --- Expand time-series from stored daily profiles ---
    minutes_now = my_predbat.minutes_now
    forecast_minutes = my_predbat.forecast_minutes

    my_predbat.rate_import = expand_rates(data["rate_import_day"], minutes_now, end_minute=minutes_now + forecast_minutes)
    my_predbat.rate_export = expand_rates(data["rate_export_day"], minutes_now, end_minute=minutes_now + forecast_minutes)

    # --- Octopus Intelligent (IOG) dispatch windows ---
    # Scenario files written before this existed carry no "iog" entry; those run without any
    # IOG-adjusted windows, exactly as before - matches self.io_adjusted's own default of {}.
    iog = params.get("iog", {"enabled": False})
    if iog.get("enabled"):
        my_predbat.io_adjusted = expand_io_adjusted(iog.get("run_start_minute", 0), iog.get("run_slots", 0), minutes_now, end_minute=minutes_now + forecast_minutes)
    else:
        my_predbat.io_adjusted = {}

    my_predbat.load_minutes = expand_load_minutes(data["load_day_kwh"], minutes_now=minutes_now)
    my_predbat.load_minutes_age = 14       # 14 days of history are populated
    my_predbat.days_previous = [1]         # always read exactly 1 day back; avoids template config variance
    my_predbat.days_previous_weight = [1.0]
    my_predbat.load_forecast_only = False  # use historical load_minutes, not load_forecast
    my_predbat.load_forecast = {}          # clear any forecast load from template

    # Hard wire some optimisation options.
    my_predbat.best_soc_keep = 0
    my_predbat.best_soc_min = 0.0


    pv_normal, pv10, pv90 = expand_pv_forecast(data["pv_day_kw"], forecast_minutes, minutes_now=minutes_now)
    my_predbat.pv_forecast_minute = pv_normal
    my_predbat.pv_forecast_minute10 = pv10
    my_predbat.pv_forecast_minute90 = pv90

    # Follow the shipped default rather than pinning a literal, so the benchmark measures the planning
    # path users actually run. Read the CONFIG_ITEMS default rather than get_arg so this stays independent
    # of whatever the template debug dump happened to capture - the point of pinning it here at all is that
    # every scenario is planned the same way regardless of which template it was run against.
    my_predbat.calculate_second_pass = my_predbat.config_index["calculate_second_pass"]["default"]

    # --- Rebuild PV step dicts via step_data_history ---
    my_predbat.pv_forecast_minute_step = my_predbat.step_data_history(
        pv_normal,
        minutes_now,
        forward=True,
        cloud_factor=my_predbat.metric_cloud_coverage,
    )
    my_predbat.pv_forecast_minute10_step = my_predbat.step_data_history(
        pv10,
        minutes_now,
        forward=True,
        cloud_factor=min(my_predbat.metric_cloud_coverage + 0.2, 1.0) if my_predbat.metric_cloud_coverage else None,
        flip=True,
    )

    # --- Rescan the new rates before anything consumes their summaries ---
    # Replacing rate_import/rate_export above does not update the min/max/average summaries or
    # rate_min_forward - those only move when rate_scan runs. Without this the whole scenario is
    # planned against the TEMPLATE's rates: on scenario 10, rate_min_forward stayed at the template's
    # 18.081 while the scenario's cheapest forward import was 5.54, so battery_value_rate credited
    # leftover battery at 3.4x its replacement cost and every window charged to 100%. set_rate_thresholds
    # and rate_scan_window below both read these, so the rescan has to come first.
    # rate_scan only recomputes rate_min_forward when print is True, so it is called explicitly here
    # rather than turning the per-scenario logging on.
    if my_predbat.rate_import:
        my_predbat.rate_scan(my_predbat.rate_import, print=False)
        my_predbat.rate_min_forward = my_predbat.rate_min_forward_calc(my_predbat.rate_import)
    if my_predbat.rate_export:
        my_predbat.rate_scan_export(my_predbat.rate_export, print=False)

    # --- Rebuild rate thresholds and charge/export windows ---
    if my_predbat.rate_import or my_predbat.rate_export:
        my_predbat.set_rate_thresholds()

    if my_predbat.rate_export:
        my_predbat.high_export_rates, export_lowest, export_highest = my_predbat.rate_scan_window(my_predbat.rate_export, 5, my_predbat.rate_export_cost_threshold, True, alt_rates=my_predbat.rate_import)
        if my_predbat.rate_high_threshold == 0 and export_lowest <= my_predbat.rate_export_max:
            my_predbat.rate_export_cost_threshold = export_lowest

    if my_predbat.rate_import:
        my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False, alt_rates=my_predbat.rate_export)
        if my_predbat.rate_low_threshold == 0 and highest >= my_predbat.rate_min:
            my_predbat.rate_import_cost_threshold = highest

    # --- Load scaling for the three simulated futures ---
    # Absent in scenario files written before this existed, which keep the template's own values.
    scaling = params.get("load_scaling")
    if scaling:
        my_predbat.load_scaling = scaling["nominal"]
        my_predbat.load_scaling10 = scaling["pv10"]
        my_predbat.load_scaling90 = scaling["pv90"]

    # --- Feature flags ---
    # Scenario files written before this existed carry no "features" entry; those keep the template's
    # own settings so previously recorded benchmark results stay comparable, exactly as "clock" does.
    features = params.get("features")
    if features:
        my_predbat.set_export_low_power = features["set_export_low_power"]
        my_predbat.iboost_enable = features["iboost_enable"]
        # The whole block is written every scenario, not just the enabled ones. Setting these only
        # when iboost is on leaves the previous scenario's values behind, which makes a plan depend
        # on what ran before it - the same trap run_debug_cases documents. The off-case values are the
        # template's, so a scenario with iboost disabled plans exactly as it did before this existed.
        my_predbat.iboost_solar = features.get("iboost_solar", True)
        my_predbat.iboost_smart = features.get("iboost_smart", False)
        my_predbat.iboost_charging = features.get("iboost_charging", False)
        my_predbat.iboost_on_export = features.get("iboost_on_export", False)
        my_predbat.iboost_prevent_discharge = features.get("iboost_prevent_discharge", False)
        my_predbat.iboost_max_energy = features.get("iboost_max_energy", 3.0)
        my_predbat.iboost_max_power = features["iboost_max_power_kw"] / 60.0 if "iboost_max_power_kw" in features else 0.04
        my_predbat.iboost_today = features.get("iboost_today", 0.0)
        # Mirror the per-cycle reset in fetch_config_options. calculate_plan writes iboost_next back
        # onto the instance, and the scenario runner never calls fetch, so without this a scenario
        # inherits the previous one's iboost carry-over. No scenario's plan moves today - verified by
        # running the suite with and without it - but the hazard is real and the reset is free.
        my_predbat.iboost_next = my_predbat.iboost_today
        my_predbat.iboost_running = False
        my_predbat.iboost_running_solar = False
        my_predbat.iboost_running_full = False
        my_predbat.iboost_energy_today = {}
        my_predbat.iboost_plan = []
        # fetch_sensor_data builds the plan in the product, and the scenario runner never calls fetch,
        # so it is built here on the same condition. Without it iboost_plan stays empty and the
        # optimiser's iboost path - which only fires on a non-empty plan - is never reached.
        if my_predbat.iboost_enable and (((not my_predbat.iboost_solar) and (not my_predbat.iboost_charging)) or my_predbat.iboost_smart):
            my_predbat.iboost_plan = my_predbat.plan_iboost_smart()


# ---------------------------------------------------------------------------
# Single scenario runner
# ---------------------------------------------------------------------------


def run_scenario(my_predbat, scenario, debug=False):
    """Apply one scenario and run calculate_plan, returning a metrics dict.

    The template must already have been loaded via read_debug_yaml before
    calling this function. This function only modifies the randomised fields
    and then re-runs the optimisation.

    Args:
        my_predbat: PredBat instance with template already loaded
        scenario: scenario dict
        debug: if True, save the final plan HTML to plan_scenario{id}.html

    Returns:
        dict with id, seed, and all plan metrics; failed=True on exception
    """
    scenario_id = scenario["id"]
    seed = scenario["seed"]

    base_result = {
        "id": scenario_id,
        "seed": seed,
        "metric": None,
        "cost": None,
        "cost_pv10": None,
        "cost_pv90": None,
        "import_kwh_battery": None,
        "import_kwh_house": None,
        "export_kwh": None,
        "soc_min": None,
        "soc_final": None,
        "battery_cycles": None,
        "carbon_g": None,
        "runtime_s": None,
        "failed": False,
        "error": None,
    }

    try:
        apply_scenario_to_predbat(my_predbat, scenario)
        my_predbat.args["threads"] = 0
        my_predbat.plan_valid = False
        my_predbat.debug_enable = False  # disable debug output for normal scenario runs (huge overhead)
        t_start = time.perf_counter()
        my_predbat.calculate_plan(recompute=True)

        # Normal prediction (cost = raw import/export money)
        cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
            my_predbat.charge_limit_best,
            my_predbat.charge_window_best,
            my_predbat.export_window_best,
            my_predbat.export_limits_best,
            PV_SCENARIO_NOMINAL,
            end_record=my_predbat.end_record,
            save="best",
        )

        # PV10 prediction (pessimistic solar) needed for compute_metric
        cost10, _, _, _, _, soc10, _, _, _, final_iboost10, _ = my_predbat.run_prediction(
            my_predbat.charge_limit_best,
            my_predbat.charge_window_best,
            my_predbat.export_window_best,
            my_predbat.export_limits_best,
            PV_SCENARIO_PV10,
            end_record=my_predbat.end_record,
        )

        # PV90 prediction (optimistic solar, lighter load). Scoring the plan in each future is what makes a
        # hedging feature measurable at all: a plan that weights PV90 spends money the central forecast says
        # is wasted, so scoring only the nominal future marks it down no matter how well it does on the day
        # it was hedging for.
        cost90 = my_predbat.run_prediction(
            my_predbat.charge_limit_best,
            my_predbat.charge_window_best,
            my_predbat.export_window_best,
            my_predbat.export_limits_best,
            PV_SCENARIO_PV90,
            end_record=my_predbat.end_record,
        )[0]

        # Combined metric: cost + battery value adjustment + pv10 weighting + carbon + self-sufficiency + cycle cost
        metric, _ = my_predbat.compute_metric(
            my_predbat.end_record,
            soc,
            soc10,
            cost,
            cost10,
            final_iboost,
            final_iboost10,
            battery_cycle,
            metric_keep,
            final_carbon_g,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
        )

        t_elapsed = time.perf_counter() - t_start
        base_result.update(
            {
                "metric": round(float(metric), 4),
                "cost": round(float(cost), 4),
                "cost_pv10": round(float(cost10), 4),
                "cost_pv90": round(float(cost90), 4),
                "import_kwh_battery": round(float(import_kwh_battery), 4),
                "import_kwh_house": round(float(import_kwh_house), 4),
                "export_kwh": round(float(export_kwh), 4),
                "soc_min": round(float(soc_min), 4),
                "soc_final": round(float(soc), 4),
                "battery_cycles": round(float(battery_cycle), 4),
                "carbon_g": round(float(final_carbon_g), 2),
                "runtime_s": round(t_elapsed, 3),
                "end_record": int(my_predbat.end_record),
            }
        )

        if debug:
            html_plan, _raw_plan = my_predbat.publish_html_plan(
                my_predbat.pv_forecast_minute_step,
                my_predbat.pv_forecast_minute10_step,
                my_predbat.load_minutes_step,
                my_predbat.load_minutes_step10,
                my_predbat.end_record,
                publish=False,
            )
            plan_filename = "plan_scenario{}.html".format(scenario_id)
            open(plan_filename, "w").write(html_plan)
            print("  Wrote plan to {}".format(plan_filename))

    except Exception as e:
        base_result["failed"] = True
        base_result["error"] = str(e) + "\n" + traceback.format_exc()

    return base_result



# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


def profile_scenario(my_predbat, scenarios_file, template_yaml, scenario_id=0, top_n=30, sort_key="cumulative", prof_output=None, callers_of=None, line_profile_funcs=None):
    """Run cProfile on a single scenario's optimisation and print the top hotspots.

    Args:
        my_predbat: PredBat instance
        scenarios_file: path to scenarios YAML file
        template_yaml: path to template debug YAML
        scenario_id: which scenario id to profile (default 0)
        top_n: number of top functions to print (default 30)
        sort_key: pstats sort key, e.g. "cumulative", "tottime", "calls" (default "cumulative")
        prof_output: if set, also write a .prof binary file to this path
        callers_of: if set, print pstats caller breakdown for this function name (e.g. "round")
        line_profile_funcs: if set, list of fully qualified "module:function" strings to line-profile
                            instead of cProfile (requires line_profiler package)
    """
    from tests.test_infra import reset_inverter

    print("Loading template: {}".format(template_yaml))
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(template_yaml)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()

    scenarios = load_scenarios(scenarios_file)

    matching = [s for s in scenarios if s["id"] == scenario_id]
    if not matching:
        print("ERROR: No scenario with id={} found in {}".format(scenario_id, scenarios_file))
        return

    scenario = matching[0]
    print("Profiling scenario id={} seed={} ...".format(scenario["id"], scenario["seed"]))

    apply_scenario_to_predbat(my_predbat, scenario)
    my_predbat.args["threads"] = 0
    my_predbat.plan_valid = False
    # Disable debug output for profiling - the template may have debug_enable=true which forces
    # stat recording on every minute step of every prediction call (huge overhead).
    # Must set self.debug_enable directly since expose_config only updates item["value"] and
    # self.debug_enable is assigned by fetch_config_options() which is not called here.
    my_predbat.debug_enable = False

    # --- line_profiler mode ---
    if line_profile_funcs:
        try:
            import line_profiler  # type: ignore
        except ImportError:
            print("ERROR: line_profiler is not installed. Run: pip install line_profiler")
            return

        lp = line_profiler.LineProfiler()
        import sys as _sys
        import inspect as _inspect
        for spec in line_profile_funcs:
            if ":" in spec:
                mod_name, func_name = spec.rsplit(":", 1)
            else:
                mod_name, func_name = None, spec

            found = False
            for mod in list(_sys.modules.values()):
                if mod_name and not (getattr(mod, "__name__", "").endswith(mod_name) or (getattr(mod, "__file__", None) or "").endswith(mod_name + ".py")):
                    continue
                # Search module-level functions first
                func = getattr(mod, func_name, None)
                if callable(func):
                    lp.add_function(func)
                    print("  line-profiling {}.{}".format(getattr(mod, "__name__", "?"), func_name))
                    found = True
                    break
                # Search classes inside the module
                for _name, obj in _inspect.getmembers(mod, _inspect.isclass):
                    method = getattr(obj, func_name, None)
                    if method and callable(method):
                        lp.add_function(method)
                        print("  line-profiling {}.{}.{}".format(getattr(mod, "__name__", "?"), _name, func_name))
                        found = True
                        break
                if found:
                    break
            if not found:
                print("  WARNING: could not find function '{}' to line-profile".format(spec))

        lp_wrapper = lp(my_predbat.calculate_plan)
        lp_wrapper(recompute=True)
        lp.print_stats()
        return

    # --- cProfile mode ---
    pr = cProfile.Profile()
    pr.enable()
    my_predbat.calculate_plan(recompute=True)
    pr.disable()

    if prof_output:
        pr.dump_stats(prof_output)
        print("Profile data written to {}".format(prof_output))

    stream = io.StringIO()
    ps = pstats.Stats(pr, stream=stream)
    ps.strip_dirs()
    ps.sort_stats(sort_key)
    ps.print_stats(top_n)
    print(stream.getvalue())

    if callers_of:
        print("\n--- Callers of '{}' ---".format(callers_of))
        caller_stream = io.StringIO()
        ps2 = pstats.Stats(pr, stream=caller_stream)
        ps2.strip_dirs()
        ps2.sort_stats("cumulative")
        ps2.print_callers(callers_of)
        print(caller_stream.getvalue())


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def run_scenarios_from_file(my_predbat, scenarios_file, template_yaml, results_file, debug=False, scenario_id=None):
    """Run all scenarios from a scenarios YAML file and write results to JSON.

    Loads the template debug YAML ONCE, then applies each scenario's overrides
    in-memory without re-loading the template between scenarios.

    Args:
        my_predbat: an initialised PredBat instance
        scenarios_file: path to YAML file written by save_scenarios
        template_yaml: path to a debug YAML (e.g. predbat_debug_*.yaml)
        results_file: path where JSON results will be written
        debug: if True, save each scenario's final plan to plan_scenario{N}.html
        scenario_id: if set, run only the scenario with this id number
    """
    # Load template once
    print("Loading template: {}".format(template_yaml))
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(template_yaml)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()

    # Load scenarios
    scenarios = load_scenarios(scenarios_file)
    if scenario_id is not None:
        scenarios = [s for s in scenarios if s["id"] == scenario_id]
        if not scenarios:
            print("ERROR: No scenario with id={} found in {}".format(scenario_id, scenarios_file))
            return
    total = len(scenarios)
    print("Loaded {} scenario(s) from {}".format(total, scenarios_file))

    results = []
    for i, scenario in enumerate(scenarios):
        result = run_scenario(my_predbat, scenario, debug=debug)
        status = "FAILED" if result["failed"] else "ok"
        print("Scenario {}/{} seed={} metric={} cost={} runtime={}s [{}]".format(i + 1, total, result["seed"], result["metric"], result["cost"], result["runtime_s"], status))
        if result["failed"]:
            print("  ERROR: {}".format(result["error"]))
        results.append(result)

    # results_file is optional so an in-process caller - the plan regression test - can compare the
    # results directly instead of round-tripping them through a file it would then have to clean up
    if results_file:
        _save_results(results, results_file, scenarios_file, template_yaml)
    return results


def _save_results(results, results_file, scenarios_file, template_yaml):
    """Write results list to a JSON file with a run_info header.

    Args:
        results: list of result dicts from run_scenario
        results_file: output file path
        scenarios_file: path to the scenarios YAML (stored in run_info)
        template_yaml: path to the template YAML (stored in run_info)
    """
    output = {
        "run_info": {
            "template_yaml": template_yaml,
            "scenarios_file": scenarios_file,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "results": results,
    }
    with open(results_file, "w") as f:
        json.dump(output, f, indent=2)
        # Trailing newline so the checked-in baseline does not trip the end-of-file hook every time it is
        # regenerated - that is what produced the stray pre-commit.ci fixup commits on earlier branches.
        f.write("\n")
    passed = sum(1 for r in results if not r["failed"])
    failed = len(results) - passed
    print("Wrote {} result(s) to {} ({} passed, {} failed)".format(len(results), results_file, passed, failed))


# ---------------------------------------------------------------------------
# Results comparison
# ---------------------------------------------------------------------------


def compare_results(file_a, file_b):
    """Compare two random_results JSON files and print a table of metric/cost differences.

    Scenarios are matched by id.  Only scenarios present in both files are compared.
    A positive diff means file_b is *worse* (higher metric = worse plan); a negative diff means improvement.

    Args:
        file_a: path to the baseline results JSON (first run)
        file_b: path to the comparison results JSON (second run)
    """
    with open(file_a, "r") as f:
        data_a = json.load(f)
    with open(file_b, "r") as f:
        data_b = json.load(f)

    info_a = data_a.get("run_info", {})
    info_b = data_b.get("run_info", {})

    results_a = {r["id"]: r for r in data_a.get("results", [])}
    results_b = {r["id"]: r for r in data_b.get("results", [])}

    common_ids = sorted(set(results_a) & set(results_b))
    if not common_ids:
        print("No common scenario ids found between the two result files.")
        return

    print("Comparing results:")
    print("  A: {} ({})".format(file_a, info_a.get("timestamp", "?")))
    print("  B: {} ({})".format(file_b, info_b.get("timestamp", "?")))
    print("")

    # Column widths
    header = "{:>4}  {:>12}  {:>12}  {:>10}  {:>12}  {:>12}  {:>10}  {:>8}  {:>8}  {:>10}  {:>8}".format(
        "ID", "metric_A", "metric_B", "met_diff", "cost_A", "cost_B", "cost_diff", "time_A", "time_B", "time_diff", "status"
    )
    print(header)
    print("-" * len(header))

    metric_diffs = []
    cost_diffs = []
    runtime_diffs = []
    # Cost in each simulated future. Result files written before these were recorded simply contribute
    # nothing here, so old baselines stay comparable on the columns they do carry.
    future_diffs = {"cost_pv10": [], "cost": [], "cost_pv90": []}

    for sid in common_ids:
        ra = results_a[sid]
        rb = results_b[sid]

        status = ""
        if ra.get("failed") or rb.get("failed"):
            status = "FAIL"

        # Flag a differing end_record: the metric is measured over end_record, so if it changed
        # between the two runs the metric diff is comparing different horizons and is not reliable.
        era = ra.get("end_record")
        erb = rb.get("end_record")
        if era is not None and erb is not None and era != erb:
            status = (status + " " if status else "") + "ER {}->{}".format(era, erb)

        ma = ra.get("metric")
        mb = rb.get("metric")
        ca = ra.get("cost")
        cb = rb.get("cost")
        ta = ra.get("runtime_s")
        tb = rb.get("runtime_s")

        if ma is not None and mb is not None:
            met_diff = mb - ma
            metric_diffs.append(met_diff)
            met_diff_str = "{:+.4f}".format(met_diff)
            ma_str = "{:.4f}".format(ma)
            mb_str = "{:.4f}".format(mb)
        else:
            met_diff_str = "n/a"
            ma_str = "n/a"
            mb_str = "n/a"

        if ca is not None and cb is not None:
            cost_diff = cb - ca
            cost_diffs.append(cost_diff)
            cost_diff_str = "{:+.4f}".format(cost_diff)
            ca_str = "{:.4f}".format(ca)
            cb_str = "{:.4f}".format(cb)
        else:
            cost_diff_str = "n/a"
            ca_str = "n/a"
            cb_str = "n/a"

        ta_str = "n/a"
        tb_str = "n/a"
        time_diff_str = "n/a"
        if ta is not None:
            ta_str = "{:.3f}s".format(ta)
        if tb is not None:
            tb_str = "{:.3f}s".format(tb)
        if ta is not None and tb is not None:
            runtime_diffs.append(tb - ta)
            # Shown as a percentage as well as seconds: the absolute numbers only mean anything
            # against the machine that produced them, but the ratio survives the comparison
            time_diff_str = "{:+.1f}%".format(((tb - ta) / ta * 100) if ta else 0.0)

        for key, diffs in future_diffs.items():
            fa = ra.get(key)
            fb = rb.get(key)
            if fa is not None and fb is not None:
                diffs.append(fb - fa)

        print("{:>4}  {:>12}  {:>12}  {:>10}  {:>12}  {:>12}  {:>10}  {:>8}  {:>8}  {:>10}  {:>8}".format(
            sid, ma_str, mb_str, met_diff_str, ca_str, cb_str, cost_diff_str, ta_str, tb_str, time_diff_str, status
        ))

    print("-" * len(header))

    # Summary statistics
    if metric_diffs:
        avg_met = sum(metric_diffs) / len(metric_diffs)
        min_met = min(metric_diffs)
        max_met = max(metric_diffs)
        worse_count = sum(1 for d in metric_diffs if d > 0.01)
        better_count = sum(1 for d in metric_diffs if d < -0.01)
        unchanged_count = len(metric_diffs) - worse_count - better_count
        print("")
        print("Metric summary ({} scenarios compared):".format(len(metric_diffs)))
        print("  Average diff : {:+.4f}  (+ = B worse, - = B better)".format(avg_met))
        print("  Min diff     : {:+.4f}".format(min_met))
        print("  Max diff     : {:+.4f}".format(max_met))
        print("  B worse      : {}".format(worse_count))
        print("  B better     : {}".format(better_count))
        print("  Unchanged    : {}".format(unchanged_count))

    if cost_diffs:
        avg_cost = sum(cost_diffs) / len(cost_diffs)
        print("")
        print("Cost summary (raw import/export, no battery value adjustment):")
        print("  Average diff : {:+.4f}".format(avg_cost))
        print("  Min diff     : {:+.4f}".format(min(cost_diffs)))
        print("  Max diff     : {:+.4f}".format(max(cost_diffs)))

    # The same plan scored in each of the three simulated futures. A change that helps only when the sun
    # shows up - PV90 weighting is the obvious one - is invisible in the nominal column and shows here.
    labels = {"cost_pv10": "PV10 (cloudy, heavier load)", "cost": "nominal", "cost_pv90": "PV90 (sunny, lighter load)"}
    if any(future_diffs.values()):
        print("")
        print("Cost by simulated future (- = B cheaper in that future):")
        print("  {:<30} {:>10} {:>10} {:>10} {:>8} {:>8} {:>10}".format("future", "avg", "min", "max", "better", "worse", "unchanged"))
        for key in ("cost_pv10", "cost", "cost_pv90"):
            diffs = future_diffs[key]
            if not diffs:
                print("  {:<30} {:>10}".format(labels[key], "n/a"))
                continue
            better = sum(1 for d in diffs if d < -0.01)
            worse = sum(1 for d in diffs if d > 0.01)
            print("  {:<30} {:>+10.4f} {:>+10.4f} {:>+10.4f} {:>8} {:>8} {:>10}".format(
                labels[key], sum(diffs) / len(diffs), min(diffs), max(diffs), better, worse, len(diffs) - better - worse
            ))

    if runtime_diffs:
        avg_rt_a = sum(ra.get("runtime_s") or 0 for ra in results_a.values() if ra.get("runtime_s") is not None) / max(1, sum(1 for ra in results_a.values() if ra.get("runtime_s") is not None))
        avg_rt_b = sum(rb.get("runtime_s") or 0 for rb in results_b.values() if rb.get("runtime_s") is not None) / max(1, sum(1 for rb in results_b.values() if rb.get("runtime_s") is not None))
        avg_rt_diff = sum(runtime_diffs) / len(runtime_diffs)
        print("")
        print("Runtime summary (optimisation wall-clock time):")
        print("  Average A    : {:.3f}s".format(avg_rt_a))
        print("  Average B    : {:.3f}s".format(avg_rt_b))
        print("  Average diff : {:+.3f}s  (+ = B slower, - = B faster)".format(avg_rt_diff))
        print("  Min diff     : {:+.3f}s".format(min(runtime_diffs)))
        print("  Max diff     : {:+.3f}s".format(max(runtime_diffs)))
        total_a = sum(r.get("runtime_s") or 0 for r in results_a.values())
        total_b = sum(r.get("runtime_s") or 0 for r in results_b.values())
        if total_a:
            print("  Total        : {:.2f}s -> {:.2f}s  ({:+.1f}%)".format(total_a, total_b, (total_b - total_a) / total_a * 100))


RANDOM_TEMPLATE = "cases/predbat_debug_agile1.yaml"
RANDOM_SCENARIOS = "cases/random_scenarios.yaml"
RANDOM_BASELINE = "cases/random_results.json"
# Every recorded field except these has to match the baseline exactly. runtime_s is wall-clock and so
# says more about the machine than the code; timestamp is when the baseline was taken.
RANDOM_IGNORED_FIELDS = ("runtime_s",)


def run_random_scenario_tests(my_predbat):
    """Replay the 20 scenario benchmark and fail if any plan differs from the committed baseline.

    This is the gate the planning work is held to: every optimiser change is expected to leave all
    twenty plans bit-identical, so a change that moves one has either changed behaviour or introduced
    a bug, and either way wants a deliberate baseline regeneration rather than a quiet drift.

    Timing is reported but never asserted on. The suite runs on developer machines and CI runners of
    wildly different speeds, so a runtime threshold here would either be so loose it caught nothing or
    so tight it failed for reasons that have nothing to do with the change under test.

    my_predbat is deliberately unused - each run gets a fresh instance, for the reason run_debug_cases
    gives: read_debug_yaml only restores what its dump carries, so replaying a plan on the shared
    instance both inherits and leaves behind state that other tests depend on.
    """
    print("**** Running random scenario plan regression ****")

    if not os.path.exists(RANDOM_BASELINE):
        print("ERROR: no baseline at {} - regenerate it with ./run_random".format(RANDOM_BASELINE))
        return True

    with open(RANDOM_BASELINE, "r") as file_handle:
        baseline = {r["id"]: r for r in json.load(file_handle).get("results", [])}

    # Imported here rather than at module scope: unit_test imports this module, so a top level
    # import would be circular
    from unit_test import create_predbat

    scenario_predbat = create_predbat()
    results = run_scenarios_from_file(scenario_predbat, RANDOM_SCENARIOS, RANDOM_TEMPLATE, None)
    current = {r["id"]: r for r in results}

    missing = sorted(set(baseline) - set(current))
    extra = sorted(set(current) - set(baseline))
    if missing or extra:
        print("ERROR: scenario ids do not match the baseline - missing {} extra {}".format(missing, extra))
        return True

    failed = False
    compared_fields = 0
    total_base = 0.0
    total_now = 0.0
    for sid in sorted(baseline):
        want = baseline[sid]
        got = current[sid]
        total_base += want.get("runtime_s") or 0.0
        total_now += got.get("runtime_s") or 0.0
        for field in want:
            if field in RANDOM_IGNORED_FIELDS:
                continue
            compared_fields += 1
            if want[field] != got.get(field):
                print("ERROR: scenario {} {} changed: {} -> {}".format(sid, field, want[field], got.get(field)))
                failed = True

    # Informational only - see the docstring for why this is not an assertion
    if total_base:
        print("Runtime: baseline {:.2f}s, this run {:.2f}s ({:+.1f}%) - not asserted on, machine dependent".format(total_base, total_now, (total_now - total_base) / total_base * 100))

    if failed:
        print("ERROR: plans differ from {}. If the change is intended, regenerate with ./run_random".format(RANDOM_BASELINE))
    else:
        print("All {} scenarios match the baseline across {} compared fields".format(len(baseline), compared_fields))
    return failed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hhmm_to_minute(hhmm):
    """Convert "HH:MM" string to minute-of-day integer."""
    parts = str(hhmm).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _in_window(minute, start, end):
    """Return True if minute is inside [start, end) handling midnight wrap."""
    if start <= end:
        return start <= minute < end
    # Wraps midnight (e.g. 23:00 to 06:00)
    return minute >= start or minute < end


def _sample_rate_params(rng, rate_type, cheap):
    """Sample random rate parameters appropriate for the given rate type.

    Args:
        rng: random.Random instance
        rate_type: one of RATE_TYPES
        cheap: if True, generate import-style rates (higher day, lower night);
               if False, generate export-style rates (often single or lower)

    Returns:
        dict of params for generate_rates_day
    """
    if rate_type == "single":
        if cheap:
            return {"rate": round(rng.uniform(20.0, 35.0), 2)}
        else:
            return {"rate": round(rng.uniform(5.0, 20.0), 2)}

    elif rate_type == "dual":
        if cheap:
            night_rate = round(rng.uniform(5.0, 15.0), 2)
            day_rate = round(rng.uniform(20.0, 40.0), 2)
        else:
            night_rate = round(rng.uniform(3.0, 8.0), 2)
            day_rate = round(rng.uniform(8.0, 20.0), 2)
        night_start_hour = rng.choice([0, 23])
        night_end_hour = rng.randint(5, 8)
        return {
            "night_rate": night_rate,
            "day_rate": day_rate,
            "night_start_hhmm": "{}:30".format(night_start_hour),
            "night_end_hhmm": "{}:30".format(night_end_hour),
        }

    elif rate_type == "triple":
        if cheap:
            night_rate = round(rng.uniform(5.0, 12.0), 2)
            shoulder_rate = round(rng.uniform(18.0, 30.0), 2)
            peak_rate = round(rng.uniform(30.0, 50.0), 2)
        else:
            night_rate = round(rng.uniform(3.0, 8.0), 2)
            shoulder_rate = round(rng.uniform(8.0, 15.0), 2)
            peak_rate = round(rng.uniform(15.0, 25.0), 2)
        night_end_hour = rng.randint(5, 8)
        peak_start_hour = rng.randint(16, 18)
        peak_end_hour = rng.randint(19, 21)
        return {
            "night_rate": night_rate,
            "shoulder_rate": shoulder_rate,
            "peak_rate": peak_rate,
            "night_start_hhmm": "00:30",
            "night_end_hhmm": "{}:30".format(night_end_hour),
            "peak_start_hhmm": "{}:00".format(peak_start_hour),
            "peak_end_hhmm": "{}:00".format(peak_end_hour),
        }

    elif rate_type == "halfhourly":
        if cheap:
            base = round(rng.uniform(15.0, 25.0), 2)
            low = round(rng.uniform(3.0, 12.0), 2)
            high = round(rng.uniform(30.0, 50.0), 2)
        else:
            base = round(rng.uniform(8.0, 18.0), 2)
            low = round(rng.uniform(2.0, 8.0), 2)
            high = round(rng.uniform(18.0, 30.0), 2)
        return {"base_rate": base, "low_rate": low, "high_rate": high, "num_highs": rng.randint(1, 3), "num_lows": rng.randint(1, 3)}

    else:  # negative_halfhourly — troughs go negative to test plunge-pricing scenarios
        base = round(rng.uniform(5.0, 15.0), 2)
        low = round(rng.uniform(-15.0, 0.0), 2)
        high = round(rng.uniform(25.0, 50.0), 2)
        return {"base_rate": base, "low_rate": low, "high_rate": high, "num_highs": rng.randint(1, 3), "num_lows": rng.randint(1, 3)}
