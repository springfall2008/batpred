# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from futurerate import FutureRate


def run_nordpool_test(my_predbat):
    """
    Test the compute metric function
    """

    print("**** Running Nordpool tests ****")
    my_predbat.args["futurerate_url"] = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices?date=DATE&market=N2EX_DayAhead&deliveryArea=UK&currency=GBP"
    my_predbat.args["futurerate_adjust_import"] = False
    my_predbat.args["futurerate_adjust_export"] = False
    my_predbat.args["futurerate_peak_start"] = "16:00:00"
    my_predbat.args["futurerate_peak_end"] = "19:00:00"
    my_predbat.args["futurerate_peak_premium_import"] = 14
    my_predbat.args["futurerate_peak_premium_export"] = 6.5
    my_predbat.args["futurerate_adjust_import"] = True
    my_predbat.args["futurerate_adjust_export"] = True
    failed = False

    fixed = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/OUTGOING-SEG-EO-FIX-12M-24-04-05/electricity-tariffs/E-1R-OUTGOING-SEG-EO-FIX-12M-24-04-05-A/standard-unit-rates/")
    if max(fixed.values()) <= 0:
        print("ERROR: Fixed rates can not be zero")
        failed = True
    if min(fixed.values()) != max(fixed.values()):
        print("ERROR: Fixed rates can not change")
        failed = True
    if len(fixed) > 6 * 24 * 60:
        print("ERROR: Fixed rates too long got {}".format(len(fixed)))
        failed = True

    # Obtain Agile octopus data
    rates_agile = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/")
    if not rates_agile:
        print("ERROR: No import rate data from Octopus url {}".format("https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/"))
        failed = True
    rates_agile_export = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/")
    if not rates_agile_export:
        print("ERROR: No export rate data from Octopus url {}".format("https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/"))
        failed = True
    print("Agile rates downloaded...")

    future = FutureRate(my_predbat)
    rate_import, rate_export = future.futurerate_analysis(rates_agile, rates_agile_export)
    if not rate_import:
        print("ERROR: No rate import data")
        return True
    if not rate_export:
        print("ERROR: No rate export data")
        return True

    future.download_futurerate_data_func = lambda x: ("empty")  # Mock the download function
    rate_import2, rate_export2 = future.futurerate_analysis(rates_agile, rates_agile_export)
    for key in rate_import:
        if rate_import[key] != rate_import2.get(key, None):
            print("ERROR: Rate import data not the same got {} vs {}".format(rate_import[key], rate_import2.get(key, None)))
            failed = True
            break
    for key in rate_export:
        if rate_export[key] != rate_export2.get(key, None):
            print("ERROR: Rate export data not the same got {} vs {}".format(rate_export[key], rate_export2.get(key, None)))
            failed = True
            break

    # Compute the minimum value in the hash, ignoring the keys
    min_import = min(rate_import.values())
    min_export = min(rate_export.values())
    max_import = max(rate_import.values())
    max_export = max(rate_export.values())

    if min_import == max_import:
        print("ERROR: Rate import data is flat")
        failed = True
    if min_export == max_export:
        print("ERROR: Rate import data is flat")
        failed = True
    if min_import < -15 or max_import > 100:
        print("ERROR: Rate import data out of range got min {} max {}".format(min_import, max_import))
        failed = True
    if min_export < 0 or max_export > 100:
        print("ERROR: Rate export data out of range got min {} max {}".format(min_export, max_export))
        failed = True

    # Compare Agile rates against Nordpool
    max_diff = 0
    rate_diff = 0
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile.get(minute, None)
        rate_nordpool = rate_import.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff = abs(rate_octopus - rate_nordpool)
            max_diff = max(max_diff, rate_diff)
            # print("Import: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, rate_diff))
    if max_diff > 10:
        print("ERROR: Rate import data difference too high")
        failed = True

    rate_diff_export = 0
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile_export.get(minute, None)
        rate_nordpool = rate_export.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff_export = abs(rate_octopus - rate_nordpool)
            max_diff = max(rate_diff_export, rate_diff)
            # print("Export: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, rate_diff))
    if rate_diff_export > 10:
        print("ERROR: Rate export data difference too high")
        failed = True

    return failed
