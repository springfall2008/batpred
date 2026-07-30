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

# The dropdown's escape hatch: leaves the URL fields blank for a hand-entered tariff
CUSTOM_ID = "custom"

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

BUILTIN_TARIFFS = [
    {"id": "cap_seg", "name": "Price cap import / SEG export", "rates_import": [{"rate": 24.86}], "rates_export": [{"rate": 4.1}]},
    {
        "id": "eon_next_drive",
        "name": "Eon Next Drive import / Fixed export",
        "rates_import": [{"rate": 6.7, "start": "00:00:00", "end": "07:00:00"}, {"rate": 24.86, "start": "07:00:00", "end": "00:00:00"}],
        "rates_export": [{"rate": 16.5}],
    },
    {
        "id": "igo_fixed",
        "name": "Intelligent GO import / Fixed export",
        "import_octopus_url": "{}/INTELLI-VAR-24-10-29/electricity-tariffs/E-1R-INTELLI-VAR-24-10-29-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_FIXED,
    },
    {
        "id": "igo_prime",
        "name": "Intelligent GO import / Prime export",
        "import_octopus_url": "{}/INTELLI-VAR-24-10-29/electricity-tariffs/E-1R-INTELLI-VAR-24-10-29-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_PRIME,
    },
    {
        "id": "igo_agile",
        "name": "Intelligent GO import / Agile export",
        "import_octopus_url": "{}/INTELLI-VAR-24-10-29/electricity-tariffs/E-1R-INTELLI-VAR-24-10-29-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "go_fixed",
        "name": "GO import / Fixed export",
        "import_octopus_url": "{}/GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_FIXED,
    },
    {
        "id": "go_prime",
        "name": "GO import / Prime export",
        "import_octopus_url": "{}/GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_PRIME,
    },
    {
        "id": "go_agile",
        "name": "GO import / Agile export",
        "import_octopus_url": "{}/GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "agile_fixed",
        "name": "Agile import / Fixed export",
        "import_octopus_url": "{}/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_FIXED,
    },
    {
        "id": "agile_prime",
        "name": "Agile import / Prime export",
        "import_octopus_url": "{}/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_PRIME,
    },
    {
        "id": "agile_agile",
        "name": "Agile import / Agile export",
        "import_octopus_url": "{}/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "flux",
        "name": "Flux import / Flux export",
        "import_octopus_url": "{}/FLUX-IMPORT-23-02-14/electricity-tariffs/E-1R-FLUX-IMPORT-23-02-14-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
        "export_octopus_url": "{}/FLUX-EXPORT-23-02-14/electricity-tariffs/E-1R-FLUX-EXPORT-23-02-14-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
    },
    {
        "id": "cosy_fixed",
        "name": "Cosy import / Fixed export",
        "import_octopus_url": "{}/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_FIXED,
    },
    {
        "id": "cosy_prime",
        "name": "Cosy import / Prime export",
        "import_octopus_url": "{}/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_PRIME,
    },
    {
        "id": "cosy_agile",
        "name": "Cosy import / Agile export",
        "import_octopus_url": "{}/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "snug_fixed",
        "name": "Snug import / Fixed export",
        "import_octopus_url": "{}/SNUG-24-11-07/electricity-tariffs/E-1R-SNUG-24-11-07-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_FIXED,
    },
    {
        "id": "snug_prime",
        "name": "Snug import / Prime export",
        "import_octopus_url": "{}/SNUG-24-11-07/electricity-tariffs/E-1R-SNUG-24-11-07-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": _OUTGOING_PRIME,
    },
    {
        "id": "iflux",
        "name": "Intelligent Flux import / export",
        # There is no INTELLI-FLUX-EXPORT product - Octopus publishes both the import and
        # export rates for Intelligent Flux under the import product code below. Do not
        # "fix" this to a distinct export code; that product does not exist and 404s.
        "import_octopus_url": "{}/INTELLI-FLUX-IMPORT-23-07-14/electricity-tariffs/E-1R-INTELLI-FLUX-IMPORT-23-07-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/INTELLI-FLUX-IMPORT-23-07-14/electricity-tariffs/E-1R-INTELLI-FLUX-IMPORT-23-07-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
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


def merged_catalogue(compare_list=None):
    """Return the dropdown's entries: built-ins, then the user's own, then Custom.

    A user entry sharing a built-in id replaces it rather than appearing twice -
    the user's own definition is the more specific one. Malformed entries are
    skipped so one bad line in apps.yaml cannot empty the dropdown.
    """
    catalogue = [dict(entry) for entry in BUILTIN_TARIFFS]
    by_id = {entry["id"]: index for index, entry in enumerate(catalogue)}

    for entry in compare_list or []:
        converted = convert_compare_entry(entry)
        if converted is None:
            continue
        if converted["id"] in by_id:
            catalogue[by_id[converted["id"]]] = converted
        else:
            by_id[converted["id"]] = len(catalogue)
            catalogue.append(converted)

    catalogue.append({"id": CUSTOM_ID, "name": "Custom - enter URLs below"})
    return catalogue
