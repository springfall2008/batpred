# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import re


def test_fetch_tariffs(my_predbat):
    """
    Test the octopus_free_line function (used by fetch_tariffs related code)

    This function parses legacy octopus free electricity session data.

    Tests various scenarios:
    - Simple PM time slot
    - AM time slot
    - Mixed AM/PM slot
    - Invalid data
    - Missing regex groups
    """
    print("**** Running fetch_tariffs tests ****")
    failed = False

    # Test 1: Parse a simple PM time slot
    print("*** Test 1: Simple PM time slot (5-7pm)")
    free_sessions = []
    line = "Free Electricity: Monday 15th January 5-7pm"
    res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)

    my_predbat.octopus_free_line(res, free_sessions)

    if len(free_sessions) == 0:
        print("ERROR Test 1: No sessions created")
        failed = True
    elif len(free_sessions) != 1:
        print("ERROR Test 1: Expected 1 session, got {}".format(len(free_sessions)))
        failed = True
    else:
        session = free_sessions[0]
        if "start" not in session or "end" not in session or "rate" not in session:
            print("ERROR Test 1: Session missing required fields")
            failed = True
        elif session["rate"] != 0.0:
            print("ERROR Test 1: Expected rate 0.0, got {}".format(session["rate"]))
            failed = True

    # Test 2: Parse AM time slot
    print("*** Test 2: AM time slot (8-10am)")
    free_sessions = []
    line = "Free Electricity: Tuesday 3rd February 8-10am"
    res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)

    my_predbat.octopus_free_line(res, free_sessions)

    if len(free_sessions) != 1:
        print("ERROR Test 2: Expected 1 session, got {}".format(len(free_sessions)))
        failed = True
    elif free_sessions[0]["rate"] != 0.0:
        print("ERROR Test 2: Expected rate 0.0, got {}".format(free_sessions[0]["rate"]))
        failed = True

    # Test 3: Mixed AM/PM slot (11am-2pm)
    print("*** Test 3: Mixed AM/PM slot (11am-2pm)")
    free_sessions = []
    line = "Free Electricity: Wednesday 21st March 11am-2pm"
    res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)

    my_predbat.octopus_free_line(res, free_sessions)

    if len(free_sessions) != 1:
        print("ERROR Test 3: Expected 1 session, got {}".format(len(free_sessions)))
        failed = True

    # Test 4: No regex match (should not crash)
    print("*** Test 4: No regex match")
    free_sessions = []
    res = None

    my_predbat.octopus_free_line(res, free_sessions)

    if len(free_sessions) != 0:
        print("ERROR Test 4: Expected 0 sessions for None regex, got {}".format(len(free_sessions)))
        failed = True

    # Test 5: Invalid time format (should not crash)
    print("*** Test 5: Invalid time format")
    free_sessions = []
    line = "Free Electricity: Thursday 1st April invalid-time"
    res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)

    my_predbat.octopus_free_line(res, free_sessions)

    # Should handle gracefully, might be 0 or 1 session depending on error handling
    # Just check it doesn't crash

    # Test 6: Multiple sessions
    print("*** Test 6: Multiple sessions")
    free_sessions = []
    lines = [
        "Free Electricity: Monday 1st May 5-7pm",
        "Free Electricity: Tuesday 2nd May 6-8pm",
        "Free Electricity: Wednesday 3rd May 7-9pm",
    ]

    for line in lines:
        res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)
        my_predbat.octopus_free_line(res, free_sessions)

    if len(free_sessions) != 3:
        print("ERROR Test 6: Expected 3 sessions, got {}".format(len(free_sessions)))
        failed = True
    else:
        for i, session in enumerate(free_sessions):
            if session["rate"] != 0.0:
                print("ERROR Test 6: Session {} has rate {}, expected 0.0".format(i, session["rate"]))
                failed = True

    # Test 7: Verify session has start and end times
    print("*** Test 7: Session structure")
    free_sessions = []
    line = "Free Electricity: Friday 10th June 12-2pm"
    res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)

    my_predbat.octopus_free_line(res, free_sessions)

    if len(free_sessions) == 1:
        session = free_sessions[0]
        if "start" not in session:
            print("ERROR Test 7: Session missing 'start' field")
            failed = True
        elif "end" not in session:
            print("ERROR Test 7: Session missing 'end' field")
            failed = True
        elif not isinstance(session["start"], str):
            print("ERROR Test 7: Session start should be string, got {}".format(type(session["start"])))
            failed = True
        elif not isinstance(session["end"], str):
            print("ERROR Test 7: Session end should be string, got {}".format(type(session["end"])))
            failed = True

    if not failed:
        print("**** All fetch_tariffs tests PASSED ****")
    else:
        print("**** Some fetch_tariffs tests FAILED ****")

    return failed

    # Save original state
    old_tariffs = my_predbat.tariffs.copy() if hasattr(my_predbat, "tariffs") else {}

    # Test 1: Get import tariff
    print("*** Test 1: Get import tariff")
    my_predbat.tariffs = {
        "import": {
            "productCode": "AGILE-FLEX-22-11-25",
            "tariffCode": "E-1R-AGILE-FLEX-22-11-25-A",
            "deviceID": "12345678",
            "data": [
                {"value_inc_vat": 15.5, "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z"},
            ],
            "standing": [
                {"value_inc_vat": 45.0, "valid_from": "2025-01-01T00:00:00Z", "valid_to": None},
            ],
        }
    }

    tariff = my_predbat.get_tariff("import")

    if tariff is None:
        print("ERROR Test 1: Expected import tariff, got None")
        failed = True
    elif "productCode" not in tariff or tariff["productCode"] != "AGILE-FLEX-22-11-25":
        print("ERROR Test 1: Expected productCode 'AGILE-FLEX-22-11-25', got {}".format(tariff.get("productCode")))
        failed = True
    elif "tariffCode" not in tariff or tariff["tariffCode"] != "E-1R-AGILE-FLEX-22-11-25-A":
        print("ERROR Test 1: Expected tariffCode 'E-1R-AGILE-FLEX-22-11-25-A', got {}".format(tariff.get("tariffCode")))
        failed = True
    elif "data" not in tariff:
        print("ERROR Test 1: Expected 'data' field in tariff")
        failed = True
    elif "standing" not in tariff:
        print("ERROR Test 1: Expected 'standing' field in tariff")
        failed = True

    # Test 2: Get export tariff
    print("*** Test 2: Get export tariff")
    my_predbat.tariffs["export"] = {
        "productCode": "OUTGOING-FIX-12M-19-05-13",
        "tariffCode": "E-1R-OUTGOING-FIX-12M-19-05-13-A",
        "deviceID": "87654321",
        "data": [
            {"value_inc_vat": 5.5, "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z"},
        ],
        "standing": [
            {"value_inc_vat": 0.0, "valid_from": "2025-01-01T00:00:00Z", "valid_to": None},
        ],
    }

    tariff = my_predbat.get_tariff("export")

    if tariff is None:
        print("ERROR Test 2: Expected export tariff, got None")
        failed = True
    elif tariff.get("productCode") != "OUTGOING-FIX-12M-19-05-13":
        print("ERROR Test 2: Expected productCode 'OUTGOING-FIX-12M-19-05-13', got {}".format(tariff.get("productCode")))
        failed = True

    # Test 3: Get gas tariff
    print("*** Test 3: Get gas tariff")
    my_predbat.tariffs["gas"] = {
        "productCode": "VAR-22-11-01",
        "tariffCode": "G-1R-VAR-22-11-01-A",
        "deviceID": "11223344",
        "data": [
            {"value_inc_vat": 3.5, "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T23:59:59Z"},
        ],
        "standing": [
            {"value_inc_vat": 28.5, "valid_from": "2025-01-01T00:00:00Z", "valid_to": None},
        ],
    }

    tariff = my_predbat.get_tariff("gas")

    if tariff is None:
        print("ERROR Test 3: Expected gas tariff, got None")
        failed = True
    elif tariff.get("productCode") != "VAR-22-11-01":
        print("ERROR Test 3: Expected productCode 'VAR-22-11-01', got {}".format(tariff.get("productCode")))
        failed = True

    # Test 4: Get missing tariff type
    print("*** Test 4: Get non-existent tariff type")
    tariff = my_predbat.get_tariff("nonexistent")

    if tariff is not None:
        print("ERROR Test 4: Expected None for missing tariff, got {}".format(tariff))
        failed = True

    # Test 5: Verify all three tariffs can coexist
    print("*** Test 5: All tariffs coexist")
    import_tariff = my_predbat.get_tariff("import")
    export_tariff = my_predbat.get_tariff("export")
    gas_tariff = my_predbat.get_tariff("gas")

    if import_tariff is None or export_tariff is None or gas_tariff is None:
        print("ERROR Test 5: One or more tariffs is None")
        failed = True
    elif import_tariff.get("productCode") != "AGILE-FLEX-22-11-25":
        print("ERROR Test 5: Import tariff corrupted")
        failed = True
    elif export_tariff.get("productCode") != "OUTGOING-FIX-12M-19-05-13":
        print("ERROR Test 5: Export tariff corrupted")
        failed = True
    elif gas_tariff.get("productCode") != "VAR-22-11-01":
        print("ERROR Test 5: Gas tariff corrupted")
        failed = True

    # Test 6: Verify deviceID field
    print("*** Test 6: DeviceID field present")
    if import_tariff.get("deviceID") != "12345678":
        print("ERROR Test 6: Expected import deviceID '12345678', got {}".format(import_tariff.get("deviceID")))
        failed = True
    if export_tariff.get("deviceID") != "87654321":
        print("ERROR Test 6: Expected export deviceID '87654321', got {}".format(export_tariff.get("deviceID")))
        failed = True
    if gas_tariff.get("deviceID") != "11223344":
        print("ERROR Test 6: Expected gas deviceID '11223344', got {}".format(gas_tariff.get("deviceID")))
        failed = True

    # Test 7: Verify data structure
    print("*** Test 7: Tariff data structure")
    import_data = import_tariff.get("data", [])
    if not import_data or len(import_data) == 0:
        print("ERROR Test 7: Import tariff has no data")
        failed = True
    elif "value_inc_vat" not in import_data[0]:
        print("ERROR Test 7: Import data missing value_inc_vat")
        failed = True
    elif "valid_from" not in import_data[0]:
        print("ERROR Test 7: Import data missing valid_from")
        failed = True
    elif "valid_to" not in import_data[0]:
        print("ERROR Test 7: Import data missing valid_to")
        failed = True

    # Test 8: Verify standing charge structure
    print("*** Test 8: Standing charge structure")
    import_standing = import_tariff.get("standing", [])
    if not import_standing or len(import_standing) == 0:
        print("ERROR Test 8: Import tariff has no standing charge")
        failed = True
    elif "value_inc_vat" not in import_standing[0]:
        print("ERROR Test 8: Standing charge missing value_inc_vat")
        failed = True
    elif import_standing[0].get("valid_to") is not None:
        print("ERROR Test 8: Standing charge should have valid_to=None, got {}".format(import_standing[0].get("valid_to")))
        failed = True

    # Restore original state
    my_predbat.tariffs = old_tariffs

    if not failed:
        print("**** All fetch_tariffs tests PASSED ****")
    else:
        print("**** Some fetch_tariffs tests FAILED ****")

    return failed
