# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

# Everything battery_value_rate reads - snapshotted and restored around each test that pokes at it
VALUE_RATE_STATE = (
    "rate_min_forward",
    "rate_min",
    "rate_max",
    "rate_max_base",
    "inverter_loss",
    "battery_loss",
    "battery_loss_discharge",
    "rate_export_min",
    "rate_export_max",
    "rate_export_max_forward",
    "metric_battery_cycle",
    "metric_battery_value_export_scaling",
)


def compute_metric_test(
    my_predbat,
    name,
    end_record=None,
    soc=0,
    soc10=0,
    soc90=0,
    cost=0,
    cost10=0,
    cost90=None,
    final_iboost=0,
    final_iboost10=0,
    battery_cycle=0,
    metric_keep=0,
    final_carbon_g=0,
    import_kwh_battery=0,
    import_kwh_house=0,
    export_kwh=0,
    assert_metric=0,
    battery_value_scaling=1.0,
    rate_export_min=1.0,
    rate_export_max=0.0,
    metric_battery_value_export_scaling=1.0,
    iboost_value_scaling=1.0,
    inverter_loss=1.0,
    battery_loss=1.0,
    metric_battery_cycle=0.0,
    pv_metric10_weight=0.0,
    pv_metric90_weight=0.0,
    battery_loss_discharge=1.0,
    metric_self_sufficiency=0.0,
    carbon_metric=0.0,
    rate_min=1.0,
    rate_max=99.0,
):
    """
    Test the compute metric function
    """
    my_predbat.metric_battery_value_scaling = battery_value_scaling
    my_predbat.rate_export_min = rate_export_min
    my_predbat.rate_export_max = rate_export_max
    my_predbat.metric_battery_value_export_scaling = metric_battery_value_export_scaling
    my_predbat.iboost_value_scaling = iboost_value_scaling
    my_predbat.inverter_loss = inverter_loss
    my_predbat.battery_loss = battery_loss
    my_predbat.metric_battery_cycle = metric_battery_cycle
    my_predbat.pv_metric10_weight = pv_metric10_weight
    my_predbat.pv_metric90_weight = pv_metric90_weight
    my_predbat.battery_loss_discharge = battery_loss_discharge
    my_predbat.metric_self_sufficiency = metric_self_sufficiency
    my_predbat.rate_min = rate_min
    my_predbat.rate_max = rate_max
    # battery_value_rate prefers the session-free base rate for its ceiling, so pin it alongside
    # rate_max or a value left behind by an earlier test would decide these metrics instead.
    my_predbat.rate_max_base = rate_max
    # Empty means "no forward export data", which sends battery_value_rate back to rate_export_max
    my_predbat.rate_export_max_forward = {}
    if not end_record:
        end_record = my_predbat.forecast_minutes

    my_predbat.rate_min_forward = {n: rate_min for n in range(my_predbat.forecast_minutes + my_predbat.minutes_now + end_record + 1)}
    if carbon_metric:
        my_predbat.carbon_enable = True
        my_predbat.carbon_metric = carbon_metric
    else:
        my_predbat.carbon_enable = False
        my_predbat.carbon_metric = 99

    print("Metric Test {}".format(name))

    metric, battery_value = my_predbat.compute_metric(
        end_record,
        soc,
        soc10,
        cost,
        cost10,
        final_iboost,
        final_iboost10,
        battery_cycle,
        metric_keep,
        final_carbon_g,
        import_kwh_battery,
        import_kwh_house,
        export_kwh,
        soc90=soc90,
        cost90=cost90,
    )
    if abs(metric - assert_metric) > 0.1:
        print("ERROR: Test {} Metric {} should be {}".format(name, metric, assert_metric))
        return True
    return False


def save_state(my_predbat):
    state_dict = {}
    save_items = [
        "metric_battery_value_scaling",
        "rate_export_min",
        "rate_export_max",
        "metric_battery_value_export_scaling",
        "iboost_value_scaling",
        "inverter_loss",
        "battery_loss",
        "metric_battery_cycle",
        "pv_metric10_weight",
        "pv_metric90_weight",
        "battery_loss_discharge",
        "metric_self_sufficiency",
        "rate_min",
        "rate_max",
        "rate_max_base",
        "carbon_enable",
        "carbon_metric",
        "rate_min_forward",
        "rate_export_max_forward",
    ]
    for item in save_items:
        state_dict[item] = getattr(my_predbat, item)
    return state_dict


def restore_state(my_predbat, state_dict):
    for item, value in state_dict.items():
        setattr(my_predbat, item, value)


def battery_value_rate_test(my_predbat):
    """Pin battery_value_rate, the single definition shared by the metric, the dashboard and savings.

    The rate_max ceiling is the case worth pinning: output.py's copy of this formula omitted it, so on
    a flat tariff the savings report valued the battery 15.3% above what the planner used. Anyone
    re-splitting the formula would reintroduce that divergence, and only the ceiling case catches it.
    """
    failed = False
    saved = {name: getattr(my_predbat, name) for name in VALUE_RATE_STATE}
    try:
        my_predbat.inverter_loss = 0.96
        my_predbat.battery_loss = 0.97
        my_predbat.battery_loss_discharge = 0.97
        my_predbat.metric_battery_cycle = 0.0
        my_predbat.rate_export_min = 0.0
        my_predbat.rate_export_max = 6.9
        my_predbat.rate_export_max_forward = {}
        my_predbat.metric_battery_value_export_scaling = 1.0
        my_predbat.rate_min = 6.9

        # Normal spread: the gross-up applies and the ceiling does not bind
        my_predbat.rate_max = my_predbat.rate_max_base = 28.85
        my_predbat.rate_min_forward = {10: 6.9}
        expected = 6.9 / 0.96 / 0.97
        got = my_predbat.battery_value_rate(10)
        if abs(got - expected) > 1e-6:
            print("ERROR: battery_value_rate is {}, expected the grossed-up {}".format(got, expected))
            failed = True

        # Flat tariff: rate_min_forward == rate_max, so the ceiling MUST bind and pull the value
        # below the gross-up. Without the ceiling this returns 1.0739*R instead of 0.9312*R.
        my_predbat.rate_max = my_predbat.rate_max_base = 6.9
        expected_capped = 6.9 * 0.96 * 0.97
        got = my_predbat.battery_value_rate(10)
        if abs(got - expected_capped) > 1e-6:
            print("ERROR: battery_value_rate is {} on a flat tariff, expected the capped {} - the rate_max ceiling is not being applied".format(got, expected_capped))
            failed = True

        # The 1p floor applies when the forward rate is negative (plunge pricing)
        my_predbat.rate_max = my_predbat.rate_max_base = 28.85
        my_predbat.rate_min_forward = {10: -5.0}
        got = my_predbat.battery_value_rate(10)
        if abs(got - 1.0) > 1e-6:
            print("ERROR: battery_value_rate is {} at a negative forward rate, expected the 1.0 floor".format(got))
            failed = True
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed


def battery_value_rate_ceiling_base_test(my_predbat):
    """Pin the ceiling to the base import tariff, not the session-inflated one.

    load_saving_slot writes the session price into the import rates too, so rate_max can be a one-off
    event price rather than the tariff's real peak. Ceiling on that and a stored kWh gets credited far
    above anything the tariff will ever pay for it, which is what makes freeze charge look profitable
    on a flat tariff. rate_max_base is captured before the session is layered on.
    """
    failed = False
    saved = {name: getattr(my_predbat, name) for name in VALUE_RATE_STATE}
    try:
        my_predbat.inverter_loss = 0.96
        my_predbat.battery_loss = 0.97
        my_predbat.battery_loss_discharge = 0.97
        my_predbat.metric_battery_cycle = 0.0
        my_predbat.rate_export_min = 0.0
        my_predbat.rate_export_max = 0.0
        my_predbat.rate_export_max_forward = {}
        my_predbat.metric_battery_value_export_scaling = 1.0
        my_predbat.rate_min = 29.2
        my_predbat.rate_min_forward = {10: 29.2}

        # A 100p saving session sets rate_max, but the real tariff is flat at 29.2p. The ceiling must
        # come from the base rate, so the value stays at the flat tariff's 0.9312*R.
        my_predbat.rate_max = 100.0
        my_predbat.rate_max_base = 29.2
        expected = 29.2 * 0.96 * 0.97
        got = my_predbat.battery_value_rate(10)
        if abs(got - expected) > 1e-6:
            print("ERROR: battery_value_rate is {} with a session-inflated rate_max, expected the base-capped {}".format(got, expected))
            failed = True

        # Unset base (an older debug file being replayed) falls back to rate_max so nothing regresses
        my_predbat.rate_max = 31.2
        my_predbat.rate_max_base = 0
        expected = 31.2 * 0.96 * 0.97
        got = my_predbat.battery_value_rate(10)
        if abs(got - expected) > 1e-6:
            print("ERROR: battery_value_rate is {} with rate_max_base unset, expected the rate_max fallback {}".format(got, expected))
            failed = True
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed


def battery_value_export_scaling_test(my_predbat):
    """Pin the export-risk haircut: full value when export can recover the import cost, discounted when it cannot.

    The two cases that matter are the extremes. A system that cannot sell surplus at all must take the
    full haircut, and a system whose export matches the cheapest import must take none - otherwise the
    haircut fires on ordinary tariffs, where export is always somewhat below import, and restructures
    plans that have no export problem at all.
    """
    failed = False
    saved = {name: getattr(my_predbat, name) for name in VALUE_RATE_STATE}
    try:
        my_predbat.inverter_loss = 0.96
        my_predbat.battery_loss = 0.97
        my_predbat.battery_loss_discharge = 0.97
        my_predbat.metric_battery_cycle = 0.0
        my_predbat.rate_export_min = 0.0
        my_predbat.rate_min = 6.9
        my_predbat.rate_max = my_predbat.rate_max_base = 28.85
        my_predbat.rate_min_forward = {10: 6.9}
        my_predbat.rate_export_max_forward = {}
        my_predbat.metric_battery_value_export_scaling = 0.8
        full = 6.9 / 0.96 / 0.97

        for export_max, recovery, label in ((0.0, 0.0, "no export at all"), (6.9, 1.0, "export matches the cheapest import"), (3.45, 0.5, "export recovers half")):
            my_predbat.rate_export_max = export_max
            expected = full * (0.8 + 0.2 * recovery)
            got = my_predbat.battery_value_rate(10)
            if abs(got - expected) > 1e-6:
                print("ERROR: battery_value_rate is {} with {} (export_max {}), expected {}".format(got, label, export_max, expected))
                failed = True

        # Export above the cheapest import must not push the value ABOVE replacement cost
        my_predbat.rate_export_max = 50.0
        got = my_predbat.battery_value_rate(10)
        if abs(got - full) > 1e-6:
            print("ERROR: battery_value_rate is {} when export beats import, expected it capped at replacement cost {}".format(got, full))
            failed = True

        # Setting the option to 1.0 must disable the haircut entirely, even with zero export
        my_predbat.rate_export_max = 0.0
        my_predbat.metric_battery_value_export_scaling = 1.0
        got = my_predbat.battery_value_rate(10)
        if abs(got - full) > 1e-6:
            print("ERROR: battery_value_rate is {} with the export scaling disabled at 1.0, expected the undiscounted {}".format(got, full))
            failed = True
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed


def battery_value_export_forward_test(my_predbat):
    """Pin the export-recovery ratio to the forward base export tariff.

    The haircut asks "can I sell surplus?", which is a property of the tariff. Answering it with
    rate_export_max - the highest export price anywhere in the horizon, saving sessions included -
    let a single 2-hour 100p session switch the haircut off for a whole 48-hour plan on a system whose
    export tariff pays nothing. The value then sat above what a stored kWh can realise, and the planner
    freeze charged every window up to end_record because holding energy scored better than using it.

    Scoping to the forward base tariff fixes both halves: sessions never enter it, and a session that
    has already passed cannot keep counting.
    """
    failed = False
    saved = {name: getattr(my_predbat, name) for name in VALUE_RATE_STATE}
    try:
        my_predbat.inverter_loss = 0.96
        my_predbat.battery_loss = 0.97
        my_predbat.battery_loss_discharge = 0.97
        my_predbat.metric_battery_cycle = 0.0
        my_predbat.rate_export_min = 0.0
        my_predbat.rate_min = 6.9
        my_predbat.rate_max = my_predbat.rate_max_base = 28.85
        my_predbat.rate_min_forward = {10: 6.9}
        my_predbat.metric_battery_value_export_scaling = 0.8
        full = 6.9 / 0.96 / 0.97

        # A saving session pushes rate_export_max to 100p, but the export tariff itself pays nothing,
        # so the forward base view is 0 and the full haircut must still apply.
        my_predbat.rate_export_max = 100.0
        my_predbat.rate_export_max_forward = {10: 0.0}
        expected = full * 0.8
        got = my_predbat.battery_value_rate(10)
        if abs(got - expected) > 1e-6:
            print("ERROR: battery_value_rate is {} with a saving session in rate_export_max, expected the haircut {} - a one-off event is standing in for the tariff".format(got, expected))
            failed = True

        # Base tariff that genuinely recovers half the import cost takes half the haircut
        my_predbat.rate_export_max_forward = {10: 3.45}
        expected = full * 0.9
        got = my_predbat.battery_value_rate(10)
        if abs(got - expected) > 1e-6:
            print("ERROR: battery_value_rate is {} with a base export of 3.45, expected {}".format(got, expected))
            failed = True

        # A real export tariff matching the cheapest import still takes no haircut - the forward view
        # must not be read as "export is always worthless"
        my_predbat.rate_export_max_forward = {10: 6.9}
        got = my_predbat.battery_value_rate(10)
        if abs(got - full) > 1e-6:
            print("ERROR: battery_value_rate is {} with a base export matching import, expected the undiscounted {}".format(got, full))
            failed = True

        # No forward data at all (an older debug file being replayed) falls back to rate_export_max
        my_predbat.rate_export_max = 3.45
        my_predbat.rate_export_max_forward = {}
        expected = full * 0.9
        got = my_predbat.battery_value_rate(10)
        if abs(got - expected) > 1e-6:
            print("ERROR: battery_value_rate is {} with no forward export data, expected the rate_export_max fallback {}".format(got, expected))
            failed = True
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed


def run_compute_metric_tests(my_predbat):
    """
    Test the compute metric function
    """
    failed = False
    print("**** Running compute metric tests ****")
    failed |= battery_value_rate_test(my_predbat)
    failed |= battery_value_rate_ceiling_base_test(my_predbat)
    failed |= battery_value_export_scaling_test(my_predbat)
    failed |= battery_value_export_forward_test(my_predbat)
    state_dict = save_state(my_predbat)
    failed |= compute_metric_test(my_predbat, "zero", assert_metric=0)
    failed |= compute_metric_test(my_predbat, "cost", cost=10.0, assert_metric=10)
    failed |= compute_metric_test(my_predbat, "cost_bat", cost=10.0, soc=10, rate_min=5, assert_metric=10 - 5 * 10)
    failed |= compute_metric_test(my_predbat, "cost_iboost", cost=10.0, final_iboost=50, iboost_value_scaling=0.8, assert_metric=10 - 50 * 0.8)
    failed |= compute_metric_test(my_predbat, "cost_keep", cost=10.0, metric_keep=5, assert_metric=10 + 5)
    failed |= compute_metric_test(my_predbat, "cost10", cost=10.0, cost10=20, pv_metric10_weight=0.5, assert_metric=10 + 10 * 0.5)
    # The blend is a signed weighted average, so a cheaper-than-nominal PV10 now pulls the metric
    # DOWN. The previous downside-only clamp left the metric at `cost` in this case.
    failed |= compute_metric_test(my_predbat, "cost10_cheaper", cost=20.0, cost10=10.0, pv_metric10_weight=0.5, assert_metric=0.5 * 20 + 0.5 * 10)
    failed |= compute_metric_test(my_predbat, "cost90", cost=10.0, cost10=10.0, cost90=4.0, pv_metric10_weight=0.1, pv_metric90_weight=0.2, assert_metric=0.7 * 10 + 0.1 * 10 + 0.2 * 4)
    failed |= compute_metric_test(my_predbat, "cost_carbon", cost=10.0, final_carbon_g=100, carbon_metric=2.0, assert_metric=10 + 100 / 1000 * 2.0)
    failed |= compute_metric_test(my_predbat, "cost_battery_cycle", cost=10.0, battery_cycle=25, metric_battery_cycle=0.1, assert_metric=10 + 25 * 0.1)
    # Test rate_max capping: rate_min=5 but rate_max=2 should cap it to 2
    # With inverter_loss=1.0, battery_loss=1.0, metric_battery_cycle=0, the cap is: max(min(5, 2*1*1-0), 0) = max(min(5, 2), 0) = 2
    # So battery_value = 10 * 1.0 * max(2, 1.0, rate_export_min) = 10 * 2 = 20, metric = 10 - 20 = -10
    failed |= compute_metric_test(my_predbat, "cost_rate_max", cost=10.0, soc=10, rate_min=5, rate_max=2.0, assert_metric=10 - 2 * 10)
    restore_state(my_predbat, state_dict)
    return failed
