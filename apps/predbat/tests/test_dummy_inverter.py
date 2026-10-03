# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the simulated inverter and battery (dummy_inverter.py)."""

from config import INVERTER_DEF
from dummy_inverter import DUMMY_CAPABILITIES, DUMMY_DEFAULTS, DummyInverter, in_window, simulate_minute
from mock_base import MockBase

IDLE = {"charge": {"enable": False}, "export": {"enable": False}, "reserve": 4}


def lossless(**overrides):
    """Default parameters with no losses, so the arithmetic in a test is exact."""
    params = dict(DUMMY_DEFAULTS)
    params.update({"battery_loss": 1.0, "battery_loss_discharge": 1.0, "inverter_loss": 1.0})
    params.update(overrides)
    return params


def close(value, expected, tolerance=1e-6):
    """True when value is within tolerance of expected."""
    return abs(value - expected) <= tolerance


def check(condition, message):
    """Print message and return 1 when condition is false, else 0."""
    if not condition:
        print("ERROR: " + message)
        return 1
    return 0


def test_eco():
    """Eco: surplus PV charges the battery, a shortfall is drawn from it, a full battery exports and then clips."""
    failed = 0
    soc, flows = simulate_minute(lossless(), IDLE, 5.0, 2.0, 0.5, 720)
    failed += check(close(flows["battery"], -1.5) and close(flows["grid"], 0.0) and close(soc, 5.0 + 1.5 / 60), "surplus PV should charge the battery: {} {}".format(soc, flows))
    soc, flows = simulate_minute(lossless(), IDLE, 5.0, 0.0, 0.8, 0)
    failed += check(close(flows["battery"], 0.8) and close(flows["grid"], 0.0), "the battery should cover the load: {}".format(flows))
    soc, flows = simulate_minute(lossless(), IDLE, 10.0, 7.0, 0.5, 720)
    # The 5 kW inverter limit binds first: its AC output serves the 0.5 kW load, so only 4.5 kW reaches the grid
    failed += check(close(flows["grid"], -4.5) and close(flows["clipped"], 2.0) and close(flows["pv"], 5.0), "a full battery should clip PV above the inverter limit: {}".format(flows))
    soc, flows = simulate_minute(lossless(export_limit=3000), IDLE, 10.0, 4.0, 0.5, 720)
    failed += check(close(flows["grid"], -3.0) and close(flows["clipped"], 0.5), "PV above the export limit should clip: {}".format(flows))
    soc, flows = simulate_minute(lossless(), IDLE, 0.4, 0.0, 0.8, 0)
    failed += check(close(flows["battery"], 0.0) and close(flows["grid"], 0.8), "the battery should not go below the reserve: {}".format(flows))
    return failed


def test_export_shares_the_inverter_limit():
    """During a forced export the battery only gets what PV leaves of the inverter's AC limit."""
    controls = {"charge": {"enable": False}, "export": {"enable": True, "start_time": "09:00:00", "end_time": "12:00:00", "target_soc": 20, "rate": 3600}, "reserve": 4}
    failed = 0
    soc, flows = simulate_minute(lossless(), controls, 8.0, 1.0, 0.4, 600)
    failed += check(close(flows["battery"], 3.6), "with little PV the export should run at its rate: {}".format(flows))
    soc, flows = simulate_minute(lossless(), controls, 8.0, 4.0, 0.4, 600)
    failed += check(close(flows["battery"], 1.0), "with 4 kW of PV only 1 kW of the 5 kW limit is left for the battery: {}".format(flows))
    soc, flows = simulate_minute(lossless(), controls, 8.0, 6.0, 0.4, 600)
    failed += check(close(flows["battery"], 0.0) and close(flows["clipped"], 1.0), "PV above the limit leaves nothing for the battery and clips: {}".format(flows))
    soc, flows = simulate_minute(lossless(), controls, 2.0, 0.0, 0.4, 600)
    failed += check(close(flows["battery"], 0.0), "the export should stop at its target SoC: {}".format(flows))
    controls["export"]["rate"] = 0
    soc, flows = simulate_minute(lossless(), controls, 8.0, 3.0, 0.4, 600)
    failed += check(close(flows["battery"], 0.0) and close(flows["grid"], -2.6), "a zero-rate export window freezes the battery and exports PV: {}".format(flows))
    return failed


def test_charge_window_and_losses():
    """A charge window charges from PV first then the grid, stops at its target, and pays the losses."""
    controls = {"charge": {"enable": True, "start_time": "23:30:00", "end_time": "05:30:00", "target_soc": 80, "rate": 3000}, "export": {"enable": False}, "reserve": 4}
    failed = 0
    soc, flows = simulate_minute(lossless(), controls, 5.0, 0.0, 0.5, 60)
    failed += check(close(flows["battery"], -3.0) and close(flows["grid"], 3.5), "grid should supply the charge and the load: {}".format(flows))
    soc, flows = simulate_minute(lossless(), controls, 8.0, 0.0, 0.5, 60)
    failed += check(close(flows["battery"], 0.0), "the charge should stop at its target: {}".format(flows))
    soc, flows = simulate_minute(DUMMY_DEFAULTS, controls, 5.0, 0.0, 0.5, 60)
    failed += check(close(soc, 5.0 + 3.0 / 60 * 0.96) and close(flows["grid"], 0.5 + 3.0 / 0.96), "charging should pay battery and inverter losses: {} {}".format(soc, flows))
    return failed


def test_in_window():
    """Windows are [start, end), may run past midnight, and an empty or unreadable window is never active."""
    cases = [(600, "09:00:00", "12:00:00", True), (720, "09:00:00", "12:00:00", False), (10, "23:30:00", "05:30:00", True), (720, "23:30:00", "05:30:00", False), (600, "10:00:00", "10:00:00", False), (600, "bad", "12:00:00", False)]
    for minute, start, end, expected in cases:
        if in_window(minute, start, end) != expected:
            print("ERROR: in_window({}, {}, {}) should be {}".format(minute, start, end, expected))
            return 1
    return 0


def test_component():
    """The component wires Predbat to its entities, obeys control writes, and simulates forwards."""
    base = MockBase()
    dummy = DummyInverter(base, config={"battery_size": 8, "soc_initial": 50, "load": 0.5, "pv_peak": 0})
    failed = 0
    dummy.automatic_config()
    failed += check(base.args.get("inverter_type") == ["DUMMY"], "automatic_config should select the DUMMY inverter type")
    failed += check(base.args.get("soc_kw") == ["sensor.predbat_dummy_battery_soc"], "soc_kw should point at the dummy SoC sensor: {}".format(base.args.get("soc_kw")))
    failed += check(base.args.get("discharge_target_soc") == ["number.predbat_dummy_export_target_soc"], "discharge_target_soc should point at the dummy control")

    failed += check(dummy.update_control("switch.predbat_dummy_export_enable", "turn_on") and dummy.controls["export"]["enable"], "a switch write should enable the export")
    dummy.update_control("select.predbat_dummy_export_start_time", "00:00:00")
    dummy.update_control("select.predbat_dummy_export_end_time", "23:59:00")
    dummy.update_control("number.predbat_dummy_export_target_soc", "25")
    failed += check(dummy.controls["export"]["target_soc"] == 25.0, "a number write should set the target")
    failed += check(not dummy.update_control("number.predbat_other_thing", 1), "a write to another entity should not be taken as a control")
    failed += check(dummy.update_control("select.predbat_dummy_charge_start_time", "nonsense") and dummy.controls["charge"]["start_time"] == "00:00:00", "an unreadable time should be ignored")

    dummy.step(1000)
    dummy.step(1060)
    failed += check(close(dummy.soc_kwh, 2.0, 0.01), "an hour of export should take 8 kWh at 50% down to its 25% target: {}".format(dummy.soc_kwh))
    failed += check(dummy.totals["export"] > 1.0, "the export should be counted: {}".format(dummy.totals))
    dummy.publish()
    failed += check(base.get_state_wrapper("sensor.predbat_dummy_battery_soc") == round(dummy.soc_kwh, 3), "the SoC sensor should be published")
    return failed


def test_inverter_def_matches():
    """INVERTER_DEF["DUMMY"] carries the capabilities the model implements."""
    row = INVERTER_DEF.get("DUMMY", {})
    for key, value in DUMMY_CAPABILITIES.items():
        if row.get(key, None) != value:
            print("ERROR: INVERTER_DEF['DUMMY'][{}] is {} but the model implements {}".format(key, row.get(key), value))
            return 1
    return 0


def run_dummy_inverter_tests(my_predbat):
    """Run every dummy inverter test, returning a non-zero count on failure."""
    failed = 0
    failed += test_eco()
    failed += test_export_shares_the_inverter_limit()
    failed += test_charge_window_and_losses()
    failed += test_in_window()
    failed += test_component()
    failed += test_inverter_def_matches()
    return failed
