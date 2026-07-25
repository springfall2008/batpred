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

import os
from datetime import date

from const import MINUTE_WATT

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
# construction time; nothing here talks to Home Assistant.
MINIMAL_APPS_YAML = """pred_bat:
  module: predbat
  class: PredBat
  prefix: predbat
  timezone: {timezone}
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

# The default discharge power cap. A leaked full-precision value from a previous
# sample can flip a plan at a decision boundary, so it is reset explicitly.
DEFAULT_BATTERY_RATE_MAX_EXPORT = 0.0333


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
        self.service_store_enable = False
        self.service_store = []
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

    def get_service_store(self):
        """Return and clear the recorded service calls."""
        stored = self.service_store
        self.service_store = []
        return stored


def write_minimal_apps_yaml(work_dir, timezone):
    """Write the headless apps.yaml into ``work_dir`` and return its path."""
    os.makedirs(work_dir, exist_ok=True)
    path = os.path.join(work_dir, "apps.yaml")
    with open(path, "w") as handle:
        handle.write(MINIMAL_APPS_YAML.format(timezone=timezone))
    return path


def create_headless_predbat(work_dir, timezone, log):
    """Construct a PredBat instance with no Home Assistant connection.

    PredBat's Hass base class reads apps.yaml from ``$PREDBAT_APPS_FILE`` at
    construction time, so the environment variable is set before the import-time
    construction happens. The predbat import is deliberately local to this
    function so merely importing ``annual`` does not drag in the whole engine.
    """
    path = write_minimal_apps_yaml(work_dir, timezone)
    os.environ["PREDBAT_APPS_FILE"] = path

    import predbat

    instance = predbat.PredBat()
    instance.states = {}
    instance.reset()
    instance.update_time()
    instance.ha_interface = AnnualNullHA()
    instance.auto_config()
    instance.load_user_config()
    instance.fetch_config_options()
    instance.config_root = work_dir
    instance.save_restore_dir = work_dir
    instance.args["threads"] = 0
    instance.log = log
    return instance


def apply_hardware(predbat, battery, solar):
    """Map the config's battery block onto the PredBat instance.

    Rates are stored internally as kW per minute, matching
    ``Compare.apply_hardware_overrides()``. With no battery block the system is
    given zero capacity, which is how the no-battery scenario is expressed.
    """
    if battery is None:
        predbat.soc_max = 0.0
        predbat.soc_kw = 0.0
        predbat.battery_rate_max_charge = 0.0
        predbat.battery_rate_max_charge_dc = 0.0
        predbat.battery_rate_max_discharge = 0.0
        predbat.battery_rate_max_export = 0.0
        predbat.inverter_limit = (solar[0]["kwp"] if solar else 5.0) * 1000 / MINUTE_WATT
        predbat.export_limit = predbat.inverter_limit
        predbat.inverter_hybrid = False
        return

    predbat.soc_max = battery["size_kwh"]
    predbat.soc_kw = min(predbat.soc_kw, predbat.soc_max)
    predbat.inverter_limit = battery["inverter_kw"] * 1000 / MINUTE_WATT
    predbat.export_limit = battery["export_limit_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_charge = battery["charge_rate_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_charge_dc = predbat.battery_rate_max_charge
    predbat.battery_rate_max_discharge = battery["discharge_rate_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_export = predbat.battery_rate_max_discharge
    predbat.inverter_hybrid = battery["hybrid"]


def reset_sample_state(predbat):
    """Reset every field a previous sample could have left behind.

    Without this, a month's result silently depends on what ran before it: the
    numbers stay plausible while becoming order-dependent. The list covers the
    accumulators, the previous plan, the manual overrides, and the two fields
    ``tests/test_single_debug.py`` documents as leaking between debug cases.
    """
    predbat.dynamic_load_baseline = {}
    predbat.battery_rate_max_export = DEFAULT_BATTERY_RATE_MAX_EXPORT

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

    predbat.octopus_intelligent_charging = False
    predbat.load_forecast_only = True
    predbat.load_scaling = 1.0
    predbat.load_scaling10 = 1.0
    predbat.load_inday_adjustment = 1.0
    predbat.load_scaling_dynamic = None
    predbat.manual_load_adjust = {}
    predbat.iboost_enable = False
    predbat.carbon_enable = False
    predbat.plan_debug = False
    predbat.debug_enable = False
    predbat.rate_import_replicated = {}
    predbat.rate_export_replicated = {}
    predbat.savings_last_updated = None
