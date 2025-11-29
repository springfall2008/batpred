# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

def compute_metric_test(
    my_predbat,
    name,
    end_record=None,
    soc=0,
    soc10=0,
    cost=0,
    cost10=0,
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
    iboost_value_scaling=1.0,
    inverter_loss=1.0,
    battery_loss=1.0,
    metric_battery_cycle=0.0,
    pv_metric10_weight=0.0,
    battery_loss_discharge=1.0,
    metric_self_sufficiency=0.0,
    carbon_metric=0.0,
    rate_min=1.0,
):
    """
    Test the compute metric function
    """
    my_predbat.metric_battery_value_scaling = battery_value_scaling
    my_predbat.rate_export_min = rate_export_min
    my_predbat.iboost_value_scaling = iboost_value_scaling
    my_predbat.inverter_loss = inverter_loss
    my_predbat.battery_loss = battery_loss
    my_predbat.metric_battery_cycle = metric_battery_cycle
    my_predbat.pv_metric10_weight = pv_metric10_weight
    my_predbat.battery_loss_discharge = battery_loss_discharge
    my_predbat.metric_self_sufficiency = metric_self_sufficiency
    my_predbat.rate_min = rate_min
    if not end_record:
        end_record = my_predbat.forecast_minutes

    my_predbat.rate_min_forward = {n: rate_min for n in range(my_predbat.forecast_minutes + my_predbat.minutes_now)}
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
    )
    if abs(metric - assert_metric) > 0.1:
        print("ERROR: Test {} Metric {} should be {}".format(name, metric, assert_metric))
        return True
    return False


def run_compute_metric_tests(my_predbat):
    """
    Test the compute metric function
    """
    failed = False
    failed |= compute_metric_test(my_predbat, "zero", assert_metric=0)
    failed |= compute_metric_test(my_predbat, "cost", cost=10.0, assert_metric=10)
    failed |= compute_metric_test(my_predbat, "cost_bat", cost=10.0, soc=10, rate_min=5, assert_metric=10 - 5 * 10)
    failed |= compute_metric_test(my_predbat, "cost_iboost", cost=10.0, final_iboost=50, iboost_value_scaling=0.8, assert_metric=10 - 50 * 0.8)
    failed |= compute_metric_test(my_predbat, "cost_keep", cost=10.0, metric_keep=5, assert_metric=10 + 5)
    failed |= compute_metric_test(my_predbat, "cost10", cost=10.0, cost10=20, pv_metric10_weight=0.5, assert_metric=10 + 10 * 0.5)
    failed |= compute_metric_test(my_predbat, "cost_carbon", cost=10.0, final_carbon_g=100, carbon_metric=2.0, assert_metric=10 + 100 / 1000 * 2.0)
    failed |= compute_metric_test(my_predbat, "cost_battery_cycle", cost=10.0, battery_cycle=25, metric_battery_cycle=0.1, assert_metric=10 + 25 * 0.1)
    return failed