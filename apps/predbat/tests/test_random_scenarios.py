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
import random
import datetime
import time
import cProfile
import pstats
import io
import traceback

import yaml

from tests.test_infra import reset_inverter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MINUTES_PER_DAY = 1440
RATE_HISTORY_DAYS = 3             # days of past + future rates to generate
RATE_FUTURE_DAYS = 2

PV10_FRACTION = 0.6               # pv10 = pv * this fraction
GAUSSIAN_PV_SIGMA_MINUTES = 180   # 3-hour std dev for PV bell curve
PV_SUNRISE_MINUTE_MIN = 240       # 04:00 earliest sunrise
PV_SUNRISE_MINUTE_MAX = 480       # 08:00 latest sunrise
PV_SUNSET_MINUTE_MIN = 960        # 16:00 earliest sunset
PV_SUNSET_MINUTE_MAX = 1320       # 22:00 latest sunset

LOAD_GAUSSIAN_SIGMA_MINUTES = 60  # 1-hour std dev for load peaks

BATTERY_SOC_OPTIONS_KWH = [2.4, 4.8, 7.0, 9.5, 13.5, 20.0]
PV_PEAK_OPTIONS_KW = [0.0, 1.0, 2.0, 4.0, 6.0, 10.0]

RATE_TYPES = ["single", "dual", "triple", "halfhourly"]

# ---------------------------------------------------------------------------
# Daily profile generators  (return list[float] with exactly 1440 elements)
# ---------------------------------------------------------------------------


def generate_rates_day(rate_type, params, seed):
    """Generate a single-day (1440-minute) per-minute rate profile in p/kWh.

    Args:
        rate_type: One of "single", "dual", "triple", "halfhourly"
        params: dict of rate parameters (see per-type docs below)
        seed: integer seed for halfhourly random generation

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

    elif rate_type == "halfhourly":
        # 48 half-hourly slots, randomly priced, each slot is 30 minutes
        rng = random.Random(seed)
        base = float(params.get("base_rate", 10.0))
        spread = float(params.get("spread", 20.0))
        slots = [max(0.0, base + rng.uniform(-spread / 2, spread / 2)) for _ in range(48)]
        for m in range(MINUTES_PER_DAY):
            slot = m // 30
            profile[m] = slots[slot]

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
    import_rate_type = rng.choice(RATE_TYPES)
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

    return {
        "id": scenario_id,
        "seed": seed,
        "params": {
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


def expand_pv_forecast(day_kw_profile, forecast_minutes, pv10_fraction=PV10_FRACTION):
    """Build predbat pv_forecast_minute and pv_forecast_minute10 from a daily profile.

    Args:
        day_kw_profile: list[float] of 1440 per-minute kW values
        forecast_minutes: how many minutes forward to generate (e.g. 2880 = 48h)
        pv10_fraction: fraction of normal forecast used for the P10 (pessimistic) forecast

    Returns:
        tuple(dict[int, float], dict[int, float]) = (pv_forecast_minute, pv_forecast_minute10)
    """
    pv_normal = {}
    pv10 = {}
    for minute in range(0, forecast_minutes + 1):
        idx = minute % MINUTES_PER_DAY
        kw = day_kw_profile[idx]
        pv_normal[minute] = kw / 60.0         # kW -> kWh per minute (predbat format)
        pv10[minute] = kw * pv10_fraction / 60.0
    return pv_normal, pv10


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

    # --- Fix forecast horizon so all scenarios are comparable ---
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.forecast_plan_hours = 24
    my_predbat.forecast_days = 1

    # --- Expand time-series from stored daily profiles ---
    minutes_now = my_predbat.minutes_now
    forecast_minutes = my_predbat.forecast_minutes

    my_predbat.rate_import = expand_rates(data["rate_import_day"], minutes_now, end_minute=minutes_now + forecast_minutes)
    my_predbat.rate_export = expand_rates(data["rate_export_day"], minutes_now, end_minute=minutes_now + forecast_minutes)
    my_predbat.load_minutes = expand_load_minutes(data["load_day_kwh"], minutes_now=minutes_now)
    my_predbat.load_minutes_age = 14       # 14 days of history are populated
    my_predbat.days_previous = [1]         # always read exactly 1 day back; avoids template config variance
    my_predbat.days_previous_weight = [1.0]
    my_predbat.load_forecast_only = False  # use historical load_minutes, not load_forecast
    my_predbat.load_forecast = {}          # clear any forecast load from template

    # Hard wire some optimisation options.
    my_predbat.best_soc_keep = 0
    my_predbat.best_soc_min = 0.0


    pv_normal, pv10 = expand_pv_forecast(data["pv_day_kw"], forecast_minutes)
    my_predbat.pv_forecast_minute = pv_normal
    my_predbat.pv_forecast_minute10 = pv10
    my_predbat.calculate_second_pass = False

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
            False,
            end_record=my_predbat.end_record,
            save="best",
        )

        # PV10 prediction (pessimistic solar) needed for compute_metric
        cost10, _, _, _, _, soc10, _, _, _, final_iboost10, _ = my_predbat.run_prediction(
            my_predbat.charge_limit_best,
            my_predbat.charge_window_best,
            my_predbat.export_window_best,
            my_predbat.export_limits_best,
            True,
            end_record=my_predbat.end_record,
        )

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
                "import_kwh_battery": round(float(import_kwh_battery), 4),
                "import_kwh_house": round(float(import_kwh_house), 4),
                "export_kwh": round(float(export_kwh), 4),
                "soc_min": round(float(soc_min), 4),
                "soc_final": round(float(soc), 4),
                "battery_cycles": round(float(battery_cycle), 4),
                "carbon_g": round(float(final_carbon_g), 2),
                "runtime_s": round(t_elapsed, 3),
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

    _save_results(results, results_file, scenarios_file, template_yaml)


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
    header = "{:>4}  {:>12}  {:>12}  {:>10}  {:>12}  {:>12}  {:>10}  {:>8}  {:>8}  {:>8}".format(
        "ID", "metric_A", "metric_B", "met_diff", "cost_A", "cost_B", "cost_diff", "time_A", "time_B", "status"
    )
    print(header)
    print("-" * len(header))

    metric_diffs = []
    cost_diffs = []
    runtime_diffs = []

    for sid in common_ids:
        ra = results_a[sid]
        rb = results_b[sid]

        status = ""
        if ra.get("failed") or rb.get("failed"):
            status = "FAIL"

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
        if ta is not None:
            ta_str = "{:.3f}s".format(ta)
        if tb is not None:
            tb_str = "{:.3f}s".format(tb)
        if ta is not None and tb is not None:
            runtime_diffs.append(tb - ta)

        print("{:>4}  {:>12}  {:>12}  {:>10}  {:>12}  {:>12}  {:>10}  {:>8}  {:>8}  {:>8}".format(
            sid, ma_str, mb_str, met_diff_str, ca_str, cb_str, cost_diff_str, ta_str, tb_str, status
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

    else:  # halfhourly
        base = round(rng.uniform(10.0, 25.0), 2)
        spread = round(rng.uniform(10.0, 30.0), 2)
        return {"base_rate": base, "spread": spread}
