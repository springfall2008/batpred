# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import reset_rates, reset_inverter, simple_scenario, reset_rates2


def run_model_tests(my_predbat, prediction_kernel=False):
    print("**** Running Model tests{} ****".format(" (C++ prediction kernel enabled)" if prediction_kernel else ""))
    my_predbat.prediction_kernel_enable = prediction_kernel
    reset_inverter(my_predbat)
    import_rate = 10.0
    export_rate = 5.0
    reset_rates(my_predbat, import_rate, export_rate)

    failed = False
    # Freeze Export residual discharge is real battery energy entering the AC balance.
    # House load consumes it first; any excess is exported. Normal battery discharge is
    # disabled here so only this configured path is under test.
    failed |= simple_scenario(
        "freeze_export_ac_flow_default_zero",
        my_predbat,
        1.0,
        0,
        assert_final_metric=10.0,
        assert_final_soc=10.0,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_freeze_export_discharge_rate=0.0,
        battery_rate_max_charge=0.0,
        assert_battery_cycle=0.0,
    )
    # Freeze Export must still let the battery discharge to cover a genuine load shortfall
    # (customisation.md "Freeze Export during Demand": "allows battery discharge, but not
    # battery charging") - distinct from the residual-leak path above, which only fires when
    # nothing else already moved the battery. No PV, no residual-discharge config: before the
    # #4676 fix this stayed flat (battery_draw pinned at 0) and billed the whole load as import.
    failed |= simple_scenario(
        "freeze_export_ac_flow_shortfall_discharges",
        my_predbat,
        1.0,
        0,
        assert_final_metric=0.0,
        assert_final_soc=9.0,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_freeze_export_discharge_rate=0.0,
        battery_rate_max_charge=1.0,
        assert_battery_cycle=1.0,
    )
    # battery_draw is DC but the shortfall from get_diff is AC, so it has to be grossed up
    # through the inverter the same way the ECO branch does - otherwise the battery only
    # covers inverter_loss of the load and the remainder is billed as a phantom import.
    # Rates here are deliberately non-binding so the loss factor is the only thing in play:
    # 1kWh of AC load over the hour needs 1/0.9 = 1.111kWh out of the battery.
    failed |= simple_scenario(
        "freeze_export_shortfall_grosses_up_inverter_loss",
        my_predbat,
        1.0,
        0,
        assert_final_metric=0.0,
        assert_final_soc=8.89,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_limit=5.0,
        inverter_loss=0.9,
        inverter_freeze_export_discharge_rate=0.0,
        battery_rate_max_charge=2.0,
        assert_battery_cycle=1.1111,
    )
    # Solar only pays the inverter loss on a hybrid (inverter_loss_ac is 1.0 otherwise, see
    # prediction.py), but the battery always sits behind the inverter, so the DC gross-up on the
    # shortfall discharge applies to both topologies. These four must pair up exactly: Freeze
    # Export is ECO with charging disabled, and in a shortfall slot no charging could happen
    # anyway, so freeze and ECO cannot legitimately differ here.
    # Non-hybrid: pv_ac = 0.4 (no solar loss), shortfall 0.6 AC, battery draw 0.6/0.9 = 0.6667 DC.
    # Hybrid:     pv_ac = 0.36 (solar loss),   shortfall 0.64 AC, battery draw 0.64/0.9 = 0.7111 DC.
    for freeze_label, freeze_limit in (("eco", 100), ("freeze_export", 99)):
        failed |= simple_scenario(
            "{}_shortfall_with_pv_ac_coupled".format(freeze_label),
            my_predbat,
            1.0,
            0.4,
            assert_final_metric=0.0,
            assert_final_soc=9.3333,
            battery_size=10.0,
            battery_soc=10.0,
            hybrid=False,
            discharge=freeze_limit,
            end_record=60,
            inverter_limit=5.0,
            inverter_loss=0.9,
            battery_rate_max_charge=2.0,
            assert_battery_cycle=0.6667,
        )
        failed |= simple_scenario(
            "{}_shortfall_with_pv_hybrid".format(freeze_label),
            my_predbat,
            1.0,
            0.4,
            assert_final_metric=0.0,
            assert_final_soc=9.2889,
            battery_size=10.0,
            battery_soc=10.0,
            hybrid=True,
            discharge=freeze_limit,
            end_record=60,
            inverter_limit=5.0,
            inverter_loss=0.9,
            battery_rate_max_charge=2.0,
            assert_battery_cycle=0.7111,
        )
    # When inverter_freeze_export_discharge_rate is configured the user is telling Predbat
    # their inverter does NOT cover house load during Freeze Export - it only leaks this fixed
    # rate. So the configured rate wins over the shortfall discharge above. Same scenario as
    # freeze_export_ac_flow_240w_one_hour but with a realistic (non-zero) battery discharge
    # rate, which is what a real AlphaESS install has.
    failed |= simple_scenario(
        "freeze_export_residual_rate_overrides_shortfall_discharge",
        my_predbat,
        1.0,
        0,
        assert_final_metric=7.6,
        assert_final_soc=9.76,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_freeze_export_discharge_rate=240.0,
        battery_rate_max_charge=1.0,
        assert_battery_cycle=0.24,
    )
    failed |= simple_scenario(
        "freeze_export_ac_flow_240w_one_hour",
        my_predbat,
        1.0,
        0,
        assert_final_metric=7.6,
        assert_final_soc=9.76,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_freeze_export_discharge_rate=240.0,
        battery_rate_max_charge=0.0,
        assert_battery_cycle=0.24,
    )
    failed |= simple_scenario(
        "freeze_export_ac_flow_respects_inverter_loss",
        my_predbat,
        1.0,
        0,
        assert_final_metric=8.08,
        assert_final_soc=9.76,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_loss=0.8,
        inverter_freeze_export_discharge_rate=240.0,
        battery_rate_max_charge=0.0,
        assert_battery_cycle=0.24,
    )
    failed |= simple_scenario(
        "freeze_export_ac_flow_no_load_exports_residual",
        my_predbat,
        0,
        0,
        assert_final_metric=-1.2,
        assert_final_soc=9.76,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_freeze_export_discharge_rate=240.0,
        battery_rate_max_charge=0.0,
        assert_battery_cycle=0.24,
    )
    # Live AlphaESS behaviour: PV nearly covers the house, but Freeze Export residual
    # discharge continues and the surplus reaches grid (487 W load, 466 W PV, 269 W battery).
    failed |= simple_scenario(
        "freeze_export_ac_flow_surplus_reaches_grid",
        my_predbat,
        0.487,
        0.466,
        assert_final_metric=-1.24,
        assert_final_soc=9.731,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        inverter_freeze_export_discharge_rate=269.0,
        battery_rate_max_charge=0.0,
        assert_battery_cycle=0.269,
    )
    failed |= simple_scenario(
        "freeze_export_ac_flow_not_outside_freeze",
        my_predbat,
        1.0,
        0,
        assert_final_metric=10.0,
        assert_final_soc=10.0,
        battery_size=10.0,
        battery_soc=10.0,
        discharge=100,
        end_record=60,
        inverter_freeze_export_discharge_rate=240.0,
        battery_rate_max_charge=0.0,
        assert_battery_cycle=0.0,
    )
    failed |= simple_scenario(
        "freeze_export_ac_flow_reserve_floor",
        my_predbat,
        1.0,
        0,
        assert_final_metric=9.0,
        assert_final_soc=4.0,
        battery_size=10.0,
        battery_soc=4.1,
        reserve=4.0,
        discharge=99,
        end_record=60,
        inverter_freeze_export_discharge_rate=240.0,
        battery_rate_max_charge=0.0,
        assert_battery_cycle=0.1,
    )
    # Freeze Export PV recapture (#4207) is only real on inverters whose freeze mode is a genuine
    # "Feed-in First" (load, then export, then battery) - FoxESS. Everything else just disables
    # charging, so PV above the export limit really is clipped. inverter_support_feedin_first is
    # what separates the two, and these pairs differ in nothing else.
    #
    # 2kW PV, no load, 0.5kW export limit: 1.5kW of PV has nowhere to go. With Feed-in First the
    # battery takes exactly that overflow (1.5kWh over the hour) and nothing is clipped; without
    # it the battery holds and the same 1.5kWh is clipped. Either way 0.5kWh is exported, so the
    # bill is identical and only the SoC/clipping tell the two models apart.
    # assert_clipped is the only figure here that is not bounded by end_record - it is a running
    # total over the whole 24 hour horizon, so 1.5kW of clipping reads as 0.125 * 287 steps.
    for feedin_first, expect_soc, expect_cycle, expect_clipped in ((True, 11.5, 1.5, 0), (False, 10.0, 0.0, 35.875)):
        failed |= simple_scenario(
            "freeze_export_feedin_first_{}".format(feedin_first),
            my_predbat,
            0,
            2.0,
            assert_final_metric=-export_rate * 0.5,
            assert_final_soc=expect_soc,
            battery_soc=10.0,
            discharge=99,
            end_record=60,
            export_limit=0.5,
            inverter_limit=10.0,
            battery_rate_max_charge=5.0,
            inverter_support_feedin_first=feedin_first,
            assert_battery_cycle=expect_cycle,
            assert_clipped=expect_clipped,
        )
    # Same contrast on a hybrid with a lossy inverter, where the recapture happens on the DC side.
    # pv_ac = 2 * 0.8 = 1.6kW, so the AC overflow past the 0.5kW export limit is 1.1kW; charging it
    # DC-side costs the loss reciprocal, 1.1 / 0.8 = 1.375kW (1.375kWh over the hour). Without
    # Feed-in First that 1.1kWh of AC PV is clipped instead.
    for feedin_first, expect_soc, expect_cycle, expect_clipped in ((True, 11.375, 1.375, 0), (False, 10.0, 0.0, 26.308)):
        failed |= simple_scenario(
            "freeze_export_feedin_first_hybrid_{}".format(feedin_first),
            my_predbat,
            0,
            2.0,
            assert_final_metric=-export_rate * 0.5,
            assert_final_soc=expect_soc,
            battery_soc=10.0,
            hybrid=True,
            inverter_loss=0.8,
            discharge=99,
            end_record=60,
            export_limit=0.5,
            inverter_limit=10.0,
            battery_rate_max_charge=5.0,
            inverter_support_feedin_first=feedin_first,
            assert_battery_cycle=expect_cycle,
            assert_clipped=expect_clipped,
        )
    # inverter_can_charge_during_export still vetoes the recapture on a Feed-in First inverter -
    # a user who has told Predbat the battery cannot charge while exporting is believed either way.
    failed |= simple_scenario(
        "freeze_export_feedin_first_no_charge_during_export",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 0.5,
        assert_final_soc=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        export_limit=0.5,
        inverter_limit=10.0,
        battery_rate_max_charge=5.0,
        inverter_support_feedin_first=True,
        inverter_can_charge_during_export=False,
        assert_battery_cycle=0.0,
        assert_clipped=35.875,
    )
    # PV surplus the export limit can absorb on its own must leave SoC flat even with Feed-in
    # First - freeze is still a freeze, only genuine overflow moves the battery. 2kW PV against a
    # 5kW export limit is all exportable, so nothing is recaptured and nothing is clipped.
    failed |= simple_scenario(
        "freeze_export_feedin_first_within_export_limit",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 2.0,
        assert_final_soc=10.0,
        battery_soc=10.0,
        discharge=99,
        end_record=60,
        export_limit=5.0,
        inverter_limit=10.0,
        battery_rate_max_charge=5.0,
        inverter_support_feedin_first=True,
        assert_battery_cycle=0.0,
        assert_clipped=0,
    )
    # A full battery has no headroom to recapture into, so the overflow is clipped regardless of
    # Feed-in First - battery_to_max, not the charge rate, is the binding clamp here.
    failed |= simple_scenario(
        "freeze_export_feedin_first_full_battery",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 0.5,
        assert_final_soc=100.0,
        battery_soc=100.0,
        discharge=99,
        end_record=60,
        export_limit=0.5,
        inverter_limit=10.0,
        battery_rate_max_charge=5.0,
        inverter_support_feedin_first=True,
        assert_battery_cycle=0.0,
        assert_clipped=35.875,
    )
    if failed:
        return failed

    failed |= simple_scenario("zero", my_predbat, 0, 0, 0, 0, with_battery=False)
    failed |= simple_scenario("load_only", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("load_bat_ac", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)
    failed |= simple_scenario("load_bat_ac2", my_predbat, 0.5, 0, assert_final_metric=0, assert_final_soc=100 - (24 * 0.5) / 0.8, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc2", my_predbat, 0.5, 0, assert_final_metric=0, assert_final_soc=100 - (24 * 0.5) / 0.8, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)
    failed |= simple_scenario("load_bat_ac3", my_predbat, 1.0, 0, assert_final_metric=import_rate * 0.2 * 24, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc3", my_predbat, 1.0, 0, assert_final_metric=import_rate * 0.2 * 24, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)

    failed |= simple_scenario("load_empty_bat1", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=4, with_battery=True, battery_soc=4, reserve=4)
    failed |= simple_scenario("load_empty_bat2", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=3, with_battery=True, battery_soc=3, reserve=4)
    failed |= simple_scenario("load_empty_bat_chrg1", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5 + import_rate * 1, assert_final_soc=4, with_battery=True, battery_soc=3, reserve=4, charge=4)
    failed |= simple_scenario("load_empty_bat_chrg2", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5 + import_rate * 2, assert_final_soc=5, with_battery=True, battery_soc=3, reserve=4, charge=5)
    failed |= simple_scenario("load_empty_bat_chrg3", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=5, with_battery=True, battery_soc=5, reserve=4, charge=5)

    failed |= simple_scenario(
        "hold_during discharge",
        my_predbat,
        0.1,
        0,
        assert_final_metric=import_rate,
        assert_final_soc=5 - 23 * 0.1,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge2",
        my_predbat,
        0.1,
        0,
        assert_final_metric=import_rate,
        assert_final_soc=5 - 23 * 0.1 / 0.8,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge3",
        my_predbat,
        0.1,
        0,
        assert_final_metric=import_rate,
        assert_final_soc=5 - 23 * 0.1 / 0.8,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=True,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv1",
        my_predbat,
        0.1,
        0.1,
        assert_final_metric=0,
        assert_final_soc=5,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv2",
        my_predbat,
        0.1,
        0.2,
        assert_final_metric=0,
        assert_final_soc=5 + 0.1 * 24,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv3",
        my_predbat,
        0.1,
        0.2,
        assert_final_metric=0,
        assert_final_soc=5 + 0.1 * 24,  # For AC Coupled PV arrives as AC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=False,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv4",
        my_predbat,
        0.1,
        0.2,
        assert_final_metric=0,
        assert_final_soc=5 + ((0.2 * 0.8) - 0.1) * 24,  # For DC Coupled PV arrives as DC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=True,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv5",
        my_predbat,
        0.2,
        0.1,
        assert_final_metric=0.1 * import_rate,
        assert_final_soc=5 - (0.1 / 0.8) * 23,  # For AC Coupled PV arrives as AC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=False,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv6",
        my_predbat,
        0.2,
        0.1,
        assert_final_metric=0.1 * import_rate,
        assert_final_soc=5 - ((0.2 / 0.8) - 0.1) * 23,  # For DC Coupled PV arrives as DC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=True,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "load_bat_dc_pv",
        my_predbat,
        4,
        0.5,
        assert_final_metric=import_rate * 24 * 3.2,
        assert_final_soc=100 - 24 * 0.5,
        with_battery=True,
        battery_soc=100.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario(
        "load_bat_dc_pv2",
        my_predbat,
        4,
        4,
        assert_final_metric=import_rate * 24 * 3.2,
        assert_final_soc=50 + 24,
        with_battery=True,
        battery_soc=50.0,
        inverter_loss=0.8,
        hybrid=True,
        assert_clipped=2 * 24,  # 1 for the battery on DC and 1 for the PV on AC
    )
    failed |= simple_scenario("load_carbon", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False, carbon=3, assert_final_carbon=3 * 24)
    failed |= simple_scenario(
        "load_carbon_loss_ac",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
    )
    failed |= simple_scenario(
        "load_carbon_loss_dc",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario(
        "pv_carbon_ac",
        my_predbat,
        0,
        1,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=-3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
    )
    failed |= simple_scenario(
        "pv_carbon_dc",
        my_predbat,
        0,
        1,
        assert_final_metric=-export_rate * 24 * 0.8,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=-3 * 24 * 0.8,
        inverter_limit=3.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario("load_car", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 3, assert_final_soc=0, with_battery=False, charge_car=2.0)
    failed |= simple_scenario("load_car_bat_yes", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 2, assert_final_soc=100.0 - 24 * 1, with_battery=True, charge_car=2.0, battery_soc=100.0)
    failed |= simple_scenario(
        "load_car_bat_no",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24 * 3,
        assert_final_soc=100.0,
        with_battery=True,
        charge_car=2.0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )
    failed |= simple_scenario(
        "load_car_bat_no2",
        my_predbat,
        1,
        0,
        assert_final_metric=0,
        assert_final_soc=100.0 - 24,
        with_battery=True,
        charge_car=0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )
    failed |= simple_scenario(
        "load_car_bat_no3",
        my_predbat,
        0.5,
        0,
        assert_final_metric=import_rate * 3,
        assert_final_soc=100.0 - 24 * 0.5,
        with_battery=True,
        charge_car=60,
        car_soc=97.0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )
    failed |= simple_scenario(
        "load_car_bat_no4",
        my_predbat,
        0.5,
        0,
        assert_final_metric=0,
        assert_final_soc=100.0 - 24 * 0.5,
        with_battery=True,
        charge_car=60,
        car_soc=97.0,
        battery_soc=100.0,
        car_charging_from_battery=True,
        car_energy_reported_load=False,
    )
    failed |= simple_scenario(
        "load_car_bat_no5",
        my_predbat,
        0,
        0,
        assert_final_metric=9 * import_rate * 10,
        assert_final_soc=100.0 - 10,
        with_battery=True,
        charge_car=10,
        car_soc=0.0,
        battery_soc=100.0,
        car_charging_from_battery=True,
        car_energy_reported_load=True,
    )
    failed |= simple_scenario(
        "load_car_bat_no6",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=100.0,
        with_battery=True,
        charge_car=1,
        car_soc=0.0,
        battery_soc=100.0,
        car_charging_from_battery=True,
        car_energy_reported_load=False,
    )

    failed |= simple_scenario("load_discharge", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge2", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 24, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario("load_discharge3", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 48, battery_soc=100.0, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("load_discharge4", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=100.0, with_battery=True, battery_loss=0.1)

    # Discharge curve has 0.05 for -9 which is 0.5 max rate
    failed |= simple_scenario("discharge_curve1", my_predbat, 1, 0, assert_final_metric=import_rate * 20 * 0.5 + 4 * import_rate, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_size=10, battery_temperature=-9)
    # Discharge curve has 0.01 for -10 which is 0.1 max rate
    failed |= simple_scenario("discharge_curve2", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 0.90, assert_final_soc=7.6, battery_soc=10.0, with_battery=True, battery_temperature=-10, battery_size=10)

    failed |= simple_scenario(
        "load_discharge_car",
        my_predbat,
        0.5,
        0,
        assert_final_metric=import_rate * 14 * 4.5 + import_rate * 10 * 3.5,
        assert_final_soc=0,
        battery_soc=10.0,
        with_battery=True,
        charge_car=4.0,
    )
    failed |= simple_scenario(
        "load_discharge_car2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 24 * 1.5,
        assert_final_soc=100 - 24 * 2.5,
        battery_soc=100.0,
        with_battery=True,
        charge_car=4.0,
        discharge=0,
        inverter_limit=3.5,
        battery_rate_max_charge=2.5,
    )
    failed |= simple_scenario("load_discharge_fast", my_predbat, 2, 0, assert_final_metric=import_rate * 38, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge_fast_big", my_predbat, 2, 0, assert_final_metric=import_rate * 24, assert_final_soc=76, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario("load_discharge_reserve", my_predbat, 1, 0, assert_final_metric=import_rate * 15, assert_final_soc=1, battery_soc=10.0, with_battery=True, reserve=1.0)
    failed |= simple_scenario("load_discharge_reserve2", my_predbat, 1, 0, assert_final_metric=import_rate * 20, assert_final_soc=2, battery_soc=10.0, with_battery=True, reserve=2.0, battery_loss=0.5)
    failed |= simple_scenario("load_discharge_loss", my_predbat, 1, 0, assert_final_metric=import_rate * 19, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_loss=0.5)
    # Forced export with PV on a lossy hybrid inverter. The battery exports through the inverter (DC->AC) so
    # when battery + solar would exceed the export limit the battery discharge must be scaled back by the loss
    # reciprocal to bring grid export down to the limit. Otherwise a small residual is left above the limit and
    # gets clipped off the solar. Regression test: with the scale-back correct, no solar should be clipped.
    # battery_draw(DC) = (export_limit - pv_ac) / inverter_loss = (3 - 2*0.8) / 0.8 = 1.75 kW, over 24h = 42 kWh.
    failed |= simple_scenario(
        "export_pv_clip_loss",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 3,
        assert_final_soc=100 - 42,
        battery_soc=100.0,
        with_battery=True,
        hybrid=True,
        inverter_loss=0.8,
        export_limit=3.0,
        inverter_limit=10.0,
        battery_rate_max_charge=5.0,
        discharge=0,
        assert_clipped=0,
    )
    # Forced export with PV so large that even stopping the battery leaves the solar over the export limit. With
    # inverter_can_charge_during_export the battery should charge from the surplus PV (DC side) to keep grid export
    # at the limit, rather than clipping the solar. Regression test for the AC/DC unit mismatch in that charge branch.
    # remaining_ac = pv_ac - export_limit = 2*0.8 - 1 = 0.6; hybrid DC charge = 0.6 / 0.8 = 0.75 kW, over 24h = 18 kWh.
    failed |= simple_scenario(
        "export_pv_charge_clip_loss",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=40 + 18,
        battery_soc=40.0,
        with_battery=True,
        hybrid=True,
        inverter_loss=0.8,
        export_limit=1.0,
        inverter_limit=10.0,
        battery_rate_max_charge=1.0,
        discharge=0,
        inverter_can_charge_during_export=True,
        assert_clipped=0,
    )
    # Band case for the scale-back vs charge decision. The AC over-export (1.5kW) is larger than the battery's
    # AC contribution (battery_draw 2kW DC * inverter_loss 0.5 = 1kW) but smaller than the raw DC discharge (2kW).
    # The branch pivot must use the AC contribution: even after stopping the battery the 1kW AC PV is still over
    # the 0.5kW export limit, so the battery should charge to absorb the 0.5kW surplus (0.25kW DC, 6kWh over 24h)
    # instead of clipping it. Comparing against the raw DC value sends this to the scale-back path which just
    # stops the battery and clips the solar.
    failed |= simple_scenario(
        "export_pv_charge_band_loss",
        my_predbat,
        0,
        1,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.25 * 24,
        battery_soc=50.0,
        with_battery=True,
        inverter_loss=0.5,
        export_limit=0.5,
        inverter_limit=10.0,
        battery_rate_max_charge=2.0,
        discharge=0,
        inverter_can_charge_during_export=True,
        assert_clipped=0,
    )
    # Full battery during a high-PV forced export. PV alone (2kW) exceeds the 0.5kW export limit so the charge
    # path is entered, but the battery is already at 100% so it has no headroom to absorb anything. The charge
    # must be clamped by battery_to_max (0 here) so all 1.5kW AC surplus is clipped. Clamping by battery_to_min
    # instead would let the model "charge" a full battery and under-report the clipping (clip 12 instead of 36).
    failed |= simple_scenario(
        "export_pv_charge_full_battery",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=100,
        battery_soc=100.0,
        with_battery=True,
        export_limit=0.5,
        inverter_limit=10.0,
        battery_rate_max_charge=1.0,
        discharge=0,
        inverter_can_charge_during_export=True,
        assert_clipped=24 * 1.5,
    )
    # Hybrid forced export where PV (4kW DC) exceeds the inverter limit (2kW) but the grid export limit is not
    # binding, so the inverter-limit charge branch absorbs the surplus PV into the battery. total_inverted counts
    # the battery and the DC-diverted PV 1:1, so the battery must charge by reduce_by = pv - inverter_limit = 2kW
    # (not reduce_by * inverter_loss). Charging the full 2kW DC keeps total_inverted exactly on the 2kW limit with
    # no clipping; charging only 1.6kW (the under-charge bug) leaves total_inverted at 2.4kW and clips 0.4kW of PV.
    failed |= simple_scenario(
        "export_pv_inverter_limit_charge",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 1.6 * 24,
        assert_final_soc=100 + 2.0 * 24,
        battery_soc=100.0,
        battery_size=200.0,
        with_battery=True,
        hybrid=True,
        inverter_loss=0.8,
        export_limit=100.0,
        inverter_limit=2.0,
        battery_rate_max_charge=1.0,
        battery_rate_max_charge_dc=10.0,
        discharge=0,
        inverter_can_charge_during_export=True,
        assert_clipped=0,
    )
    failed |= simple_scenario("load_pv", my_predbat, 1, 1, assert_final_metric=0, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv10_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, pv10=True)

    # Test charge_scaling10 feature - battery charge rate is de-rated in PV10 mode
    # With pv10=False, battery charges at full 1kW rate, so 10kWh battery charges in 10 hours
    # Cost = 10kWh * import_rate = 100 pence
    failed |= simple_scenario(
        "charge_scaling10_baseline",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        pv10=False,
        charge_scaling10=0.5,
    )
    # With pv10=True and charge_scaling10=0.5, battery charges at 0.5kW rate
    # In 24 hours at 0.5kW, we can charge 12kWh, but battery is only 10kWh so it fills up
    # Cost = 10kWh * import_rate = 100 pence (same as baseline as battery still fills)
    failed |= simple_scenario(
        "charge_scaling10_pv10_full_charge",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        pv10=True,
        charge_scaling10=0.5,
    )
    # With pv10=True and charge_scaling10=0.5 and a limited charge window (12 hours)
    # At 0.5kW rate we can only charge 6kWh in 12 hours
    # pv10=False baseline: 12 hours * 1kW = 12kWh but battery is 10kWh so charges full
    failed |= simple_scenario(
        "charge_scaling10_limited_window_baseline",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        pv10=False,
        charge_scaling10=0.5,
        charge_period_divide=2,
    )
    # With pv10=True and charge_scaling10=0.5 and 12 hour window
    # At 0.5kW rate we can only charge 6kWh in 12 hours
    failed |= simple_scenario(
        "charge_scaling10_limited_window_pv10",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 6,
        assert_final_soc=6,
        with_battery=True,
        charge=10,
        battery_size=10,
        pv10=True,
        charge_scaling10=0.5,
        charge_period_divide=2,
    )

    failed |= simple_scenario("pv_only_loss_ac", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, inverter_loss=0.5)
    failed |= simple_scenario("pv_only_loss_hybrid", my_predbat, 0, 1, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=0, with_battery=False, inverter_loss=0.5, hybrid=True)
    failed |= simple_scenario("pv_only_bat", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_loss", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=12, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("pv_only_bat_100%", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, battery_size=10)
    failed |= simple_scenario("pv_only_bat_ac_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_clips2b", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, battery_rate_max_charge_dc=2.0)
    failed |= simple_scenario("pv_only_bat_ac_clips2c", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, battery_rate_max_charge=2.0)
    failed |= simple_scenario("pv_only_bat_ac_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 48, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5, assert_clipped=24 * 1.5)
    failed |= simple_scenario(
        "pv_only_bat_ac_export_limit_loss",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 0.1,
        assert_final_soc=12,
        with_battery=True,
        export_limit=0.1,
        inverter_loss=0.5,
        assert_clipped=24 * 2.9,
    )
    failed |= simple_scenario("pv_only_bat_ac_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5, assert_clipped=24 * 1)
    failed |= simple_scenario("pv_only_bat_dc_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario("pv_only_bat_dc_clips2dc", my_predbat, 0, 2, assert_final_metric=0, assert_final_soc=48, with_battery=True, hybrid=True, battery_rate_max_charge_dc=2.0)
    failed |= simple_scenario("pv_only_bat_dc_clips2dch", my_predbat, 0, 2, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=36, with_battery=True, hybrid=True, battery_rate_max_charge_dc=1.5)
    failed |= simple_scenario("pv_only_bat_dc_clips2l", my_predbat, 0, 2, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5)
    failed |= simple_scenario("pv_only_bat_dc_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True, assert_clipped=24 * 1)
    failed |= simple_scenario("pv_only_bat_dc_clips3l", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5, assert_clipped=24 * 1)
    failed |= simple_scenario(
        "pv_only_bat_dc_clips3l2",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24,
        with_battery=True,
        hybrid=True,
        inverter_loss=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario("pv_only_bat_dc_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5, assert_clipped=24 * 1.5)
    failed |= simple_scenario(
        "pv_only_bat_dc_export_limit_loss",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 0.1,
        assert_final_soc=24,
        with_battery=True,
        hybrid=True,
        export_limit=0.1,
        inverter_loss=0.5,
        assert_clipped=24 * 1.9,
    )

    # Export limit less than battery max discharge rate, no solar - battery should just be rate-limited, no clipping
    failed_local, prediction = simple_scenario(
        "export_limit_no_clip_no_solar",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.5,  # 0.5 kW * 24h = 12 kWh exported at 5p
        assert_final_soc=100 - 12,  # 12 kWh drained from 100 kWh battery
        with_battery=True,
        battery_soc=100.0,
        battery_size=100,
        battery_rate_max_charge=1.0,  # 1 kW max discharge, higher than the export limit
        export_limit=0.5,  # 0.5 kW export limit - less than max battery discharge rate
        discharge=0,  # export all the way to empty
        return_prediction_handle=True,
    )
    failed |= failed_local
    total_clipped = max(prediction.predict_clipped_best.values()) if prediction.predict_clipped_best else 0
    if total_clipped > 0:
        print("ERROR: export_limit_no_clip_no_solar: clipping should be 0 but got {}".format(total_clipped))
        failed = True

    failed |= simple_scenario("pv_only_bat_dc_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5, assert_clipped=24 * 1)
    failed |= simple_scenario("battery_charge", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)

    failed |= simple_scenario("battery_charge_low_off", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=24.59)
    failed |= simple_scenario("battery_charge_low_on", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=88.8947)
    failed |= simple_scenario(
        "battery_charge_low_on_monitor", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=24.59, set_charge_window=False
    )

    failed |= simple_scenario(
        "battery_charge_low_temp1", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=24.59, battery_temperature=20
    )
    failed |= simple_scenario(
        "battery_charge_low_temp2", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=80.00, battery_temperature=1
    )
    failed |= simple_scenario(
        "battery_charge_low_temp3", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=88.8947, battery_temperature=1
    )

    if failed:
        return failed
    failed |= simple_scenario("battery_charge_prev_charge", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)

    failed |= simple_scenario(
        "battery_charge_freeze",
        my_predbat,
        0.5,
        0,
        assert_final_metric=import_rate * 24 * 0.5,
        assert_final_soc=5,
        with_battery=True,
        charge=0.5,
        battery_soc=5,
        battery_size=10,
        reserve=0.5,
        set_charge_freeze=True,
    )
    failed |= simple_scenario(
        "battery_charge_freeze2",
        my_predbat,
        0.5,
        1,
        assert_final_metric=0,
        assert_final_soc=5 + 0.5 * 24,
        with_battery=True,
        charge=0.5,
        battery_soc=5,
        battery_size=100,
        reserve=0.5,
        set_charge_freeze=True,
    )
    failed |= simple_scenario("battery_charge_load", my_predbat, 1, 0, assert_final_metric=import_rate * 34, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_load2", my_predbat, 2, 0, assert_final_metric=import_rate * (34 + 24), assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv2", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario("battery_charge_pv3", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario("battery_charge_pv4_ac", my_predbat, 0, 2, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5, inverter_limit=2)
    failed |= simple_scenario(
        "battery_charge_pv4_dc",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_ac",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_dc",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_dc_b",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=24 * 2,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=1.0,
        inverter_limit=1.0,
        battery_rate_max_charge_dc=2.0,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_dc_c",
        my_predbat,
        0,
        3,
        assert_final_metric=0,
        assert_final_soc=24 * 3,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=1.0,
        inverter_limit=1.0,
        battery_rate_max_charge_dc=10.0,
        hybrid=True,
        export_limit=10.0,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_dc_d",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24 * 2,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=1.0,
        inverter_limit=1.0,
        battery_rate_max_charge_dc=2.0,
        hybrid=True,
        export_limit=10.0,
        assert_clipped=24 * 1,
    )

    failed |= simple_scenario(
        "battery_charge_pv6_ac",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 2,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
    )
    failed |= simple_scenario(
        "battery_charge_pv6_dc",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
        assert_clipped=24 * 1,
    )
    failed |= simple_scenario(
        "battery_charge_pv_term_dc1",
        my_predbat,
        0,
        0.5,
        assert_final_metric=import_rate * 10 * 0.5,
        assert_final_soc=10 + 14 * 0.5,
        with_battery=True,
        charge=10,
        battery_size=100,
        hybrid=True,
        assert_keep=0,
    )
    failed |= simple_scenario(
        "battery_charge_pv_term_dc2",
        my_predbat,
        0,
        0.5,
        assert_final_metric=import_rate * 10 * 0.5,
        assert_final_soc=10 + 14 * 0.5,
        with_battery=True,
        charge=9.95,
        battery_size=100,
        hybrid=True,
        assert_keep=((1 / 60 * 5) - 0.05) * import_rate,
    )
    failed |= simple_scenario(
        "battery_charge_pv_load1",
        my_predbat,
        0.5,
        1,
        assert_final_metric=import_rate * 0.5 * 10 - export_rate * 14 * 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
    )
    failed |= simple_scenario("battery_charge_pv_load2_ac", my_predbat, 0.5, 1, assert_final_metric=import_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0)
    failed |= simple_scenario(
        "battery_charge_pv_load2_hybrid",
        my_predbat,
        0.5,
        1,
        assert_final_metric=import_rate * 0.5 * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_soc=0,
        hybrid=True,
    )
    failed |= simple_scenario("battery_charge_pv_load3_ac", my_predbat, 0.5, 2, assert_final_metric=-export_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0)
    failed |= simple_scenario(
        "battery_charge_pv_load3_hybrid",
        my_predbat,
        0.5,
        2,
        assert_final_metric=-export_rate * 0.5 * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_soc=0,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_part1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 1,
        assert_final_soc=1,
        with_battery=True,
        charge=10,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": 10}],
    )
    failed |= simple_scenario(
        "battery_charge_part1.5",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 1.5,
        assert_final_soc=1.5,
        with_battery=True,
        charge=10,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 150, "average": 10}],
    )
    failed |= simple_scenario("battery_discharge", my_predbat, 0, 0, assert_final_metric=-export_rate * 10, assert_final_soc=0, with_battery=True, discharge=0, battery_soc=10)
    failed |= simple_scenario(
        "battery_discharge_keep",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.5 + ((1 + (1 / 12)) * import_rate * 0.5 * 0.5),
        keep=1,
        keep_weight=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_keep2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 1,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=1,
        assert_keep=23 * import_rate * 0.5 + ((1 + (1 / 12)) * import_rate * 0.5 * 0.5),
        keep=1,
        keep_weight=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_loss2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.25,
        assert_final_soc=100 - 24 * 0.5,
        battery_soc=100.0,
        with_battery=True,
        inverter_loss=0.5,
        discharge=0,
        inverter_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_load",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate + 1 * import_rate * 0.5,
        keep=1.0,
        keep_weight=1.0,
    )
    failed |= simple_scenario(
        "battery_load_keep_four_hour",
        my_predbat,
        1.0,
        0,
        assert_final_metric=import_rate * 20,
        assert_final_soc=0,
        with_battery=True,
        battery_soc=4,
        assert_keep=20 * import_rate * 4 + 53,
        keep=4.0,
        keep_weight=1.0,
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep_mode_test1",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.8 + 1 * import_rate * 0.8 * 0.5,
        keep=1.0,
        keep_weight=0.8,
        save="test",
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep_mode_test2",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.8 + 1 * import_rate * 0.8 * 0.5,
        keep=1.0,
        keep_weight=0.8,
        save="none",
    )
    failed |= simple_scenario(
        "battery_discharge_pv_ac",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 10 - export_rate * 24 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv_ac_load",
        my_predbat,
        0.1,
        0.5,
        assert_final_metric=-export_rate * 9 - export_rate * 24 * 0.4,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv2_ac",
        my_predbat,
        0,
        1.5,
        assert_final_metric=-export_rate * 10 * 2.5 - export_rate * 14 * 1.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv3_ac",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 10 * 3 - export_rate * 14 * 2,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv4_ac",
        my_predbat,
        0,
        5.0,
        assert_final_metric=-export_rate * 10 * 6 - export_rate * 14 * 5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv5_ac",
        my_predbat,
        1,
        5.0,
        assert_final_metric=-export_rate * 24 * 4.5,
        assert_final_soc=50 - 24 * 1,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        inverter_limit=2,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_pv_hybrid",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 20 - export_rate * 4 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        hybrid=True,
    )
    failed |= simple_scenario("battery_discharge_pv2_hybrid", my_predbat, 0, 1.5, assert_final_metric=-export_rate * 24, assert_final_soc=22, with_battery=True, discharge=0, battery_soc=10, hybrid=True)
    failed |= simple_scenario("battery_discharge_pv3_hybrid", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, discharge=0, battery_soc=0, hybrid=True)
    failed |= simple_scenario("battery_discharge_pv3_hybrid2", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, discharge=0, battery_soc=0, hybrid=True, assert_clipped=24 * 1)
    failed |= simple_scenario("battery_discharge_pv3_hybrid3", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=48, with_battery=True, discharge=0, battery_soc=0, hybrid=True, battery_rate_max_charge_dc=2.0)
    failed |= simple_scenario(
        "battery_discharge_pv4_hybrid",
        my_predbat,
        1,
        5,
        assert_final_metric=0,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        hybrid=True,
        inverter_limit=2,
        inverter_loss=0.5,
        assert_clipped=24 * 2,
    )
    failed |= simple_scenario("battery_discharge_freeze", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10)
    failed |= simple_scenario("battery_discharge_freeze2", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10, set_export_freeze_only=True)
    failed |= simple_scenario("battery_discharge_freeze_only", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=0, battery_soc=10, set_export_freeze_only=True)

    # Force discharge with PV: penalty = discharge_hours * pv_kw * export_rate = 24 * 0.5 * export_rate (full 24h forecast window in these model tests)
    failed |= simple_scenario(
        "battery_discharge_pv_no_export_on_pv1",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 24 * 1.5,
        assert_final_soc=100 - 24,
        with_battery=True,
        discharge=0,
        battery_soc=100,
        assert_keep=24 * 0.5 * export_rate * 5,
        calculate_export_on_pv=False,
    )
    # No force discharge window: pv_ac is exported but battery_draw=0 so no penalty
    failed |= simple_scenario(
        "battery_discharge_pv_no_export_on_pv2",
        my_predbat,
        0,
        0.5,
        assert_final_metric=0,
        assert_final_soc=10 + 0.5 * 24,
        with_battery=True,
        battery_soc=10,
        assert_keep=0,
        calculate_export_on_pv=False,
    )
    if failed:
        return failed

    failed |= simple_scenario("battery_discharge_hold", my_predbat, 0, 0.5, assert_final_metric=-0, assert_final_soc=10 + 24 * 0.5, with_battery=True, discharge=98, battery_soc=10)
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 - 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        battery_rate_max_charge_dc=10.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv_b",
        my_predbat,
        1,
        4,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        battery_rate_max_charge_dc=10.0,
        assert_clipped=24 * 1.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv2",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.0 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        assert_clipped=24 * 0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv4",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        # 1.5kW AC PV surplus (2kW - 0.5kW export limit). AC-coupled charging stores AC * inverter_loss as DC,
        # so absorbing all 1.5kW only needs 1.5 * 0.5 = 0.75kW DC, which is within the 1kW charge rate. The
        # battery therefore soaks up all the surplus and nothing is clipped (was previously under-charging at
        # 0.5kW DC and clipping the rest due to an AC/DC unit mismatch in the export-limit charge branch).
        assert_final_soc=50 + 0.75 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        inverter_loss=0.5,
        assert_clipped=0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv5",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        battery_rate_max_charge=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv6",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        battery_rate_max_charge=1.0,
        inverter_can_charge_during_export=False,
        assert_clipped=24 * 1.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 - 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.0,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv2",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv3",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 1.0,
        assert_final_soc=50 + 0 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=1.0,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv4",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
        assert_clipped=24 * 0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv5",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 2 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
        battery_rate_max_charge_dc=2.0,
        assert_clipped=24 * 0.5,
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10 / 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10 / 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario("battery_charge_ac_loss_pv", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24 * 0.5, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5)
    failed |= simple_scenario(
        "battery_charge_ac_loss_pv2",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24 * 0.5,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=0,
        assert_final_soc=24 * 1,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv2",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "iboost_pv",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=50,
        battery_soc=50,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv2",
        my_predbat,
        1,
        1,
        assert_final_metric=0,
        assert_final_soc=50 - 24,
        battery_soc=50,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv3",
        my_predbat,
        1,
        1,
        assert_final_metric=0,
        assert_final_soc=50,
        battery_soc=50,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        iboost_solar_excess=True,
        assert_final_iboost=0,
        assert_iboost_running=False,
        assert_iboost_running_solar=False,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv4",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=100,
        battery_soc=90,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        iboost_solar_excess=True,
        assert_final_iboost=24 - 10,
        assert_iboost_running=False,
        assert_iboost_running_solar=False,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv5",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=100,
        battery_soc=100,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        iboost_solar_excess=True,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_gas1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas=True,
        rate_gas=5.0,
        gas_scale=0.8,
        iboost_charging=False,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_gas2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas=True,
        rate_gas=10.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_gas3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas_export=True,
        rate_gas=4.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_gas4",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas_export=True,
        rate_gas=5.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate * 0.9,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_rate2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold_export=export_rate,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold_export=export_rate - 1,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_charge1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 + 12),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=12,
        charge_period_divide=2,
        export_limit=1,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 * 10 + 10),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=100,
        end_record=12 * 60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 * 10 + 10),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=True,
        assert_final_iboost=100,
        end_record=12 * 60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge4",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_rate_threshold=import_rate - 1,
        iboost_charging=True,
        assert_final_iboost=0,
        end_record=12 * 60,
    )
    failed |= simple_scenario(
        "iboost_discharge1",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10,
        assert_final_soc=0,
        battery_soc=10,
        with_battery=True,
        discharge=0,
        battery_size=10,
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_discharge2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        discharge=0,
        battery_size=100,
        iboost_enable=True,
        export_limit=1,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_discharge3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        discharge=0,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_prevent_discharge1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        iboost_prevent_discharge=False,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_prevent_discharge2",
        my_predbat,
        0,
        0,
        assert_final_metric=24 * import_rate,
        assert_final_soc=100,
        battery_soc=100,
        with_battery=True,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        iboost_prevent_discharge=True,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "keep_discharge1",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        battery_soc=10,
        with_battery=True,
        discharge=0,
        battery_size=10,
        keep=1.0,
        keep_weight=1.0,
        assert_final_iboost=0,
        assert_keep=import_rate * 14 + import_rate * 1 * 0.5,
    )

    # Alternating high/low rates
    reset_rates2(my_predbat, import_rate, export_rate)
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=120,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_smart1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=120,
        iboost_max_energy=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    # iboost_smart_min_length (60) is greater than plan_interval_minutes (30), so each window spans two
    # sub-slots and its true average is import_rate * 1.5 (GH#4817). The totals below are the actual
    # achieved metric/iboost from the fixed averaging, not a hand-derived formula.
    failed |= simple_scenario(
        "iboost_smart2",
        my_predbat,
        0,
        0,
        assert_final_metric=950,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=65,
        iboost_max_energy=60,
        iboost_smart_min_length=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_smart3",
        my_predbat,
        0,
        0,
        assert_final_metric=900,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=60,
        iboost_max_energy=55,
        iboost_smart_min_length=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )

    failed |= simple_scenario(
        "iboost_rate_pv1",
        my_predbat,
        0,
        1.0,
        assert_final_metric=-export_rate * 12 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12,
        export_limit=1,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    failed |= simple_scenario(
        "iboost_rate_pv2",
        my_predbat,
        0,
        1.0,
        assert_final_metric=-export_rate * 12 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12 * 1,
        export_limit=2,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    failed |= simple_scenario(
        "iboost_rate_pv3",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 12 * 2 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12 * 2,
        export_limit=2,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )

    # PV AC limit tests (AC-coupled / non-hybrid inverters only)
    reset_rates(my_predbat, import_rate, export_rate)
    reset_inverter(my_predbat)
    # No clipping when pv_ac_limit is above the actual PV output
    failed |= simple_scenario("pv_ac_limit_no_clip", my_predbat, 0, 1.0, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, pv_ac_limit=2.0, assert_clipped=0)
    # Clipping when pv_ac_limit is below the actual PV output (non-hybrid AC-coupled)
    failed |= simple_scenario("pv_ac_limit_ac_clip", my_predbat, 0, 2.0, assert_final_metric=-export_rate * 24 * 1.5, assert_final_soc=0, with_battery=False, pv_ac_limit=1.5, assert_clipped=24 * 0.5)
    # With a load, clipping still applies; load is met from grid when pv is capped
    failed |= simple_scenario(
        "pv_ac_limit_ac_clip_with_load",
        my_predbat,
        0.5,
        2.0,
        assert_final_metric=-export_rate * 24 * 1.0,
        assert_final_soc=0,
        with_battery=False,
        pv_ac_limit=1.5,
        assert_clipped=24 * 0.5,
    )
    # pv_ac_limit must NOT apply to hybrid inverters (PV is DC-coupled, clipping handled by inverter_limit)
    failed |= simple_scenario("pv_ac_limit_hybrid_ignored", my_predbat, 0, 2.0, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True, pv_ac_limit=1.5, assert_clipped=0)

    # ---- Clipping Peak Cost Penalty Tests ----
    reset_rates(my_predbat, import_rate, export_rate)
    reset_inverter(my_predbat)

    # No penalty when peak PV is below the clipping limit
    # 0.5kW PV, 1kW inverter limit => no clipping, metric is just export revenue
    failed |= simple_scenario(
        "clipping_peak_no_clip",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=0,
        with_battery=False,
        inverter_limit=1.0,
        clipping_buffer_enable=True,
        clipping_cost_weight=1.0,
    )

    # Penalty when peak PV exceeds inverter limit and no battery to absorb
    # 2kW PV, 1kW inverter limit, no battery => with_battery=False means battery_rate_max_scaling=0
    # AC-coupled PV isn't clipped by inverter_limit, so full 2kW is exported
    # Without clipping penalty: metric = -export_rate * 24 * 2 = -240p
    # With clipping penalty: extra cost added for the 1kW excess above inverter_limit
    failed_no_penalty, pred_no_penalty = simple_scenario(
        "clipping_peak_baseline",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 24 * 2.0,
        assert_final_soc=0,
        with_battery=False,
        inverter_limit=1.0,
        clipping_buffer_enable=False,
        return_prediction_handle=True,
    )
    failed |= failed_no_penalty

    failed_with_penalty, pred_with_penalty = simple_scenario(
        "clipping_peak_with_penalty",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 24 * 2.0,  # will differ due to penalty; checked below
        assert_final_soc=0,
        with_battery=False,
        inverter_limit=1.0,
        clipping_buffer_enable=True,
        clipping_cost_weight=1.0,
        return_prediction_handle=True,
        ignore_failed=True,
    )
    # The penalty should make the metric less negative (higher) than without
    metric_no_penalty = round(pred_no_penalty.predict_metric_best[max(pred_no_penalty.predict_metric_best.keys())] / 100.0, 2) if pred_no_penalty.predict_metric_best else 0
    metric_with_penalty = round(pred_with_penalty.predict_metric_best[max(pred_with_penalty.predict_metric_best.keys())] / 100.0, 2) if pred_with_penalty.predict_metric_best else 0
    if metric_with_penalty <= metric_no_penalty:
        print("ERROR: clipping_peak_with_penalty metric {} should be > {} (penalty should increase metric)".format(metric_with_penalty, metric_no_penalty))
        failed = True
    else:
        print("Run scenario clipping_peak_with_penalty: PASS (metric {} > baseline {})".format(metric_with_penalty, metric_no_penalty))

    # No penalty when battery has headroom to absorb excess
    # 2kW PV, 1kW inverter limit, but battery at 0% with 100kWh capacity => battery absorbs all excess
    failed |= simple_scenario(
        "clipping_peak_battery_absorbs",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24,
        with_battery=True,
        battery_soc=0.0,
        battery_size=100.0,
        inverter_limit=1.0,
        clipping_buffer_enable=True,
        clipping_cost_weight=1.0,
    )

    # Low power charging must not make the plan more expensive when the charge window overlaps PV production.
    # The planner costs every charge window at the full charge rate as low power is only applied to the final
    # plan, so a throttled rate that caps how much PV reaches the battery pushes the cost above the plan.
    reset_rates(my_predbat, import_rate, export_rate)
    reset_inverter(my_predbat)

    # 6kW of PV for the first 2 hours only, with an 8 hour charge window to 12kWh and a 6kW max charge rate.
    # At full rate the PV alone fills the battery inside those 2 hours, costing nothing. Throttled to fit the
    # 8 hour window the battery would take only 1.5kW, exporting the other 4.5kW of PV at 5p and then
    # importing the missing 9kWh at 10p once the sun has gone - 45p worse than the planner costed it at.
    low_power_pv = {
        "load_amount": 0,
        "pv_amount": 6.0,
        "pv_hours": 2,
        "charge": 12,
        "charge_window_best": [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 480, "average": import_rate}],
        "battery_size": 20,
        "battery_soc": 0,
        "battery_rate_max_charge": 6.0,
        "inverter_limit": 10.0,
        "export_limit": 10.0,
        "assert_final_soc": 12,
        "assert_final_metric": 0,
    }
    failed |= simple_scenario("low_power_pv_full_rate", my_predbat, set_charge_low_power=False, **low_power_pv)
    failed |= simple_scenario("low_power_pv_low_power", my_predbat, set_charge_low_power=True, **low_power_pv)

    # With no PV in the window low power charging still applies, the whole 12kWh comes from the grid either way
    low_power_dark = dict(low_power_pv)
    low_power_dark["pv_amount"] = 0
    low_power_dark["assert_final_metric"] = import_rate * 12
    failed |= simple_scenario("low_power_dark_full_rate", my_predbat, set_charge_low_power=False, **low_power_dark)
    failed |= simple_scenario("low_power_dark_low_power", my_predbat, set_charge_low_power=True, **low_power_dark)

    my_predbat.prediction_kernel_enable = False
    if failed:
        print("**** ERROR: Some Model tests failed ****")
    return failed
