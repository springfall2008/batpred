# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def run_test_manual_api(my_predbat):
    failed = 0
    print("Test manual API")

    # Reset
    my_predbat.api_select("manual_api", "")
    original_limit = my_predbat.args["inverter_limit"]

    my_predbat.args["inverter_limit"] = [3600, 3500]
    my_predbat.args["inverter_limit_charge"] = [3600, 3600]
    limit = my_predbat.get_arg("inverter_limit", 0, index=0)
    if limit != 3600:
        print("ERROR: T1 Expecting inverter limit 0 to be 3600 got {}".format(limit))
        failed = 1
    limit = my_predbat.get_arg("inverter_limit", 0, index=1)
    if limit != 3500:
        print("ERROR: T2 Expecting inverter limit 0 to be 3500 got {}".format(limit))
        failed = 1
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T3 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit=1000")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")

    limit = my_predbat.get_arg("inverter_limit", 0, index=0)
    expected = 1000
    if limit != expected:
        print("ERROR: T4 Expecting inverter limit 0 to be {} got {}".format(expected, limit))
        failed = 1

    limit = my_predbat.get_arg("inverter_limit", 0, index=1)
    expected = 3500
    if limit != expected:
        print("ERROR: T5 Expecting inverter limit 0 to be {} got {}".format(expected, limit))
        failed = 1

    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [1000, 3500]
    if limits != expected:
        print("ERROR: T6 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "[inverter_limit=1000]")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T7 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(1)=1000")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 1000]
    if limits != expected:
        print("ERROR: T8 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(0)=900")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [900, 1000]
    if limits != expected:
        print("ERROR: T8 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(0)=800")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [800, 1000]
    if limits != expected:
        print("ERROR: T9 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "off")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T3 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.args["inverter_limit"] = original_limit
    my_predbat.args["rates_export_override"] = []

    export_override = my_predbat.get_arg("rates_export_override", [])
    if export_override != []:
        print("ERROR: T10 Expecting rate export override to be {} got {}".format([], export_override))
        failed = 1

    my_predbat.api_select("manual_api", "rates_export_override?start=17:00:00&end=19:00:00&rate=0")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = [{"start": "17:00:00", "end": "19:00:00", "rate": "0"}]
    if export_override != expected:
        print("ERROR: T11 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "rates_export_override(1)?start=12:00:00&end=13:00:00&rate=2")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = [{"start": "17:00:00", "end": "19:00:00", "rate": "0"}, {"start": "12:00:00", "end": "13:00:00", "rate": "2"}]
    if export_override != expected:
        print("ERROR: T12 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "off")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = []
    if export_override != expected:
        print("ERROR: T13 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit_charge(0)=800")
    my_predbat.api_select("manual_api", "inverter_limit_charge(1)=400")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit_charge", [])
    expected = [800, 400]
    if limits != expected:
        print("ERROR: T14 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1
    limit0 = my_predbat.get_arg("inverter_limit_charge", index=0, default=0)
    if limit0 != 800:
        print("ERROR: T15 Expecting inverter limit 0 to be {} got {}".format(800, limit0))
        failed = 1
    limit1 = my_predbat.get_arg("inverter_limit_charge", index=1, default=0)
    if limit1 != 400:
        print("ERROR: T16 Expecting inverter limit 1 to be {} got {}".format(400, limit1))
        failed = 1

    del my_predbat.args["inverter_limit_charge"]

    my_predbat.api_select("manual_api", "off")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")

    return failed
