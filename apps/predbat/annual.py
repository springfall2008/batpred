# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Annual prediction engine.

Projects a year of household electricity costs using the real Predbat planning
engine, reporting each month under four scenarios: no PV or battery, PV only
(no battery), PV and battery without Predbat, and PV and battery with Predbat.
Performs no HTTP itself; the weather and tariff modules own all network access.
"""

import calendar
import math
import os
from datetime import date, datetime, timedelta

import pytz

from annual_costs import build_costs, build_payback, resolve_costs
from annual_load import build_load_forecast, OctopusConsumptionLoadProfile, SyntheticLoadProfile
from annual_tariff import AnnualTariff
from annual_weather import AnnualWeather, resolve_postcode
from const import MINUTE_WATT, PREDICT_STEP
from prediction import Prediction
from tariff_catalogue import PRICE_CAP_IMPORT_P, SEG_EXPORT_P

VALID_SHAPES = ["night", "day", "flat"]

DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_SAMPLES_PER_MONTH = 2
DEFAULT_PV10_DERATE_FALLBACK = 0.7
DEFAULT_DECLINATION = 35
DEFAULT_AZIMUTH = 180
DEFAULT_EFFICIENCY = 0.95
DEFAULT_HYBRID = True

# A typical domestic panel in 2026. Only used to turn a panel count into kWp.
DEFAULT_PANEL_WATTS = 400.0

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
    must be strictly greater than it. ``maximum`` is always inclusive. When ``integer`` is
    set, a fractional float (``2.5``) is rejected rather than silently truncated - plain
    ``int()`` truncates without complaint, which would let a mistyped panel count or sample
    rate through as a different, smaller whole number.
    """
    if isinstance(value, bool):
        raise AnnualConfigError("{} must be a number, not a boolean (got {})".format(field, value))
    if integer and isinstance(value, float) and not value.is_integer():
        raise AnnualConfigError("{} must be a whole number, got {}".format(field, value))
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


def _coerce_bool(value):
    """Coerce a config value to a bool the way the rest of the codebase does (see web.py's setting toggles).

    A bare ``bool`` passes through unchanged. Anything else is compared, case-insensitively,
    against the usual truthy string forms - ``bool("false")`` is ``True`` in plain Python
    (any non-empty string is truthy), which would silently turn an explicit ``debug: "false"``
    in a YAML/JSON config into debug mode being enabled.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "on", "1", "yes")


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
        has_kwp = "kwp" in array
        has_panels = "panels" in array
        if has_kwp and has_panels:
            # Two figures that disagree are a mistake worth surfacing. Guessing which the
            # user meant would silently model a different system than they described.
            raise AnnualConfigError("annual.solar[{}] has both kwp and panels; give one or the other, not both".format(index))
        if not has_kwp and not has_panels:
            raise AnnualConfigError("annual.solar[{}] is missing kwp (the array's peak power in kW) or panels (how many panels it has)".format(index))
        normalised = dict(array)
        if has_panels:
            panels = _require_number(array["panels"], "annual.solar[{}].panels".format(index), minimum=0, exclusive_minimum=True, integer=True)
            panel_watts = _require_number(array.get("panel_watts", DEFAULT_PANEL_WATTS), "annual.solar[{}].panel_watts".format(index), minimum=0, exclusive_minimum=True)
            # Retained alongside the derived kwp so the web form can show back what the
            # user actually typed rather than replacing it with a computed decimal.
            normalised["panels"] = panels
            normalised["panel_watts"] = panel_watts
            normalised["kwp"] = panels * panel_watts / 1000.0
        else:
            normalised["kwp"] = _require_number(array["kwp"], "annual.solar[{}].kwp".format(index), minimum=0, exclusive_minimum=True)
        # declination is a roof pitch in degrees: 0 (flat) to 90 (vertical) inclusive.
        normalised["declination"] = _require_number(array.get("declination", DEFAULT_DECLINATION), "annual.solar[{}].declination".format(index), minimum=0, maximum=90)
        # azimuth follows Predbat's convention (0 = north, 180 = south) and
        # convert_azimuth() (solar_model.py) accepts negative values too, so the bound
        # here is deliberately wide rather than tighter and risking rejecting a valid
        # existing config.
        normalised["azimuth"] = _require_number(array.get("azimuth", DEFAULT_AZIMUTH), "annual.solar[{}].azimuth".format(index), minimum=-360, maximum=360)
        normalised["efficiency"] = _require_number(array.get("efficiency", DEFAULT_EFFICIENCY), "annual.solar[{}].efficiency".format(index), minimum=0, exclusive_minimum=True, maximum=1)
        # annual_weather.py reads this with `array.get("azimuth_zero_south", False)`; coerced
        # here for the same reason as "debug"/"hybrid" below - a "false" string left
        # unconverted from a submitted form or YAML file is truthy in plain Python, which
        # would silently flip which azimuth convention every array's PV geometry uses.
        normalised["azimuth_zero_south"] = _coerce_bool(array.get("azimuth_zero_south", False))
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
        "hybrid": _coerce_bool(raw.get("hybrid", DEFAULT_HYBRID)),
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
        "car_rate_kw": _require_number(raw.get("car_rate_kw", DEFAULT_CAR_RATE_KW), "annual.load.car_rate_kw", minimum=0, exclusive_minimum=True),
    }


# What a household with no PV and no battery actually pays: the Ofgem price cap for
# import, and a typical fixed Smart Export Guarantee rate for the export they cannot
# have without generation. Rates come from tariff_catalogue so the cap figure is stated
# in exactly one place.
DEFAULT_BASELINE_TARIFF = {"rates_import": [{"rate": PRICE_CAP_IMPORT_P}], "rates_export": [{"rate": SEG_EXPORT_P}]}


def _validate_tariff(raw, path="annual.tariff"):
    """Normalise a tariff block, requiring at least one import rate source.

    A URL containing {dno_region} with no dno_region supplied is rejected here
    rather than left to 404 at fetch time, where it would surface as an
    unavailable month and read like an Octopus outage.

    ``path`` names the block being validated, because two of them come through here:
    the main tariff and ``baseline_tariff``. Hard-coded messages sent every baseline
    problem to "annual.tariff", telling the user to fix a block that was perfectly
    valid - the one thing an error message must never do.
    """
    if not isinstance(raw, dict):
        raise AnnualConfigError("{} is required and must be a mapping".format(path))
    if not raw.get("import_octopus_url") and not raw.get("rates_import"):
        raise AnnualConfigError("{} requires either import_octopus_url or rates_import".format(path))

    templated = [name for name in ["import_octopus_url", "export_octopus_url"] if raw.get(name) and "{dno_region}" in raw[name]]
    if templated and not raw.get("dno_region"):
        raise AnnualConfigError("{path}.{fields} uses {{dno_region}} but {path}.dno_region is not set; supply your Octopus region letter, for example 'A' for Eastern England".format(path=path, fields=", ".join(templated)))

    tariff = dict(raw)
    tariff["standing_charge_p_per_day"] = _require_number(raw.get("standing_charge_p_per_day", 0.0), "{}.standing_charge_p_per_day".format(path), minimum=0)
    return tariff


def _validated_costs(raw):
    """Return the validated install-cost settings, as an AnnualConfigError on failure.

    annual_costs.resolve_costs raises ValueError because it is a pure module with no
    dependency on this one; translating here keeps every config problem a single
    exception type for the CLI and web layer to catch.
    """
    try:
        return resolve_costs(raw)
    except ValueError as error:
        raise AnnualConfigError(str(error))


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

    location = dict(location)
    if "latitude" in location:
        location["latitude"] = _require_number(location["latitude"], "annual.location.latitude", minimum=-90, maximum=90)
    if "longitude" in location:
        location["longitude"] = _require_number(location["longitude"], "annual.location.longitude", minimum=-180, maximum=180)

    solar = _validate_solar(raw.get("solar"))
    battery = _validate_battery(raw.get("battery"))
    if not solar and battery is None:
        raise AnnualConfigError("annual needs at least one of solar or battery: with neither there is nothing to evaluate")

    samples_per_month = _require_number(raw.get("samples_per_month", DEFAULT_SAMPLES_PER_MONTH), "annual.samples_per_month", minimum=1, integer=True)

    if today is None:
        today = date.today()
    # Capped at the most recent COMPLETE calendar year, not the current (in-progress) one:
    # Open-Meteo answers a mid-year request with short but internally-consistent arrays, so
    # _payload_problem() cannot tell a truncated current-year download from a genuinely
    # complete one, and it gets cached with no expiry - permanently pinning the remaining
    # months as "unavailable" until the work dir is deleted by hand. See annual_weather.py.
    year = _require_number(raw.get("year", today.year - 1), "annual.year", minimum=MINIMUM_YEAR, maximum=today.year - 1, integer=True)

    return {
        "location": dict(location),
        "year": year,
        "solar": solar,
        "battery": battery,
        "load": _validate_load(raw.get("load")),
        "tariff": _validate_tariff(raw.get("tariff")),
        # The counterfactual bill is what the household would pay with no system at all,
        # and such a household is not on a battery tariff: the smart import tariffs are
        # only worth having once you have something to shift load into. Pricing the
        # no-PV/battery scenario on the same tariff as the battery scenarios therefore
        # understates what the system is worth. Defaults to the Ofgem price cap.
        "baseline_tariff": _validate_tariff(raw.get("baseline_tariff") or DEFAULT_BASELINE_TARIFF, path="annual.baseline_tariff"),
        "samples_per_month": samples_per_month,
        "costs": _validated_costs(raw.get("costs")),
        "debug": _coerce_bool(raw.get("debug", False)),
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
    the scenario-3 smart-car overrides ``_run_scenarios()`` sets on the with-car
    leg, and the field ``tests/test_single_debug.py`` documents as leaking between
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

    # Scenario 3's smart-car overrides (_run_scenarios()). Reset here rather than relying on
    # every consumer being gated on num_cars (prediction.py, plan.py) - that guarantee is
    # fragile in a file that has already had several state-leak bugs. Safe to reset even
    # though _run_scenarios() sets these on the with-car leg: prepare_sample() calls this
    # function BEFORE that leg sets them, never after.
    predbat.car_charging_planned = [False]
    predbat.car_charging_limit = [0.0]
    predbat.car_charging_soc = [0.0]
    predbat.car_charging_rate = [DEFAULT_CAR_RATE_KW]
    predbat.car_charging_battery_size = [50.0]
    predbat.car_charging_plan_smart = [False]
    predbat.car_charging_from_battery = False


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

# A charging session longer than this will not reliably fit inside a typical cheap
# overnight band (Flux, Cosy), so car_charging_schedule() splits into more, shorter
# sessions per week once a single weekly session would exceed it.
CAR_SESSION_MAX_HOURS = 6.0

# However many sessions a week would be needed to keep every session under
# CAR_SESSION_MAX_HOURS, never plan more than one a day.
MAX_SESSIONS_PER_WEEK = 7


def car_charging_schedule(annual_kwh, car_rate_kw):
    """Derive how often, and how much, a car charges per week from its annual energy.

    Smearing an annual car figure evenly across 365 days is wrong in a way that
    matters: a 2,500 kWh/year smear is 6.85 kWh/day, under an hour at 7.4 kW, which
    fits trivially inside any cheap overnight window. A dumb timer would then get the
    cheap rate just as easily as Predbat, and the EV would contribute almost nothing
    to the measured saving. Real owners charge in sessions of tens of kWh that can
    overflow a short cheap band, forcing part of the session onto an expensive rate
    under a timer while Predbat spreads it across the cheapest half-hours instead -
    that overflow is where smart charging earns its keep, and smearing deletes it.

    The schedule is derived from the annual energy and the charger's power, not
    configured directly: one session a week carries the whole week's energy unless
    that session would run longer than ``CAR_SESSION_MAX_HOURS`` at the given rate, in
    which case the week's energy is split across as many sessions as needed to bring
    each under the cap, capped at ``MAX_SESSIONS_PER_WEEK`` (one a day). When even a
    daily session cannot get under the cap (a very low charge rate against a very high
    annual figure), seven sessions are still returned, but each will run long - the
    caller is responsible for logging that the overflow this model is meant to capture
    is then understated.

    Returns (sessions_per_week, session_kwh). ``sessions_per_week * session_kwh`` equals
    ``annual_kwh / 52.0`` to within floating-point rounding (division and its inverse),
    so summing the week's sessions recovers the annual total.

    Note a deliberate 52-vs-7 mismatch: a session is sized here as ``annual_kwh / 52.0``
    per week, but ``run_day()`` blends it back in at ``sessions_per_week / 7`` of each
    sampled day - and 52 weeks * 7 days = 364, one short of a real (365 or 366 day) year.
    So the car energy actually modelled over a year is ``annual_kwh * 365 / 364``, about
    +0.27% high. Harmless at that size, but it is a real error hiding behind two numbers
    that look interchangeable: "fixing" only one of the 52 here or the 7 in ``run_day()``
    (e.g. switching this to weeks-per-365-days) without the matching change to the other
    would turn a harmless 0.27% into a much larger one. Keep the two divisors paired if
    either is ever revisited.
    """
    weekly_kwh = annual_kwh / 52.0
    if weekly_kwh <= 0 or car_rate_kw <= 0:
        # A configured car (annual_kwh > 0) with an unusable rate is silently dropped here:
        # (0, 0.0) reads to a caller exactly like "no car configured" at all. Not reachable
        # through _validate_load() today (car_rate_kw is validated greater than zero there),
        # but a caller reaching this function some other way gets no signal that a real car
        # was discarded. Not raised, since this guard is defensive rather than a real
        # validation boundary - see car_charging_overflow_warning() below for the one
        # warning this module does emit about the schedule it derives.
        return 0, 0.0

    session_hours = weekly_kwh / car_rate_kw
    if session_hours <= CAR_SESSION_MAX_HOURS:
        return 1, weekly_kwh

    sessions_per_week = min(MAX_SESSIONS_PER_WEEK, math.ceil(session_hours / CAR_SESSION_MAX_HOURS))
    return sessions_per_week, weekly_kwh / sessions_per_week


def car_charging_overflow_warning(car_charging_kwh, car_rate_kw):
    """Return a warning message if the car's sessions cannot fit under the six-hour cap.

    This is a static property of ``car_charging_kwh``/``car_rate_kw`` alone, not of any
    particular sampled day, so a caller should log it once per run (``AnnualPredictor.run()``
    does) rather than once per sampled day - the same condition would otherwise repeat
    identically for every one of the roughly two dozen days a run samples. Returns None
    when there is nothing to warn about, including when no car is configured at all.
    """
    if car_charging_kwh <= 0:
        return None
    sessions_per_week, session_kwh = car_charging_schedule(car_charging_kwh, car_rate_kw)
    if sessions_per_week >= MAX_SESSIONS_PER_WEEK and session_kwh > car_rate_kw * CAR_SESSION_MAX_HOURS + 1e-6:
        return (
            "Annual: car charging needs {:.1f} kWh/week at {:.1f} kW, which cannot fit into {} sessions of {:.0f} hours or less; sessions are running long ({:.1f} hours), so the timer/Predbat overflow this model is meant to capture is understated".format(
                sessions_per_week * session_kwh, car_rate_kw, MAX_SESSIONS_PER_WEEK, CAR_SESSION_MAX_HOURS, session_kwh / car_rate_kw
            )
        )
    return None


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

        # _billed_result only costs minutes < DAY_MINUTES of the billed day (day_offset 0), so a
        # window that runs past its own day's boundary gets only partially billed there: the
        # baselines (scenarios 1/2) would then be charged for less car energy than scenario 3,
        # which always bills the full session, making Predbat's saving look bigger than it is (or,
        # on a day whose cheap band happens to run low, even negative). Pull the start back so the
        # window still ends inside its own day whenever the session fits in a day at all. When
        # minutes_needed itself exceeds a whole day, day_end - minutes_needed is before the day
        # even starts, so clamp to the day's start instead of producing a negative offset - the
        # window still overflows into the next day, which is unavoidable for a session that long.
        day_end = base + DAY_MINUTES
        if aligned_start + minutes_needed > day_end:
            aligned_start = max(base, day_end - minutes_needed)

        windows.append({"start": aligned_start, "end": aligned_start + minutes_needed})
    return windows


def add_car_to_load(load_forecast, windows, car_kwh):
    """Return a copy of the cumulative load series with the car's energy inserted.

    Used by the two baseline scenarios, where the car is simply extra load in a
    fixed timer window rather than something Predbat schedules. ``car_kwh`` is
    already the energy this leg is charging - a full weekly session, or zero on the
    without-car leg ``run_day()`` blends against (see ``car_charging_schedule()``) -
    and ``windows`` holds one window per day of the plan, each independently sized by
    ``timer_charge_window`` to hold the full ``car_kwh`` — so every window gets
    the full session amount, not a share of it. Dividing by ``len(windows)`` here
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

    This ALWAYS runs with ``save=None`` (``run_prediction()``'s default) and must
    keep doing so: ``save="best"`` (or ``"compare"``/``"yesterday"``) switches on
    ``enable_standing_charge`` inside ``prediction.py`` (see ``_capture_plan()``,
    which needs a save="best" run for an unrelated reason and re-runs the prediction
    from scratch rather than reusing this one, precisely to keep that flag off here).
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


def prepare_sample(predbat, config, weather, tariff, load_source, day, midnight_utc, car_kwh):
    """Inject every per-day input into the PredBat instance for one sampled day.

    ``car_kwh`` is the energy this specific leg is charging (a full weekly session, or
    zero for the without-car leg run_day() blends against - see run_day()), not the raw
    annual config figure, so num_cars below reflects what this leg actually models.
    """
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
    # not part of reset_sample_state()'s leaked-state list — it is a value this leg's car_kwh
    # determines, not a scenario override to merely clear — so it is set here, from the leg's
    # own car_kwh, before _apply_rates() runs rather than left at whatever the previous
    # sample's scenario 3 happened to leave it as (or the headless bootstrap's own default of 1).
    predbat.num_cars = 1 if car_kwh > 0 else 0

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


def _capture_plan(predbat, pv_step, pv_step10, load_step, load_step10, end_record):
    """Return the current scenario's plan as the JSON structure the web plan renderer consumes.

    This is the same ``raw_plan`` the live ``/plan`` page renders from - ``publish_html_plan()``
    builds it from ``charge_limit_best``/``charge_window_best``/``export_*_best`` and the step
    data, exactly as they stand for the scenario just costed. ``publish=False`` keeps it from
    touching Home Assistant, so this is safe in a headless run: it only reads state and returns.

    ``publish_html_plan()`` unconditionally reads ``predict_soc_best``/
    ``predict_clipped_best``/``predict_iboost_best``/``predict_carbon_best``/
    ``predict_metric_best`` off ``predbat``, which ``run_prediction()`` only populates
    when called with ``save="best"`` (or ``"compare"``/``"yesterday"`` - see
    ``plan.py``). This function therefore re-runs the prediction once more, purely to
    populate that state, rather than asking the BILLED run (``_billed_result()``) to
    pass ``save="best"`` itself. That is deliberate, not a missed optimisation:
    ``save="best"`` also switches on ``enable_standing_charge`` inside
    ``prediction.py``, which would add a standing charge into the returned cost - but
    the annual engine already accounts for standing charge separately, as its own
    per-month ``standing_charge_p`` field built from the annual config's tariff, not
    from the live instance's unrelated ``metric_standing_charge`` (``fetch.py``). Do
    not "simplify" this back to reusing the billed run's ``save`` flag - that
    silently double-counts the standing charge in every debug-captured scenario. The
    extra prediction is a real doubling of planning cost per scenario, but it only
    happens under the opt-in debug flag.
    """
    predbat.run_prediction(predbat.charge_limit_best, predbat.charge_window_best, predbat.export_window_best, predbat.export_limits_best, False, end_record=end_record, save="best")
    _, raw_plan = predbat.publish_html_plan(pv_step, pv_step10, load_step, load_step10, end_record, publish=False, prediction=predbat.prediction)
    return raw_plan


def _run_scenarios(predbat, config, weather, tariff, load_source, day, midnight_utc, car_kwh, car_rate_kw, plans=None, baseline_tariff=None):
    """Run all four scenarios against one sampled day at a fixed car charging energy.

    ``car_kwh`` is the actual energy this leg charges - either a full weekly charging
    session or zero, never the smeared daily average an earlier version of this tool
    used (see ``car_charging_schedule()``). ``run_day()`` calls this once when no car is
    configured, or twice (once per leg) and blends the two sets of results when one is.

    ``plans``, when supplied, is a dict this function fills in place with one entry per
    scenario key, each holding that scenario's plan captured against the same PV and load
    series it was billed against - a plan drawn from a different series would defeat the
    point of the feature, which is cross-checking the billed numbers. Leaving it ``None``
    (the default) skips every capture, so a non-debug run pays no extra cost. Each
    capture (see ``_capture_plan()``) re-runs the prediction it captures from, rather
    than reusing the billed ``_billed_result()`` run, so the billed ``cost_p``/
    ``import_kwh``/etc are always from a ``save=None`` run and never carry the standing
    charge ``save="best"`` adds - see ``test_annual_debug_capture()`` and
    ``test_annual_integration.py``'s identical-billed-figures regression guard.
    """
    prepare_sample(predbat, config, weather, tariff, load_source, day, midnight_utc, car_kwh)

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
    # Price the counterfactual on its OWN tariff. A household with no PV and no battery is
    # not on a smart import tariff - those only pay off once there is something to shift
    # load into - so charging the baseline at the battery tariff's overnight rate credits
    # it with a saving it could never have had, and understates the system.
    #
    # The rates are restored immediately afterwards, from the tariff's own output rather
    # than from predbat.rate_import: _apply_rates REPLICATES what it is given, so reusing
    # the installed dict would re-replicate an already-replicated series. Every later
    # scenario in this leg therefore runs on exactly what prepare_sample() installed.
    if baseline_tariff is not None:
        baseline_import, baseline_export = baseline_tariff.rates_for(midnight_utc, PLAN_MINUTES)
        _apply_rates(predbat, baseline_import, baseline_export)

    # Constructed AFTER the rate swap, never before: Prediction snapshots the rates off
    # predbat at construction time (prediction.py, `self.rate_import = base.rate_import`),
    # so a Prediction built first would be billed at the main tariff no matter what was
    # installed afterwards - the swap would appear to work and change nothing.
    predbat.prediction = Prediction(predbat, zero_step, zero_step, load_step, load_step, soc_kw=0, soc_max=0)
    results["no_pvbat"] = _billed_result(predbat, DAY_MINUTES, zero_step)
    if plans is not None:
        plans["no_pvbat"] = _capture_plan(predbat, zero_step, zero_step, load_step, load_step, DAY_MINUTES)

    if baseline_tariff is not None:
        main_import, main_export = tariff.rates_for(midnight_utc, PLAN_MINUTES)
        _apply_rates(predbat, main_import, main_export)

    # Scenario 1b: PV but no battery. apply_hardware gives the array its real inverter
    # and export limits - a PV-only system still has an inverter, and clipping matters -
    # while soc_max=0 leaves it with nowhere to store surplus, so everything the house
    # cannot use at the moment it is generated is exported. That difference is the whole
    # point of this scenario: it is what makes PV-only payback different from a fixed
    # fraction of the PV-plus-battery figure.
    #
    # Like scenarios 1 and 2 this is a single prediction with empty windows. It must NOT
    # call calculate_plan(): the planner is what makes a run expensive, and there is
    # nothing to plan without a battery.
    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = 0
    predbat.charge_limit_best = []
    predbat.charge_window_best = []
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.prediction = Prediction(predbat, actual_step, actual_step, load_step, load_step, soc_kw=0, soc_max=0)
    results["pv_only"] = _billed_result(predbat, DAY_MINUTES, actual_step)
    if plans is not None:
        plans["pv_only"] = _capture_plan(predbat, actual_step, actual_step, load_step, load_step, DAY_MINUTES)

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
    if plans is not None:
        plans["without_predbat"] = _capture_plan(predbat, actual_step, actual_step, load_step, load_step, DAY_MINUTES)

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
    # Open-Meteo gives this model a forecast and a monthly P10 ratio but no P90 series, so the
    # pv90 (upside) scenario runs on the same PV as nominal - its upside comes from load_scaling90
    # alone. This must be assigned explicitly on every sampled day: one PredBat instance is reused
    # for the whole year (see AnnualRun), so leaving it to calculate_plan()'s fallback would pin
    # every later day's "upside" to the first sampled day's solar profile.
    predbat.pv_forecast_minute90 = dict(forecast_pv)
    predbat.calculate_plan(recompute=True, debug_mode=False, publish=False)

    # Swap in the actuals before costing. There is no forecast/actual split for load (only PV
    # has one) — predbat_load_step is just the household load re-sampled onto the PREDICT_STEP
    # grid, identical regardless of which PV series build_step_data() was called with.
    predbat_load_step, _, _ = build_step_data(predbat, forecast_pv, p10_pv)
    predbat.prediction = Prediction(predbat, actual_step, actual_step, predbat_load_step, predbat_load_step, soc_kw=START_SOC_KWH)
    results["with_predbat"] = _billed_result(predbat, DAY_MINUTES, actual_step)
    if plans is not None:
        plans["with_predbat"] = _capture_plan(predbat, actual_step, actual_step, predbat_load_step, predbat_load_step, DAY_MINUTES)

    return results


SCENARIO_KEYS = ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]

SCENARIO_FIELDS = ["cost_p", "import_kwh", "export_kwh", "pv_generated_kwh", "battery_throughput_kwh"]


def _blend_results(with_car, without_car, fraction):
    """Blend two full four-scenario result dicts field by field.

    ``fraction`` is the share of the week charging actually happens
    (``sessions_per_week / 7`` - see ``car_charging_schedule()``), so every field of
    every scenario blends linearly: ``fraction * with_car + (1 - fraction) *
    without_car``. ``pv_generated_kwh`` is identical between the two legs (the car has
    no effect on solar generation), so its blend is a no-op - a useful self-check that
    the two legs really are the same sampled day underneath.
    """
    return {key: {field: fraction * with_car[key][field] + (1 - fraction) * without_car[key][field] for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}


def run_day(predbat, config, weather, tariff, load_source, day, midnight_utc, plans=None, baseline_tariff=None):
    """Run all four scenarios against one sampled day and return their billed figures.

    A configured car charges in weekly sessions, not a daily smear (see
    ``car_charging_schedule()``), so the sampled day is planned TWICE when a car is
    configured: once carrying a full session, once with no car at all, and the two are
    blended by how often a session actually happens that week. Blending per sampled day,
    rather than dedicating separate sample days to "car" and "no car", keeps the
    irradiance-percentile stratification of the sampled days intact instead of
    confounding solar percentile with charging state. A config with no car runs a
    single leg, exactly as before this blending was introduced.

    ``plans``, when supplied, is a list this function appends one entry per leg to, each
    ``{"leg": ..., "scenarios": {...}}``: ``"single"`` for the no-car path, or
    ``"with_car"`` followed by ``"without_car"`` for the blended path. Leaving it ``None``
    (the default) appends nothing, so a non-debug run captures no plans.
    """
    car_charging_kwh = config["load"].get("car_charging_kwh", 0.0)
    car_rate_kw = config["load"].get("car_rate_kw", DEFAULT_CAR_RATE_KW)

    if car_charging_kwh <= 0:
        leg_plans = {} if plans is not None else None
        result = _run_scenarios(predbat, config, weather, tariff, load_source, day, midnight_utc, car_kwh=0.0, car_rate_kw=car_rate_kw, plans=leg_plans, baseline_tariff=baseline_tariff)
        if plans is not None:
            plans.append({"leg": "single", "scenarios": leg_plans})
        return result

    # The "sessions running long" overflow warning is a static property of the config
    # (car_charging_kwh, car_rate_kw), not of this particular day, so it is emitted once
    # per run by AnnualPredictor.run() (via car_charging_overflow_warning()) rather than
    # here, where it would otherwise repeat once per sampled day.
    sessions_per_week, session_kwh = car_charging_schedule(car_charging_kwh, car_rate_kw)

    with_car_plans = {} if plans is not None else None
    with_car = _run_scenarios(predbat, config, weather, tariff, load_source, day, midnight_utc, car_kwh=session_kwh, car_rate_kw=car_rate_kw, plans=with_car_plans, baseline_tariff=baseline_tariff)
    if plans is not None:
        plans.append({"leg": "with_car", "scenarios": with_car_plans})

    without_car_plans = {} if plans is not None else None
    without_car = _run_scenarios(predbat, config, weather, tariff, load_source, day, midnight_utc, car_kwh=0.0, car_rate_kw=car_rate_kw, plans=without_car_plans, baseline_tariff=baseline_tariff)
    if plans is not None:
        plans.append({"leg": "without_car", "scenarios": without_car_plans})

    # sessions_per_week / 7, not / 52: this is a fraction of a WEEK (how many of its 7 days
    # actually carry a session), independent of car_charging_schedule()'s own 52-weeks-per-year
    # sizing above. See car_charging_schedule()'s docstring for the harmless ~+0.27% this
    # 52-vs-7 pairing produces, and why the two must be changed together if either is.
    fraction = sessions_per_week / float(MAX_SESSIONS_PER_WEEK)
    return _blend_results(with_car, without_car, fraction)


def average_rate(rates, minutes):
    """Return the mean rate across the first ``minutes`` of a rate dict."""
    values = [rates[minute] for minute in range(minutes) if minute in rates]
    return (sum(values) / len(values)) if values else 0.0


class AnnualPredictor:
    """Projects a year of electricity costs under four scenarios using the Predbat engine."""

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

    @staticmethod
    def _synthesised_sides(fallback_months, year, month):
        """Return the sorted sides ('import'/'export') whose rates for this one month came from the tariff's current-rates fallback.

        ``fallback_months`` is ``AnnualTariff.fallback_months`` - a set of
        ``(year, month, side)`` triples covering every month in the run, not just this
        one - so this filters down to the (usually empty) subset that applies here.
        Attached to each month's row so a synthesised month is not indistinguishable
        from a real one in the results document, the chart or the table.
        """
        return sorted(side for fb_year, fb_month, side in fallback_months if fb_year == year and fb_month == month)

    @staticmethod
    def _tariff_fallback_caveats(fallback_months, unpaid_export_months, year):
        """Return the caveat strings describing a tariff's current-rates fallback and/or wholly-unpriced export months.

        Both are properties of the whole run (``AnnualTariff`` accumulates them across
        every ``fetch_month`` call), not of any single sampled day, so this is called
        once after the month loop rather than from inside it. Returns an empty list
        when the tariff needed neither.

        Both sets are filtered down to ``year`` first: December's spill fetch
        (``fetch_month(year + 1, 1)``, done so the last sampled day's 48 hour plan has
        rates to spill into) can itself hit either fallback, which would otherwise leak
        a January-of-next-year entry into a caveat about this year's run - a document
        for 2025 gaining a stray "2026-01" that names a month with no row anywhere in
        it.
        """
        fallback_months = {entry for entry in fallback_months if entry[0] == year}
        unpaid_export_months = {entry for entry in unpaid_export_months if entry[0] == year}
        caveats = []
        if fallback_months:
            fallback_list = ", ".join("{}-{:02d} ({})".format(fb_year, fb_month, side) for fb_year, fb_month, side in sorted(fallback_months))
            caveats.append("No historical rates were available for {} on this tariff, likely because it launched after {}. Those months' rates are today's rates repeated across the month, not what was actually charged then.".format(fallback_list, year))
        if unpaid_export_months:
            unpaid_list = ", ".join("{}-{:02d}".format(unpaid_year, unpaid_month) for unpaid_year, unpaid_month in sorted(unpaid_export_months))
            caveats.append("No export rates at all (historical or current) could be found for {} on this tariff, so export was priced at zero for those months. If this tariff pays for export, savings for those months are understated.".format(unpaid_list))
        return caveats

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
        self.caveats.append(
            "The no_pvbat counterfactual is priced on its own baseline tariff, since a household with no PV or battery would not be on a battery tariff. Both scenarios still use ONE standing charge - the main tariff's - so any difference in standing charge between the two tariffs is NOT included in the reported savings or payback."
        )
        self.caveats.append("export_credit_p_estimate is money ALREADY included inside cost_p (which prices every export minute at its real rate); it is informational only - adding it to cost_p double-counts export income.")
        self.caveats.append(
            "The without_predbat baseline charges in the single cheapest contiguous band of each day, mirroring Predbat's own savings baseline. On a half-hourly tariff such as Agile the cheapest band is often one 30 minute slot, so the baseline is a more pessimistic comparator there than on a banded tariff (Economy 7, Cosy, Flux) where it covers the whole cheap period. Compare predbat_vs_baseline_p across tariffs with that in mind."
        )

        # A static property of the config, not of any one sampled day, so this is checked and
        # logged exactly once here rather than inside run_day(), which runs once per sampled
        # day (roughly two dozen times per year at the default samples_per_month).
        car_overflow_warning = car_charging_overflow_warning(self.config["load"].get("car_charging_kwh", 0.0), self.config["load"].get("car_rate_kw", DEFAULT_CAR_RATE_KW))
        if car_overflow_warning:
            self.log("Warn: {}".format(car_overflow_warning))
            self.caveats.append(car_overflow_warning)

        self.predbat = create_headless_predbat(self.work_dir, self.config["timezone"], self.log)
        self.load_source = await self._build_load_source()
        self.tariff = AnnualTariff(self.config["tariff"], log=self.log, predbat=self.predbat, storage=self.storage, timezone=self.config["timezone"])
        # Prices the no-PV/battery counterfactual only. Its own AnnualTariff because it
        # may be a completely different product with its own rate downloads and cache.
        self.baseline_tariff = AnnualTariff(self.config["baseline_tariff"], log=self.log, predbat=self.predbat, storage=self.storage, timezone=self.config["timezone"])

        baseline_fallback_months = []
        zone = pytz.timezone(self.config["timezone"])
        months = []
        total_units = 12
        completed = 0

        for month in range(1, 13):
            if progress:
                progress(completed, total_units, "Month {:02d}/{}".format(month, year))

            days_in_month = calendar.monthrange(year, month)[1]
            standing_charge_p = self.tariff.standing_charge_p_per_day * days_in_month

            baseline_ready = await self.baseline_tariff.fetch_month(year, month)
            if not baseline_ready:
                # Falls back to the main tariff for this month rather than losing it, but
                # that silently changes what no_pvbat means, so it is recorded.
                baseline_fallback_months.append(month)
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
            month_plans = []
            for day, weight in samples:
                midnight_utc = zone.localize(datetime(day.year, day.month, day.day)).astimezone(pytz.utc)
                day_plans = [] if self.config["debug"] else None
                try:
                    result = run_day(self.predbat, self.config, self.weather, self.tariff, self.load_source, day, midnight_utc, plans=day_plans, baseline_tariff=self.baseline_tariff if baseline_ready else None)
                except Exception as exc:  # noqa: BLE001 - one bad sample must not abort the whole year
                    self.log("Warn: Annual: {} in month {} failed to plan/cost ({}: {}); excluding it from this month's total".format(day.isoformat(), month, type(exc).__name__, exc))
                    failed_days.append(day.isoformat())
                    continue
                surviving_samples.append((day, weight))
                day_results.append(result)
                if day_plans is not None:
                    month_plans.extend(dict(entry, day=day.isoformat()) for entry in day_plans)

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
                scenarios[key] = {name: round(value, 3) for name, value in entry.items()}

            row = {
                "month": month,
                "status": "ok" if not failed_days else "degraded",
                "days": days_in_month,
                "sampled_days": [day.isoformat() for day, _ in surviving_samples],
                "failed_days": failed_days,
                "standing_charge_p": round(standing_charge_p, 3),
                "scenarios": scenarios,
                # Which sides of *this* month's rates (if any) came from the tariff's
                # current-rates fallback rather than a real historical download -
                # carried onto the row itself, not just into a run-wide caveat, so a
                # synthesised month is not indistinguishable from a real one in the
                # JSON, the chart or the table.
                "rates_synthesised": self._synthesised_sides(self.tariff.fallback_months, year, month),
            }
            if self.config["debug"]:
                row["plans"] = month_plans
            months.append(row)
            completed += 1

        self.caveats.extend(self._tariff_fallback_caveats(self.tariff.fallback_months, self.tariff.unpaid_export_months, year))

        if baseline_fallback_months:
            # Falling back to the main tariff keeps the month rather than losing it, but it
            # silently changes what no_pvbat means there, so it has to be said out loud.
            self.caveats.append(
                "No baseline-tariff rates were available for month(s) {}, so the no-PV/battery counterfactual there was priced on the main tariff instead, which understates what the system is worth in those months.".format(sorted(baseline_fallback_months))
            )

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
                annual_scenarios[key] = {field: round(sum(entry["scenarios"][key][field] for entry in included), 3) for field in SCENARIO_FIELDS + ["export_credit_p_estimate"]}
            standing_total = round(sum(entry["standing_charge_p"] for entry in included), 3)
            savings["pv_battery_vs_none_p"] = round(annual_scenarios["no_pvbat"]["cost_p"] - annual_scenarios["without_predbat"]["cost_p"], 3)
            savings["predbat_vs_baseline_p"] = round(annual_scenarios["without_predbat"]["cost_p"] - annual_scenarios["with_predbat"]["cost_p"], 3)
        else:
            no_result_message = "No month produced a usable result, so no annual totals or savings could be calculated."
            if no_result_message not in self.caveats:
                self.caveats.append(no_result_message)

        total_kwp = sum(float(array.get("kwp", 0) or 0) for array in self.config.get("solar") or [])
        battery_kwh = float((self.config.get("battery") or {}).get("size_kwh", 0) or 0)
        costs = build_costs(total_kwp, battery_kwh, self.config["costs"])
        payback = build_payback(annual_scenarios, costs, len(included), self.config["costs"])
        if not payback.get("available"):
            payback_message = "Payback could not be calculated: {}".format(payback["reason"])
        else:
            payback_message = "Payback is simple payback - capital divided by the modelled annual saving. It ignores panel degradation, electricity price inflation, battery replacement and finance costs, so treat it as a comparison aid rather than a financial projection."
        if payback_message not in self.caveats:
            self.caveats.append(payback_message)

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
                "costs": costs,
                "payback": payback,
            },
            "caveats": self.caveats,
        }
