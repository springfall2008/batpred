# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone, timedelta


def _set_time(my_predbat, minutes):
    """
    Move the mocked clock to the given number of minutes past midnight
    """
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=minutes)
    my_predbat.minutes_now = minutes


def run_test_manual_rates_history(my_predbat):
    """
    Manual import/export rate overrides must be retained for 24 hours after their slot so that
    today_cost() still prices already-consumed energy at the overridden rate, and the yesterday
    savings baseline (which shifts the rate table back a day) sees the same rate. This matches how
    Octopus saving sessions and Axle VPP sessions (which persist in the API session list) behave
    once their slot has passed.

    manual_soc / manual_load / manual_soc_max keep the old behaviour: past slots are dropped.
    """
    failed = False
    print("Test manual rates retained for slots earlier today")

    # Friday 19 Dec 2025, so weekday strings written back by manual_rates() are "Fri HH:MM"
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.args["plan_interval_minutes"] = 30
    my_predbat.args["manual_import_value"] = 0.0
    my_predbat.args["manual_export_value"] = 0.0
    my_predbat.args["manual_soc_value"] = 100

    for item in ("manual_import_rates", "manual_export_rates", "manual_soc"):
        my_predbat.manual_select(item, "off")

    # At 11:00 the user zero-rates 11:00 and 11:30
    _set_time(my_predbat, 660)
    my_predbat.manual_select("manual_import_rates", "11:00=0")
    my_predbat.manual_select("manual_import_rates", "11:30=0")
    my_predbat.manual_select("manual_soc", "11:00=50")

    rates = my_predbat.manual_rates("manual_import_rates", default_rate=0.0)
    if sorted(rates.keys()) != list(range(660, 720)):
        print("ERROR: T1 Expected import override on minutes 660-719 while slot is live, got {}".format(sorted(rates.keys())))
        failed = True
    else:
        print("PASS: T1 Import override present for 660-719 at 11:00")

    # Both slots have now passed (12:10). Previously they were pruned here.
    _set_time(my_predbat, 730)
    rates = my_predbat.manual_rates("manual_import_rates", default_rate=0.0)
    if sorted(rates.keys()) != list(range(660, 720)) or any(v != 0.0 for v in rates.values()):
        print("ERROR: T2 Expected import override retained on minutes 660-719 at 12:10, got {}".format(rates))
        failed = True
    else:
        print("PASS: T2 Import override retained after slot passed")

    # The stored selection must not have been pruned either, so the next cycle sees it too
    value = my_predbat.config_index["manual_import_rates"].get("value", "")
    if "2025-12-19 11:00=0.0" not in value or "2025-12-19 11:30=0.0" not in value:
        print("ERROR: T3 Expected stored selection to keep past slots, got {}".format(value))
        failed = True
    else:
        print("PASS: T3 Stored selection keeps past slots: {}".format(value))

    # It must flow through into the rate table used by today_cost()
    rate_import = {m: 27.0 for m in range(-24 * 60, 48 * 60)}
    replicated = {}
    rate_import = my_predbat.apply_manual_rates(rate_import, rates, is_import=True, rate_replicate=replicated)
    if rate_import[661] != 0.0 or rate_import[719] != 0.0 or rate_import[659] != 27.0 or rate_import[720] != 27.0:
        print("ERROR: T4 apply_manual_rates did not zero 660-719: 659={} 661={} 719={} 720={}".format(rate_import[659], rate_import[661], rate_import[719], rate_import[720]))
        failed = True
    elif replicated.get(700) != "manual":
        print("ERROR: T4 Expected rate_replicate tag 'manual' on past minute, got {}".format(replicated.get(700)))
        failed = True
    else:
        print("PASS: T4 Past override applied to rate_import")

    # Non-rate manual items keep the old drop-when-past behaviour
    soc = my_predbat.manual_rates("manual_soc", default_rate=100)
    if soc:
        print("ERROR: T5 Expected manual_soc past slot to be dropped, got {}".format(soc))
        failed = True
    else:
        print("PASS: T5 manual_soc still drops past slots")

    # The dropdown must still list future slots and offer 'off' plus the composite current value
    options = my_predbat.config_index["manual_import_rates"].get("options", [])
    if "off" not in options or not any(o.startswith("Fri 12:30") for o in options) or value not in options:
        print("ERROR: T6 Unexpected dropdown options {}".format(options[:5]))
        failed = True
    else:
        print("PASS: T6 Dropdown still offers future slots, the composite value and off")

    # The plan card clears a slot by weekday string; that must still remove a past (absolute-date) entry
    my_predbat.manual_select("manual_import_rates", "[Fri 11:00=0.0]")
    rates = my_predbat.manual_rates("manual_import_rates", default_rate=0.0)
    if sorted(rates.keys()) != list(range(690, 720)):
        print("ERROR: T6b Expected only 11:30 slot to remain after clearing 11:00, got {}".format(sorted(rates.keys())))
        failed = True
    else:
        print("PASS: T6b Clearing a past slot by weekday string removes the absolute-date entry")

    # Re-selecting the same past slot with a different rate replaces rather than duplicates
    my_predbat.manual_select("manual_import_rates", "Fri 11:30=5")
    rates = my_predbat.manual_rates("manual_import_rates", default_rate=0.0)
    value = my_predbat.config_index["manual_import_rates"].get("value", "")
    if value.count("11:30=") != 1 or rates.get(700) != 5.0:
        print("ERROR: T6c Expected single 11:30 entry at 5.0, got value={} rates[700]={}".format(value, rates.get(700)))
        failed = True
    else:
        print("PASS: T6c Re-selecting a past slot replaces the existing entry")

    # Export overrides follow the same path: select at 11:00 (a time-only selection made later
    # would resolve to tomorrow), then move past the slot
    _set_time(my_predbat, 660)
    my_predbat.manual_select("manual_export_rates", "11:00=20")
    _set_time(my_predbat, 760)
    export_rates = my_predbat.manual_rates("manual_export_rates", default_rate=0.0)
    export_value = my_predbat.config_index["manual_export_rates"].get("value", "")
    if sorted(export_rates.keys()) != list(range(660, 690)) or export_rates.get(670) != 20.0 or "2025-12-19 11:00=20.0" not in export_value:
        print("ERROR: T6d Expected export override retained on 660-689 at 20.0, got {} value={}".format(export_rates, export_value))
        failed = True
    else:
        print("PASS: T6d Export override retained after slot passed")

    # Retention covers yesterday too: after midnight the entries survive as negative minutes so the
    # savings baseline (rate table shifted back a day) still sees them
    my_predbat.midnight_utc = datetime(2025, 12, 20, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    _set_time(my_predbat, 10)
    rates = my_predbat.manual_rates("manual_import_rates", default_rate=0.0)
    value = my_predbat.config_index["manual_import_rates"].get("value", "")
    if sorted(rates.keys()) != list(range(690 - 1440, 720 - 1440)) or rates.get(700 - 1440) != 5.0 or "2025-12-19 11:30=5.0" not in value:
        print("ERROR: T7 Expected yesterday's 11:30 override retained at minutes {}-{}, got {} value={}".format(690 - 1440, 719 - 1440, rates, value))
        failed = True
    else:
        print("PASS: T7 Yesterday's override retained after midnight as negative minutes")

    # Yesterday's negative minutes must flow into the rate table at the keys the savings baseline reads
    rate_import = {m: 27.0 for m in range(-24 * 60, 48 * 60)}
    rate_import = my_predbat.apply_manual_rates(rate_import, rates, is_import=True, rate_replicate={})
    if rate_import[700 - 1440] != 5.0 or rate_import[689 - 1440] != 27.0 or rate_import[720 - 1440] != 27.0:
        print("ERROR: T7b apply_manual_rates did not apply yesterday's override: {} {} {}".format(rate_import[689 - 1440], rate_import[700 - 1440], rate_import[720 - 1440]))
        failed = True
    else:
        print("PASS: T7b Yesterday's override applied to rate_import at negative minutes")

    # But not the day before: once the slot is more than 24 hours old it is dropped
    my_predbat.midnight_utc = datetime(2025, 12, 21, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    _set_time(my_predbat, 10)
    rates = my_predbat.manual_rates("manual_import_rates", default_rate=0.0)
    export_rates = my_predbat.manual_rates("manual_export_rates", default_rate=0.0)
    if rates or export_rates:
        print("ERROR: T8 Expected overrides from two days ago to be dropped, got import={} export={}".format(rates, export_rates))
        failed = True
    else:
        print("PASS: T8 Overrides older than 24 hours dropped")

    # Clean up
    for item in ("manual_import_rates", "manual_export_rates", "manual_soc"):
        my_predbat.manual_select(item, "off")
    my_predbat.manual_soc_keep = {}
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    return failed
