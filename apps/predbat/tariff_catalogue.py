# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Curated tariff catalogue for the Annual prediction tab's dropdown.

The entries mirror the commented-out ``compare_list`` template in
``config/apps.yaml`` so a user does not have to hand-copy a YAML block to get a
realistic tariff. Compare's key names differ from the annual engine's, so the
mapping lives here and nowhere else.
"""

# Compare writes rates_import_octopus_url; the annual engine reads import_octopus_url
COMPARE_KEY_MAP = {
    "rates_import_octopus_url": "import_octopus_url",
    "rates_export_octopus_url": "export_octopus_url",
}

# Keys that mean the same thing in both and need no translation
PASSTHROUGH_KEYS = ["rates_import", "rates_export"]

# The dropdown's escape hatch: leaves the URL fields blank for a hand-entered tariff.
# Import and export ids live in separate namespaces, so one constant serves both lists.
CUSTOM_ID = "custom"

# "I am not paid for what I export." Modelled as a flat 0p export rate rather than as a
# missing export tariff: an absent rate leaves the minutes unstamped, which reads to the
# rest of the engine as "unknown", whereas 0p is the actual fact being stated. Cost-wise
# this is also exactly right for a physical zero-export (G99) limitation - curtailed
# solar and unpaid exported solar both earn nothing - though the reported export kWh
# will still show the surplus leaving the house.
NO_EXPORT_ID = "no_export"

# What a household with no PV and no battery is assumed to be on. Named here so the form
# and the engine's own default cannot drift apart. The export side is inert for that
# scenario - no PV means nothing to export - but it is carried anyway so the baseline
# tariff has the same shape as any other.
BASELINE_DEFAULT_IMPORT_ID = "price_cap"
BASELINE_DEFAULT_EXPORT_ID = "seg"

# Ofgem price cap, 1 July - 30 September 2026, direct debit, England/Scotland/Wales,
# including VAT. Named rather than repeated so the next cap change is a one-line edit.
# Source: https://www.ofgem.gov.uk/information-consumers/energy-advice-households/energy-price-cap-unit-rates-and-standing-charges
PRICE_CAP_IMPORT_P = 26.11
PRICE_CAP_STANDING_CHARGE_P = 57.19

# A typical FIXED Smart Export Guarantee rate. Fixed SEG offers sit around 3-8p/kWh in
# mid-2026, so this is deliberately at the conservative end - the smart export tariffs
# that pay far more are separate entries in this catalogue, and a household on the price
# cap is not on one of them.
SEG_EXPORT_P = 4.1

_OCTOPUS = "https://api.octopus.energy/v1/products"

# The two Octopus flat-rate export products offered against each import tariff below.
# "Fixed" (OUTGOING-VAR) pays one rate around the clock. "Prime" is flat only in the sense
# that its rates are fixed for 12 months: it pays a materially higher rate over the evening
# peak (16:00-19:00 local), so it rewards holding charge for the evening rather than
# exporting whenever there is surplus. That makes it a genuinely different optimisation
# target, which is why it is offered alongside Fixed rather than replacing it.
#
# Prime launched in June 2026, so it has no rates for the historical year the annual tool
# replays. That is handled by AnnualTariff's current-rates fallback (annual_tariff.py) -
# without it, an empty export download is treated as "export unpaid" and the whole year
# silently prices export at zero. Do not add a new export product here without checking
# that its rates actually exist for the modelled year, or that the fallback covers it.
_OUTGOING_FIXED = "{}/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)
_OUTGOING_PRIME = "{}/OUTGOING-PRIME-FIX-12M-26-06-23/electricity-tariffs/E-1R-OUTGOING-PRIME-FIX-12M-26-06-23-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)

# There is no INTELLI-FLUX-EXPORT product - Octopus publishes both the import and the
# export rates for Intelligent Flux under the one import product code. Do not "fix" the
# export entry below to a distinct export code; that product does not exist and 404s.
_INTELLI_FLUX = "{}/INTELLI-FLUX-IMPORT-23-07-14/electricity-tariffs/E-1R-INTELLI-FLUX-IMPORT-23-07-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)

# EDF Kraken API base URL for EDF tariffs
_EDF = "https://api.edfgb-kraken.energy/v1/products"


# Import and export are chosen independently, so they are listed independently. They used
# to be one list of pre-paired combinations, which could not express a pairing nobody had
# thought to enumerate - and, because the pairs were matched back to the dropdown on their
# import URL alone, two pairs sharing an import (Agile/Fixed and Agile/Prime) were
# indistinguishable: reloading a saved config showed whichever came first in the list.
IMPORT_TARIFFS = [
    {"id": "price_cap", "name": "Price cap", "rates_import": [{"rate": PRICE_CAP_IMPORT_P}]},
    {
        "id": "eon_next_drive",
        "name": "Eon Next Drive",
        "rates_import": [{"rate": 6.7, "start": "00:00:00", "end": "07:00:00"}, {"rate": PRICE_CAP_IMPORT_P, "start": "07:00:00", "end": "00:00:00"}],
    },
    {"id": "agile", "name": "Octopus Agile", "import_octopus_url": "{}/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)},
    {"id": "intelligent_go", "name": "Octopus Intelligent GO", "import_octopus_url": "{}/INTELLI-VAR-24-10-29/electricity-tariffs/E-1R-INTELLI-VAR-24-10-29-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)},
    {"id": "go", "name": "Octopus GO", "import_octopus_url": "{}/GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)},
    {"id": "cosy", "name": "Octopus Cosy", "import_octopus_url": "{}/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{{dno_region}}/standard-unit-rates".format(_OCTOPUS)},
    {"id": "snug", "name": "Octopus Snug", "import_octopus_url": "{}/SNUG-24-11-07/electricity-tariffs/E-1R-SNUG-24-11-07-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)},
    {"id": "flux", "name": "Octopus Flux", "import_octopus_url": "{}/FLUX-IMPORT-23-02-14/electricity-tariffs/E-1R-FLUX-IMPORT-23-02-14-{{dno_region}}/standard-unit-rates".format(_OCTOPUS)},
    {"id": "intelligent_flux", "name": "Octopus Intelligent Flux", "import_octopus_url": _INTELLI_FLUX},
    {"id": "edf_go", "name": "EDF Go Electric", "import_octopus_url": "{}/EDF_EV_FIX_GOELEC_12M_HH/electricity-tariffs/E-1R-EDF_EV_FIX_GOELEC_12M_HH-{{dno_region}}/standard-unit-rates/".format(_EDF)},
    {
        "id": "edf_empower_fixed",
        "name": "EDF Empower Fixed",
        "rates_import": [
            {"rate": 10, "start": "00:00:00", "end": "03:00:00"},
            {"rate": 20, "start": "03:00:00", "end": "16:00:00"},
            {"rate": 30, "start": "16:00:00", "end": "19:00:00"},
            {"rate": 20, "start": "19:00:00", "end": "00:00:00"},
        ],
    },
]

EXPORT_TARIFFS = [
    {"id": NO_EXPORT_ID, "name": "No export payment", "rates_export": [{"rate": 0.0}]},
    {"id": "seg", "name": "SEG fixed rate ({}p)".format(SEG_EXPORT_P), "rates_export": [{"rate": SEG_EXPORT_P}]},
    {"id": "outgoing_fixed", "name": "Octopus Outgoing Fixed", "export_octopus_url": _OUTGOING_FIXED},
    {"id": "outgoing_prime", "name": "Octopus Outgoing Prime", "export_octopus_url": _OUTGOING_PRIME},
    {"id": "agile_outgoing", "name": "Octopus Agile Outgoing", "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS)},
    {"id": "flux_export", "name": "Octopus Flux export", "export_octopus_url": "{}/FLUX-EXPORT-23-02-14/electricity-tariffs/E-1R-FLUX-EXPORT-23-02-14-{{dno_region}}/standard-unit-rates".format(_OCTOPUS)},
    {"id": "intelligent_flux_export", "name": "Octopus Intelligent Flux export", "export_octopus_url": _INTELLI_FLUX},
    {"id": "eon_next_export", "name": "Eon Next export (16.5p)", "rates_export": [{"rate": 16.5}]},
    {"id": "edf_export_12m_v1", "name": "EDF Export 12m v1 (15p)", "rates_export": [{"rate": 15.0}]},
    {"id": "edf_export_12m_v2", "name": "EDF Export 12m v2 (13p)", "rates_export": [{"rate": 13.0}]},
    {"id": "edf_export_exclusive_12m_v3", "name": "EDF Export Exclusive 12m v3 (18p)", "rates_export": [{"rate": 18.0}]},
]


def convert_compare_entry(entry):
    """Convert one Compare ``compare_list`` entry into the annual engine's shape.

    Returns None when the entry cannot be used - it is not a mapping, it has no
    id, or it carries no rate source at all. Compare allows an entry with neither
    (the 'current' pseudo-tariff, which means "whatever is configured"), and that
    has no meaning here, so it is dropped rather than offered as a broken choice.
    """
    if not isinstance(entry, dict):
        return None
    if not entry.get("id") or not entry.get("name"):
        return None

    converted = {"id": entry["id"], "name": entry["name"]}
    for source_key, target_key in COMPARE_KEY_MAP.items():
        if entry.get(source_key):
            converted[target_key] = entry[source_key]
    for key in PASSTHROUGH_KEYS:
        if entry.get(key):
            converted[key] = entry[key]

    if not converted.get("import_octopus_url") and not converted.get("rates_import"):
        return None
    return converted


def _merged(builtins, keys, compare_list, custom_name):
    """Return one side's dropdown entries: built-ins, then the user's own, then Custom.

    ``keys`` are the rate-source keys belonging to this side; a user entry carrying
    none of them has nothing to offer this dropdown and is left out of it. That is how
    one paired ``compare_list`` entry lands in both lists, or in only the list it can
    actually supply - an import-only entry is not offered as an export tariff.

    A user entry sharing a built-in id replaces it rather than appearing twice: the
    user's own definition is the more specific one. Malformed entries are skipped so
    one bad line in apps.yaml cannot empty the dropdown.
    """
    catalogue = [dict(entry) for entry in builtins]
    by_id = {entry["id"]: index for index, entry in enumerate(catalogue)}

    for entry in compare_list or []:
        converted = convert_compare_entry(entry)
        if converted is None:
            continue
        side = {key: converted[key] for key in keys if converted.get(key)}
        if not side:
            continue
        side["id"] = converted["id"]
        side["name"] = converted["name"]
        if side["id"] in by_id:
            catalogue[by_id[side["id"]]] = side
        else:
            by_id[side["id"]] = len(catalogue)
            catalogue.append(side)

    catalogue.append({"id": CUSTOM_ID, "name": custom_name})
    return catalogue


def match_entry(catalogue, tariff, url_key, rates_key):
    """Return the catalogue entry one side of ``tariff`` was chosen from, or None.

    The single rule for mapping a stored tariff back to the choice that produced it.
    Both the configuration form (to re-select its dropdowns) and the compare table (to
    name a run's tariffs) go through here, so the two cannot describe the same tariff
    differently.

    Basic-rates entries are matched on their RATES, not a URL: several entries ("Price
    cap", "Eon Next Drive", "No export payment") carry no URL at all, and matching on
    URL alone identified none of them.

    Each side is matched against its OWN list. The single paired dropdown this catalogue
    replaced matched on the import URL alone, so "Agile / Prime" and "Agile / Fixed"
    were indistinguishable and a saved config reloaded as whichever came first.

    Returns None both for a side that is unset and for one that matches nothing - a
    hand-entered URL, or a ``compare_list`` entry the user has since removed. The Custom
    placeholder is never returned: it carries no rate source, so naming a tariff after
    it would describe nothing. Callers that want an id for the dropdown map None onto
    ``CUSTOM_ID`` themselves, which is a question about the form rather than about the
    catalogue.
    """
    tariff = tariff or {}
    current_url = tariff.get(url_key)
    current_rates = tariff.get(rates_key)
    if not current_url and not current_rates:
        return None
    for entry in catalogue or []:
        if current_url and entry.get(url_key) == current_url:
            return entry
        if not current_url and entry.get(rates_key) == current_rates:
            return entry
    return None


def merged_import_catalogue(compare_list=None):
    """Return the import dropdown's entries: built-ins, then the user's own, then Custom."""
    return _merged(IMPORT_TARIFFS, ["import_octopus_url", "rates_import"], compare_list, "Custom - enter URL below")


def merged_export_catalogue(compare_list=None):
    """Return the export dropdown's entries: built-ins, then the user's own, then Custom."""
    return _merged(EXPORT_TARIFFS, ["export_octopus_url", "rates_export"], compare_list, "Custom - enter URL below")
