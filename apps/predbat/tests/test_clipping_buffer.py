# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from const import PREDICT_STEP


def run_clipping_buffer_tests(my_predbat):
    """
    Tests for the plan-side clipping buffer (#4036)
    """
    failed = False
    failed |= test_disabled_by_default(my_predbat)
    failed |= test_no_clipping_no_buffer(my_predbat)
    failed |= test_sustained_overshoot_sized(my_predbat)
    failed |= test_buffer_capped_by_max(my_predbat)
    failed |= test_buffer_raised_by_min(my_predbat)
    failed |= test_limit_override_wins(my_predbat)
    failed |= test_most_restrictive_limit_wins(my_predbat)
    failed |= test_buffer_released_after_the_peak(my_predbat)
    failed |= test_soc_max_ceiling_applied(my_predbat)
    failed |= test_no_limit_configured_is_inert(my_predbat)
    return failed


def setup(my_predbat, pv_kw_by_minute, enable=True, forecast="pv_estimate", inverter_limit_kw=5.5, export_limit_kw=5.5, min_kwh=0, max_kwh=0, override_w=0, soc_max=16.77, horizon=24 * 60):
    """Configure a predbat instance with a synthetic PV shape, in kW keyed by minute offset."""
    my_predbat.clipping_buffer_enable = enable
    my_predbat.clipping_buffer_forecast = forecast
    my_predbat.clipping_buffer_min_kwh = min_kwh
    my_predbat.clipping_buffer_max_kwh = max_kwh
    my_predbat.clipping_buffer_limit_override = override_w
    my_predbat.inverter_limit = inverter_limit_kw / 60.0
    my_predbat.export_limit = export_limit_kw / 60.0
    my_predbat.soc_max = soc_max
    my_predbat.battery_loss = 1.0
    my_predbat.forecast_minutes = horizon
    my_predbat.minutes_now = 0

    # The stepped curves are what the planner simulates and what the buffer sizes against, so the
    # tests build them directly - the shapes below are already the shape under test, with no cloud
    # modulation wanted on top.
    pv_step = {}
    for minute in range(0, horizon, PREDICT_STEP):
        pv_step[minute] = sum(pv_kw_by_minute(m) / 60.0 for m in range(minute, minute + PREDICT_STEP))
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute90_step = pv_step
    return my_predbat


def flat(kw, start, end):
    """A PV shape that sits at kw between start and end and is zero elsewhere."""

    def shape(minute):
        return kw if start <= minute < end else 0.0

    return shape


def check(name, got, expected, tolerance=0.05):
    """Compare a computed buffer against an expected kWh figure."""
    if abs(got - expected) > tolerance:
        print("**** ERROR: {} - got {} expected {}".format(name, got, expected))
        return True
    return False


def test_disabled_by_default(my_predbat):
    """With the feature off nothing is computed, whatever the forecast looks like."""
    setup(my_predbat, flat(9.0, 600, 900), enable=False)
    my_predbat.calculate_clipping_buffer()
    if my_predbat.clipping_buffer_forecast_kwh or my_predbat.clipping_buffer_kwh:
        print("**** ERROR: test_disabled_by_default - buffer computed while disabled")
        return True
    # And the ceiling helper must not restrict anything
    return check("test_disabled_by_default ceiling", my_predbat.clipping_buffer_soc_max(700), 16.77)


def test_no_clipping_no_buffer(my_predbat):
    """PV that stays under the limit needs no headroom."""
    setup(my_predbat, flat(4.0, 600, 900))
    my_predbat.calculate_clipping_buffer()
    if my_predbat.clipping_buffer_forecast_kwh:
        print("**** ERROR: test_no_clipping_no_buffer - buffer reserved with no clipping")
        return True
    return False


def test_sustained_overshoot_sized(my_predbat):
    """2 kW over the limit for two hours needs 4 kWh of headroom."""
    setup(my_predbat, flat(7.5, 600, 720))
    my_predbat.calculate_clipping_buffer()
    return check("test_sustained_overshoot_sized", my_predbat.clipping_buffer_kwh, 4.0)


def test_buffer_capped_by_max(my_predbat):
    """clipping_buffer_max_kwh caps what is held back."""
    setup(my_predbat, flat(7.5, 600, 720), max_kwh=1.5)
    my_predbat.calculate_clipping_buffer()
    return check("test_buffer_capped_by_max", my_predbat.clipping_buffer_kwh, 1.5)


def test_buffer_raised_by_min(my_predbat):
    """clipping_buffer_min_kwh raises a small requirement to the configured floor."""
    setup(my_predbat, flat(6.0, 600, 630), min_kwh=2.0)
    my_predbat.calculate_clipping_buffer()
    return check("test_buffer_raised_by_min", my_predbat.clipping_buffer_kwh, 2.0)


def test_limit_override_wins(my_predbat):
    """A configured override replaces the inverter and export limits."""
    setup(my_predbat, flat(7.5, 600, 720), override_w=7000)
    my_predbat.calculate_clipping_buffer()
    # Only 0.5 kW over a 7 kW override, for two hours
    return check("test_limit_override_wins", my_predbat.clipping_buffer_kwh, 1.0)


def test_most_restrictive_limit_wins(my_predbat):
    """The tighter of the inverter and export limits sets the clipping point."""
    setup(my_predbat, flat(7.5, 600, 720), inverter_limit_kw=7.5, export_limit_kw=5.5)
    my_predbat.calculate_clipping_buffer()
    return check("test_most_restrictive_limit_wins", my_predbat.clipping_buffer_kwh, 4.0)


def test_buffer_released_after_the_peak(my_predbat):
    """Headroom is only demanded ahead of the clipping, not after it."""
    setup(my_predbat, flat(7.5, 600, 720))
    my_predbat.calculate_clipping_buffer()
    failed = False
    # Before the clipping starts the full buffer is required
    failed |= check("test_buffer_released_after_the_peak before", my_predbat.soc_max - my_predbat.clipping_buffer_soc_max(300), 4.0)
    # Once it has passed nothing is held back
    failed |= check("test_buffer_released_after_the_peak after", my_predbat.soc_max - my_predbat.clipping_buffer_soc_max(900), 0.0)
    return failed


def test_soc_max_ceiling_applied(my_predbat):
    """A requirement larger than the battery is clamped to the battery, never negative."""
    setup(my_predbat, flat(9.0, 0, 24 * 60), soc_max=5.0)
    my_predbat.calculate_clipping_buffer()
    failed = check("test_soc_max_ceiling_applied buffer", my_predbat.clipping_buffer_kwh, 5.0)
    if my_predbat.clipping_buffer_soc_max(0) < 0:
        print("**** ERROR: test_soc_max_ceiling_applied - negative ceiling")
        failed = True
    return failed


def test_no_limit_configured_is_inert(my_predbat):
    """With no inverter or export limit there is nothing to clip against."""
    setup(my_predbat, flat(9.0, 600, 900), inverter_limit_kw=0, export_limit_kw=0)
    my_predbat.calculate_clipping_buffer()
    if my_predbat.clipping_buffer_forecast_kwh:
        print("**** ERROR: test_no_limit_configured_is_inert - buffer reserved with no limit")
        return True
    return False
