# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Test the data_age_days / data_age_required_days metrics used by the "Data Age" card on the
Metrics Dashboard. data_age_days is how many days of load history were actually retrieved
(depth, not staleness) - see fetch.py's "Found N load_today datapoints going back N days" log
line. The dashboard should only warn when the retrieved depth falls short of what the
configured days_previous actually needs, not on a flat threshold.
"""

from predbat_metrics import metrics


def test_data_age_metrics_round_trip(my_predbat):
    """
    data_age_days and data_age_required_days should round-trip through to_dict() with the
    values fetch_sensor_data() would set: load_minutes_age, and max(max_days_previous - 1, 0).
    """
    print("**** test_data_age_metrics_round_trip ****")

    m = metrics()

    # Mirrors fetch.py's fetch_sensor_data(): m.data_age_days.set(self.load_minutes_age) and
    # m.data_age_required_days.set(max(self.max_days_previous - 1, 0))
    my_predbat.load_minutes_age = 8
    my_predbat.max_days_previous = 8  # e.g. days_previous=[1, 7] -> max_days_previous = 8

    m.data_age_days.set(my_predbat.load_minutes_age)
    m.data_age_required_days.set(max(my_predbat.max_days_previous - 1, 0))

    data = m.to_dict()

    assert data["data_age_days"] == 8, f"Expected data_age_days=8, got {data['data_age_days']}"
    assert data["data_age_required_days"] == 7, f"Expected data_age_required_days=7 (max_days_previous - 1), got {data['data_age_required_days']}"
    print("✓ 8 days retrieved, 7 days required (days_previous up to 7) - dashboard should show OK")

    # Insufficient history: only 2 days retrieved but 7 needed - dashboard should warn
    my_predbat.load_minutes_age = 2
    m.data_age_days.set(my_predbat.load_minutes_age)

    data = m.to_dict()
    assert data["data_age_days"] < data["data_age_required_days"], "Shortfall in retrieved history should be visible as data_age_days < data_age_required_days"
    print("✓ 2 days retrieved, 7 required - shortfall correctly visible for the dashboard's warn condition")

    print("✓ Test passed: data_age_days/data_age_required_days round-trip correctly")
    return False
