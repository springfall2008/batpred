# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for the AgilePredict source of future rates (futurerate_agilepredict).

AgilePredict publishes a forecast of Octopus Agile prices, per region, in p/kWh, for up to 14 days.
These tests cover region detection, conversion of the forecast to minute data, which of
import/export are fetched, precedence of Nord Pool data over the forecast, and failure handling.
"""

from datetime import timedelta
from unittest.mock import patch

from futurerate import FutureRate, AGILEPREDICT_DEFAULT_URL
from tests.test_futurerate_auto import MockFutureRateBase, NORDPOOL_URL, IMPORT_ENTITY, EXPORT_ENTITY, _make_nordpool_data


def _make_agilepredict_data(base, start_minute, slots, price=10.0, step=0.0):
    """Return an AgilePredict API payload with *slots* half-hour prices starting *start_minute* after midnight UTC."""
    prices = []
    for slot in range(slots):
        t = base.midnight_utc + timedelta(minutes=start_minute + slot * 30)
        prices.append({"date_time": t.isoformat(), "agile_pred": price + slot * step, "agile_low": price - 1, "agile_high": price + 1})
    return [{"name": "Region | G", "created_at": base.midnight_utc.isoformat(), "prices": prices}]


def _make_base(adjust_import=True, adjust_export=False, tariff="E-1R-AGILE-24-10-01-G"):
    base = MockFutureRateBase()
    base.args["futurerate_agilepredict"] = True
    base.args["futurerate_adjust_import"] = adjust_import
    base.args["futurerate_adjust_export"] = adjust_export
    base.args["metric_octopus_import"] = IMPORT_ENTITY
    if tariff:
        base.set_entity_attr(IMPORT_ENTITY, "tariff_code", tariff)
    return base


def _test_region_from_tariff_code(my_predbat):
    base = _make_base()
    future = FutureRate(base)
    region = future.agilepredict_region()
    if region != "G":
        print("ERROR: expected region G, got {}".format(region))
        return True
    return False


def _test_region_from_export_tariff(my_predbat):
    base = _make_base(tariff=None)
    base.args["metric_octopus_export"] = EXPORT_ENTITY
    base.set_entity_attr(EXPORT_ENTITY, "tariff_code", "E-1R-AGILE-OUTGOING-19-05-13-n")
    future = FutureRate(base)
    region = future.agilepredict_region()
    if region != "N":
        print("ERROR: expected region N from the export tariff, got {}".format(region))
        return True
    return False


def _test_region_override(my_predbat):
    base = _make_base()
    base.args["futurerate_agilepredict_region"] = "c"
    future = FutureRate(base)
    region = future.agilepredict_region()
    if region != "C":
        print("ERROR: expected override region C, got {}".format(region))
        return True
    return False


def _test_region_invalid_skips_fetch(my_predbat):
    base = _make_base(tariff="FIX-12M-24-01-01")
    future = FutureRate(base)
    with patch.object(future, "download_agilepredict_data") as mock_dl:
        mi, me = future.futurerate_analysis({}, {})
    if mock_dl.called:
        print("ERROR: should not fetch AgilePredict without a region")
        return True
    if mi or me:
        print("ERROR: expected no future rates without a region")
        return True
    return False


def _test_minute_conversion(my_predbat):
    base = _make_base()
    future = FutureRate(base)
    payload = _make_agilepredict_data(base, 1380, 4, price=10.0, step=1.0)
    with patch.object(future, "download_agilepredict_data", return_value=payload):
        mi, me = future.futurerate_analysis({}, {})
    expected = {1380: 10.0, 1409: 10.0, 1410: 11.0, 1440: 12.0, 1470: 13.0, 1499: 13.0}
    for minute, value in expected.items():
        if mi.get(minute) != value:
            print("ERROR: minute {} expected {} got {}".format(minute, value, mi.get(minute)))
            return True
    if 1379 in mi or 1500 in mi:
        print("ERROR: rates outside the forecast should not be present")
        return True
    if me:
        print("ERROR: export should be empty when futurerate_adjust_export is off")
        return True
    return False


def _test_import_and_export_urls(my_predbat):
    base = _make_base(adjust_import=True, adjust_export=True)
    future = FutureRate(base)
    urls = []

    def fake(url):
        urls.append(url)
        return _make_agilepredict_data(base, 0, 2, price=5.0 if "export=true" in url else 20.0)

    with patch.object(future, "download_agilepredict_data", side_effect=fake):
        mi, me = future.futurerate_analysis({}, {})
    if len(urls) != 2:
        print("ERROR: expected 2 fetches, got {}".format(urls))
        return True
    if not urls[0].startswith(AGILEPREDICT_DEFAULT_URL + "G/") or "export=true" in urls[0]:
        print("ERROR: bad import url {}".format(urls[0]))
        return True
    if "export=true" not in urls[1]:
        print("ERROR: bad export url {}".format(urls[1]))
        return True
    if mi.get(0) != 20.0 or me.get(0) != 5.0:
        print("ERROR: import/export values wrong {} {}".format(mi.get(0), me.get(0)))
        return True
    return False


def _test_url_override_and_fixed_days(my_predbat):
    """futurerate_agilepredict_url is a URL, not an entity - it must be used as given. The days
    requested must not depend on the plan length, so every instance asks for the same URL."""
    from tests.test_futurerate_auto import MockFutureRateBase as _Base

    class StrictBase(_Base):
        def get_arg(self, arg, default=None, indirect=True):
            value = self.args.get(arg, default)
            if indirect and isinstance(value, str) and "." in value:
                return default  # what the real get_arg does with a dotted string it can't resolve
            return value

    urls = []
    for plan_hours in (24, 48):
        base = StrictBase()
        base.args.update({"futurerate_agilepredict": True, "futurerate_adjust_import": True, "futurerate_agilepredict_region": "G", "futurerate_agilepredict_url": "https://mirror.example/api"})
        base.forecast_plan_hours = plan_hours
        future = FutureRate(base)
        with patch.object(future, "download_agilepredict_data", side_effect=lambda url: urls.append(url) or None):
            future.futurerate_analysis({}, {})
    if len(urls) != 2 or not all(u.startswith("https://mirror.example/api/G/") for u in urls):
        print("ERROR: URL override not used: {}".format(urls))
        return True
    if urls[0] != urls[1]:
        print("ERROR: URL depends on plan length: {}".format(urls))
        return True
    return False


def _test_download_failure_does_not_flag_errors(my_predbat):
    """AgilePredict is optional: an outage falls back to copied rates, so it must not mark Predbat's status as errored."""
    base = _make_base()
    statuses = []
    base.record_status = lambda message, debug=None, had_errors=False: statuses.append(had_errors)
    future = FutureRate(base)
    with patch("futurerate.requests.get", side_effect=Exception("boom")):
        data = future.download_agilepredict_data_func(AGILEPREDICT_DEFAULT_URL + "G/")
    if data is not None or any(statuses):
        print("ERROR: failure should give None without had_errors, got {} {}".format(data, statuses))
        return True
    return False


def _test_disabled_does_not_fetch(my_predbat):
    base = _make_base()
    base.args["futurerate_agilepredict"] = False
    future = FutureRate(base)
    with patch.object(future, "download_agilepredict_data") as mock_dl:
        mi, me = future.futurerate_analysis({}, {})
    if mock_dl.called or mi or me:
        print("ERROR: AgilePredict should not be used when disabled")
        return True
    return False


def _test_no_adjust_flags_does_not_fetch(my_predbat):
    base = _make_base(adjust_import=False, adjust_export=False)
    future = FutureRate(base)
    with patch.object(future, "download_agilepredict_data") as mock_dl:
        future.futurerate_analysis({}, {})
    if mock_dl.called:
        print("ERROR: AgilePredict should not be fetched when neither import nor export is adjusted")
        return True
    return False


def _test_nordpool_takes_precedence(my_predbat):
    base = _make_base()
    base.args["futurerate_url"] = NORDPOOL_URL
    base.args["futurerate_peak_start"] = "00:00:00"
    base.args["futurerate_peak_end"] = "00:00:00"
    future = FutureRate(base)
    nord = _make_nordpool_data(base, hours=2)
    ap = _make_agilepredict_data(base, 0, 8, price=33.0)
    with patch.object(future, "download_futurerate_data", return_value=nord), patch.object(future, "download_agilepredict_data", return_value=ap):
        mi, _ = future.futurerate_analysis({}, {})
    if mi.get(30) == 33.0:
        print("ERROR: Nord Pool minute was overwritten by AgilePredict")
        return True
    if mi.get(180) != 33.0:
        print("ERROR: AgilePredict should fill beyond Nord Pool coverage, got {}".format(mi.get(180)))
        return True
    return False


def _test_download_failure_is_empty(my_predbat):
    base = _make_base()
    future = FutureRate(base)
    with patch.object(future, "download_agilepredict_data", return_value=None):
        mi, me = future.futurerate_analysis({}, {})
    if mi or me:
        print("ERROR: a failed download should give no future rates")
        return True
    return False


def _test_malformed_payload_is_empty(my_predbat):
    base = _make_base()
    future = FutureRate(base)
    for payload in [{"error": "x"}, [], [{"prices": [{"date_time": "garbage", "agile_pred": 1}]}], [{"prices": [{"date_time": base.midnight_utc.isoformat()}]}]]:
        with patch.object(future, "download_agilepredict_data", return_value=payload):
            mi, _ = future.futurerate_analysis({}, {})
        if mi:
            print("ERROR: malformed payload {} produced rates".format(payload))
            return True
    return False


class _FetchCachedStorage:
    def __init__(self):
        self.calls = []

    async def load(self, module, filename, format="yaml"):
        return None

    async def save(self, module, filename, data, format="yaml", expiry=None):
        return True

    async def fetch_cached(self, module, filename, fetch_fn, fresh_minutes=30, stale_minutes=35, format="yaml"):
        self.calls.append((module, filename, fresh_minutes))
        return await fetch_fn()


class _Components:
    def __init__(self, storage):
        self._storage = storage

    def get_component(self, name):
        return self._storage if name == "storage" else None


def _test_download_uses_shared_cache(my_predbat):
    base = _make_base()
    storage = _FetchCachedStorage()
    base.components = _Components(storage)
    future = FutureRate(base)
    payload = _make_agilepredict_data(base, 0, 2)
    with patch.object(future, "download_agilepredict_data_func", return_value=payload):
        data = future.download_agilepredict_data(AGILEPREDICT_DEFAULT_URL + "G/?days=3")
    if data != payload:
        print("ERROR: expected payload through fetch_cached")
        return True
    if len(storage.calls) != 1 or storage.calls[0][0] != "agilepredict":
        print("ERROR: expected one fetch_cached call under module agilepredict, got {}".format(storage.calls))
        return True
    return False


def _test_download_without_storage(my_predbat):
    base = _make_base()
    future = FutureRate(base)
    payload = _make_agilepredict_data(base, 0, 2)
    with patch.object(future, "download_agilepredict_data_func", return_value=payload) as mock_fn:
        data = future.download_agilepredict_data(AGILEPREDICT_DEFAULT_URL + "G/?days=3")
    if data != payload or mock_fn.call_count != 1:
        print("ERROR: expected a direct download without storage")
        return True
    return False


_SUBTESTS = [
    ("region_from_tariff_code", _test_region_from_tariff_code),
    ("region_from_export_tariff", _test_region_from_export_tariff),
    ("region_override", _test_region_override),
    ("region_invalid_skips_fetch", _test_region_invalid_skips_fetch),
    ("minute_conversion", _test_minute_conversion),
    ("import_and_export_urls", _test_import_and_export_urls),
    ("url_override_and_fixed_days", _test_url_override_and_fixed_days),
    ("download_failure_does_not_flag_errors", _test_download_failure_does_not_flag_errors),
    ("disabled_does_not_fetch", _test_disabled_does_not_fetch),
    ("no_adjust_flags_does_not_fetch", _test_no_adjust_flags_does_not_fetch),
    ("nordpool_takes_precedence", _test_nordpool_takes_precedence),
    ("download_failure_is_empty", _test_download_failure_is_empty),
    ("malformed_payload_is_empty", _test_malformed_payload_is_empty),
    ("download_uses_shared_cache", _test_download_uses_shared_cache),
    ("download_without_storage", _test_download_without_storage),
]


def test_futurerate_agilepredict(my_predbat=None):
    """Run all AgilePredict future rate tests."""
    print("**** Running futurerate AgilePredict tests ****")
    failed = False
    for name, fn in _SUBTESTS:
        result = fn(my_predbat)
        if result:
            print("  FAIL: {}".format(name))
            failed = True
        else:
            print("  PASS: {}".format(name))
    return failed
