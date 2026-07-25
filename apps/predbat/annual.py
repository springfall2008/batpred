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

from datetime import date

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
