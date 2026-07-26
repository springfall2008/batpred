# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Annual prediction engine.

Projects a year of household electricity costs using the real Predbat planning
engine, reporting each month under three scenarios: no PV or battery, PV and
battery without Predbat, and with Predbat. Performs no HTTP itself; the weather
and tariff modules own all network access.
"""

import calendar
import os
from datetime import date, datetime, timedelta

import pytz

from annual_load import build_load_forecast, OctopusConsumptionLoadProfile, SyntheticLoadProfile
from annual_tariff import AnnualTariff
from annual_weather import AnnualWeather, resolve_postcode
from const import MINUTE_WATT, PREDICT_STEP
from prediction import Prediction

VALID_SHAPES = ["night", "day", "flat"]

DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_SAMPLES_PER_MONTH = 2
DEFAULT_PV10_DERATE_FALLBACK = 0.7
DEFAULT_DECLINATION = 35
DEFAULT_AZIMUTH = 180
DEFAULT_EFFICIENCY = 0.95
DEFAULT_HYBRID = True

# The Open-Meteo ERA5 archive, which the weather module draws on, starts in 1940.
MINIMUM_YEAR = 1940

# Substrings that mark a config value as secret and therefore scrubbable
SECRET_MARKERS = ["_key", "password", "token", "secret"]


class AnnualConfigError(ValueError):
    """Raised when the annual prediction config is invalid or self-contradictory."""


def scrub_secrets(config):
    """Return a deep copy of the config with secret-looking values replaced by "xxx".

    Mirrors the redaction ``create_debug_yaml()`` applies, so a results document or
    debug dump can never carry an API key.
    """
    if isinstance(config, dict):
        scrubbed = {}
        for key, value in config.items():
            if any(marker in str(key).lower() for marker in SECRET_MARKERS):
                scrubbed[key] = "xxx"
            else:
                scrubbed[key] = scrub_secrets(value)
        return scrubbed
    if isinstance(config, list):
        return [scrub_secrets(item) for item in config]
    return config


def _require_number(value, field, minimum=None, maximum=None, integer=False, exclusive_minimum=False):
    """Coerce a config value to a number, raising AnnualConfigError with an actionable message.

    Rejects booleans explicitly (``True`` silently becoming ``1.0`` would be a confusing
    outcome), converts with ``int()``/``float()`` inside a try/except so a malformed value
    never escapes as a bare ``ValueError``/``TypeError``, and enforces the optional bounds.
    ``minimum`` is inclusive unless ``exclusive_minimum`` is set, in which case the value
    must be strictly greater than it. ``maximum`` is always inclusive.
    """
    if isinstance(value, bool):
        raise AnnualConfigError("{} must be a number, not a boolean (got {})".format(field, value))
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise AnnualConfigError("{} must be a number, got {!r}".format(field, value))
    if minimum is not None:
        if exclusive_minimum and number <= minimum:
            raise AnnualConfigError("{} must be greater than {}, got {}".format(field, minimum, number))
        if not exclusive_minimum and number < minimum:
            raise AnnualConfigError("{} must be at least {}, got {}".format(field, minimum, number))
    if maximum is not None and number > maximum:
        raise AnnualConfigError("{} must be at most {}, got {}".format(field, maximum, number))
    return number


def _validate_solar(raw):
    """Normalise the solar array list, applying defaults and rejecting arrays without kwp."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise AnnualConfigError("annual.solar must be a list of arrays")

    arrays = []
    for index, array in enumerate(raw):
        if not isinstance(array, dict):
            raise AnnualConfigError("annual.solar[{}] must be a mapping".format(index))
        if "kwp" not in array:
            raise AnnualConfigError("annual.solar[{}] is missing kwp, the array's peak power in kW".format(index))
        normalised = dict(array)
        normalised["kwp"] = _require_number(array["kwp"], "annual.solar[{}].kwp".format(index), minimum=0, exclusive_minimum=True)
        normalised["declination"] = array.get("declination", DEFAULT_DECLINATION)
        normalised["azimuth"] = array.get("azimuth", DEFAULT_AZIMUTH)
        normalised["efficiency"] = _require_number(array.get("efficiency", DEFAULT_EFFICIENCY), "annual.solar[{}].efficiency".format(index), minimum=0, exclusive_minimum=True, maximum=1)
        arrays.append(normalised)
    return arrays


def _validate_battery(raw):
    """Normalise the battery block, or return None for a run with no battery."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AnnualConfigError("annual.battery must be a mapping")
    if "size_kwh" not in raw:
        raise AnnualConfigError("annual.battery is missing size_kwh")
    if "inverter_kw" not in raw:
        raise AnnualConfigError("annual.battery is missing inverter_kw")

    inverter_kw = _require_number(raw["inverter_kw"], "annual.battery.inverter_kw", minimum=0, exclusive_minimum=True)
    return {
        "size_kwh": _require_number(raw["size_kwh"], "annual.battery.size_kwh", minimum=0, exclusive_minimum=True),
        "inverter_kw": inverter_kw,
        "export_limit_kw": _require_number(raw.get("export_limit_kw", inverter_kw), "annual.battery.export_limit_kw", minimum=0),
        "hybrid": bool(raw.get("hybrid", DEFAULT_HYBRID)),
        "charge_rate_kw": _require_number(raw.get("charge_rate_kw", inverter_kw), "annual.battery.charge_rate_kw", minimum=0, exclusive_minimum=True),
        "discharge_rate_kw": _require_number(raw.get("discharge_rate_kw", inverter_kw), "annual.battery.discharge_rate_kw", minimum=0, exclusive_minimum=True),
    }


def _validate_load(raw):
    """Normalise the load block and enforce the Octopus / manual exclusivity rule."""
    if not isinstance(raw, dict):
        raise AnnualConfigError("annual.load is required and must be a mapping")

    has_octopus = "octopus" in raw
    octopus = raw.get("octopus")
    has_manual = ("annual_kwh" in raw) or ("car_charging_kwh" in raw)

    if has_octopus and has_manual:
        raise AnnualConfigError("annual.load.octopus and annual.load.annual_kwh/car_charging_kwh are mutually exclusive: the Octopus consumption series already includes any car charging, so supplying both would double-count it")

    if not has_octopus and "annual_kwh" not in raw:
        raise AnnualConfigError("annual.load requires either annual_kwh or an octopus block")

    if has_octopus:
        if not isinstance(octopus, dict) or not octopus.get("api_key") or not octopus.get("account_id"):
            raise AnnualConfigError("annual.load.octopus requires both api_key and account_id")
        return {"octopus": dict(octopus), "shape": raw.get("shape", "flat"), "car_charging_kwh": 0.0}

    shape = raw.get("shape", "flat")
    if shape not in VALID_SHAPES:
        raise AnnualConfigError("annual.load.shape must be one of {}, got '{}'".format(VALID_SHAPES, shape))

    return {
        "annual_kwh": _require_number(raw["annual_kwh"], "annual.load.annual_kwh", minimum=0),
        "shape": shape,
        "car_charging_kwh": _require_number(raw.get("car_charging_kwh", 0.0), "annual.load.car_charging_kwh", minimum=0),
    }


def _validate_tariff(raw):
    """Normalise the tariff block, requiring at least one import rate source.

    A URL containing {dno_region} with no dno_region supplied is rejected here
    rather than left to 404 at fetch time, where it would surface as an
    unavailable month and read like an Octopus outage.
    """
    if not isinstance(raw, dict):
        raise AnnualConfigError("annual.tariff is required and must be a mapping")
    if not raw.get("import_octopus_url") and not raw.get("rates_import"):
        raise AnnualConfigError("annual.tariff requires either import_octopus_url or rates_import")

    templated = [name for name in ["import_octopus_url", "export_octopus_url"] if raw.get(name) and "{dno_region}" in raw[name]]
    if templated and not raw.get("dno_region"):
        raise AnnualConfigError("annual.tariff.{} uses {{dno_region}} but annual.tariff.dno_region is not set; supply your Octopus region letter, for example 'A' for Eastern England".format(", ".join(templated)))

    tariff = dict(raw)
    tariff["standing_charge_p_per_day"] = _require_number(raw.get("standing_charge_p_per_day", 0.0), "annual.tariff.standing_charge_p_per_day", minimum=0)
    return tariff


def validate_config(config, today=None):
    """Validate and normalise an annual prediction config, returning a fully defaulted copy.

    Accepts either the wrapped form ({"annual": {...}}) or the inner mapping directly.
    Raises AnnualConfigError with an actionable message on any problem.
    """
    if not isinstance(config, dict):
        raise AnnualConfigError("The annual config must be a mapping")

    raw = config.get("annual", config)
    if not isinstance(raw, dict):
        raise AnnualConfigError("The annual config must be a mapping")

    location = raw.get("location")
    if not isinstance(location, dict):
        raise AnnualConfigError("annual.location is required, with either a postcode or latitude and longitude")
    if not location.get("postcode") and not ("latitude" in location and "longitude" in location):
        raise AnnualConfigError("annual.location needs either a postcode or both latitude and longitude")

    solar = _validate_solar(raw.get("solar"))
    battery = _validate_battery(raw.get("battery"))
    if not solar and battery is None:
        raise AnnualConfigError("annual needs at least one of solar or battery: with neither there is nothing to evaluate")

    samples_per_month = _require_number(raw.get("samples_per_month", DEFAULT_SAMPLES_PER_MONTH), "annual.samples_per_month", minimum=1, integer=True)

    if today is None:
        today = date.today()
    year = _require_number(raw.get("year", today.year - 1), "annual.year", minimum=MINIMUM_YEAR, maximum=today.year, integer=True)

    return {
        "location": dict(location),
        "year": year,
        "solar": solar,
        "battery": battery,
        "load": _validate_load(raw.get("load")),
        "tariff": _validate_tariff(raw.get("tariff")),
        "samples_per_month": samples_per_month,
        "timezone": raw.get("timezone", DEFAULT_TIMEZONE),
        "pv10_derate_fallback": _require_number(raw.get("pv10_derate_fallback", DEFAULT_PV10_DERATE_FALLBACK), "annual.pv10_derate_fallback", minimum=0, exclusive_minimum=True, maximum=1),
        "raw": scrub_secrets(raw),
    }


# Minimal apps.yaml for a headless run. PredBat's Hass base class reads this at
# construction time; nothing here talks to Home Assistant. timezone is quoted since it
# is interpolated raw into YAML and a value containing ':' or '#' would otherwise be
# invalid or reparsed as something other than a plain string.
MINIMAL_APPS_YAML = """pred_bat:
  module: predbat
  class: PredBat
  prefix: predbat
  timezone: "{timezone}"
  currency_symbols:
  - '£'
  - 'p'
  threads: 0
  db_enable: false
  db_mirror_ha: false
  db_primary: false
  web_enable: false
  mcp_enable: false
  notify_devices: []
  days_previous:
  - 1
  days_previous_weight:
  - 1
  forecast_hours: 48
"""


class AnnualNullHA:
    """A no-op Home Assistant interface for headless annual runs.

    Provides the subset of the interface PredBat touches during ``auto_config()``,
    ``load_user_config()`` and ``fetch_config_options()``. Nothing is published and
    no history exists, which is correct: every input the annual tool needs is
    injected directly.
    """

    def __init__(self):
        """Create an empty in-memory state store."""
        self.history_enable = False
        self.dummy_items = {}
        self.db_primary = False

    def get_state(self, entity_id, default=None, attribute=None, refresh=False, raw=False):
        """Return a stored state, the supplied default, or all states when no entity is given."""
        if not entity_id:
            return {}
        if entity_id in self.dummy_items:
            result = self.dummy_items[entity_id]
            if raw:
                return result
            if isinstance(result, dict):
                return result.get(attribute, "") if attribute else result.get("state", default)
            return default if attribute else result
        return default

    def set_state(self, entity_id, state, attributes=None):
        """Store a state locally so subsequent reads round trip."""
        self.dummy_items[entity_id] = state
        return state

    def get_history(self, entity_id, now=None, days=30):
        """Return None: a headless annual run has no Home Assistant history."""
        return None

    def call_service(self, service, **kwargs):
        """Accept and discard a service call."""
        return None


def write_minimal_apps_yaml(work_dir, timezone):
    """Write the headless apps.yaml into ``work_dir`` and return its path."""
    os.makedirs(work_dir, exist_ok=True)
    path = os.path.join(work_dir, "apps.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(MINIMAL_APPS_YAML.format(timezone=timezone))
    return path


def create_headless_predbat(work_dir, timezone, log):
    """Construct a PredBat instance with no Home Assistant connection.

    ``Hass.__init__`` is the only place that reads ``$PREDBAT_APPS_FILE``, and it does so
    synchronously while ``PredBat()`` is constructed below, so the environment variable only
    needs to be set for the duration of that one call. It is restored to whatever it held
    before (unset if it was unset) in a ``finally`` block, so a later ``PredBat()`` in the
    same process — including ``unit_test.py``'s own ``create_predbat()`` — never silently
    picks up this work directory's apps.yaml. The predbat import is deliberately local to
    this function so merely importing ``annual`` does not drag in the whole engine.
    """
    path = write_minimal_apps_yaml(work_dir, timezone)
    previous_apps_file = os.environ.get("PREDBAT_APPS_FILE")
    os.environ["PREDBAT_APPS_FILE"] = path
    try:
        import predbat

        instance = predbat.PredBat()
    finally:
        if previous_apps_file is None:
            os.environ.pop("PREDBAT_APPS_FILE", None)
        else:
            os.environ["PREDBAT_APPS_FILE"] = previous_apps_file

    instance.states = {}
    instance.log = log
    instance.reset()
    instance.update_time()
    instance.ha_interface = AnnualNullHA()
    instance.auto_config()
    instance.load_user_config()
    instance.fetch_config_options()
    configure_offline_mode(instance)
    instance.config_root = work_dir
    instance.save_restore_dir = work_dir
    instance.args["threads"] = 0
    return instance


def apply_hardware(predbat, battery, solar):
    """Map the config's battery block onto the PredBat instance.

    Rates are stored internally as kW per minute, matching
    ``Compare.apply_hardware_overrides()``. With no battery block the system is
    given zero capacity, which is how the no-battery scenario is expressed. This
    function is the sole owner of ``battery_rate_max_export``: unlike the other
    accumulators, it is hardware-derived rather than per-sample state, so
    ``reset_sample_state()`` must never overwrite it. ``soc_kw``, the prediction's
    starting SOC, is set deterministically here rather than clamped against
    whatever it happened to hold before, since clamping only makes sense when a
    caller has deliberately set a starting SOC first — a later task does that
    explicitly, after calling this function.
    """
    if battery is None:
        predbat.soc_max = 0.0
        predbat.soc_kw = 0.0
        predbat.battery_rate_max_charge = 0.0
        # Synthetic configs have no separate DC figure to scale proportionally (unlike
        # compare.py's apply_hardware_overrides(), which scales an inherited DC/AC ratio),
        # so the DC rate is simply set equal to the AC rate.
        predbat.battery_rate_max_charge_dc = 0.0
        predbat.battery_rate_max_discharge = 0.0
        predbat.battery_rate_max_export = 0.0
        # A zero-capacity battery has no meaningful minimum reserve either.
        predbat.battery_rate_min = 0.0
        # Sum kWp across every array, not just the first: a PV-only run's export limit
        # must match what the battery run would see from the same solar array(s), or the
        # difference between scenarios - this tool's whole output - is computed against
        # two different caps.
        predbat.inverter_limit = (sum(array["kwp"] for array in solar) if solar else 5.0) * 1000 / MINUTE_WATT
        predbat.export_limit = predbat.inverter_limit
        predbat.inverter_hybrid = False
        return

    predbat.soc_max = battery["size_kwh"]
    predbat.soc_kw = predbat.soc_max
    predbat.inverter_limit = battery["inverter_kw"] * 1000 / MINUTE_WATT
    predbat.export_limit = battery["export_limit_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_charge = battery["charge_rate_kw"] * 1000 / MINUTE_WATT
    # Synthetic configs have no separate DC figure to scale proportionally (unlike
    # compare.py's apply_hardware_overrides(), which scales an inherited DC/AC ratio), so
    # the DC rate is simply set equal to the AC rate.
    predbat.battery_rate_max_charge_dc = predbat.battery_rate_max_charge
    predbat.battery_rate_max_discharge = battery["discharge_rate_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_export = predbat.battery_rate_max_discharge
    predbat.inverter_hybrid = battery["hybrid"]


def reset_sample_state(predbat):
    """Reset every field a previous sample could have left behind.

    Without this, a month's result silently depends on what ran before it: the
    numbers stay plausible while becoming order-dependent. The list covers the
    accumulators, the previous plan, the manual overrides, the starting SOC, the
    full rate-derived family that ``calculate_plan`` seeds its best windows from,
    and the field ``tests/test_single_debug.py`` documents as leaking between
    debug cases (``dynamic_load_baseline``).

    Deliberately excluded: ``battery_rate_max_export`` is hardware-derived and
    owned solely by ``apply_hardware()``, not reset here; the offline-mode
    choices (Octopus intelligent charging disabled, load taken from
    load_forecast, iBoost/carbon disabled, debug off) are one-shot configuration
    handled by ``configure_offline_mode()``, not per-sample leaks.
    """
    predbat.dynamic_load_baseline = {}

    predbat.soc_kw = 0.0

    predbat.cost_today_sofar = 0
    predbat.carbon_today_sofar = 0
    predbat.iboost_today = 0
    predbat.import_today_now = 0
    predbat.export_today_now = 0
    predbat.load_minutes_now = 0
    predbat.pv_today_now = 0

    predbat.manual_charge_times = []
    predbat.manual_export_times = []
    predbat.manual_freeze_charge_times = []
    predbat.manual_freeze_export_times = []
    predbat.manual_demand_times = []
    predbat.manual_all_times = []

    predbat.charge_limit_best = []
    predbat.charge_window_best = []
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.charge_limit = []
    predbat.charge_window = []
    predbat.export_window = []
    predbat.export_limits = []
    predbat.plan_valid = False

    # The rate-derived family calculate_plan() reads to seed the best charge/export
    # windows (plan.py). Clearing rate_import_replicated/rate_export_replicated but
    # leaving these downstream products in place would look handled while still leaking.
    predbat.low_rates = []
    predbat.high_export_rates = []
    predbat.rate_import = {}
    predbat.rate_export = {}
    predbat.rate_import_replicated = {}
    predbat.rate_export_replicated = {}
    predbat.rate_import_cost_threshold = 99
    predbat.rate_export_cost_threshold = 99
    predbat.rate_min = 0
    predbat.rate_max = 0
    predbat.rate_average = 0

    predbat.load_inday_adjustment = 1.0
    predbat.load_scaling_dynamic = None
    predbat.manual_load_adjust = {}
    predbat.savings_last_updated = None


def configure_offline_mode(predbat):
    """Apply the one-shot configuration choices that make sense only for an offline run.

    These are deliberate choices, not per-sample leaks: they are set once, after
    ``fetch_config_options()`` has populated its own defaults, and are not part of
    ``reset_sample_state()`` because re-applying them every sample would silently
    override whatever ``fetch_config_options()`` or a scenario override set.

    ``fetch_config_options()`` derives ``calculate_best_charge``, ``calculate_best_export``,
    ``set_charge_window`` and ``set_export_window`` from ``predbat_mode`` (``fetch.py``), and
    the minimal headless ``apps.yaml`` has no ``mode`` key, so ``get_arg("mode")`` returns the
    "Monitor" default — which sets all four False. With all four False, ``calculate_plan()``'s
    charge- and export-window branches (``plan.py``, gated on
    ``self.low_rates and self.calculate_best_charge and self.set_charge_window`` and the export
    equivalent) never fire, ``charge_window_best``/``export_window_best`` fall back to the
    (empty) live ``charge_window``/``export_window``, and the "with Predbat" scenario would plan
    no charging or exporting at all — a silent demand-only system indistinguishable from
    scenario 1. These four are therefore forced on here, matching what
    ``PREDBAT_MODE_CONTROL_CHARGEDISCHARGE`` sets in a live install with full control enabled.
    """
    predbat.octopus_intelligent_charging = False
    predbat.load_forecast_only = True
    predbat.load_scaling = 1.0
    predbat.load_scaling10 = 1.0
    predbat.iboost_enable = False
    predbat.carbon_enable = False
    predbat.plan_debug = False
    predbat.debug_enable = False
    predbat.calculate_best_charge = True
    predbat.calculate_best_export = True
    predbat.set_charge_window = True
    predbat.set_export_window = True


def _percentile_indices(count, samples):
    """Return ``samples`` distinct indices spread evenly through ``count`` sorted items.

    Index i sits at percentile (i + 0.5) / samples, so two samples land at the 25th
    and 75th percentiles and each represents an equal share of the month. Collisions
    are resolved by scanning forward toward the end of the array and, failing that,
    backward from the target, which only matters when the sample count approaches
    the number of candidate days. With no candidates at all there is nothing to
    index, so this returns an empty list rather than the meaningless index -1.
    """
    if count <= 0:
        return []

    chosen = []
    used = set()
    for index in range(samples):
        target = min(count - 1, int(count * ((index + 0.5) / samples)))
        while target in used and target < count - 1:
            target += 1
        while target in used and target > 0:
            target -= 1
        if target in used:
            continue
        used.add(target)
        chosen.append(target)
    return chosen


def select_samples(weather, year, month, samples_per_month, has_solar=True):
    """Choose the days to plan for one month, with the weight in days each represents.

    Days are ranked by their *actual* PV energy and sampled at even percentiles, so an
    unlucky sunny or dull draw cannot swing the month. Ranking uses actuals rather than
    the forecast: the aim is to represent what the month really contained, not what was
    predicted. Days without a following day are excluded because the 48 hour plan needs one.

    Weights sum to the number of days in the month, up to ordinary floating-point
    rounding, so a month with fewer usable candidates than requested is scaled up
    rather than silently under-counted.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    all_days = [date(year, month, day) for day in range(1, days_in_month + 1)]

    if has_solar:
        candidates = [day for day in all_days if weather.has_actual(day) and weather.has_actual(day + timedelta(days=1))]
        candidates.sort(key=lambda day: (weather.daily_actual_kwh(day), day))
    else:
        # With no PV there is nothing to rank by, so fall back to evenly spaced calendar days.
        # Deliberately no following-day filter here: that guard exists solely to keep the
        # sample within the weather series, and a battery-only run has no weather series to
        # run past the end of. Day 2's rates come from the tariff module (which fetches the
        # month plus a 2-day buffer, and the orchestrator additionally fetches the next month)
        # and its load is synthetic and unbounded, so the last day of the month is a
        # legitimate sample and must not be filtered out.
        candidates = all_days

    if not candidates:
        return []

    # _percentile_indices() already guarantees distinct indices, so no set() is needed here.
    indices = _percentile_indices(len(candidates), samples_per_month)
    chosen = sorted(candidates[index] for index in indices)
    weight = days_in_month / float(len(chosen))
    return [(day, weight) for day in chosen]


DAY_MINUTES = 24 * 60
PLAN_MINUTES = 48 * 60

# Every sample starts from an empty battery. The compute_metric correction values
# whatever charge is left at the end, so the starting level does not bias the cost.
START_SOC_KWH = 0.0

# Cars are charged at this rate when the config gives no explicit figure
DEFAULT_CAR_RATE_KW = 7.4

# Maximum charge slots the dumb-battery baseline is allowed, matching the
# calculate_savings_max_charge_slots convention in calculate_yesterday()
BASELINE_MAX_CHARGE_SLOTS = 1

# The smart car in scenario 3 must be ready by this time, matching the
# "car_charging_plan_time" default in config.py. Only the first day of the 48
# hour plan is billed (see run_day()'s end_record), so a single morning ready
# time is enough to give Predbat a fair one-shot charging decision to make.
DEFAULT_CAR_READY_TIME = "07:00:00"


def build_step_data(predbat, pv_minute, pv_minute10):
    """Build the 5-minute step arrays the Prediction engine consumes.

    Mirrors the calls ``calculate_plan()`` makes in ``plan.py``. Because
    ``load_forecast_only`` is set, the historical branch of ``step_data_history``
    contributes nothing and the whole load profile comes from ``load_forecast``.
    """
    load_step = predbat.step_data_history(
        predbat.load_minutes,
        predbat.minutes_now,
        forward=False,
        scale_today=1.0,
        scale_fixed=1.0,
        type_load=True,
        load_forecast=predbat.load_forecast,
        load_scaling_dynamic=None,
        cloud_factor=None,
        load_adjust={},
        load_baseline={},
    )
    pv_step = predbat.step_data_history(pv_minute, predbat.minutes_now, forward=True, cloud_factor=None)
    pv10_step = predbat.step_data_history(pv_minute10, predbat.minutes_now, forward=True, cloud_factor=None, flip=True)
    return load_step, pv_step, pv10_step


def timer_charge_window(rate_import, car_kwh, car_rate_kw):
    """Return the fixed off-peak timer windows a non-Predbat household would use.

    Finds the cheapest contiguous band of each day and starts the charge there,
    extending past the band if the car needs longer than the cheap rate lasts.
    Returns one window per day of the 48 hour plan so the second day matches the first.
    """
    if car_kwh <= 0 or car_rate_kw <= 0:
        return []

    minutes_needed = int(round((car_kwh / car_rate_kw) * 60.0))
    if minutes_needed <= 0:
        return []

    # fetch.py's step_data_history() samples load_forecast once every PREDICT_STEP (5) minutes,
    # so a window whose length or start is not on that grid is billed at a quantised length
    # rather than its true one (e.g. an 81 minute need would be billed as 85 minutes, +5%).
    # Round the duration UP so the car is never under-delivered, and align the start DOWN to
    # the grid.
    minutes_needed = -(-minutes_needed // PREDICT_STEP) * PREDICT_STEP

    windows = []
    for day_offset in range(2):
        base = day_offset * DAY_MINUTES
        day_rates = {minute: rate_import.get(base + minute, 0.0) for minute in range(DAY_MINUTES)}
        if not day_rates:
            continue
        cheapest = min(day_rates.values())
        # The first minute of the longest run at the cheapest rate
        start = None
        best_start = 0
        best_length = 0
        for minute in range(DAY_MINUTES + 1):
            at_cheapest = minute < DAY_MINUTES and day_rates[minute] <= cheapest + 1e-9
            if at_cheapest and start is None:
                start = minute
            elif not at_cheapest and start is not None:
                if minute - start > best_length:
                    best_length = minute - start
                    best_start = start
                start = None
        aligned_start = base + (best_start // PREDICT_STEP) * PREDICT_STEP
        windows.append({"start": aligned_start, "end": aligned_start + minutes_needed})
    return windows


def add_car_to_load(load_forecast, windows, car_kwh):
    """Return a copy of the cumulative load series with the car's energy inserted.

    Used by the two baseline scenarios, where the car is simply extra load in a
    fixed timer window rather than something Predbat schedules. ``car_kwh`` is
    already the day's energy (``run_day`` divides the annual figure by 365) and
    ``windows`` holds one window per day of the plan, each independently sized by
    ``timer_charge_window`` to hold the full ``car_kwh`` — so every window gets
    the full daily amount, not a share of it. Dividing by ``len(windows)`` here
    would silently bill only half the car's energy on the one day that matters
    (day 1 is the only one ``_billed_result`` costs).
    """
    if not windows or car_kwh <= 0:
        return dict(load_forecast)

    per_window = car_kwh
    additions = {}
    for window in windows:
        length = max(1, window["end"] - window["start"])
        per_minute = per_window / length
        for minute in range(window["start"], window["end"]):
            additions[minute] = additions.get(minute, 0.0) + per_minute

    result = {}
    running_extra = 0.0
    for minute in sorted(load_forecast.keys()):
        result[minute] = load_forecast[minute] + running_extra
        running_extra += additions.get(minute, 0.0)
    return result


def _apply_rates(predbat, rate_import, rate_export):
    """Install the day's rates and run the scans the planner depends on."""
    predbat.rate_import = rate_import
    predbat.rate_export = rate_export
    predbat.rate_low_threshold = 0
    predbat.rate_high_threshold = 0

    if predbat.rate_import:
        predbat.rate_scan(predbat.rate_import, print=False)
        predbat.rate_import, predbat.rate_import_replicated = predbat.rate_replicate(predbat.rate_import, is_import=True)
        predbat.rate_scan(predbat.rate_import, print=False)
    if predbat.rate_export:
        predbat.rate_scan_export(predbat.rate_export, print=False)
        predbat.rate_export, predbat.rate_export_replicated = predbat.rate_replicate(predbat.rate_export, is_import=False)
        predbat.rate_scan_export(predbat.rate_export, print=False)

    predbat.set_rate_thresholds()

    if predbat.rate_export:
        predbat.high_export_rates, export_lowest, _ = predbat.rate_scan_window(predbat.rate_export, 5, predbat.rate_export_cost_threshold, True)
        if predbat.rate_high_threshold == 0 and export_lowest <= predbat.rate_export_max:
            predbat.rate_export_cost_threshold = export_lowest
    else:
        predbat.high_export_rates = []

    if predbat.rate_import:
        predbat.low_rates, _, highest = predbat.rate_scan_window(predbat.rate_import, 5, predbat.rate_import_cost_threshold, False)
        if predbat.rate_low_threshold == 0 and highest >= predbat.rate_min:
            predbat.rate_import_cost_threshold = highest
    else:
        predbat.low_rates = []


def _baseline_charge_window(predbat):
    """Return the dumb battery's charge windows: the cheapest static band, charged to full.

    Mirrors the baseline in ``calculate_yesterday()`` — a household without Predbat
    that sets a timer for the cheapest rate and charges to 100%.
    """
    if not predbat.rate_import or predbat.soc_max <= 0:
        return [], []
    day_values = [value for minute, value in predbat.rate_import.items() if minute < DAY_MINUTES]
    if not day_values or min(day_values) == max(day_values):
        return [], []

    combine = predbat.combine_charge_slots
    predbat.combine_charge_slots = True
    windows, _, _ = predbat.rate_scan_window(predbat.rate_import, 5, min(day_values), False, return_raw=True)
    predbat.combine_charge_slots = combine

    windows = [window for window in windows if window["start"] < PLAN_MINUTES][:BASELINE_MAX_CHARGE_SLOTS]
    return windows, [predbat.soc_max for _ in windows]


def _billed_result(predbat, end_record, pv_step):
    """Run one scenario to completion and return its billed figures.

    The battery-value correction (metric_end minus metric_start) values whatever
    charge is left at the end, so a scenario cannot look cheap simply by finishing
    on an empty battery. This is exactly the correction ``Compare.run_scenario()``
    applies (``compare.py``), including passing zero for ``battery_cycle``,
    ``metric_keep``, ``final_carbon_g``, ``import_kwh_battery``, ``import_kwh_house``
    and ``export_kwh`` in the end-of-period ``compute_metric()`` call, matching what
    the start-of-period call already zeroes. Passing the real values there instead
    (an earlier version of this function did) would leak the optimiser's internal
    planning heuristics into a reported billed cost: ``metric_keep`` in particular
    is added into ``compute_metric()``'s "metric" unconditionally, not gated by a
    zero-default weight the way the carbon/self-sufficiency/cycle terms are, so it
    would inflate ``cost_p`` for any scenario whose plan happens to accrue it —
    typically the battery scenarios, never the no-battery baseline — for a reason
    that has nothing to do with money actually billed.
    """
    cost, import_kwh_battery, import_kwh_house, export_kwh, _, final_soc, _, battery_cycle, _, final_iboost, _ = predbat.run_prediction(
        predbat.charge_limit_best, predbat.charge_window_best, predbat.export_window_best, predbat.export_limits_best, False, end_record=end_record
    )
    metric_start, _ = predbat.compute_metric(end_record, predbat.soc_kw, predbat.soc_kw, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    metric_end, _ = predbat.compute_metric(end_record, final_soc, final_soc, cost, cost, final_iboost, final_iboost, 0, 0, 0, 0, 0, 0)

    pv_generated = sum(value for minute, value in pv_step.items() if minute < end_record)
    return {
        "cost_p": metric_end - metric_start,
        "import_kwh": import_kwh_battery + import_kwh_house,
        "export_kwh": export_kwh,
        "pv_generated_kwh": pv_generated,
        "battery_throughput_kwh": battery_cycle,
    }


def prepare_sample(predbat, config, weather, tariff, load_source, day, midnight_utc):
    """Inject every per-day input into the PredBat instance for one sampled day."""
    reset_sample_state(predbat)

    # calculate_plan() spins up a multiprocessing Pool sized from args["threads"] (plan.py)
    # unless it is exactly 0. The annual tool plans hundreds of individual days per run, each a
    # small, fast calculation, so per-day pool creation is both wasted overhead and, on a
    # spawn-based multiprocessing start method, unsafe: pool workers read the module-level
    # PRED_GLOBAL dict in prediction.py, which is only populated in the parent process. This
    # matches create_headless_predbat()'s own choice for a fully offline run; it is forced here
    # too so run_day() behaves the same way against a caller-supplied PredBat instance (such as
    # the standard unit test fixture) that has not gone through that bootstrap.
    predbat.args["threads"] = 0

    predbat.midnight_utc = midnight_utc
    predbat.now_utc = midnight_utc
    predbat.minutes_now = 0
    predbat.forecast_plan_hours = 48
    predbat.forecast_minutes = PLAN_MINUTES
    predbat.forecast_days = 2
    predbat.end_record = PLAN_MINUTES

    predbat.load_minutes = {}
    predbat.load_minutes_age = 0
    predbat.load_forecast = build_load_forecast(load_source, day, 2)

    # set_rate_thresholds() (called from _apply_rates() below) reads num_cars to decide
    # whether to widen rate_import_cost_threshold for car charging (fetch.py). num_cars is
    # not part of reset_sample_state()'s leaked-state list — it is a value this day's config
    # determines, not a scenario override to merely clear — so it is set here, from config,
    # before _apply_rates() runs rather than left at whatever the previous sample's scenario 3
    # happened to leave it as (or the headless bootstrap's own default of 1).
    predbat.num_cars = 1 if config["load"].get("car_charging_kwh", 0.0) > 0 else 0

    rate_import, rate_export = tariff.rates_for(midnight_utc, PLAN_MINUTES)
    _apply_rates(predbat, rate_import, rate_export)

    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = START_SOC_KWH


def _plan_smart_car(predbat, day, car_kwh):
    """Configure a single smart-charged car and return its planned charging slots.

    ``calculate_plan()`` never calls ``plan_car_charging()`` itself — that only
    happens in the real fetch cycle (``fetch_sensor_data_car_planning()`` in
    ``fetch.py``) or in ``Compare.recompute_car_charging()``, both of which this
    headless tool bypasses. Without calling it here, ``car_charging_slots`` would
    stay at whatever empty placeholder scenario setup left it as, ``in_car_slot()``
    would report zero load for every minute, and the car would silently cost
    nothing at all in the "with Predbat" scenario — a much bigger and more
    misleading error than merely losing Predbat's optimisation credit. The
    ready-by time and max price mirror ``config.py``'s defaults for a car with no
    explicit user override.

    Unlike ``timer_charge_window()`` (which always extends past the cheap band
    until the car's full energy fits), ``plan_car_charging()`` stops at the
    ready time and can therefore return a plan short of ``car_kwh`` if the
    cheap-rate windows before then cannot hold it all. A shortfall here would
    make scenario 3 look artificially cheap for delivering less car energy than
    the other two scenarios are billed for — inflating Predbat's apparent
    saving, the mirror image of ``add_car_to_load()``'s bug where the baseline
    scenarios were billed for too little and Predbat's saving looked smaller
    than it was — so a shortfall is logged rather than left to pass unnoticed.
    """
    predbat.car_charging_now = [False]
    predbat.car_charging_plan_time = [DEFAULT_CAR_READY_TIME]
    predbat.car_charging_plan_max_price = [0]
    slots = predbat.plan_car_charging(0, predbat.low_rates)
    planned_kwh = sum(slot.get("kwh", 0.0) for slot in slots)
    if planned_kwh < car_kwh - 1e-6:
        predbat.log("Warn: Annual: {} smart car plan only fitted {:.2f} of {:.2f} kWh into the cheap-rate windows before the {} ready time".format(day, planned_kwh, car_kwh, DEFAULT_CAR_READY_TIME))
    return slots


def run_day(predbat, config, weather, tariff, load_source, day, midnight_utc):
    """Run all three scenarios against one sampled day and return their billed figures."""
    car_kwh = config["load"].get("car_charging_kwh", 0.0) / 365.0
    car_rate_kw = config["load"].get("car_rate_kw", DEFAULT_CAR_RATE_KW)

    prepare_sample(predbat, config, weather, tariff, load_source, day, midnight_utc)

    actual_pv = weather.pv_minutes("actual", midnight_utc, PLAN_MINUTES) if config["solar"] else {}
    forecast_pv = weather.pv_minutes("forecast", midnight_utc, PLAN_MINUTES) if config["solar"] else {}
    p10_pv = weather.pv_minutes_p10(midnight_utc, PLAN_MINUTES, day.month) if config["solar"] else {}

    timer_windows = timer_charge_window(predbat.rate_import, car_kwh, car_rate_kw)
    baseline_load = add_car_to_load(predbat.load_forecast, timer_windows, car_kwh)

    results = {}

    # Scenario 1: no PV, no battery. The car still charges on the same timer, so the
    # only difference between the scenarios is the system being evaluated.
    predbat.num_cars = 0
    predbat.load_forecast = baseline_load
    load_step, actual_step, _ = build_step_data(predbat, actual_pv, actual_pv)
    zero_step = {minute: 0.0 for minute in actual_step}
    predbat.charge_limit_best = []
    predbat.charge_window_best = []
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.prediction = Prediction(predbat, zero_step, zero_step, load_step, load_step, soc_kw=0, soc_max=0)
    results["no_pvbat"] = _billed_result(predbat, DAY_MINUTES, zero_step)

    # Scenario 2: PV and battery on a dumb cheapest-rate timer, no export optimisation
    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = START_SOC_KWH
    charge_window, charge_limit = _baseline_charge_window(predbat)
    predbat.charge_window_best = charge_window
    predbat.charge_limit_best = charge_limit
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.prediction = Prediction(predbat, actual_step, actual_step, load_step, load_step, soc_kw=START_SOC_KWH)
    results["without_predbat"] = _billed_result(predbat, DAY_MINUTES, actual_step)

    # Scenario 3: Predbat plans on the FORECAST, then is costed against the ACTUALS.
    # Skipping the Prediction swap below would hand Predbat perfect foresight.
    predbat.load_forecast = build_load_forecast(load_source, day, 2)
    if car_kwh > 0:
        predbat.num_cars = 1
        predbat.car_charging_planned = [True]
        predbat.car_charging_plan_smart = [True]
        predbat.car_charging_battery_size = [max(car_kwh * 2, 50.0)]
        predbat.car_charging_limit = [car_kwh]
        predbat.car_charging_soc = [0.0]
        predbat.car_charging_rate = [car_rate_kw]
        # In scenarios 1 and 2 the car's energy is baked into ordinary house load
        # (add_car_to_load()), which the battery may serve freely. Leaving this False would
        # pin discharge_rate_now to battery_rate_min for every car-charging minute
        # (prediction.py), forcing scenario 3's battery idle exactly when the other two
        # scenarios let it help — the same physics must apply to both sides of the comparison.
        predbat.car_charging_from_battery = True
        # low_rates was computed from this day's rates in prepare_sample() and untouched
        # since, so it is safe to use for planning the car here.
        predbat.car_charging_slots = [_plan_smart_car(predbat, day, car_kwh)]
    else:
        predbat.num_cars = 0
        # A single empty slot list, not an empty outer list: fetch_config_options() always
        # sizes car_charging_slots for at least the configured car count (fetch.py), and
        # other code (including the standard test fixture's reset_inverter()) indexes
        # car_charging_slots[0] unconditionally. num_cars=0 already keeps every car-planning
        # loop from iterating it, so this is a shape placeholder rather than a live car.
        predbat.car_charging_slots = [[]]

    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = START_SOC_KWH
    predbat.pv_forecast_minute = forecast_pv
    predbat.pv_forecast_minute10 = p10_pv
    predbat.calculate_plan(recompute=True, debug_mode=False, publish=False)

    # Swap in the actuals before costing. There is no forecast/actual split for load (only PV
    # has one) — predbat_load_step is just the household load re-sampled onto the PREDICT_STEP
    # grid, identical regardless of which PV series build_step_data() was called with.
    predbat_load_step, _, _ = build_step_data(predbat, forecast_pv, p10_pv)
    predbat.prediction = Prediction(predbat, actual_step, actual_step, predbat_load_step, predbat_load_step, soc_kw=START_SOC_KWH)
    results["with_predbat"] = _billed_result(predbat, DAY_MINUTES, actual_step)

    return results


SCENARIO_KEYS = ["no_pvbat", "without_predbat", "with_predbat"]

SCENARIO_FIELDS = ["cost_p", "import_kwh", "export_kwh", "pv_generated_kwh", "battery_throughput_kwh"]


def average_rate(rates, minutes):
    """Return the mean rate across the first ``minutes`` of a rate dict."""
    values = [rates[minute] for minute in range(minutes) if minute in rates]
    return (sum(values) / len(values)) if values else 0.0


class AnnualPredictor:
    """Projects a year of electricity costs under three scenarios using the Predbat engine."""

    def __init__(self, config, log=None, storage=None, work_dir="./annual_work"):
        """Validate the config and prepare the run."""
        self.log = log or print
        self.config = validate_config(config)
        self.storage = storage
        self.work_dir = work_dir
        self.predbat = None
        self.weather = None
        self.tariff = None
        self.load_source = None
        self.caveats = []
        # month -> [scenario keys] where export exceeded generation, so self_consumed_kwh was
        # clamped to zero rather than being genuinely negative (see run()'s post-loop caveat)
        self.grid_arbitrage_scenarios = {}

    async def _resolve_location(self, weather_fetch):
        """Return (latitude, longitude) from the config, resolving a postcode if needed."""
        location = self.config["location"]
        if "latitude" in location and "longitude" in location:
            return location["latitude"], location["longitude"]
        resolved = await resolve_postcode(location["postcode"], weather_fetch, self.log)
        if not resolved:
            raise AnnualConfigError("annual.location.postcode '{}' could not be resolved; supply latitude and longitude instead".format(location["postcode"]))
        return resolved

    async def _build_load_source(self):
        """Build the load profile source: synthetic, or Octopus consumption data.

        The synthetic fallback is only used to backfill an isolated missing day within
        an otherwise successful Octopus download; a download that fails outright raises
        ``AnnualConfigError`` rather than silently substituting the synthetic profile.
        """
        load_config = self.config["load"]
        year = self.config["year"]

        if "octopus" not in load_config:
            return SyntheticLoadProfile(annual_kwh=load_config["annual_kwh"], shape=load_config["shape"], year=year)

        # A synthetic profile at the UK average backs the real data so an isolated
        # missing day does not silently become zero consumption
        fallback = SyntheticLoadProfile(annual_kwh=2700.0, shape="flat", year=year)
        source = OctopusConsumptionLoadProfile(
            api_key=load_config["octopus"]["api_key"],
            account_id=load_config["octopus"]["account_id"],
            log=self.log,
            storage=self.storage,
            fallback=fallback,
        )
        if not await source.fetch(year):
            raise AnnualConfigError("Octopus consumption data could not be downloaded for {}; check the API key and account id".format(year))
        return source

    def _reweight_survivors(self, surviving_samples, days_in_month):
        """Rescale surviving samples so they still represent a full month between them.

        ``select_samples()`` gives every chosen sample an equal weight of
        ``days_in_month / len(chosen)``, on the assumption all of them get planned
        successfully. When one or more are dropped after a ``run_day()`` failure, keeping
        their original weight would under-represent the month by the dropped days' share -
        a month that lost half its samples would silently contribute about half its true
        cost to the annual total, even though ``standing_charge_p`` and ``days`` still
        reflect the full month. Recomputing the weight from the surviving count alone
        keeps the total weight equal to ``days_in_month``; the caller still records
        ``"degraded"``/``failed_days`` so the reduced sample count remains visible.
        """
        if not surviving_samples:
            return surviving_samples
        reweighted = days_in_month / float(len(surviving_samples))
        return [(day, reweighted) for day, _ in surviving_samples]

    def _month_scenarios(self, samples, day_results):
        """Weight each sample's daily figures into monthly totals per scenario."""
        totals = {key: {field: 0.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}
        for (_, weight), result in zip(samples, day_results):
            for key in SCENARIO_KEYS:
                for field in SCENARIO_FIELDS:
                    totals[key][field] += result[key][field] * weight
        return totals

    async def run(self, progress=None):
        """Run the full annual projection and return the results document."""
        year = self.config["year"]
        samples_per_month = self.config["samples_per_month"]
        has_solar = bool(self.config["solar"])

        weather_client = AnnualWeather(
            self.config["solar"],
            latitude=0.0,
            longitude=0.0,
            log=self.log,
            storage=self.storage,
            p10_fallback=self.config["pv10_derate_fallback"],
        )
        latitude, longitude = await self._resolve_location(weather_client.fetch_json)
        weather_client.latitude = latitude
        weather_client.longitude = longitude

        self.weather = await weather_client.fetch(year) if has_solar else None
        if has_solar and not self.weather.forecast_available:
            self.caveats.append("The Open-Meteo forecast archive did not cover {}, so Predbat planned against actuals and P10 used the flat {} derate. Savings are likely overstated.".format(year, self.config["pv10_derate_fallback"]))
        elif has_solar and self.weather.fallback_months:
            self.caveats.append("Months {} had too few forecast/actual day pairs, so their P10 used the flat {} derate.".format(sorted(self.weather.fallback_months), self.config["pv10_derate_fallback"]))
        if has_solar:
            self.caveats.append("The forecast-versus-ERA5 gap includes systematic model bias as well as forecast error, so measured solar uncertainty is slightly overstated.")
        self.caveats.append("self_consumed_kwh is approximate: when the battery exports grid-charged energy it is understated.")
        self.caveats.append("export_credit_p_estimate is money ALREADY included inside cost_p (which prices every export minute at its real rate); it is informational only - adding it to cost_p double-counts export income.")

        self.predbat = create_headless_predbat(self.work_dir, self.config["timezone"], self.log)
        self.load_source = await self._build_load_source()
        self.tariff = AnnualTariff(self.config["tariff"], log=self.log, predbat=self.predbat, storage=self.storage)

        zone = pytz.timezone(self.config["timezone"])
        months = []
        total_units = 12
        completed = 0

        for month in range(1, 13):
            if progress:
                progress(completed, total_units, "Month {:02d}/{}".format(month, year))

            days_in_month = calendar.monthrange(year, month)[1]
            standing_charge_p = self.tariff.standing_charge_p_per_day * days_in_month

            if not await self.tariff.fetch_month(year, month):
                months.append({"month": month, "status": "unavailable", "reason": "no rate data available", "days": days_in_month, "standing_charge_p": standing_charge_p})
                completed += 1
                continue
            # The 48 hour plan for the last sampled day can spill into the next month
            next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
            if not await self.tariff.fetch_month(next_year, next_month):
                spill_message = "Rate data for {}-{:02d} could not be downloaded, so any plan hours for month {} spilling into it may be costed as free.".format(next_year, next_month, month)
                self.log("Warn: Annual: {}".format(spill_message))
                if spill_message not in self.caveats:
                    self.caveats.append(spill_message)

            samples = select_samples(self.weather, year, month, samples_per_month, has_solar=has_solar)
            if not samples:
                months.append({"month": month, "status": "unavailable", "reason": "no usable weather days", "days": days_in_month, "standing_charge_p": standing_charge_p})
                completed += 1
                continue

            surviving_samples = []
            day_results = []
            failed_days = []
            for day, weight in samples:
                midnight_utc = zone.localize(datetime(day.year, day.month, day.day)).astimezone(pytz.utc)
                try:
                    result = run_day(self.predbat, self.config, self.weather, self.tariff, self.load_source, day, midnight_utc)
                except Exception as exc:  # noqa: BLE001 - one bad sample must not abort the whole year
                    self.log("Warn: Annual: {} in month {} failed to plan/cost ({}: {}); excluding it from this month's total".format(day.isoformat(), month, type(exc).__name__, exc))
                    failed_days.append(day.isoformat())
                    continue
                surviving_samples.append((day, weight))
                day_results.append(result)

            if not day_results:
                months.append({"month": month, "status": "unavailable", "reason": "every sampled day failed to plan", "days": days_in_month, "standing_charge_p": standing_charge_p, "failed_days": failed_days})
                completed += 1
                continue

            if failed_days:
                surviving_samples = self._reweight_survivors(surviving_samples, days_in_month)

            totals = self._month_scenarios(surviving_samples, day_results)
            first_midnight = zone.localize(datetime(surviving_samples[0][0].year, surviving_samples[0][0].month, surviving_samples[0][0].day)).astimezone(pytz.utc)
            _, rate_export = self.tariff.rates_for(first_midnight, DAY_MINUTES)
            export_rate = average_rate(rate_export, DAY_MINUTES)

            scenarios = {}
            for key in SCENARIO_KEYS:
                entry = {field: totals[key][field] for field in SCENARIO_FIELDS}
                # An approximation, not a second income stream: cost_p already prices export at
                # the real per-minute export rate for every minute it happened, so the export
                # credit is already inside it. This is a cruder second estimate of the same
                # money (a single day's flat average export rate), kept only for a human-
                # readable "how much of that came from export" figure. Adding it to cost_p
                # double-counts the export income - see the results-document caveat below.
                entry["export_credit_p_estimate"] = entry["export_kwh"] * export_rate
                self_consumed = entry["pv_generated_kwh"] - entry["export_kwh"]
                entry["self_consumed_kwh"] = max(0.0, self_consumed)
                meaningful = self_consumed >= 0.0
                rounded = {name: round(value, 3) for name, value in entry.items()}
                rounded["self_consumed_kwh_meaningful"] = meaningful
                scenarios[key] = rounded
                if not meaningful:
                    self.grid_arbitrage_scenarios.setdefault(month, []).append(key)

            months.append(
                {
                    "month": month,
                    "status": "ok" if not failed_days else "degraded",
                    "days": days_in_month,
                    "sampled_days": [day.isoformat() for day, _ in surviving_samples],
                    "failed_days": failed_days,
                    "standing_charge_p": round(standing_charge_p, 3),
                    "scenarios": scenarios,
                }
            )
            completed += 1

        if self.grid_arbitrage_scenarios:
            arbitrage_count = sum(len(keys) for keys in self.grid_arbitrage_scenarios.values())
            first_month = sorted(self.grid_arbitrage_scenarios)[0]
            message = "self_consumed_kwh was clamped to zero rather than negative for {} scenario/month row(s) (from month {}): export exceeded generation there. See self_consumed_kwh_meaningful.".format(arbitrage_count, first_month)
            self.caveats.append(message)

        if progress:
            progress(total_units, total_units, "Complete")

        return self._build_results(months)

    def _build_results(self, months):
        """Assemble the final results document from the per-month rows.

        A month that is entirely ``"unavailable"`` (no rate data, no usable weather days, or
        every sampled day failed) contributes nothing. An ``"ok"`` or ``"degraded"`` month
        (some, but not all, of its sampled days failed - see ``run()``) still carries real
        figures and is included. When no month is included at all, ``annual.scenarios`` and
        ``annual.standing_charge_p`` are ``None`` and ``annual.savings`` is empty rather than
        reporting a fabricated zero-cost, zero-saving year.
        """
        included = [entry for entry in months if entry["status"] in ("ok", "degraded")]
        excluded = [entry["month"] for entry in months if entry["status"] not in ("ok", "degraded")]

        annual_scenarios = None
        standing_total = None
        savings = {}
        if included:
            annual_scenarios = {}
            for key in SCENARIO_KEYS:
                annual_scenarios[key] = {field: round(sum(entry["scenarios"][key][field] for entry in included), 3) for field in SCENARIO_FIELDS + ["export_credit_p_estimate", "self_consumed_kwh"]}
            standing_total = round(sum(entry["standing_charge_p"] for entry in included), 3)
            savings["pv_battery_vs_none_p"] = round(annual_scenarios["no_pvbat"]["cost_p"] - annual_scenarios["without_predbat"]["cost_p"], 3)
            savings["predbat_vs_baseline_p"] = round(annual_scenarios["without_predbat"]["cost_p"] - annual_scenarios["with_predbat"]["cost_p"], 3)
        else:
            no_result_message = "No month produced a usable result, so no annual totals or savings could be calculated."
            if no_result_message not in self.caveats:
                self.caveats.append(no_result_message)

        return {
            "year": self.config["year"],
            "config": self.config["raw"],
            "months": months,
            "annual": {
                "scenarios": annual_scenarios,
                "standing_charge_p": standing_total,
                "savings": savings,
                "months_included": len(included),
                "months_excluded": excluded,
            },
            "caveats": self.caveats,
        }
