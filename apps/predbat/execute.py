"""Execution module for applying optimised charge/discharge plans to inverters.

Handles the translation of PredBat's optimised plan into concrete inverter
control actions. Manages charge window programming, discharge/export scheduling,
reserve level adjustments, and multi-inverter balancing.
"""

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import timedelta, datetime
from const import MINUTE_WATT, EXPORT_LIMIT_IDLE, EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, CHARGE_STATE_PRECEDENCE, EXPORT_STATE_PRECEDENCE
from utils import dp0, dp2, dp3, calc_percent_limit, find_charge_rate, balance_inverters, allocate_export_rates, export_mode_of, export_power_of, export_target_of
from predbat_metrics import metrics
from inverter import Inverter
import time

"""
Execute Predbat plan
"""

# The precedence lists themselves live in const.py, as output.py's history reconstruction needs the
# same most-active-first ordering to collapse a slot that changed state part way through (#4843).
CHARGE_SIDE_STATES = set(CHARGE_STATE_PRECEDENCE)
EXPORT_SIDE_STATES = set(EXPORT_STATE_PRECEDENCE)


def resolve_multi_inverter_status(status_per_inverter, current_status):
    """Resolve one headline status across a multi-inverter fleet.

    ``status_per_inverter`` holds each inverter's own final core charge/export state, keyed by
    inverter id, as recorded during execute_plan()'s per-inverter loop - a plain dict overwrite per
    inverter, so it correctly reflects each inverter's own last-set state even if that inverter's own
    processing passed through more than one core-state assignment.

    If inverters disagree across the charge/export divide (one genuinely charging while another
    discharges at the same time) that's real cross-charging, not noise - surfaced explicitly rather
    than silently showing whichever inverter happened to be processed last. If they only disagree on
    sub-state within the same side (e.g. one still actively Charging, another already Hold charging),
    the most active one is shown - it's the most informative and matches what the fleet is actually
    doing overall.

    Falls through to ``current_status`` unchanged when no inverter reached a core charge/export state
    at all (pure Demand, Read-Only, Calibration, or a Hold-for-car/iBoost annotation with nothing else
    going on) - those cases are already correct and untouched by this resolution. Calibration always
    wins outright even if ``status_per_inverter`` holds a stale core state from an inverter processed
    before the one that entered calibration mode, since calibration force-overrides every inverter's
    controls and that must not be hidden behind a leftover Charging/Exporting label.
    """
    if current_status == "Calibration":
        return current_status
    states_present = set(status_per_inverter.values())
    charge_states_present = states_present & CHARGE_SIDE_STATES
    export_states_present = states_present & EXPORT_SIDE_STATES
    if charge_states_present and export_states_present:
        return "Cross-charging"
    if charge_states_present:
        return next(candidate for candidate in CHARGE_STATE_PRECEDENCE if candidate in charge_states_present)
    if export_states_present:
        return next(candidate for candidate in EXPORT_STATE_PRECEDENCE if candidate in export_states_present)
    return current_status


def build_status_extra(status_extra_parts):
    """Assemble the per-inverter status detail text recorded during execute_plan().

    Each entry is ``(inverter_id, lead, label, detail)``: ``lead`` is the introductory word used by the
    first inverter ("target"/"current SoC"), ``label`` is that inverter's own core charge/export state
    and ``detail`` the SoC figures.

    The per-inverter ``label`` only carries information when the fleet's inverters actually disagree.
    #4466 added it so a mixed fleet could be read at all, but emitting it unconditionally repeated the
    headline status on every system whose inverters agree - a single-inverter install showed
    "Exporting target Exporting 19%-5%". Labels are therefore dropped when every segment carries the
    same one, restoring the pre-#4466 text for a single inverter and for any fleet acting in unison,
    and kept when they differ.
    """
    if not status_extra_parts:
        return ""
    show_labels = len({label for _, _, label, _ in status_extra_parts}) > 1
    status_extra = ""
    for inverter_id, lead, label, detail in status_extra_parts:
        # Only the first inverter introduces the text; the rest are appended after a separator.
        status_extra += " {}".format(lead) if inverter_id == 0 else " /"
        status_extra += " {} {}".format(label, detail) if show_labels else " {}".format(detail)
    return status_extra


def export_target_percent_or_zero(export_limit):
    """The SoC percentage an export instruction targets, or 0 where it carries no target.

    Only EXPORT_MODE_TARGET has a target; export_target_of returns None for the other two modes so a
    caller cannot use their sentinels as if they were one. Every value this feeds here - the
    discharge floor, the displayed target, the inverter target register - is a percentage, so the
    two modes resolve to the bottom of the range rather than to None.

    Spelled out rather than written `or 0` at each site: that reads as a guard against a falsy
    target, which is not what is being guarded. The distinction matters because the callers all sit
    inside a target-mode branch already, so the fallback is unreachable for them and a reader needs
    to see that it is a type normalisation and not a live default.
    """
    target = export_target_of(export_limit)
    return 0 if target is None else target


class Execute:
    """Execution mixin for applying optimised plans to physical inverters.

    Translates the best charge/discharge plan into concrete inverter
    control actions including window programming, rate setting, reserve
    adjustment, and multi-inverter balancing.
    """

    def clear_control_ledger(self, reason):
        """Drop every control ownership record, if the ledger is configured.

        Called wherever PredBat stops controlling the inverter - read-only mode and calibration.

        Deliberately global, and both callers are genuinely fleet-wide even though they read as
        though they were per-inverter. set_read_only is one config flag for the whole install. And
        the calibration branch, on finding ANY inverter in calibration, writes charge rate,
        discharge rate, battery target and reserve to EVERY inverter through the ordinary helpers
        before breaking out - so every inverter's ownership is conferred there and every
        inverter's has to go. Scoping this to the inverter that happened to trigger it would leave
        the others owning values calibration itself had just written.

        Logged because a user whose detection reports nothing needs to be able to find out why.
        """
        if self.control_ledger is None:
            return
        if self.control_ledger.records:
            self.log("Control ledger: dropping ownership of {} control(s) - {}".format(len(self.control_ledger.records), reason))
        self.control_ledger.clear()

    def build_inverter_snapshot(self):
        """
        Build the plain per-inverter readings that balance_inverters() consumes.

        Keeping the readings as plain scalars is what lets the balancing algorithm live in utils as
        a pure function, testable without a PredBat instance or a mock Home Assistant.

        Returns:
        - list: one dict per inverter, indexed by inverter id
        """
        snapshot = []
        for inverter in self.inverters:
            snapshot.append(
                {
                    "soc_percent": inverter.soc_percent,
                    "reserve_percent": inverter.reserve_current,
                    "battery_power": inverter.battery_power,
                    "pv_power": inverter.pv_power,
                    "grid_power": inverter.grid_power,
                    "charge_rate_now": inverter.charge_rate_now * MINUTE_WATT,
                    "discharge_rate_now": inverter.discharge_rate_now * MINUTE_WATT,
                    "battery_rate_max_charge": inverter.battery_rate_max_charge * MINUTE_WATT,
                    "battery_rate_max_discharge": inverter.battery_rate_max_discharge * MINUTE_WATT,
                    "in_calibration": inverter.in_calibration,
                }
            )
        return snapshot

    def balance_inverter_rates(self, intent):
        """
        Apply fleet balancing to the executor's rate intent, if it is enabled.

        Args:
            intent (dict): inverter id -> rate intent, mutated in place
        """
        if not self.balance_inverters_enable or self.set_read_only:
            return
        if len(self.inverters) < 2:
            return
        balance_inverters(
            intent,
            self.build_inverter_snapshot(),
            self.balance_inverters_charge,
            self.balance_inverters_discharge,
            self.balance_inverters_crosscharge,
            self.balance_inverters_threshold_charge,
            self.balance_inverters_threshold_discharge,
            log_to=self.log,
        )

    def allocate_fleet_export_rates(self):
        """
        Split the planned fleet export power across the inverters, by how much each has to shed.

        A fleet-level pre-pass because it needs every inverter's target, which is why it cannot
        live inside execute_plan's per-inverter loop. adjust_battery_target_multi(check=True) is
        the existing non-writing probe, so nothing is written here.

        Outside low power mode the planned power equals the sum of the ceilings, so every inverter
        clamps at its own maximum and this reproduces today's uniform scaling exactly.

        Returns:
        - dict: inverter id -> export rate in W, empty when no export is planned
        """
        if not self.export_limits_best or not self.set_export_window:
            return {}
        if export_mode_of(self.export_limits_best[0]) != EXPORT_MODE_TARGET:
            return {}

        export_rate_adjust = export_power_of(self.export_limits_best[0]) if self.set_export_low_power else 1.0
        export_target_percent = self.export_target_soc_percent()

        needs = []
        max_rates = []
        for inverter in self.inverters:
            inv_target_percent = self.adjust_battery_target_multi(inverter, export_target_percent, False, True, check=True)
            target_kwh = inv_target_percent * inverter.soc_max / 100.0
            needs.append(max(0.0, inverter.soc_kw - target_kwh))
            max_rates.append(inverter.battery_rate_max_export * MINUTE_WATT)

        allocation = allocate_export_rates(needs, max_rates, sum(max_rates) * export_rate_adjust)
        result = {}
        for index, inverter in enumerate(self.inverters):
            result[inverter.id] = allocation[index]
        self.log("Export allocation: needs {}kWh ceilings {}W adjust {} -> {}W".format([dp2(need) for need in needs], [dp0(rate) for rate in max_rates], export_rate_adjust, [dp0(rate) for rate in allocation]))
        return result

    def apply_rate_intent(self, intent):
        """
        Apply a balanced intent across the fleet, keeping balancing's own changes silent.

        A rate balancing overrides is written without notifying, and so is the write that takes it
        back off again - the release edge matters as much as the hold, or a fleet drifting in and
        out of balance notifies on every transition. Which rates were overridden last pass is
        remembered for exactly that reason.

        Args:
            intent (dict): inverter id -> rate intent, already balanced
        """
        baseline = self.inverter_rate_intent
        overridden_now = {}
        for inverter in self.inverters:
            if inverter.id not in intent:
                continue
            entry = intent[inverter.id]
            base = baseline.get(inverter.id, {})
            previously = self.inverter_balance_overridden.get(inverter.id, ())
            charge_overridden = entry.get("charge_rate", None) != base.get("charge_rate", None)
            discharge_overridden = entry.get("discharge_rate", None) != base.get("discharge_rate", None)
            overridden_now[inverter.id] = {direction for direction, flag in (("charge", charge_overridden), ("discharge", discharge_overridden)) if flag}
            self.apply_inverter_rates(
                inverter,
                entry,
                notify_charge=not (charge_overridden or "charge" in previously),
                notify_discharge=not (discharge_overridden or "discharge" in previously),
            )
        self.inverter_balance_overridden = overridden_now

    def apply_inverter_rates(self, inverter, intent, notify_charge=True, notify_discharge=True):
        """
        Write one inverter's charge and discharge rates from its intent.

        The single point at which a rate reaches the hardware. A rate of None means no branch of
        execute_plan claimed it, so the inverter returns to its own maximum - which is what the
        resetCharge / resetDischarge flags used to express at the end of the per-inverter loop.

        Because "max" is resolved here rather than produced by the balancer, a deliberate hold of
        rate 0 can no longer be overwritten by a second writer (F5 / #829).

        Args:
            inverter: the Inverter to write to
            intent (dict): keys charge_rate and discharge_rate (W, or None for max)
            notify_charge (bool): whether a charge rate change should notify the user
            notify_discharge (bool): likewise for discharge. apply_rate_intent() decides both;
                balancing's own changes are silent on BOTH edges, as the old timer-based balancer
                was, since it runs on a minute cadence.
        """
        charge_rate = intent.get("charge_rate", None)
        discharge_rate = intent.get("discharge_rate", None)

        # A rate nobody claimed is only reset to maximum where the executor is actually driving
        # the windows. With both off - Monitor, Control SoC only - an unclaimed rate is left
        # exactly as it is. A rate something DID claim, executor or balancer, is always written.
        reset_rates = intent.get("reset_rates", True)
        if charge_rate is None:
            if reset_rates:
                inverter.adjust_charge_rate(int(inverter.battery_rate_max_charge * MINUTE_WATT), notify=notify_charge)
        else:
            inverter.adjust_charge_rate(int(charge_rate), notify=notify_charge)
        if discharge_rate is None:
            if reset_rates:
                inverter.adjust_discharge_rate(int(inverter.battery_rate_max_discharge * MINUTE_WATT), notify=notify_discharge)
        else:
            inverter.adjust_discharge_rate(int(discharge_rate), notify=notify_discharge)

    def execute_plan(self):
        # Per-inverter detail segments, assembled into the status text after the headline status is
        # resolved - see build_status_extra() for why they can't be concatenated inline.
        status_extra_parts = []
        status_hold_car = ""  # car hold status text
        status_hold_iboost = ""  # iBoost hold status text
        status_freeze_export = ""  # freeze export during demand status text
        # Each inverter's own final core charge/export state, keyed by inverter id - used after the
        # loop to detect genuine cross-charging (one inverter charging while another discharges at
        # the same time) rather than silently showing whichever inverter's status happened to be
        # set last, which hides that the fleet is fighting itself.
        status_per_inverter = {}

        in_alert = self.alert_active_keep.get(self.minutes_now, 0) > 0
        in_manual_soc = self.manual_soc_keep.get(self.minutes_now, 0) > 0
        in_manual_soc_max = self.manual_soc_max_keep.get(self.minutes_now, 0) > 0

        # Safeguard for set_charge_freeze_only: the planner never selects a charge target above the
        # reserve while the switch is on, but a plan computed before it was turned on can still be
        # in play (plans are only recomputed every few minutes, and are held across cycles by
        # metric_min_improvement_plan), and with calculate_best_charge off the limits come from the
        # inverter's own settings rather than the planner at all. Clamp here so the battery can
        # never be charged from the grid, whatever the plan says.
        if self.set_charge_freeze_only:
            for window_n in range(len(self.charge_limit_best)):
                if self.charge_limit_best[window_n] > self.reserve and not self.is_freeze_charge(self.charge_limit_best[window_n]):
                    self.log("Warn: Charge limit {}kWh for window {} clamped to freeze charge as set_charge_freeze_only is enabled".format(self.charge_limit_best[window_n], window_n))
                    self.charge_limit_best[window_n] = self.reserve

        if self.holiday_days_left > 0:
            status = "Demand (Holiday)"
        else:
            status = "Demand"

        if self.inverter_needs_reset:
            self.reset_inverter()

        # Belt and braces: the read-only branch below runs before anything that could confer
        # ownership - but ownership from BEFORE read-only was enabled would survive, and
        # PredBat is no longer controlling anything, so it is not ours to claim. set_read_only
        # is a global flag, so this is decided once rather than re-decided per inverter.
        if self.set_read_only:
            self.clear_control_ledger("read-only mode is enabled, so Predbat is not setting anything")

        isCharging = False
        isExporting = False
        intent = {}
        export_rate_alloc = self.allocate_fleet_export_rates()
        for inverter in self.inverters:
            if inverter.id not in self.count_inverter_writes:
                self.count_inverter_writes[inverter.id] = 0

            # Read-only mode
            if self.set_read_only:
                if self.set_read_only_axle:
                    status = "Read-Only (Axle)"
                else:
                    status = "Read-Only"
                continue
            # Inverter is in calibration mode
            if inverter.in_calibration:
                status = "Calibration"
                self.log("Inverter {} is in calibration mode, not executing plan and enabling charge/discharge at full rate.".format(inverter.id))
                for inverter in self.inverters:
                    inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
                    inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
                    inverter.adjust_battery_target(100.0, False)
                    inverter.adjust_reserve(0)
                # Those writes go through the ordinary helpers, so they CONFER ownership - in
                # exactly the mode where the inverter's own firmware is driving its settings.
                # A value found moved next cycle is the inverter calibrating, not a third
                # party, so ownership is dropped after the writes rather than before them.
                self.clear_control_ledger("inverter {} is calibrating, so its own firmware is driving the settings".format(inverter.id))
                # Inverters processed before this one already recorded their planned intent.
                # Applying it now would write those rates straight back over the full-rate
                # calibration settings above, and keeping it as the poll baseline would have the
                # 60s poll re-apply them for as long as calibration lasts.
                intent.clear()
                break

            charge_rate = None
            discharge_rate = None
            rate_owner = "demand"
            # Whether an unclaimed rate should be reset to maximum at all. This is what the
            # resetCharge / resetDischarge flags carried: in Monitor and Control-SoC-only modes
            # both windows are off and they started FALSE, so no rate was written. Predbat is
            # watching in those modes, not controlling.
            reset_rates = self.set_charge_window or self.set_export_window
            pause_charge_requested = False
            pause_discharge_requested = False
            resetPause = self.set_charge_window or self.set_export_window
            resetReserve = self.set_charge_window or self.set_export_window
            disabled_charge_window = False
            disabled_export = False

            # Re-programme charge window based on low rates?
            if self.set_charge_window and self.charge_window_best:
                # Find the next best window and save it
                window = self.charge_window_best[0]
                minutes_start = window["start"]
                minutes_end = window["end"]

                # Combine contiguous windows
                for windows in self.charge_window_best:
                    if minutes_end == windows["start"]:
                        minutes_end = windows["end"]
                        if self.debug_enable:
                            self.log("Combine window with next window {}-{}".format(self.time_abs_str(windows["start"]), self.time_abs_str(windows["end"])))

                # Avoid adjust avoid start time forward when it's already started
                if (inverter.charge_start_time_minutes <= self.minutes_now) and (self.minutes_now >= minutes_start):
                    self.log("Include original charge start {}, keeping this instead of new start {}".format(self.time_abs_str(inverter.charge_start_time_minutes), self.time_abs_str(minutes_start)))
                    minutes_start = inverter.charge_start_time_minutes

                # Avoid having too long a period to configure as registers only support 24-hours
                if (minutes_start < self.minutes_now) and ((minutes_end - minutes_start) >= 24 * 60):
                    minutes_start = int(self.minutes_now / self.plan_interval_minutes) * self.plan_interval_minutes
                    self.log("Move on charge window start time to avoid wrap - new start {}".format(self.time_abs_str(minutes_start)))

                # Span midnight allowed?
                if not inverter.inv_can_span_midnight:
                    if minutes_start < 24 * 60 and minutes_end >= 24 * 60:
                        minutes_end = 24 * 60 - 1

                # Are we currently in the export window?
                inExportWindow = False
                if self.set_export_window and self.export_window_best:
                    if self.minutes_now >= self.export_window_best[0]["start"] and self.minutes_now < self.export_window_best[0]["end"]:
                        inExportWindow = True

                # Check if start is within 24 hours of now and end is in the future
                if (not inExportWindow) and ((minutes_start - self.minutes_now) < (24 * 60)) and (minutes_end > self.minutes_now):
                    charge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                    charge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                    self.log("Inverter {} Charge window will be: {} - {} - current SoC {}%, target {}%".format(inverter.id, charge_start_time, charge_end_time, inverter.soc_percent, calc_percent_limit(self.charge_limit_best[0], self.soc_max)))
                    # Are we actually charging?
                    if self.minutes_now >= minutes_start and self.minutes_now < minutes_end:
                        is_freeze_charge = self.is_freeze_charge(self.charge_limit_best[0])
                        target_soc = calc_percent_limit(self.charge_limit_best[0], self.soc_max) if not is_freeze_charge else calc_percent_limit(inverter.soc_kw, inverter.soc_max)
                        inv_target_soc_percent = self.adjust_battery_target_multi(inverter, target_soc, True, False, check=True, isFreezeCharge=is_freeze_charge)

                        current_charge_rate = inverter.get_current_charge_rate()

                        # How much PV is still forecast before this charge window closes?
                        pv_window_kwh = 0.0
                        if self.set_charge_low_power:
                            for pv_minute in range(self.minutes_now, window["end"]):
                                pv_window_kwh += self.pv_forecast_minute.get(pv_minute, 0.0)

                        new_charge_rate, new_charge_rate_real = find_charge_rate(
                            self.minutes_now,
                            inverter.soc_kw,
                            window,
                            inv_target_soc_percent * inverter.soc_max / 100.0,
                            inverter.battery_rate_max_charge,
                            inverter.soc_max,
                            self.battery_charge_power_curve,
                            self.set_charge_low_power,
                            self.charge_low_power_margin,
                            self.battery_rate_min,
                            self.battery_rate_max_scaling,
                            self.battery_loss,
                            self.log,
                            inverter.battery_temperature,
                            self.battery_temperature_charge_curve,
                            current_charge_rate=current_charge_rate / MINUTE_WATT,
                            pv_window_kwh=pv_window_kwh,
                            low_power_pv_threshold_w=self.low_power_pv_threshold_w,
                            solar_full_rate=self.set_charge_low_power_solar_full_rate,
                        )
                        new_charge_rate = int(new_charge_rate * MINUTE_WATT)

                        self.log(
                            "Inverter {} Target SoC {}%, (this inverter {}%), battery temperature {}°C, select charge rate {}W (real {}W), current charge rate {}W".format(
                                inverter.id, dp0(target_soc), dp0(inv_target_soc_percent), inverter.battery_temperature, new_charge_rate, dp0(new_charge_rate_real * MINUTE_WATT), current_charge_rate
                            )
                        )

                        # No deadband here: adjust_charge_rate already suppresses a change below
                        # 5% of max, which lines up with the GE power steps. The 10% that used to
                        # live here came from the same PR (#1676) as that 5% and was never
                        # reconciled with it; being the stricter of the two it was the only one
                        # that ever fired. Intent now carries the rate we actually want rather
                        # than one a deadband has rounded off.
                        #
                        # That 5% governs the power register. Inverters with
                        # inv_output_charge_control == "current" drive a timed-current register as
                        # their real control and re-assert it every cycle by design (#4415), with
                        # write_and_poll_value's own read-compare deciding whether a write is
                        # needed - so on those types a sub-5% change can still write. That is
                        # deliberate: deadbanding the real control would cost precision, not
                        # writes.
                        max_rate = inverter.battery_rate_max_charge * MINUTE_WATT
                        charge_rate = new_charge_rate
                        rate_owner = "charge"

                        if inverter.inv_charge_discharge_with_rate:
                            discharge_rate = 0

                        # Can only freeze charge for this inverter if its SoC is above reserve and it can hold via reserve/pause
                        can_freeze_charge = True
                        if inverter.soc_kw < inverter.reserve:
                            can_freeze_charge = False
                        if not inverter.inv_has_timed_pause and (inverter.reserve_max < inverter.soc_percent):
                            can_freeze_charge = False
                        if self.is_freeze_charge(self.charge_limit_best[0]) and can_freeze_charge:
                            if self.set_soc_enable and ((self.set_reserve_enable and self.set_reserve_hold and inverter.reserve_max >= inverter.soc_percent) or inverter.inv_has_timed_pause):
                                inverter.disable_charge_window()
                                disabled_charge_window = True
                                if self.set_reserve_enable and (not inverter.inv_has_timed_pause):
                                    inverter.adjust_reserve(min(inverter.soc_percent + 1, 100))
                                    resetReserve = False
                            else:
                                inverter.adjust_charge_window(charge_start_time, charge_end_time, self.minutes_now)

                            if inverter.inv_has_timed_pause:
                                inverter.adjust_pause_mode(pause_discharge=True)
                                pause_discharge_requested = True
                                resetPause = False
                            else:
                                discharge_rate = 0

                            status = "Freeze charging"
                            status_per_inverter[inverter.id] = status
                            status_extra_parts.append((inverter.id, "target", status, "{}%".format(inverter.soc_percent)))  # Append multi-inverter target SoC's together
                            self.log("Inverter {} Freeze charging with SoC {}%".format(inverter.id, inverter.soc_percent))
                        else:
                            # We can only hold charge if a) we have a way to hold the charge level on the reserve or with a pause feature
                            # and the current charge level is above the target for all inverters
                            if self.set_soc_enable and inverter.soc_percent >= inv_target_soc_percent:
                                status = "Hold charging"
                                status_per_inverter[inverter.id] = status
                                self.log(
                                    "Inverter {} Hold charging as SoC {}% is above target SoC {}% (global soc target {}%) set_discharge_during_charge {}".format(
                                        inverter.id, inverter.soc_percent, dp0(inv_target_soc_percent), dp0(target_soc), self.set_discharge_during_charge
                                    )
                                )

                                if (inv_target_soc_percent < 100.0) and (inverter.soc_percent >= inv_target_soc_percent - 1.0):
                                    # If we are at or above the target, or within 1% below it, hold charge by disabling the charge window.
                                    # Only keep charging enabled if SOC is more than 1% below target.
                                    if self.set_soc_enable and ((self.set_reserve_enable and self.set_reserve_hold and inverter.reserve_max >= inv_target_soc_percent) or inverter.inv_has_timed_pause):
                                        inverter.disable_charge_window()
                                        disabled_charge_window = True

                                        # Hold on reserve if we can't pause or won't pause the discharge (due to higher battery level)
                                        if self.set_reserve_enable and (not inverter.inv_has_timed_pause or (inverter.soc_percent > inv_target_soc_percent)):
                                            inverter.adjust_reserve(min(inv_target_soc_percent + 1, 100, inverter.reserve_max))
                                            resetReserve = False
                                    else:
                                        inverter.adjust_charge_window(charge_start_time, charge_end_time, self.minutes_now)

                                    # Pause the discharge only if we are actually at or below the target Soc.
                                    if inverter.soc_percent <= inv_target_soc_percent:
                                        if inverter.inv_has_timed_pause:
                                            inverter.adjust_pause_mode(pause_discharge=True)
                                            pause_discharge_requested = True
                                            resetPause = False
                                        else:
                                            discharge_rate = 0
                                    # Else we will be holding on reserve
                                else:
                                    # Still charging or we have no way to hold on either reserve or pause the discharge
                                    inverter.adjust_charge_window(charge_start_time, charge_end_time, self.minutes_now)
                            else:
                                status = "Charging"
                                status_per_inverter[inverter.id] = status
                                inverter.adjust_charge_window(charge_start_time, charge_end_time, self.minutes_now)

                            status_extra_parts.append((inverter.id, "target", status, "{}%-{}%".format(inverter.soc_percent, inv_target_soc_percent)))  # append multi-inverter target SoC's together

                        if not self.set_discharge_during_charge and resetPause:
                            # Do we discharge discharge during charge
                            if inverter.inv_has_timed_pause:
                                inverter.adjust_pause_mode(pause_discharge=True)
                                pause_discharge_requested = True
                                resetPause = False
                            else:
                                discharge_rate = 0
                            self.log("Disabling discharge during charge due to set_discharge_during_charge being False")

                        isCharging = True
                        self.isCharging_Target = self.charge_limit_best[0]
                    else:
                        # Configure the charge window start/end times if in the time window to set them
                        if (self.minutes_now < minutes_end) and ((minutes_start - self.minutes_now) <= self.set_window_minutes):
                            # We must re-program if we are about to start a new charge window or the currently configured window is about to start or has started
                            # If we are going into freeze mode but haven't yet then don't configure the charge window as it will mean a spike of charging first
                            if not isCharging and self.is_freeze_charge(self.charge_limit_best[0]):
                                self.log("Charge window will be disabled as freeze charging is planned")
                                inverter.disable_charge_window()
                            else:
                                self.log(
                                    "Inverter {} configuring charge window now (now {} target set_window_minutes {} charge start time {}".format(inverter.id, self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start))
                                )

                                # Track IOG action latency for SLO metrics (if this is an IOG slot)
                                if self.octopus_intelligent_charging:
                                    # Calculate latency from slot start time
                                    slot_start_timestamp = charge_start_time.timestamp()
                                    current_timestamp = time.time()
                                    latency = max(0, current_timestamp - slot_start_timestamp)  # Don't record negative latency

                                    # Only record if we're within reasonable window (e.g., within 10 minutes of start)
                                    if latency < 600:  # 10 minutes
                                        self.iog_last_action_latency_seconds = latency
                                        self.iog_last_action_status = "success"
                                        metrics().iog_action_latency_seconds.observe(latency)
                                        metrics().iog_actions_total.labels(status="success").inc()
                                        self.log("IOG action latency: {:.1f} seconds from slot start".format(latency))

                                inverter.adjust_charge_window(charge_start_time, charge_end_time, self.minutes_now)
                        else:
                            # If there is a previous set charge window, but its more than the set window minutes in the future but we are not close to it then we can leave it enabled for now
                            # saves register writes and avoids turning off the window before the inverter knows it finished
                            if (inverter.charge_start_time_minutes != inverter.charge_end_time_minutes) and (inverter.charge_start_time_minutes - self.minutes_now) > self.set_window_minutes:
                                self.log(
                                    "Inverter {} leaving charge window as already set and more than set_window_minutes {} away (now {} charge start time {})".format(
                                        inverter.id, self.set_window_minutes, self.time_abs_str(self.minutes_now), self.time_abs_str(inverter.charge_start_time_minutes)
                                    )
                                )
                            else:
                                self.log(
                                    "Inverter {} disabled charge window while waiting for schedule (now {} target set_window_minutes {} charge start time {}) original inverter start time {}".format(
                                        inverter.id, self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start), self.time_abs_str(inverter.charge_start_time_minutes)
                                    )
                                )
                                inverter.disable_charge_window()
                else:
                    self.log(
                        "Inverter {} Disabled charge window while waiting for schedule (now {} target set_window_minutes {} charge start time {})".format(
                            inverter.id, self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)
                        )
                    )
                    inverter.disable_charge_window()
            elif self.set_charge_window:
                self.log("Inverter {} No charge window yet, waiting for schedule.".format(inverter.id))
                inverter.disable_charge_window()
            else:
                self.log("Inverter {} Set charge window is disabled".format(inverter.id))

            # Set discharge modes/window?
            if self.set_export_window and self.export_window_best:
                window = self.export_window_best[0]
                minutes_start = window["start"]
                minutes_end = window["end"]

                # Avoid adjust avoid start time forward when it's already started
                if (inverter.discharge_start_time_minutes <= self.minutes_now) and (self.minutes_now >= minutes_start):
                    minutes_start = inverter.discharge_start_time_minutes
                    # Don't allow overlap with charge window
                    if minutes_start < inverter.charge_end_time_minutes and minutes_end >= inverter.charge_start_time_minutes:
                        minutes_start = max(window["start"], self.minutes_now)
                    else:
                        self.log(
                            "Include original export start {} with our start which is {} (charge start {} end {})".format(
                                self.time_abs_str(inverter.discharge_start_time_minutes),
                                self.time_abs_str(minutes_start),
                                self.time_abs_str(inverter.charge_start_time_minutes),
                                self.time_abs_str(inverter.charge_end_time_minutes),
                            )
                        )

                # Avoid having too long a period to configure as registers only support 24-hours
                if (minutes_start < self.minutes_now) and ((minutes_end - minutes_start) >= 24 * 60):
                    minutes_start = int(self.minutes_now / self.plan_interval_minutes) * self.plan_interval_minutes
                    self.log("Move on export window start time to avoid wrap - new start {}".format(self.time_abs_str(minutes_start)))

                export_adjust = 1
                # Span midnight allowed?
                if not inverter.inv_can_span_midnight:
                    if minutes_start < 24 * 60 and minutes_end >= 24 * 60:
                        minutes_end = 24 * 60 - 1
                    export_adjust = 0

                # Overlap into charge slot if 1 minute was added, then don't add the 1 minute
                if inverter.charge_start_time_minutes == minutes_end:
                    export_adjust = 0

                # Turn minutes into time
                discharge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                discharge_end_time = self.midnight_utc + timedelta(minutes=(minutes_end + export_adjust))  # Add in 1 minute margin to allow Predbat to restore demand mode
                discharge_soc = max((export_target_percent_or_zero(self.export_limits_best[0]) * self.soc_max) / 100.0, self.reserve, self.best_soc_min)
                self.log("Next export window will be: {} - {} at reserve {}".format(discharge_start_time, discharge_end_time, self.export_limits_best[0]))
                if (self.minutes_now >= minutes_start) and (self.minutes_now < minutes_end) and (export_mode_of(self.export_limits_best[0]) != EXPORT_MODE_IDLE):
                    if not self.set_export_freeze_only and export_mode_of(self.export_limits_best[0]) == EXPORT_MODE_TARGET and (self.soc_kw > discharge_soc):
                        if self.set_export_low_power:
                            export_rate_adjust = export_power_of(self.export_limits_best[0])
                        else:
                            export_rate_adjust = 1.0

                        self.log("Exporting now - current SoC {}kWh and target {}kWh and power adjust {}".format(self.soc_kw, dp2(discharge_soc), export_rate_adjust))

                        discharge_rate = export_rate_alloc.get(inverter.id, inverter.battery_rate_max_export * export_rate_adjust * MINUTE_WATT)
                        rate_owner = "export"
                        inverter.adjust_force_export(True, discharge_start_time, discharge_end_time)
                        if inverter.inv_charge_discharge_with_rate:
                            charge_rate = 0
                        isExporting = True
                        # The window carries a plain-number target once clipped; fall back to the
                        # instruction's own target rather than to the instruction itself
                        target = self.export_window_best[0].get("target")
                        if target is None:
                            target = export_target_percent_or_zero(self.export_limits_best[0])
                        self.isExporting_Target = int(target)

                        status = "Exporting"
                        status_per_inverter[inverter.id] = status
                        status_extra_parts.append((inverter.id, "target", status, "{}%-{}%".format(inverter.soc_percent, int(target))))  # append multi-inverter target SoC's together
                        # Immediate export mode
                    else:
                        inverter.adjust_force_export(False)
                        disabled_export = True
                        if self.set_export_freeze and export_mode_of(self.export_limits_best[0]) == EXPORT_MODE_FREEZE:
                            # In export freeze mode we disable charging during export slots
                            if inverter.inv_charge_discharge_with_rate:
                                charge_rate = 0
                            if inverter.inv_has_timed_pause:
                                inverter.adjust_pause_mode(pause_charge=True)
                                pause_charge_requested = True
                                resetPause = False
                            else:
                                charge_rate = 0

                            self.log("Export Freeze as exporting is now at/below target - current SoC {}kWh and target {}kWh".format(self.soc_kw, discharge_soc))
                            status = "Freeze exporting"
                            status_per_inverter[inverter.id] = status
                            # Discharge limit (99) is meaningless when Freeze Exporting so don't display it
                            status_extra_parts.append((inverter.id, "current SoC", status, "{}%".format(inverter.soc_percent)))  # append multi-inverter target SoC's together
                            isExporting = True
                            target = self.export_window_best[0].get("target")
                            if target is None:
                                target = export_target_percent_or_zero(self.export_limits_best[0])
                            self.isExporting_Target = int(target)
                        else:
                            status = "Hold exporting"
                            status_per_inverter[inverter.id] = status
                            target = self.export_window_best[0].get("target")
                            if target is None:
                                target = export_target_percent_or_zero(self.export_limits_best[0])
                            status_extra_parts.append((inverter.id, "target", status, "{}%-{}%".format(inverter.soc_percent, inverter.soc_percent)))  # append multi-inverter target SoC's together
                            self.isExporting_Target = inverter.soc_percent
                            self.log("Export Hold (Demand mode) as export is now at/below target or freeze only is set - current SoC {}kWh and target {}kWh".format(self.soc_kw, discharge_soc))
                else:
                    if (self.minutes_now < minutes_end) and ((minutes_start - self.minutes_now) <= self.set_window_minutes) and (export_mode_of(self.export_limits_best[0]) == EXPORT_MODE_TARGET):
                        # We can't schedule freeze export only full export
                        # Don't turn off ECO mode for GE inverters except when we are within the export window as it will stop the battery being used
                        ge_inverters = inverter.inv_has_ge_eco_toggle or inverter.inv_has_ge_inverter_mode
                        inverter.adjust_force_export(inverter.inv_has_discharge_enable_time and not ge_inverters, discharge_start_time, discharge_end_time)
                    else:
                        self.log("Not setting export as we are not yet within the export window - next time is {} - {}".format(self.time_abs_str(minutes_start), self.time_abs_str(minutes_end)))
                        inverter.adjust_force_export(False)
            elif self.set_export_window:
                self.log("No export window planned")
                inverter.adjust_force_export(False)

            # if Predbat is not currently Charging or Exporting, and set_freeze_export_during_demand is On, then disable charging (i.e. set Freeze Export) (to prevent cross charging)
            if (not isCharging) and (not isExporting) and self.set_freeze_export_during_demand:
                self.log("In Demand mode, turning on Freeze Export")

                # In export freeze mode we disable charging
                if inverter.inv_charge_discharge_with_rate:
                    charge_rate = 0
                if inverter.inv_has_timed_pause:
                    inverter.adjust_pause_mode(pause_charge=True)
                    pause_charge_requested = True
                    resetPause = False
                else:
                    charge_rate = 0

                status_freeze_export = " [Freeze exporting]"

            # Car charging from battery disable? Applies regardless of car_energy_reported_load - that
            # switch only controls whether EV energy is included in the CT-clamp house-load model, not
            # whether we enforce the discharge hold.
            carHolding = False
            if self.set_charge_window and not self.car_charging_from_battery:
                for car_n in range(self.num_cars):
                    if self.car_charging_slots[car_n]:
                        window = self.car_charging_slots[car_n][0]
                        if self.car_charging_soc[car_n] >= self.car_charging_limit[car_n]:
                            self.log("Car {} is already charged, ignoring additional charging slot from {} - {}".format(car_n, self.time_abs_str(window["start"]), self.time_abs_str(window["end"])))
                        elif self.minutes_now >= window["start"] and self.minutes_now < window["end"] and window.get("kwh", 0) > 0:
                            self.log("Car charging from battery is off, next slot for car {} is {} - {}".format(car_n, self.time_abs_str(window["start"]), self.time_abs_str(window["end"])))
                            # Don't disable discharge during force charge/discharge slots but otherwise turn it off to prevent
                            # from draining the battery
                            if not isExporting:
                                if inverter.inv_has_timed_pause:
                                    if resetPause:
                                        inverter.adjust_pause_mode(pause_discharge=True)
                                        pause_discharge_requested = True
                                        resetPause = False
                                else:
                                    if discharge_rate is None:
                                        discharge_rate = 0
                                    # Not while actually charging: the battery is being filled from the grid, so it
                                    # cannot be feeding the car, and pinning reserve just above a rising SoC costs a
                                    # write for every 1% of the climb (#3899). Left to reset below for the duration,
                                    # and latched at the SoC reached once charging stops - which is the point the
                                    # inverter returns to demand and the hold starts to mean something. The sibling
                                    # iBoost hold below already sits out a charge for the same reason.
                                    if self.set_reserve_enable and status != "Charging":
                                        inverter.adjust_reserve(min(inverter.soc_percent + 1, 100))
                                        resetReserve = False
                                carHolding = True
                                self.log("Disabling battery discharge whilst car {} is charging".format(car_n))
                                if ("Hold for car" not in status) and (status_hold_car == ""):
                                    if status == "Demand":
                                        status = "Hold for car"
                                    else:
                                        status_hold_car = ", Hold for car"
                            break

            # iBoost running?
            boostHolding = False
            if self.set_charge_window and self.iboost_enable and self.iboost_prevent_discharge and self.iboost_running_full:
                # Only pause discharge on this inverter, and only annotate the status as held for
                # iBoost, if the fleet isn't already Charging/Exporting - pausing would conflict with
                # that, and the annotation must stay coupled to whether a hold actually happened here.
                if status not in ["Exporting", "Charging"]:
                    if inverter.inv_has_timed_pause:
                        if resetPause:
                            inverter.adjust_pause_mode(pause_discharge=True)
                            pause_discharge_requested = True
                            resetPause = False
                    else:
                        if discharge_rate is None:
                            discharge_rate = 0
                        if self.set_reserve_enable:
                            inverter.adjust_reserve(min(inverter.soc_percent + 1, 100))
                            resetReserve = False
                    boostHolding = True
                    self.log("Disabling battery discharge whilst iBoost is running")
                    if ("Hold for iBoost" not in status) and (status_hold_iboost == ""):
                        if status == "Demand":
                            status = "Hold for iBoost"
                        else:
                            status_hold_iboost = ", Hold for iBoost"

            # Reset pause mode; rates are resolved once by the apply pass after the loop
            if resetPause:
                inverter.adjust_pause_mode()

            intent[inverter.id] = {
                "charge_rate": charge_rate,
                "discharge_rate": discharge_rate,
                "pause_charge": pause_charge_requested,
                "pause_discharge": pause_discharge_requested,
                "owner": rate_owner,
                "reset_rates": reset_rates,
            }

            # Set the SoC just before or within the charge window
            if self.set_soc_enable:
                if isExporting:
                    export_target_percent = self.export_target_soc_percent()
                    if not disabled_export and not self.set_reserve_enable:
                        # If we are discharging and not setting reserve then we should reset the target SoC to the discharge target
                        # as some inverters can use this as a target for discharge
                        self.adjust_battery_target_multi(inverter, export_target_percent, isCharging, isExporting)
                    elif not inverter.inv_has_discharge_enable_time:
                        self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)
                    elif not self.inverter_hybrid and self.inverter_soc_reset and inverter.inv_has_target_soc:
                        # AC Coupled, charge to 100% on solar
                        self.log("Resetting charging SoC to 100% as we are not charging and inverter_soc_reset is enabled")
                        self.adjust_battery_target_multi(inverter, 100.0, isCharging, isExporting)
                    else:
                        # Reset to 0
                        self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)

                    # Immediate controls
                    if self.set_export_freeze and export_mode_of(self.export_limits_best[0]) == EXPORT_MODE_FREEZE:
                        inverter.adjust_export_immediate(inverter.soc_percent, freeze=True)
                    elif not disabled_export:
                        inverter.adjust_export_immediate(export_target_percent)
                    else:
                        inverter.adjust_export_immediate(int(EXPORT_LIMIT_IDLE))  # Dead code right, but kept in case other logic changes

                elif self.charge_limit_best and (self.minutes_now < inverter.charge_end_time_minutes) and ((inverter.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes) and not (disabled_charge_window):
                    if inverter.inv_has_charge_enable_time or isCharging:
                        # In charge freeze hold the target SoC at the current value
                        if self.is_freeze_charge(self.charge_limit_best[0]):
                            if isCharging:
                                inv_target_soc_percent = self.adjust_battery_target_multi(inverter, calc_percent_limit(self.soc_kw, self.soc_max), isCharging, isExporting, isFreezeCharge=True)
                                self.log("Inverter {} within charge freeze setting target SoC to SoC {} global target {}".format(inverter.id, dp0(inv_target_soc_percent), dp0(self.soc_kw)))
                                if inverter.soc_kw >= inverter.reserve:
                                    inverter.adjust_charge_immediate(inv_target_soc_percent, freeze=True)
                                else:
                                    inverter.adjust_charge_immediate(inv_target_soc_percent, freeze=False)
                            elif not inverter.inv_has_target_soc:
                                self.log("Inverter {} setting charging SoC to 0% as we are not charging and inverter doesn't support target SoC".format(inverter.id))
                                self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)
                            else:
                                # Not yet in the freeze, hold at 100% target SoC
                                self.log("Inverter {} not yet in charge freeze, holding target SoC at 100%".format(inverter.id))
                                self.adjust_battery_target_multi(inverter, 100.0, isCharging, isExporting)
                        else:
                            # If not charging and not hybrid we should reset the target % to 100 to avoid losing solar
                            if not self.inverter_hybrid and self.inverter_soc_reset and not isCharging and inverter.inv_has_target_soc:
                                self.log("Inverter {} resetting charging SoC as we are not charging and inverter_soc_reset is enabled".format(inverter.id))
                                self.adjust_battery_target_multi(inverter, 100.0, isCharging, isExporting)
                            elif isCharging:
                                target_soc = calc_percent_limit(max(self.charge_limit_best[0], self.reserve), self.soc_max)
                                self.log("Inverter {} setting charging SoC to {}% as per target".format(inverter.id, target_soc))
                                inv_target_soc = self.adjust_battery_target_multi(inverter, target_soc, isCharging, isExporting)
                                inverter.adjust_charge_immediate(inv_target_soc)
                            elif not inverter.inv_has_target_soc:
                                self.log("Inverter {} setting charging SoC to 0% as we are not charging and inverter doesn't support target SoC".format(inverter.id))
                                self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)
                            else:
                                target_soc = calc_percent_limit(max(self.charge_limit_best[0], self.reserve), self.soc_max)
                                self.log("Inverter {} setting charging SoC to {}% as per target for when charge window starts".format(inverter.id, target_soc))
                                self.adjust_battery_target_multi(inverter, target_soc, isCharging, isExporting)
                    else:
                        if not inverter.inv_has_target_soc:
                            # If the inverter doesn't support target SoC and soc_enable is on then do that logic here:
                            if not isCharging and not isExporting:
                                self.log("Inverter {} setting charging SoC to 0% as we are not charging or exporting and inverter doesn't support target SoC".format(inverter.id))
                                self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)
                        elif not self.inverter_hybrid and self.inverter_soc_reset and inverter.inv_has_target_soc:
                            # AC Coupled, charge to 100 on solar
                            self.log("Inverter {} resetting charging SoC to 100% as we are not charging and inverter_soc_reset is enabled".format(inverter.id))
                            self.adjust_battery_target_multi(inverter, 100.0, isCharging, isExporting)
                        else:
                            # Hybrid, no charge timer, set target SoC back to 0
                            self.log("Inverter {} setting charging SoC to 0% as we are not charging and the inverter doesn't support charge enable time".format(inverter.id))
                            self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)
                else:
                    if not inverter.inv_has_target_soc:
                        self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)
                        self.log("Inverter {} setting charging SoC to 0% as we are not within the charge window and inverter doesn't support target SoC".format(inverter.id))
                    elif not self.inverter_hybrid and self.inverter_soc_reset:
                        self.log(
                            "Inverter {} resetting charging SoC as we are not within the window or charge is disabled and inverter_soc_reset is enabled (now {} target set_soc_minutes {} charge start time {})".format(
                                inverter.id, self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)
                            )
                        )
                        self.adjust_battery_target_multi(inverter, 100.0, isCharging, isExporting)
                    else:
                        self.log(
                            "Inverter {} not setting charging SoC as we are not within the window (now {} target set_soc_minutes {} charge start time {})".format(
                                inverter.id, self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)
                            )
                        )
                        if not inverter.inv_has_charge_enable_time:
                            self.adjust_battery_target_multi(inverter, 0, isCharging, isExporting)

                    # Charge immediate
                    if isCharging:
                        if self.is_freeze_charge(self.charge_limit_best[0]):
                            inv_target_soc_percent = self.adjust_battery_target_multi(inverter, calc_percent_limit(self.soc_kw, self.soc_max), isCharging, isExporting, check=True, isFreezeCharge=True)
                            inverter.adjust_charge_immediate(inv_target_soc_percent, freeze=True)
                        else:
                            inv_target_soc_percent = self.adjust_battery_target_multi(inverter, calc_percent_limit(max(self.charge_limit_best[0], self.reserve), self.soc_max), isCharging, isExporting, check=True)
                            inverter.adjust_charge_immediate(inv_target_soc_percent, freeze=True)

            # Charging/Discharging off via service
            # Skipped while exporting: adjust_export_immediate() above already issues its own
            # charge_stop as part of starting the export, so this unconditional charge-off call adds
            # nothing but a second, later charge_stop_service write - on service-template inverters
            # (e.g. Tesla) that write a shared mode-select entity, this trailing call clobbered the
            # mode discharge_start_service had just set (GH#4165, GH#4641).
            if not isCharging and not isExporting and self.set_charge_window:
                if carHolding or boostHolding:
                    inverter.adjust_charge_immediate(inverter.soc_percent, freeze=True)
                else:
                    inverter.adjust_charge_immediate(0)
            if not isExporting and self.set_export_window:
                inverter.adjust_export_immediate(int(EXPORT_LIMIT_IDLE))

            # Reset reserve as discharge is enable but not running right now
            if self.set_reserve_enable and resetReserve:
                inverter.adjust_reserve(0)

        # Keep the EXECUTOR's intent as the poll's baseline, captured before balancing mutates it.
        # Storing the balanced intent instead would bake temporary holds into the baseline, and the
        # poll would re-apply them every cycle - the balancer returns without touching a rate once
        # the fleet is back in balance, so the hold would stick until the next plan run.
        self.inverter_rate_intent = {inverter_id: dict(value) for inverter_id, value in intent.items()}

        self.balance_inverter_rates(intent)

        # Single point at which rates reach the hardware. Runs after the loop so a balancer can see
        # the whole fleet before anything is written. Inverters that were skipped by the read-only
        # branch (continue) or the calibration branch (break) recorded no intent and are not written.
        self.apply_rate_intent(intent)

        # Count register writes - after the apply pass so the rate writes land in this cycle's count
        for inverter in self.inverters:
            self.log("Inverter {} count register writes {}".format(inverter.id, inverter.count_register_writes))
            if inverter.count_register_writes > 0:
                metrics().inverter_register_writes_total.inc(inverter.count_register_writes)
            self.count_inverter_writes[inverter.id] += inverter.count_register_writes
            inverter.count_register_writes = 0

        # Resolve the headline status across all inverters rather than leaving whichever inverter was
        # processed last to silently win.
        status = resolve_multi_inverter_status(status_per_inverter, status)
        status_extra = build_status_extra(status_extra_parts)

        # Set the charge/discharge status information
        self.set_charge_export_status(isCharging, isExporting, not (isCharging or isExporting))
        self.isCharging = isCharging
        self.isExporting = isExporting

        # append the status texts together
        status += status_hold_car + status_hold_iboost + status_freeze_export

        if in_alert > 0:
            status += " [Alert]"
        if in_manual_soc:
            status += " [Manual SoC]"
        if in_manual_soc_max:
            status += " [Manual SoC Max]"

        return status, status_extra

    def adjust_battery_target_multi(self, inverter, soc, is_charging, is_exporting, isFreezeCharge=False, check=False):
        """
        Adjust target SoC based on the current SoC of all the inverters accounting for their
        charge rates and battery capacities
        """
        target_kwh = dp2(self.soc_max * (soc / 100.0))
        soc_percent = calc_percent_limit(self.soc_kw, self.soc_max)

        if isFreezeCharge:
            new_soc_percent = calc_percent_limit(max(inverter.soc_kw, inverter.reserve), inverter.soc_max)
            if not check:
                self.log("Inverter {} adjust target SoC for hold to {}% based on requested all inverter SoC {}%".format(inverter.id, dp0(new_soc_percent), soc))
        elif soc == 100.0:
            new_soc_percent = 100.0
            if not check:
                self.log("Inverter {} adjust target SoC for charge to {}% based on requested all inverter SoC {}%".format(inverter.id, dp0(new_soc_percent), soc))
        elif soc == 0.0:
            new_soc_percent = 0.0
            if not check:
                self.log("Inverter {} adjust target SoC for export to {}% based on requested all inverter SoC {}%".format(inverter.id, dp0(new_soc_percent), soc))
        else:
            add_kwh = target_kwh - self.soc_kw
            add_this = add_kwh * (inverter.battery_rate_max_charge / self.battery_rate_max_charge)
            new_soc_kwh = max(min(inverter.soc_kw + add_this, inverter.soc_max), inverter.reserve)
            new_soc_percent = calc_percent_limit(new_soc_kwh, inverter.soc_max)
            if not check:
                self.log(
                    "Inverter {} adjust target SoC for charge to {}% ({}kWh/{}kWh {}kWh) based on going from {}% -> {}% total add is {}kWh and this battery needs to add {}kWh to get to {}kWh".format(
                        inverter.id, soc, dp2(target_kwh), dp2(self.soc_max), dp2(inverter.soc_max), soc_percent, dp0(new_soc_percent), dp2(add_kwh), dp2(add_this), dp2(new_soc_kwh)
                    )
                )
        if not check:
            inverter.adjust_battery_target(new_soc_percent, is_charging, is_exporting)
        return new_soc_percent

    def export_target_soc_percent(self):
        """
        Work out the SoC % to hand to the inverter as the export target

        Normally this is just the planned export limit. Where Predbat does not own a reserve register
        (set_reserve_enable off) the target is the only thing carrying the floor, so it is raised to the
        reserve. A planned limit of 0 means "empty it as far as you are allowed" - discharge_soc resolves
        that against the reserve when deciding how far to actually export, but an inverter whose service
        maps this target onto a device reserve would be told to drain the battery flat instead.

        Left alone when Predbat does own the reserve: adjust_reserve enforces the floor there and the two
        registers are independent, so raising the target would change behaviour for no benefit.

        Returns:
        - int: export target as a percentage of the battery
        """
        target = export_target_percent_or_zero(self.export_limits_best[0])
        if not self.set_reserve_enable:
            target = max(target, calc_percent_limit(max(self.reserve, self.best_soc_min), self.soc_max))
        return target

    def is_freeze_charge(self, charge_limit_kwh):
        """
        Check if a charge limit (in kWh) represents a freeze charge (i.e., equals reserve)
        Uses percentage comparison to avoid floating point rounding issues
        """
        return calc_percent_limit(charge_limit_kwh, self.soc_max) == self.reserve_percent

    def reset_inverter(self):
        """
        Reset inverter to safe mode
        """
        if not self.set_read_only or (self.inverter_needs_reset_force in ["set_read_only"]):
            self.last_service_hash = {}
            # Don't reset in read only mode unless forced
            for inverter in self.inverters:
                self.log("Reset inverter settings to safe mode (set_charge_window={} set_export_window={} force={})".format(self.set_charge_window, self.set_export_window, self.inverter_needs_reset_force))
                if self.set_charge_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
                    inverter.disable_charge_window()
                    inverter.adjust_charge_immediate(0)
                    inverter.adjust_battery_target(100.0, False)
                    inverter.adjust_pause_mode()
                    self.isCharging = False
                if self.set_charge_window or self.set_export_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_reserve(0)
                if self.set_export_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
                    inverter.adjust_force_export(False)
                    inverter.adjust_export_immediate(int(EXPORT_LIMIT_IDLE))
                    self.isExporting = False

        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""

    def fetch_inverter_data(self, create=True):
        """
        Fetch data about the inverters
        """
        # Find the inverters
        self.num_inverters = int(self.get_arg("num_inverters", 1))
        self.charge_window = []
        self.export_window = []
        self.export_limits = []
        self.inverter_data_last_fetch = datetime.now()
        found_first = False

        # Accumulate inverter totals into locals; only write to self.* once the loop
        # is complete so the web server never observes a partial (mid-loop) sum.
        current_charge_limit_kwh = 0.0
        soc_kw = 0.0
        soc_max = 0.0
        reserve = 0.0
        reserve_current = 0.0
        battery_rate_max_charge = 0.0
        battery_rate_max_charge_dc = 0.0
        battery_rate_max_discharge = 0.0
        battery_rate_max_export = 0.0
        battery_rate_min = 0
        charge_rate_now = 0.0
        discharge_rate_now = 0.0
        pv_power = 0
        load_power = 0
        battery_power = 0
        battery_temperature = 0
        grid_power = 0
        inverter_limit = 0.0
        export_limit = 0.0
        inverter_support_feedin_first = True

        # Create inverters list if needed
        if create or (not self.inverters) or (len(self.inverters) != self.num_inverters):
            self.inverters = []
            create = True

        # For each inverter get the details
        for id in range(self.num_inverters):
            if create:
                try:
                    inverter = Inverter(self, id)
                except Exception as e:
                    self.log("Error: Failed to create inverter {}: {}, your configuration may be incorrect".format(id, e))
                    self.inverters = []
                    return False
                self.inverters.append(inverter)
            else:
                inverter = self.inverters[id]
            inverter.update_status(self.minutes_now, quiet=not create)

            if id == 0 and (not self.computed_charge_curve or self.battery_charge_power_curve_auto) and not self.battery_charge_power_curve:
                curve = inverter.find_charge_curve(discharge=False)
                if curve and (self.battery_charge_power_curve_auto or not self.computed_charge_curve):
                    self.log("Saved computed battery charge power curve")
                    self.battery_charge_power_curve = curve
                    self.computed_charge_curve = True
                else:
                    if self.battery_charge_power_curve_default and not self.battery_charge_power_curve:
                        self.battery_charge_power_curve = self.battery_charge_power_curve_default
                        self.computed_charge_curve = True
                        self.log("Using default battery charge power curve")
                    elif not self.battery_charge_power_curve_auto:
                        # Stop retrying every cycle when not in auto mode and no curve found
                        self.computed_charge_curve = True

            if id == 0 and (not self.computed_discharge_curve or self.battery_discharge_power_curve_auto) and not self.battery_discharge_power_curve:
                curve = inverter.find_charge_curve(discharge=True)
                if curve and (self.battery_discharge_power_curve_auto or not self.computed_discharge_curve):
                    self.log("Saved computed battery discharge power curve")
                    self.battery_discharge_power_curve = curve
                    self.computed_discharge_curve = True
                else:
                    if self.battery_discharge_power_curve_default and not self.battery_discharge_power_curve:
                        self.battery_discharge_power_curve = self.battery_discharge_power_curve_default
                        self.computed_discharge_curve = True
                        self.log("Using default battery discharge power curve")
                    elif not self.battery_discharge_power_curve_auto:
                        # Stop retrying every cycle when not in auto mode and no curve found
                        self.computed_discharge_curve = True

            # As the inverters will run in lockstep, we will initially look at the programming of the first enabled one for the current window setting
            if not found_first:
                found_first = True
                self.charge_window = inverter.charge_window
                self.export_window = inverter.export_window
                self.export_limits = inverter.export_limits
                if not inverter.inv_support_discharge_freeze:
                    # Force off unsupported feature
                    self.log("Note: Inverter does not support discharge freeze - disabled")
                    self.set_export_freeze = False
                    self.set_export_freeze_only = False
                if not inverter.inv_support_charge_freeze:
                    # Force off unsupported feature
                    self.log("Note: Inverter does not support charge freeze - disabled")
                    self.set_charge_freeze = False
                if not inverter.inv_has_reserve_soc:
                    self.log("Note: Inverter does not support reserve - disabling reserve functions")
                    self.set_reserve_enable = False
                    self.set_reserve_hold = False
                    self.set_discharge_during_charge = True
            # Unlike the first-inverter-only settings above this is a fleet-wide capability: the
            # prediction models one combined battery, so a single inverter that just disables
            # charging during Freeze Export means the fleet as a whole cannot recapture PV.
            if not inverter.inv_support_feedin_first:
                inverter_support_feedin_first = False
            current_charge_limit_kwh += dp2(inverter.current_charge_limit * inverter.soc_max / 100.0)
            soc_max += inverter.soc_max
            soc_kw += inverter.soc_kw
            reserve += inverter.reserve
            reserve_current += inverter.reserve_current
            battery_rate_max_charge += inverter.battery_rate_max_charge
            battery_rate_max_charge_dc += inverter.battery_rate_max_charge_dc
            battery_rate_max_discharge += inverter.battery_rate_max_discharge
            battery_rate_max_export += inverter.battery_rate_max_export
            charge_rate_now += inverter.charge_rate_now
            discharge_rate_now += inverter.discharge_rate_now
            battery_rate_min += inverter.battery_rate_min
            inverter_limit += inverter.inverter_limit
            export_limit += inverter.export_limit
            pv_power += inverter.pv_power
            load_power += inverter.load_power
            battery_power += inverter.battery_power
            grid_power += inverter.grid_power
            battery_temperature += inverter.battery_temperature

        # Atomically publish all accumulated totals so the web server never reads partial sums
        self.current_charge_limit_kwh = current_charge_limit_kwh
        self.soc_max = dp3(soc_max)
        self.soc_kw = dp3(soc_kw)
        self.reserve = dp3(reserve)
        self.reserve_current = dp3(reserve_current)
        self.battery_rate_max_charge = battery_rate_max_charge
        self.battery_rate_max_charge_dc = battery_rate_max_charge_dc
        self.battery_rate_max_discharge = battery_rate_max_discharge
        self.battery_rate_max_export = battery_rate_max_export
        self.battery_rate_min = battery_rate_min
        self.charge_rate_now = charge_rate_now
        self.discharge_rate_now = discharge_rate_now
        self.inverter_limit = inverter_limit
        self.export_limit = export_limit
        self.pv_power = pv_power
        self.load_power = load_power
        self.battery_power = battery_power
        self.grid_power = grid_power
        self.battery_temperature = int(dp0(battery_temperature / self.num_inverters))
        self.current_charge_limit = calc_percent_limit(self.current_charge_limit_kwh, self.soc_max)
        self.inverter_support_feedin_first = inverter_support_feedin_first

        # Additional PVs without inverters
        pv_power_sensors = self.get_arg("pv_power", [], indirect=False)
        if pv_power_sensors and isinstance(pv_power_sensors, list):
            for idx in range(0, len(pv_power_sensors)):
                if idx >= self.num_inverters:
                    pv_power = self.get_arg("pv_power", default=0.0, index=idx, required_unit="W")
                    try:
                        self.pv_power += pv_power
                    except (TypeError, ValueError):
                        self.log("Warn: Invalid PV power value for sensor {}".format(pv_power_sensors[idx]))

        self.update_car_charging_power()

        self.soc_percent = calc_percent_limit(self.soc_kw, self.soc_max)
        self.reserve_percent = calc_percent_limit(self.reserve, self.soc_max)
        self.reserve_current_percent = calc_percent_limit(self.reserve_current, self.soc_max)

        if self.debug_enable:
            self.log(
                "Found {} inverters totals: min reserve {}%, current reserve {}%, soc_max {}%, SoC {}%, charge rate {}kW, discharge rate {}kW, battery_rate_min {}W, AC limit {}kW, export limit {}kW, loss charge {}%, loss discharge {}%, inverter loss {}%".format(
                    len(self.inverters),
                    self.reserve,
                    self.reserve_current,
                    self.soc_max,
                    self.soc_kw,
                    self.charge_rate_now * MINUTE_WATT,
                    self.discharge_rate_now * MINUTE_WATT,
                    self.battery_rate_min * MINUTE_WATT,
                    dp3(self.inverter_limit * MINUTE_WATT),
                    dp3(self.export_limit * MINUTE_WATT),
                    100 - int(self.battery_loss * 100),
                    100 - int(self.battery_loss_discharge * 100),
                    100 - int(self.inverter_loss * 100),
                )
            )

        # Work out current charge limits and publish charge limit base
        self.charge_limit = [self.current_charge_limit * self.soc_max / 100.0 for i in range(len(self.charge_window))]
        self.publish_charge_limit(self.charge_limit, self.charge_window, best=False)
        self.publish_inverter_data()
        self.publish_inverter_config()
        return True

    def is_template_mode(self):
        """
        True while the apps.yaml template is unedited ('Template: True'), so the plan must not run
        """
        return self.get_arg("template", False)

    def quick_inverter_data_update(self):
        """
        Quick update of inverter data for dashboard
        """
        # While template mode is set update_pred() early-returns before fetch_config_options(), so
        # the attributes update_status() reads (e.g. inverter_clock_skew_discharge_start) were
        # never created - running it would AttributeError every cycle, and for inverters without
        # has_charge_enable_time it would reach write_and_poll_switch("scheduled_charge_enable")
        # and write the real device with no plan in place (#4965)
        if self.is_template_mode():
            # fetch_inverter_data() and the plan run never stamp this in template mode, so without
            # a stamp here the 120s throttle in update_pred() would pass on every tick of
            # update_time_loop instead of once per INVERTER_QUICK_UPDATE_SECONDS
            self.inverter_data_last_fetch = datetime.now()
            return False
        if self.inverters is None:
            return False
        # Its own control-ledger cycle. This runs every 120s and reaches update_status(), which
        # writes scheduled_charge_enable through write_and_poll_switch - so it both observes and
        # confirms. Without advancing the cycle, every observation here was unconditionally STALE
        # (cycle <= confirmed_cycle) and its confirmations collided with the plan run's. Every
        # entry point that can observe or confirm gets its own cycle.
        if self.control_ledger is not None:
            self.control_ledger.begin_cycle()
        if self.fetch_inverter_data(create=False):
            self.publish_inverter_data()
            self.rebalance_inverter_rates()
            return True
        return False

    def rebalance_inverter_rates(self):
        """
        Re-derive the balance skew against fresh SoC and re-apply the executor's intent.

        The second caller of the single write path. execute_plan() owns the intent; this only
        adjusts a COPY of it, so the poll can never invent a rate the executor did not ask for and
        successive polls cannot compound their own skew on top of each other.

        Does nothing until execute_plan() has run at least once, so a poll that beats the first
        plan run cannot write anything.
        """
        if not self.inverter_rate_intent:
            return
        if not self.balance_inverters_enable or self.set_read_only:
            return

        # A poll can land between an inverter entering calibration and the next plan run, with the
        # previous plan's intent still stored. balance_inverters() declines to act on a calibrating
        # fleet, but applying the stale intent would still write planned rates over the full-rate
        # calibration settings - every 60s until execute_plan next clears it. Drop it here instead.
        if any(inverter.in_calibration for inverter in self.inverters):
            self.log("Balance: an inverter is calibrating, discarding the stored rate intent")
            self.inverter_rate_intent = {}
            return
        intent = {inverter_id: dict(value) for inverter_id, value in self.inverter_rate_intent.items()}

        # The stored intent carries explicit watt targets from the last plan run, but the limits
        # were just re-read - some configurations source inverter_limit_charge/_discharge from
        # live BMS sensors, so a ceiling can drop between plan runs. Clamp before balancing, or
        # the poll re-applies an above-ceiling rate and the capacity guard overestimates what the
        # fleet can deliver on top of it.
        for inverter in self.inverters:
            entry = intent.get(inverter.id)
            if not entry:
                continue
            if entry.get("charge_rate") is not None:
                entry["charge_rate"] = min(entry["charge_rate"], inverter.battery_rate_max_charge * MINUTE_WATT)
            if entry.get("discharge_rate") is not None:
                entry["discharge_rate"] = min(entry["discharge_rate"], inverter.battery_rate_max_discharge * MINUTE_WATT)

        self.balance_inverter_rates(intent)
        self.apply_rate_intent(intent)

    def update_car_charging_power(self):
        """
        Read the live car charging power (W) from the optional car_charging_power sensors

        This is a monitoring input only - the plan still models car charging from
        car_charging_energy - so it is kept apart from the inverter totals above. Several
        chargers can be listed and are summed, as car_charging_energy allows. A charger that
        is configured but reading zero is not the same as no charger at all, so whether the
        key is set at all is recorded separately: that is what decides if the car appears on
        the power flow diagram and is published as a sensor.
        """
        sensors = self.get_arg("car_charging_power", None, indirect=False)
        if not isinstance(sensors, list):
            sensors = [sensors] if sensors else []

        # The apps.yaml templates ship this as a regular expression matching the common chargers.
        # auto_config(final=True) deletes the key when nothing matched, but until it has run the
        # literal "re:" string is still here - treat it as unconfigured so a household with no
        # charger never gets a car drawn on the power flow diagram.
        sensors = [sensor for sensor in sensors if sensor and not (isinstance(sensor, str) and sensor.startswith("re:"))]

        if not sensors:
            self.car_charging_power_configured = False
            self.car_charging_power = 0
            return

        car_charging_power = 0.0
        for sensor in sensors:
            # Resolved one entity at a time rather than by index, as auto_config() leaves a None
            # in place of a list entry whose regular expression found nothing and an index-based
            # read would then stop at the hole instead of the chargers after it.
            #
            # No numeric default is passed, and the conversion happens here, because get_arg would
            # report a charger sitting at 'unavailable' with nothing plugged in as an error and
            # leave the whole run flagged with errors - which is normal for a charger, not a fault.
            value = self.resolve_arg("car_charging_power", sensor, default=None, required_unit="W")
            try:
                car_charging_power += float(value)
            except (ValueError, TypeError):
                pass

        # Published together, after every sensor has been read, so the web server - which runs in
        # its own thread and reads the pair independently - can never see a car declared but its
        # power still left over from the previous cycle. Same reason fetch_inverter_data publishes
        # its accumulated totals in one go rather than as it sums them.
        self.car_charging_power = car_charging_power
        self.car_charging_power_configured = True

    def publish_inverter_data(self):
        """
        Publish inverter data to dashboard
        """
        self.dashboard_item(
            self.prefix + ".pv_power",
            state=dp3(self.pv_power / 1000.0),
            attributes={
                "friendly_name": "Current PV Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "icon": "mdi:battery",
            },
        )
        self.dashboard_item(
            self.prefix + ".grid_power",
            state=dp3(self.grid_power / 1000.0),
            attributes={
                "friendly_name": "Current Grid Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "icon": "mdi:battery",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_power",
            state=dp3(self.load_power / 1000.0),
            attributes={
                "friendly_name": "Current Load Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "icon": "mdi:battery",
            },
        )
        self.dashboard_item(
            self.prefix + ".battery_power",
            state=dp3(self.battery_power / 1000.0),
            attributes={
                "friendly_name": "Current Battery Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "icon": "mdi:battery",
            },
        )
        if self.car_charging_power_configured:
            # Only published when a charger sensor is actually configured - a sensor pinned at
            # zero for everyone else is noise, and its absence is how upstream consumers tell
            # "no car charger" from "car charger idle"
            self.dashboard_item(
                self.prefix + ".car_charging_power",
                state=dp3(self.car_charging_power / 1000.0),
                attributes={
                    "friendly_name": "Current Car Charging Power",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "device_class": "power",
                    "icon": "mdi:ev-station",
                },
            )

    def publish_inverter_config(self):
        """
        Publish the static configuration the prediction runs from, aggregated over all the inverters

        These come from apps.yaml or are read back off the inverters, so unlike the settings in
        CONFIG_ITEMS they have no entity of their own. Power values are held internally in kW per
        minute and are converted to kW here to match the other power sensors.
        """
        self.dashboard_item(
            "sensor." + self.prefix + "_inverter_config",
            state=dp3(self.inverter_limit * MINUTE_WATT / 1000.0),
            attributes={
                "friendly_name": "Predbat Inverter Config",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "icon": "mdi:transmission-tower",
                "inverter_limit": dp3(self.inverter_limit * MINUTE_WATT / 1000.0),
                "export_limit": dp3(self.export_limit * MINUTE_WATT / 1000.0),
                "pv_ac_limit": dp3(self.pv_ac_limit * MINUTE_WATT / 1000.0),
                "battery_rate_max_charge": dp3(self.battery_rate_max_charge * MINUTE_WATT / 1000.0),
                "battery_rate_max_charge_dc": dp3(self.battery_rate_max_charge_dc * MINUTE_WATT / 1000.0),
                "battery_rate_max_discharge": dp3(self.battery_rate_max_discharge * MINUTE_WATT / 1000.0),
                "battery_rate_max_export": dp3(self.battery_rate_max_export * MINUTE_WATT / 1000.0),
                "battery_rate_min": dp3(self.battery_rate_min * MINUTE_WATT / 1000.0),
                "soc_max": dp3(self.soc_max),
                "reserve": dp3(self.reserve),
                "num_inverters": self.num_inverters,
                "num_cars": self.num_cars,
                "inverter_can_charge_during_export": self.inverter_can_charge_during_export,
                "inverter_support_feedin_first": self.inverter_support_feedin_first,
                "metric_standing_charge": dp2(self.metric_standing_charge),
                "forecast_minutes": self.forecast_minutes,
                "plan_interval_minutes": self.plan_interval_minutes,
            },
        )
