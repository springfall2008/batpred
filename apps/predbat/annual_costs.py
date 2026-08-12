# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Install-cost and payback model for the annual prediction tool.

Pure arithmetic over plain dicts: no I/O, no Predbat state, no network. The engine
calls this once per run to turn a system size into an estimated capital cost, and a
year of modelled savings into a payback period.

The PV rate interpolates between published band medians rather than stepping between
them. A step function makes a 4.1 kWp system cost less in total than a 4.0 kWp one,
because it drops onto the cheaper band's rate for its whole size - an artefact of the
bucketing, not a real price. Interpolating between each band's midpoint keeps total
cost monotonic across the whole range.
"""

import math

# Published median install costs, GBP per kWp, for financial year 2025/26. Each is the
# median for systems within a size band, so it describes the typical system at that
# band's CENTRE - which is where it is anchored below.
DEFAULT_COSTS = {
    "battery_install_gbp": 500.0,
    "battery_per_kwh_gbp": 300.0,
    "pv_minimum_gbp": 2500.0,
    "pv_rate_small_gbp_per_kwp": 1780.0,  # band 0-4 kWp
    "pv_rate_medium_gbp_per_kwp": 1697.0,  # band 4-10 kWp
    "pv_rate_large_gbp_per_kwp": 1262.0,  # band 10-50 kWp
    # Predbat itself. Zero when self-hosted; the hosted version is expected to charge
    # around 100 a year. RECURRING, not capital - see payback_row().
    "predbat_annual_gbp": 0.0,
    # A real quote, which beats any model of one. Zero means "no quote" rather than "free"
    # - a genuine zero-pound quote is not a thing, and treating 0 as unset matches how
    # soc_max and the inverter limits are read elsewhere.
    #
    # Solar-only and solar-plus-battery, NOT solar and battery separately: a real quote is
    # almost always for the whole installation, and nobody is handed a battery-only price
    # to copy out. The battery is the remainder of the two, so a user with one whole-system
    # quote can enter it directly and still get a PV-only payback out of the modelled solar
    # figure, without doing any arithmetic themselves.
    "quoted_pv_gbp": 0.0,
    "quoted_total_gbp": 0.0,
}

# Accepted in a stored config but ignored: an earlier revision asked for a battery-only
# price here. Rejecting it outright would make a config saved against that revision fail
# to load, and silently reading it as a whole-system total would be worse - it would treat
# a battery price as if it covered the solar too, and understate the system.
_RETIRED_COST_KEYS = ("quoted_battery_gbp",)

# Midpoints of the 0-4, 4-10 and 10-50 kWp bands the rates above are medians of.
PV_RATE_ANCHORS_KWP = (2.0, 7.0, 30.0)

_RATE_KEYS = ("pv_rate_small_gbp_per_kwp", "pv_rate_medium_gbp_per_kwp", "pv_rate_large_gbp_per_kwp")


def resolve_costs(raw):
    """Return the cost settings, merging any overrides over the defaults.

    Every value must be a finite, non-negative number; anything else (including NaN
    and +/-Infinity, both valid YAML) raises ValueError naming the field, rather than
    silently falling back to a default and quietly costing the user's system at a
    price they did not ask for.
    """
    settings = dict(DEFAULT_COSTS)
    if not raw:
        return settings
    if not isinstance(raw, dict):
        raise ValueError("annual.costs must be a mapping")
    for key, value in raw.items():
        if key in _RETIRED_COST_KEYS:
            continue
        if key not in DEFAULT_COSTS:
            raise ValueError("annual.costs.{} is not a recognised cost setting".format(key))
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("annual.costs.{} must be a number, got {!r}".format(key, value))
        if not math.isfinite(number):
            raise ValueError("annual.costs.{} must be a number, got {!r}".format(key, value))
        if number < 0:
            raise ValueError("annual.costs.{} must not be negative, got {}".format(key, number))
        settings[key] = number
    return settings


def pv_rate_gbp_per_kwp(total_kwp, settings):
    """Return the GBP-per-kWp rate for a system of this size.

    Linear interpolation between the three band anchors, clamped flat beyond the first
    and last. Clamping rather than extrapolating keeps a very small or very large system
    on a published figure instead of a straight line run off the end of the data.
    """
    rates = [settings[key] for key in _RATE_KEYS]
    anchors = PV_RATE_ANCHORS_KWP
    size = float(total_kwp)
    if size <= anchors[0]:
        return rates[0]
    if size >= anchors[-1]:
        return rates[-1]
    for index in range(len(anchors) - 1):
        low, high = anchors[index], anchors[index + 1]
        if low <= size <= high:
            fraction = (size - low) / (high - low)
            return rates[index] + fraction * (rates[index + 1] - rates[index])
    return rates[-1]


def pv_cost_gbp(total_kwp, settings):
    """Return the estimated PV install cost, or zero when there is no PV.

    The minimum install price applies to a real system only. A system of no panels
    costs nothing - returning the minimum there would invent a cost for equipment the
    user does not have and make a no-PV scenario look expensive.
    """
    size = float(total_kwp or 0)
    if size <= 0:
        return 0.0
    return max(settings["pv_minimum_gbp"], size * pv_rate_gbp_per_kwp(size, settings))


def battery_cost_gbp(size_kwh, settings):
    """Return the estimated battery install cost, or zero when there is no battery."""
    size = float(size_kwh or 0)
    if size <= 0:
        return 0.0
    return settings["battery_install_gbp"] + settings["battery_per_kwh_gbp"] * size


def build_costs(total_kwp, battery_kwh, settings):
    """Return the capital cost breakdown for a system of this size."""
    # A quote beats the model, and the two quotes are independent: a whole-system price on
    # its own still leaves the PV-only payback resting on the modelled solar figure, and a
    # solar-only price on its own still leaves the battery modelled.
    #
    # The battery is always the REMAINDER of the two rather than a figure of its own,
    # because that is the shape real quotes come in - one price for the installation, and
    # sometimes a separate one for solar alone. Deriving it means a user with a single
    # whole-system quote does no arithmetic.
    quoted_pv = float(settings.get("quoted_pv_gbp", 0) or 0)
    quoted_total = float(settings.get("quoted_total_gbp", 0) or 0)
    pv = quoted_pv if quoted_pv > 0 else pv_cost_gbp(total_kwp, settings)
    if quoted_total > 0:
        # max(0, ...) guards a whole-system quote that comes in under the solar figure
        # beside it - a contradiction only the user can resolve, but one that must not
        # produce a negative battery cost and a total that disagrees with its own parts.
        battery = max(0.0, quoted_total - pv)
    else:
        battery = battery_cost_gbp(battery_kwh, settings)
    return {
        "pv_gbp": round(pv, 2),
        "battery_gbp": round(battery, 2),
        "total_gbp": round(pv + battery, 2),
        # So the UI can label a real quote as such rather than presenting it as an
        # estimate, and can leave the £/kWp note off a figure it does not describe.
        "pv_quoted": quoted_pv > 0,
        "battery_quoted": quoted_total > 0,
        "pv_rate_gbp_per_kwp": round(pv_rate_gbp_per_kwp(total_kwp or 0, settings), 2) if (total_kwp or 0) > 0 and quoted_pv <= 0 else 0.0,
        "total_kwp": round(float(total_kwp or 0), 3),
        "battery_kwh": round(float(battery_kwh or 0), 3),
    }


def payback_row(capital_gbp, annual_saving_gbp, recurring_gbp=0.0):
    """Return one payback row: capital divided by the net annual saving.

    ``recurring_gbp`` is an ongoing yearly cost (Predbat's own fee), so it is subtracted
    from the saving rather than added to capital. Adding it to capital would understate
    it enormously - over a ten year payback a 100 a year fee is 1000, not 100.

    A net saving of zero or less reports ``pays_back: False`` with no year count. A
    negative payback period is meaningless, and dividing by a near-zero saving produces
    a huge number that reads like an answer rather than the absence of one.
    """
    gross = float(annual_saving_gbp)
    net = gross - float(recurring_gbp or 0.0)
    row = {
        "capital_gbp": round(float(capital_gbp), 2),
        "gross_annual_saving_gbp": round(gross, 2),
        "annual_saving_gbp": round(net, 2),
        "predbat_annual_gbp": round(float(recurring_gbp or 0.0), 2),
    }
    if net <= 0:
        row["pays_back"] = False
        row["years"] = None
        return row
    row["pays_back"] = True
    row["years"] = round(float(capital_gbp) / net, 2)
    return row


def build_payback(annual_scenarios, costs, months_included, settings):
    """Return the payback block for a completed run, or a reason it could not be built.

    Payback needs a full year. When a month is unavailable the annual totals cover less
    than twelve months, so every saving is understated and every payback period
    correspondingly overstated. Extrapolating a partial year to a full one would invent
    savings for months the tool could not price, so this refuses instead and says which
    it had - matching how the rest of the tool declines to count an unavailable month.
    """
    if not annual_scenarios:
        return {"available": False, "reason": "No month produced a usable result, so there is nothing to pay back."}
    if months_included != 12:
        return {"available": False, "reason": "Payback needs a full year, but only {} of 12 months could be modelled. The missing months are named in the caveats.".format(months_included)}

    baseline = annual_scenarios.get("no_pvbat", {}).get("cost_p")
    if baseline is None:
        return {"available": False, "reason": "The no-PV/battery baseline is missing, so there is nothing to compare against."}

    # Every scenario the rows below need must actually be present with a numeric cost_p.
    # A missing scenario is a different situation to a zero saving, and must not be
    # silently treated as one - see saving_gbp() below, which indexes directly once this
    # has passed, rather than defaulting a missing key to the baseline.
    required = ("no_pvbat", "pv_only", "without_predbat", "with_predbat")
    missing = [key for key in required if not isinstance(annual_scenarios.get(key, {}).get("cost_p"), (int, float))]
    if missing:
        return {"available": False, "reason": "The following scenario(s) are missing from this run, so there is nothing to compare against: {}.".format(", ".join(missing))}

    def saving_gbp(key):
        """Return the annual saving in GBP of one scenario against the no-system baseline."""
        return (baseline - annual_scenarios[key]["cost_p"]) / 100.0

    return {
        "available": True,
        "pv_only": payback_row(costs["pv_gbp"], saving_gbp("pv_only")),
        "pv_battery": payback_row(costs["total_gbp"], saving_gbp("without_predbat")),
        "pv_battery_predbat": payback_row(costs["total_gbp"], saving_gbp("with_predbat"), recurring_gbp=settings["predbat_annual_gbp"]),
    }
