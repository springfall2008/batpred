import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from predbat import PredBat
from tests.test_infra import TestHAInterface


def create_mock_predbat():
    """
    Create a fresh mock Predbat instance using the standard unit test pattern.
    """
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = False
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.ha_interface.history_enable = True

    # Standard hardware configuration
    my_predbat.minutes_now = 0
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 10.0

    class DummyInverter:
        def __init__(self):
            self.inverter_limit = 5.0 / 60.0
            self.export_limit = 5.0 / 60.0

    my_predbat.inverters = [DummyInverter()]
    my_predbat.inverter_limit = 5.0 / 60.0
    my_predbat.export_limit = 5.0 / 60.0
    my_predbat.battery_rate_max_charge = 3600
    my_predbat.battery_rate_max_discharge = 3600
    my_predbat.pv_ac_limit = 0.0
    my_predbat.export_limits = []
    my_predbat.debug_enable = True
    my_predbat.args = {"threads": 0}

    # Enable optimization flags
    my_predbat.calculate_best = True
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.charge_threshold = 20.0
    my_predbat.export_threshold = 0.0

    # Standard pricing
    for m in range(24 * 60):
        my_predbat.rate_export[m] = 12.0
        my_predbat.load_minutes[m] = 0.0
        my_predbat.pv_forecast_minute[m] = 0.01  # Non-zero base
        my_predbat.pv_forecast_minuteCS[m] = 0.01
        my_predbat.rate_import[m] = 10.0
        my_predbat.rate_min_forward[m] = 10.0

    return my_predbat


def test_clipping_buffer_plan_inverter_injection():
    """
    Test that the calculate_plan method correctly injects an export window
    to force the inverter to route surplus solar into the battery buffer.
    """
    my_predbat = create_mock_predbat()
    my_predbat.minutes_now = 11 * 60  # Start just before peak

    # Fake PV forecast: 7kW flat during noon, but inverter is 5kW
    for m in range(720, 840):  # 12:00 to 14:00
        my_predbat.pv_forecast_minute[m] = 7.0 / 60.0
        my_predbat.pv_forecast_minuteCS[m] = 7.0 / 60.0

    # Enable clipping buffer (auto-calculated)
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_forecast = "pv_estimate"
    my_predbat.clipping_buffer_can_discharge = "Always"

    # Calculate clipping limits
    rem, c_start, c_end, _ = my_predbat.calculate_clipping_buffer()

    # Verify that the calculation correctly identified a need for a buffer
    assert rem > 0, "Clipping buffer should be > 0"
    assert c_start is not None and c_end is not None, "Clipping window should be identified"

    my_predbat.calculate_plan(recompute=False, publish=False)

    # Check that an export window was injected
    injected = False
    for e_win in my_predbat.export_window_best:
        if e_win.get("clipping", False):
            injected = True
    assert injected, "An export window should have been injected for the auto clipping buffer"


def test_clipping_financial_override_protects_buffer_on_normal_rates():
    """
    Test that at a -1p import rate, the grid profit does not outweigh
    the 12p solar value, so the clipping buffer is preserved.
    """
    pb = create_mock_predbat()
    pb.clipping_buffer_enable = True
    pb.clipping_buffer_forecast = "pv_clearsky"
    pb.clipping_buffer_can_discharge = "Cost Optimal"

    # 4% losses (0.96 efficiency)
    pb.inverter_loss = 0.96
    pb.battery_loss = 0.96
    pb.battery_loss_discharge = 0.96

    # Solar spike at 12:00
    for m in range(720, 750):
        pb.pv_forecast_minuteCS[m] = 7.0 / 60.0
        pb.pv_forecast_minute[m] = 7.0 / 60.0

    # -0.1p import rate overnight (above the threshold of -0.42p)
    # At this rate, saving solar is more profitable than a grid cycle
    for m in range(120, 150):
        pb.rate_import[m] = -0.1
        pb.rate_min_forward[m] = -0.1

    # Manually set the plan as if the optimizer had already run
    pb.charge_window_best = [{"start": 120, "end": 150, "average": -0.1}]
    pb.charge_limit_best = [100.0]

    rem, c_start, c_end, _ = pb.calculate_clipping_buffer()
    assert rem > 0
    print(f"DEBUG: Calculated buffer {rem}kWh from {c_start} to {c_end}")

    pb.calculate_plan(recompute=False, publish=False)

    # Check that the charge limit was CAPPED at soc_max - buffer
    target_kwh = pb.soc_max - rem
    target_percent = (target_kwh / pb.soc_max) * 100.0
    found_cap = False
    print(f"DEBUG: Charge limits best: {pb.charge_limit_best}")
    print(f"DEBUG: Target percent cap: {target_percent}")
    for limit in pb.charge_limit_best:
        if limit <= target_percent + 0.1:
            found_cap = True
    assert found_cap, f"Hard cap should be applied to protect buffer at -1p (Target SoC should be <= {target_percent}%)"


def test_clipping_financial_override_takes_grid_cash_on_extreme_rates():
    """
    Test that at a -15p import rate, the grid profit heavily outweighs
    the 12p solar value, so the clipping buffer is abandoned.
    """
    pb = create_mock_predbat()
    pb.clipping_buffer_enable = True
    pb.clipping_buffer_forecast = "pv_clearsky"
    pb.clipping_buffer_can_discharge = "Cost Optimal"

    # 4% losses
    pb.inverter_loss = 0.96
    pb.battery_loss = 0.96
    pb.battery_loss_discharge = 0.96

    # Solar spike at 12:00
    for m in range(720, 750):
        pb.pv_forecast_minuteCS[m] = 7.0 / 60.0
        pb.pv_forecast_minute[m] = 7.0 / 60.0

    # -15p import rate overnight
    for m in range(120, 150):
        pb.rate_import[m] = -15.0
        pb.rate_min_forward[m] = -15.0

    # Manually set the plan as if the optimizer had already run
    pb.charge_window_best = [{"start": 120, "end": 150, "average": -15.0}]
    pb.charge_limit_best = [100.0]

    rem, c_start, c_end, _ = pb.calculate_clipping_buffer()
    assert rem > 0

    pb.calculate_plan(recompute=False, publish=False)

    # Check that the charge limit was NOT capped (it should be 100% / soc_max)
    target_kwh = pb.soc_max - rem
    target_percent = (target_kwh / pb.soc_max) * 100.0
    capped = False
    for limit in pb.charge_limit_best:
        if limit <= target_percent + 0.1:
            capped = True
    assert not capped, "Hard cap should be RELAXED at -15p to capture grid profit"


if __name__ == "__main__":
    test_clipping_buffer_plan_inverter_injection()
    test_clipping_financial_override_protects_buffer_on_normal_rates()
    test_clipping_financial_override_takes_grid_cash_on_extreme_rates()
    print("Clipping plan tests passed!")
