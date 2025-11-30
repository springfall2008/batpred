# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio
from octopus import OctopusAPI
from utils import minute_data

def test_download_octopus_url_wrapper(my_predbat):
    """
    Wrapper to run the async test function
    """
    return asyncio.run(test_download_octopus_url(my_predbat))

async def test_download_octopus_url(my_predbat):
    """
    Test the download_octopus_url function
    """
    print("**** Running download_octopus_url tests ****")
    failed = False

    # Test URL for VAR-22-11-01 tariff
    test_url = "https://api.octopus.energy/v1/products/VAR-22-11-01/electricity-tariffs/E-2R-VAR-22-11-01-A/standard-unit-rates/"

    # Test the download function
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    # api.now_utc = my_predbat.now_utc
    rates_data = await api.async_download_octopus_url(test_url)

    # Basic validation checks
    if not rates_data:
        print("ERROR: No rate data downloaded from URL {}".format(test_url))
        failed = True
    else:
        print("Successfully downloaded {} rate points from VAR-22-11-01 tariff".format(len(rates_data)))
        pdata, ignore_io = minute_data(rates_data, my_predbat.forecast_days + 1, my_predbat.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        if len(pdata) < 24 * 60:
            print("ERROR: Expecting at least {} minutes of rate data got {}".format(24 * 60, len(pdata)))
            failed = True
        else:
            print("Successfully processed {} minutes of rate data".format(len(pdata)))
            night_rate = pdata.get(60)
            day_rate = pdata.get(600)
            if night_rate == day_rate:
                print("ERROR: Expecting different night and day rates got {} and {}".format(night_rate, day_rate))
                failed = True

    return failed