# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Plan optimisation engine for charge/discharge scheduling.

Implements the search algorithm that finds optimal charge and discharge windows
by exploring combinations of price thresholds, window sizes, and SoC targets.
The search itself is serial Python: each fan-out queues its scenarios through
launch_run_prediction_* and the first handle read flushes them all through one
call to the C++ prediction kernel, which is where the threading now lives.
"""

from datetime import datetime, timedelta
from multiprocessing import cpu_count
from const import PREDICT_STEP, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90, TIME_FORMAT, MINUTE_WATT

from utils import calc_percent_limit, clone_windows, dp0, dp1, dp2, dp3, dp4, remove_intersecting_windows, in_car_slot
from prediction import Prediction
from prediction_kernel import kernel_status_summary, set_window_start
from predbat_metrics import metrics
import time

# How many windows the post-settle plan pass revisits when calculate_second_pass is off. The near-term
# windows are the ones about to be executed, so a small budget keeps the common path cheap; raising it
# picks up value further out at a proportional cost in planning time.
PLAN_PASS_WINDOW_BUDGET = 8


def resolve_batch_threads(threads, cpu_count_value):
    """Map the threads setting onto how many kernel lanes one batch may use.

    'auto' takes the core count and is deliberately not capped. On a fast machine the curve is very
    flat and peaks slightly below the core count - measured on the 20-scenario benchmark, best of 3:
    serial 26.33s, 4 threads 24.89s, 6 threads 24.71s, 8 threads 24.91s, 16 threads 25.04s - so a cap
    looks attractive. But re-running with each job made eight times dearer, which is how a machine
    where the kernel dominates behaves, the curve stops turning over entirely: 48.92s serial, 32.03s
    at 4, 29.98s at 6, 29.85s at 8, 28.94s at 16.

    That makes the risk asymmetric. Capping at 4 costs 0.7% on the fast machine but 10.7% on the
    kernel-heavy one, while not capping costs 1.3% at worst. The worst case for a low cap is far
    worse than the worst case for none, so 'auto' is left alone and anyone who wants fewer lanes sets
    threads: explicitly.
    """
    if threads == "auto":
        return max(cpu_count_value, 1)
    return max(int(threads), 1)


# Octopus Intelligent (IOG) charge-skew gradient.
# io_adjusted (planned-dispatch) slots are at-risk: Octopus may move or remove them later.
# Instead of a flat penalty we apply a signed per-hour gradient across each contiguous IOG
# run: the earliest slots are discounted (ranked below equally-priced firm slots, so they
# are filled first) while the latest slots are penalised (so distant, more-likely-to-vanish
# IOG slots are not relied upon). Firm slots sit neutrally in the middle at the pivot point.
# The discount only applies to imminent slots (within IO_ADJUST_DISCOUNT_HORIZON_HOURS of
# now); distant future dispatch periods keep only the penalty side until they draw closer,
# which is re-evaluated every optimisation cycle.
IO_ADJUST_SLOPE = 1.0  # Pence per hour into the IOG run
IO_ADJUST_PIVOT_HOURS = 1.5  # Hours into the run where the adjustment crosses zero (firm level)
IO_ADJUST_MAX_DISCOUNT = 3.0  # Maximum pence discount applied to the earliest IOG slots
IO_ADJUST_MAX_PENALTY = 10.0  # Maximum pence penalty applied to the latest IOG slots
IO_ADJUST_DISCOUNT_HORIZON_HOURS = 3.0  # Only discount IOG slots that start within this many hours of now


def slots_around(target_slots, slot_lengths):
    """
    Return a list of slot lengths around the target slots
    """
    slot_choices = []
    for slot_length in slot_lengths:
        if slot_length <= (target_slots * 2) and slot_length >= (target_slots // 2):
            slot_choices.append(slot_length)
    return slot_choices


def select_window_candidates(entries, price_selected, allow_freeze, accept=None):
    """
    Ordered, deduplicated list of (window_n, freeze) that a price threshold makes available.

    entries is a [price, window_n, freeze] list for one side of the search, in the order the
    optimiser considers them. A window is taken when its price passes price_selected, its freeze
    flag is allowed by allow_freeze, it has not already been taken, and accept (when given) passes
    it. Only taken windows count as seen, so a window that accept rejects is offered again if it
    appears later - which is what the capped scan this replaces did.

    The result is deliberately unbounded: capping at max_slots is the caller slicing the first
    max_slots off the front. That equivalence holds because the cap in the original scan was
    monotonic - once reached, nothing further was ever taken - and it is what lets one scan per
    price threshold serve every slot count the search tries. test_window_selection pins it against
    a reference implementation of the original capped loop.
    """
    chosen = []
    seen = set()
    for price, window_n, freeze in entries:
        if not price_selected(price):
            continue
        if freeze and not allow_freeze:
            continue
        if window_n in seen:
            continue
        if accept is not None and not accept(window_n):
            continue
        seen.add(window_n)
        chosen.append((window_n, freeze))
    return chosen


MASK_64 = (1 << 64) - 1


def scenario_hash_entry(kind, window_n, value):
    """
    Hash contribution of one (window, limit value) pair for incremental scenario deduplication.

    Applies an avalanche mixing function (SplitMix64 finaliser) to the tuple hash: Python's tuple hash is nearly
    linear in the last element for small values, so raw tuple hashes summed across windows
    produce structural collisions (e.g. swapping which of two windows carries a modification).
    The avalanche makes the summed contributions behave like independent random values.
    """
    acc = hash((kind, window_n, value)) & MASK_64
    acc = ((acc ^ (acc >> 30)) * 0xBF58476D1CE4E5B9) & MASK_64
    acc = ((acc ^ (acc >> 27)) * 0x94D049BB133111EB) & MASK_64
    return acc ^ (acc >> 31)


class Plan:
    """Plan optimisation mixin for finding optimal charge/discharge windows.

    Implements the search algorithm that explores price thresholds,
    window combinations, and SoC targets to minimise the overall cost
    metric. Scenarios are evaluated in batches: launch_run_prediction_*
    queues them and reading the first handle runs the whole batch through
    one C++ kernel call, which spreads it across its own threads.
    """

    def dynamic_load(self):
        """
        Adjust load prediction based on current load
        Return True if load status has changed and hence we need to re-plan
        """
        prev_last_load_status = self.load_last_status
        prev_last_load_car_slot = self.load_last_car_slot

        threshold_battery = self.battery_rate_max_discharge * MINUTE_WATT / 1000
        threshold_car = self.car_charging_threshold * MINUTE_WATT / 1000

        # Last period load analysis
        self.load_last_status = "baseline"
        if self.load_last_period >= threshold_battery:
            self.load_last_status = "high"
        elif (self.load_last_period < (threshold_battery * 0.9)) and (self.load_last_period < (threshold_car * 0.9)):
            # Check if the load is less than car charging threshold
            self.load_last_status = "low"
        else:
            self.load_last_status = "baseline"

        # Update entity for last load
        self.dashboard_item(
            self.prefix + ".load_energy_last_period",
            state=dp3(self.load_last_period),
            attributes={"friendly_name": "Last period load", "state_class": "measurement", "unit_of_measurement": "kW", "icon": "mdi:home-lightning-bolt", "status": self.load_last_status},
        )
        self.log("Dynamic load last period {:.2f}kW, status {}, threshold_battery {}kWh, threshold_car {}kWh,".format(self.load_last_period, self.load_last_status, threshold_battery, dp1(threshold_car)))

        # Is the car currently planned to charge?
        load_car_slot = False
        if self.car_energy_reported_load:
            for car_n in range(0, self.num_cars):
                for slot_n in range(0, len(self.car_charging_slots[car_n])):
                    slot = self.car_charging_slots[car_n][slot_n]
                    # Don't include the exact start minute as it may take a few for the load to filter through
                    if slot["start"] <= self.minutes_now < slot["end"]:
                        load_car_slot = True
                        self.log("Dynamic load adjust sees car {} charging now slot {}-{}, previous car slot {}".format(car_n, slot["start"], slot["end"], self.load_last_car_slot))
        self.load_last_car_slot = load_car_slot
        self.dynamic_load_baseline = {}
        if self.metric_dynamic_load_adjust:
            minutes_now = self.minutes_now
            minutes_end_slot = int((self.minutes_now + self.plan_interval_minutes) / self.plan_interval_minutes) * self.plan_interval_minutes
            # When dynamic load is enabled we try can do two things
            # 1. Increase the load prediction in the current self.plan_interval_minutes minute period to match the actual load (if the load is higher than expected),
            #    extending into the following period too once the load has been high for two consecutive checks in a row (mirrors the low-load debounce below)
            # 2. If the load is low and car charging is predicted then cancel off future car slots
            # Note never do this just after midnight due to the load sensor reset
            if self.load_last_status == "low" and self.minutes_now > 5:
                if load_car_slot and prev_last_load_car_slot:
                    for car_n in range(0, self.num_cars):
                        for slot_n in range(0, len(self.car_charging_slots[car_n])):
                            slot = self.car_charging_slots[car_n][slot_n]
                            if slot["end"] > minutes_now:
                                # If the slot is in the future
                                self.log("Dynamic load adjust is cancelling car {} slot {}-{} due to low load".format(car_n, slot["start"], slot["end"]))
                                self.car_charging_slots[car_n][slot_n]["kwh"] = 0

            if self.load_last_status == "high":
                have_printed = False
                minutes_end_baseline = minutes_end_slot
                if prev_last_load_status == "high":
                    # Load has been high for two consecutive checks, so also predict it will continue
                    # into the following slot to keep the plan up to date across the slot boundary
                    minutes_end_baseline = minutes_end_slot + self.plan_interval_minutes
                for minute_absolute in range(minutes_now, minutes_end_baseline, PREDICT_STEP):
                    if not self.car_energy_reported_load:
                        # If car energy is not reported as load then we should not attempt to adjust the load prediction based on car load.
                        car_load = 0
                    else:
                        car_load = sum(in_car_slot(minute_absolute, self.num_cars, self.car_charging_slots)[0])
                    load_last_period = self.load_last_period / 60 * PREDICT_STEP
                    load_last_period = max(load_last_period - car_load, 0)
                    if load_last_period > 0:
                        if not have_printed:
                            self.log("Dynamic load adjust is setting load minimum {:.2f}kW at {}".format(load_last_period, self.time_abs_str(minute_absolute)))
                            have_printed = True
                        self.dynamic_load_baseline[minute_absolute] = load_last_period
            if prev_last_load_status != self.load_last_status:
                self.log("Dynamic load status changed from {} to {}".format(prev_last_load_status, self.load_last_status))
                return True

        return False

    def find_price_levels(
        self,
        price_set,
        price_links,
        window_index,
        charge_limit,
        charge_window,
        export_window,
        export_limits,
    ):
        """
        Find the highest and lowest price levels for charge and export windows
        """
        highest_price_charge = None
        lowest_price_export = None
        highest_price_charge_level = None
        lowest_price_export_level = None
        real_highest_price_charge = None
        real_lowest_price_export = None

        for price in price_set:
            links = price_links[price]
            for key in links:
                window_n = window_index[key]["id"]
                typ = window_index[key]["type"]
                if typ == "c":
                    if (highest_price_charge_level is None) or (price < highest_price_charge_level):
                        highest_price_charge_level = price
                    if real_highest_price_charge is None or price > real_highest_price_charge:
                        real_highest_price_charge = price
                elif typ == "d":
                    if (lowest_price_export_level is None) or (price > lowest_price_export_level):
                        lowest_price_export_level = price
                    if real_lowest_price_export is None or price < real_lowest_price_export:
                        real_lowest_price_export = price

        for price in price_set:
            links = price_links[price]
            for key in links:
                window_n = window_index[key]["id"]
                typ = window_index[key]["type"]
                if typ == "c":
                    if price == real_highest_price_charge:
                        continue
                    if charge_limit[window_n] > self.reserve:
                        if highest_price_charge is None:
                            highest_price_charge = charge_window[window_n]["average"]
                        else:
                            highest_price_charge = max(highest_price_charge, charge_window[window_n]["average"])
                        if highest_price_charge_level is None:
                            highest_price_charge_level = price
                        else:
                            highest_price_charge_level = max(highest_price_charge_level, price)
                elif typ == "d":
                    if price == real_lowest_price_export:
                        continue
                    if export_limits[window_n] < 99.0:
                        if lowest_price_export is None:
                            lowest_price_export = export_window[window_n]["average"]
                        else:
                            lowest_price_export = min(lowest_price_export, export_window[window_n]["average"])
                        if lowest_price_export_level is None:
                            lowest_price_export_level = price
                        else:
                            lowest_price_export_level = min(lowest_price_export_level, price)

        if highest_price_charge is None:
            highest_price_charge = self.rate_min
        if lowest_price_export is None:
            lowest_price_export = self.rate_export_max
        if highest_price_charge_level is None:
            highest_price_charge_level = self.rate_min
        if lowest_price_export_level is None:
            lowest_price_export_level = self.rate_export_max

        return highest_price_charge, lowest_price_export, highest_price_charge_level, lowest_price_export_level

    def optimise_charge_limit_price_threads(
        self,
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        record_export_windows,
        try_charge_limit,
        charge_window,
        export_window,
        export_limits,
        end_record=None,
        region_start=None,
        region_end=None,
        fast=False,
        quiet=False,
        best_metric=9999999,
        best_cost=0,
        best_keep=0,
        best_soc_min=None,
        best_cycle=0,
        best_carbon=0,
        best_import=0,
        best_battery_value=0,
        tried_list=None,
        test_mode=False,
        levels_score=None,
        enable_coarse_fine=True,
        best_max_charge_slots=1,
        best_max_export_slots=1,
    ):
        """
        Pick an import price threshold which gives the best results
        """
        loop_price = price_set[-1]
        best_price = loop_price
        try_export = export_limits.copy()
        best_limits = try_charge_limit.copy()
        best_export_limits = try_export.copy()
        best_all_n = []
        if best_soc_min is None:
            best_soc_min = self.reserve
        step = PREDICT_STEP
        if fast:
            step = self.plan_interval_minutes
        if tried_list is None:
            tried_list = {}

        best_level_score = None
        worst_level_score = None
        level_score_range = None
        if levels_score is None:
            levels_score = {}
        else:
            best_level_score = 9999999
            worst_level_score = -9999999
            # Work out the best levels score so far
            for price in price_set:
                best_level_score = min(best_level_score, levels_score[price])
                worst_level_score = max(worst_level_score, levels_score[price])
            level_score_range = abs(worst_level_score - best_level_score)

        best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = self.run_prediction_metric(best_limits, charge_window, export_window, export_limits, end_record=end_record)

        if region_start:
            region_txt = "Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end))
        else:
            region_txt = "All regions"

        # Do we loop on export?
        if self.calculate_best_export and self.calculate_export_first:
            export_enable = True
        else:
            export_enable = False

        # Most expensive first
        all_prices = price_set[::] + [dp1(price_set[-1] - 1)]
        if not quiet:
            self.log("All prices {}".format(all_prices))
            if region_start:
                self.log("Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end)))

        price_set_charge = []
        valid_charge_windows = {}
        best_limits_reset = best_limits.copy()
        for price in price_set[::-1]:
            links = price_links[price]
            for key in links:
                window_n = window_index[key]["id"]
                typ = window_index[key]["type"]
                if typ in ["c", "cf"]:
                    if region_start and (charge_window[window_n]["start"] >= region_end or charge_window[window_n]["end"] < region_start):
                        pass
                    elif charge_window[window_n]["start"] in self.manual_all_times:
                        pass
                    else:
                        price_set_charge.append([price, window_n, typ == "cf"])
                        valid_charge_windows[window_n] = True
                        best_limits_reset[window_n] = 0

        price_set_export = []
        valid_export_windows = {}
        best_export_limits_reset = export_limits.copy()
        if export_enable:
            for price in price_set:
                links = price_links[price]
                # For prices above threshold try export
                for key in links:
                    typ = window_index[key]["type"]
                    window_n = window_index[key]["id"]
                    if typ in ["d", "df"]:
                        if region_start and (export_window[window_n]["start"] >= region_end or export_window[window_n]["end"] < region_start):
                            pass
                        elif export_window[window_n]["start"] in self.manual_all_times:
                            pass
                        else:
                            price_set_export.append([price, window_n, typ == "df"])
                            valid_export_windows[window_n] = True
                            best_export_limits_reset[window_n] = 100.0

        FINE_SLOT_LENGTHS = [48, 32, 24, 16, 14, 12, 10, 8, 6, 5, 4, 3, 2, 1, 0]
        COARSE_SLOT_LENGTHS = [32, 16, 8, 4, 2, 1, 0]
        min_freeze_percent = calc_percent_limit(self.best_soc_min, self.soc_max)

        # Scenario deduplication uses an incremental hash of the absolute limit configuration: one
        # hash contribution per (window, value) pair, summed. A candidate scenario's hash is then the
        # reset hash plus a small delta per modified window. Unmodified windows contribute their
        # baseline values, so the key is a pure function of the absolute configuration - equivalent to
        # hashing the full limit lists (including across calls sharing tried_list, where the baseline
        # changes over time) but without copying and hashing the full lists for every candidate.
        reset_sum = 0
        for window_n, value in enumerate(best_limits_reset):
            reset_sum += scenario_hash_entry(0, window_n, value)
        for window_n, value in enumerate(best_export_limits_reset):
            reset_sum += scenario_hash_entry(1, window_n, value)
        charge_hash_delta = {}
        for window_n in valid_charge_windows:
            reset_contribution = scenario_hash_entry(0, window_n, best_limits_reset[window_n])
            charge_hash_delta[window_n] = {True: scenario_hash_entry(0, window_n, self.reserve) - reset_contribution, False: scenario_hash_entry(0, window_n, self.soc_max) - reset_contribution}
        export_hash_delta = {}
        for window_n in valid_export_windows:
            reset_contribution = scenario_hash_entry(1, window_n, best_export_limits_reset[window_n])
            export_hash_delta[window_n] = {True: scenario_hash_entry(1, window_n, 99.0) - reset_contribution, False: scenario_hash_entry(1, window_n, min_freeze_percent) - reset_contribution}

        # Which charge window an export window collides with is a purely geometric question, and this
        # function only ever turns windows on and off - it never moves a window's start or end. So the
        # answer is fixed for the life of the call and is memoised here, keyed by export window. Without
        # it the trial loop below re-scans the whole charge window list for every export window of every
        # candidate: on a benchmark scenario that was 1.74 million linear scans of a ~200 entry list,
        # for 221 distinct answers. Only the collision itself is cached - the limit that follows from it
        # depends on charge_mods/best_limits_reset and changes from trial to trial.
        hit_charge_cache = {}
        hit_car_cache = {}  # (start, end) -> does this window hit a car charging slot
        export_allowed_cache = {}  # window_n -> is this export window usable at all

        def export_window_allowed(window_n):
            """Whether an export window may be exported at all, ignoring charge window collisions.

            Car charging and iboost collisions depend only on the window's own fixed geometry, so
            like hit_charge_cache above the answer holds for the life of the call. The charge window
            collision is deliberately not folded in here - that one depends on charge_mods and so
            changes from trial to trial.
            """
            allowed = export_allowed_cache.get(window_n)
            if allowed is None:
                window = export_window[window_n]
                if not self.car_charging_from_battery and self.hit_car_window(window["start"], window["end"], cache=hit_car_cache):
                    allowed = False
                elif not self.iboost_on_export and self.iboost_enable and self.iboost_plan and (self.hit_charge_window(self.iboost_plan, window["start"], window["end"]) >= 0):
                    allowed = False
                else:
                    allowed = True
                export_allowed_cache[window_n] = allowed
            return allowed

        # Start loop of trials
        for loop_price in all_prices:
            if best_level_score is not None:
                this_level_score = levels_score.get(loop_price, 9999999)
                if abs(this_level_score - best_level_score) > (0.2 * level_score_range):
                    if self.debug_enable:
                        self.log("Skipping price {} as level score {} is not within 20% of best {}".format(loop_price, this_level_score, best_level_score))
                    continue

            # Which windows a price threshold makes available depends only on the threshold and the
            # freeze flag - not on the slot counts swept below, and not on coarse vs fine. So each
            # side is scanned once per (threshold, freeze) here and every slot count is served by
            # slicing that one list, rather than rescanning price_set_* inside the four nested slot
            # loops. That scan was the hottest loop left in planning: ~900 rescans of a few hundred
            # entries per threshold, for at most a couple of dozen distinct answers.
            #
            # Slicing is exactly what capping did - see select_window_candidates - and slot counts at
            # or above the candidate count all select the same windows, so they collapse onto one
            # cache entry rather than one per entry in the slot length list.
            charge_candidates = {}
            export_candidates = {}
            charge_selection_cache = {}
            export_selection_cache = {}

            def charge_selection_for(max_slots, allow_freeze, loop_price=loop_price):
                """Charge windows this price threshold selects, capped at max_slots"""
                candidates = charge_candidates.get(allow_freeze)
                if candidates is None:
                    candidates = select_window_candidates(price_set_charge, lambda price: loop_price >= price, allow_freeze)
                    charge_candidates[allow_freeze] = candidates
                count = min(max_slots, len(candidates))
                entry = charge_selection_cache.get((count, allow_freeze))
                if entry is None:
                    taken = candidates[:count]
                    entry = ([window_n for window_n, _ in taken], dict(taken))
                    charge_selection_cache[(count, allow_freeze)] = entry
                return entry

            def export_selection_for(max_slots, allow_freeze, loop_price=loop_price):
                """Export windows this price threshold selects, capped at max_slots"""
                candidates = export_candidates.get(allow_freeze)
                if candidates is None:
                    candidates = select_window_candidates(price_set_export, lambda price: loop_price < price, allow_freeze, accept=export_window_allowed)
                    export_candidates[allow_freeze] = candidates
                count = min(max_slots, len(candidates))
                entry = export_selection_cache.get((count, allow_freeze))
                if entry is None:
                    taken = candidates[:count]
                    entry = ([window_n for window_n, _ in taken], dict(taken))
                    export_selection_cache[(count, allow_freeze)] = entry
                return entry

            for coarse in [True, False] if enable_coarse_fine else [False]:
                if not enable_coarse_fine:
                    charge_slot_choices = FINE_SLOT_LENGTHS
                    export_slot_choices = FINE_SLOT_LENGTHS
                elif coarse:
                    charge_slot_choices = COARSE_SLOT_LENGTHS
                    export_slot_choices = COARSE_SLOT_LENGTHS
                else:
                    charge_slot_choices = slots_around(best_max_charge_slots, FINE_SLOT_LENGTHS)
                    export_slot_choices = slots_around(best_max_export_slots, FINE_SLOT_LENGTHS)

                pred_table = []
                charge_freeze_options = [True, False] if (self.set_charge_freeze and not coarse) else [False]
                export_freeze_options = [True, False] if (self.set_export_freeze and not coarse) else [False]

                for max_charge_slots in charge_slot_choices:
                    for max_export_slots in export_slot_choices:
                        for try_charge_freeze in charge_freeze_options:
                            for try_export_freeze in export_freeze_options:
                                # all_n is copied into pred_item below and charge_mods is only ever read,
                                # so both cached objects can be shared between trials as they stand. The
                                # export pair can be pruned just below, so it is copied on write.
                                all_n, charge_mods = charge_selection_for(max_charge_slots, try_charge_freeze)
                                all_d, export_mods = export_selection_for(max_export_slots, try_export_freeze)

                                # Remove export hitting charge windows if this is disabled
                                if not self.calculate_export_oncharge:
                                    pruned_all_d = None
                                    pruned_export_mods = None
                                    for window_n in all_d:
                                        hit_charge = hit_charge_cache.get(window_n)
                                        if hit_charge is None:
                                            hit_charge = self.hit_charge_window(self.charge_window_best, export_window[window_n]["start"], export_window[window_n]["end"])
                                            hit_charge_cache[window_n] = hit_charge
                                        if hit_charge >= 0:
                                            if hit_charge in charge_mods:
                                                hit_charge_limit = self.reserve if charge_mods[hit_charge] else self.soc_max
                                            else:
                                                hit_charge_limit = best_limits_reset[hit_charge]
                                            if hit_charge_limit > 0.0:
                                                # Only remove if it doesn't remove the charge window entirely
                                                if not (export_window[window_n]["start"] <= self.charge_window_best[hit_charge]["start"] and export_window[window_n]["end"] >= self.charge_window_best[hit_charge]["end"]):
                                                    # Dropping the modification restores the reset value (100.0) for this window
                                                    if pruned_all_d is None:
                                                        pruned_all_d = all_d[:]
                                                        pruned_export_mods = dict(export_mods)
                                                    pruned_export_mods.pop(window_n, None)
                                                    pruned_all_d.remove(window_n)
                                    if pruned_all_d is not None:
                                        all_d = pruned_all_d
                                        export_mods = pruned_export_mods

                                # Skip this one as it's the same as selected already
                                try_hash = reset_sum
                                for window_n, freeze in charge_mods.items():
                                    try_hash += charge_hash_delta[window_n][freeze]
                                for window_n, freeze in export_mods.items():
                                    try_hash += export_hash_delta[window_n][freeze]
                                if try_hash in tried_list:
                                    try_value = tried_list[try_hash]
                                    if try_value is not True:
                                        if loop_price not in levels_score:
                                            levels_score[loop_price] = 9999999
                                        levels_score[loop_price] = min(levels_score[loop_price], tried_list[try_hash])
                                    continue

                                tried_list[try_hash] = True

                                # Materialise the full limit lists only for scenarios that will run predictions
                                try_charge_limit = best_limits_reset.copy()
                                for window_n, freeze in charge_mods.items():
                                    try_charge_limit[window_n] = self.reserve if freeze else self.soc_max
                                try_export = best_export_limits_reset.copy()
                                for window_n, freeze in export_mods.items():
                                    try_export[window_n] = 99.0 if freeze else min_freeze_percent

                                pred_item = {}
                                pred_item["handle"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, PV_SCENARIO_NOMINAL, end_record=end_record, step=step)
                                pred_item["handle10"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, PV_SCENARIO_PV10, end_record=end_record, step=step)
                                pred_item["handle90"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, PV_SCENARIO_PV90, end_record=end_record, step=step) if self.pv_metric90_weight > 0 else None
                                pred_item["charge_limit"] = try_charge_limit
                                pred_item["export_limit"] = try_export
                                pred_item["loop_price"] = loop_price
                                pred_item["all_n"] = all_n.copy()
                                pred_item["try_hash"] = try_hash
                                pred_item["max_charge_slots"] = max_charge_slots
                                pred_item["max_export_slots"] = max_export_slots
                                pred_table.append(pred_item)

                for pred in pred_table:
                    handle = pred["handle"]
                    handle10 = pred["handle10"]
                    handle90 = pred.get("handle90")
                    try_charge_limit = pred["charge_limit"]
                    try_export = pred["export_limit"]
                    loop_price = pred["loop_price"]
                    all_n = pred["all_n"]
                    try_hash = pred["try_hash"]
                    max_charge_slots = pred["max_charge_slots"]
                    max_export_slots = pred["max_export_slots"]

                    cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = handle.get()
                    cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10 = handle10.get()
                    soc90 = None
                    cost90 = None
                    final_iboost90 = 0.0
                    if handle90 is not None:
                        (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = handle90.get()

                    metric, battery_value = self.compute_metric(
                        end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90
                    )

                    tried_list[try_hash] = metric

                    # Optimise
                    if self.debug_enable:
                        selected = metric < best_metric
                        if export_enable:
                            self.log(
                                "Optimise {} all for buy/sell price band <= {} metric {} keep {} soc_min {} import {} export {} soc {} windows {} export on {}".format(
                                    "[SELECTED]" if selected else "",
                                    loop_price,
                                    dp2(metric),
                                    dp2(metric_keep),
                                    dp2(soc_min),
                                    dp2(import_kwh_battery + import_kwh_house),
                                    dp2(export_kwh),
                                    dp1(soc),
                                    try_charge_limit,
                                    try_export,
                                )
                            )
                        else:
                            self.log(
                                "Optimise {} all for buy/sell price band <= {} metric {} keep {} soc_min {} import {} export {}  soc {} windows {} export off".format(
                                    "[SELECTED]" if selected else "",
                                    loop_price,
                                    dp2(metric),
                                    dp2(metric_keep),
                                    dp2(soc_min),
                                    dp2(import_kwh_battery + import_kwh_house),
                                    dp2(export_kwh),
                                    dp1(soc),
                                    try_charge_limit,
                                )
                            )

                    if loop_price not in levels_score:
                        levels_score[loop_price] = 9999999
                    levels_score[loop_price] = min(levels_score[loop_price], metric)

                    # For the first pass just pick the most cost effective threshold, consider soc keep later
                    if metric < best_metric:
                        best_metric = metric
                        best_keep = metric_keep
                        best_price = loop_price
                        best_limits = try_charge_limit
                        best_export_limits = try_export
                        best_cycle = battery_cycle
                        best_carbon = final_carbon_g
                        best_soc_min = soc_min
                        best_cost = cost
                        best_import = import_kwh_battery + import_kwh_house
                        best_battery_value = battery_value
                        best_all_n = all_n.copy()
                        best_max_charge_slots = max_charge_slots
                        best_max_export_slots = max_export_slots

        if self.debug_enable:
            self.log(
                "Optimise all charge {} price band {} total simulations {} at cost {} metric {} keep {} cycle {} carbon {} import {} battery_value {} soc_min {} limits {} export {}".format(
                    region_txt,
                    dp2(best_price),
                    len(tried_list),
                    dp2(best_cost),
                    dp2(best_metric),
                    dp2(best_keep),
                    dp2(best_cycle),
                    dp0(best_carbon),
                    dp2(best_import),
                    dp2(best_battery_value),
                    dp1(best_soc_min),
                    best_limits,
                    best_export_limits,
                )
            )

        # Perform charge limit levelling on best_all_n
        if best_all_n:
            best_all_n.sort()
            metric, battery_value, cost, keep, cycle, carbon, import_this, export_this = self.run_prediction_metric(best_limits, charge_window, export_window, best_export_limits, end_record=end_record)
            best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import, best_metric_plan = self.optimise_charge_limit(
                0, record_charge_windows, best_limits, charge_window, export_window, best_export_limits, all_n=best_all_n, end_record=end_record
            )
            if self.debug_enable:
                self.log("Best all_n {} best_limits {} => {} metric {}".format(best_all_n, [best_limits[window_n] for window_n in best_all_n], best_soc, metric))
            for window_n in best_all_n:
                best_limits[window_n] = best_soc
                try_charge_limit[window_n] = best_soc

            metric, battery_value, cost, keep, cycle, carbon, import_this, export_this = self.run_prediction_metric(best_limits, charge_window, export_window, best_export_limits, end_record=end_record)

        return (
            best_limits,
            best_export_limits,
            best_metric,
            best_cost,
            best_keep,
            best_soc_min,
            best_cycle,
            best_carbon,
            best_import,
            best_battery_value,
            tried_list,
            levels_score,
            best_max_charge_slots,
            best_max_export_slots,
        )

    def launch_run_prediction_single(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step=PREDICT_STEP):
        """Queue a prediction and return a handle to its result.

        Nothing runs here: the inputs are read when the batch is flushed, so no list or window dict
        passed in may be mutated until the returned handle's get() has been called.
        """
        return self.prediction.queue_run_prediction_single(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step)

    def launch_run_prediction_charge(self, loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a prediction and return a handle to its result.

        Nothing runs here: the inputs are read when the batch is flushed, so no list or window dict
        passed in may be mutated until the returned handle's get() has been called.
        """
        return self.prediction.queue_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    def launch_run_prediction_charge_min_max(self, loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a prediction and return a handle to its result.

        Nothing runs here: the inputs are read when the batch is flushed, so no list or window dict
        passed in may be mutated until the returned handle's get() has been called.
        """
        return self.prediction.queue_run_prediction_charge_min_max(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    def launch_run_prediction_export(self, this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record):
        """Queue a prediction and return a handle to its result.

        Nothing runs here: the inputs are read when the batch is flushed, so no list or window dict
        passed in may be mutated until the returned handle's get() has been called.
        """
        return self.prediction.queue_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record)

    def scenario_summary_title(self, record_time):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % self.plan_interval_minutes
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, self.plan_interval_minutes):
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            dstamp = minute_timestamp.strftime(TIME_FORMAT)
            stamp = minute_timestamp.strftime("%H:%M")
            if txt:
                txt += ", "
            txt += "%8s" % str(stamp)
            if record_time[dstamp] > 0:
                break
        return txt

    def scenario_summary(self, record_time, datap):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % self.plan_interval_minutes
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, self.plan_interval_minutes):
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = datap[stamp]
            if not isinstance(value, str):
                value = dp2(value)
                if value > 10000:
                    value = dp0(value)
            if txt:
                txt += ", "
            txt += "%8s" % str(value)
            if record_time[stamp] > 0:
                break
        return txt

    def scenario_summary_state(self, record_time):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % self.plan_interval_minutes
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, self.plan_interval_minutes):
            minute_relative_start = max(minute_absolute - self.minutes_now, 0)
            minute_relative_end = minute_relative_start + self.plan_interval_minutes
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = ""

            charge_window_n = -1
            for try_minute in range(this_minute_absolute, minute_absolute + self.plan_interval_minutes, 5):
                charge_window_n = self.in_charge_window(self.charge_window_best, try_minute)
                if charge_window_n >= 0 and self.charge_limit_best[charge_window_n] == 0:
                    charge_window_n = -1
                if charge_window_n >= 0:
                    break

            export_window_n = -1
            for try_minute in range(this_minute_absolute, minute_absolute + self.plan_interval_minutes, 5):
                export_window_n = self.in_charge_window(self.export_window_best, try_minute)
                if export_window_n >= 0 and self.export_limits_best[export_window_n] == 100.0:
                    export_window_n = -1
                if export_window_n >= 0:
                    break

            soc_percent = calc_percent_limit(self.predict_soc_best.get(minute_relative_start, 0.0), self.soc_max)
            soc_percent_end = calc_percent_limit(self.predict_soc_best.get(minute_relative_end, 0.0), self.soc_max)
            soc_percent_max = max(soc_percent, soc_percent_end)
            soc_percent_min = min(soc_percent, soc_percent_end)

            if charge_window_n >= 0 and export_window_n >= 0:
                value = "Chrg/Exp"
            elif charge_window_n >= 0:
                charge_target = self.charge_limit_best[charge_window_n]
                if self.is_freeze_charge(charge_target):
                    value = "FrzChrg"
                else:
                    value = "Chrg"
            elif export_window_n >= 0:
                export_target = self.export_limits_best[export_window_n]
                if export_target >= soc_percent_max:
                    if export_target == 99:
                        value = "FrzExp"
                    else:
                        value = "HldExp"
                else:
                    value = "Exp"

            if record_time[stamp] > 0:
                break
            if txt:
                txt += ", "
            txt += "%8s" % str(value)
        return txt

    def record_length(self, charge_window, charge_limit, best_price):
        """
        Limit the forecast length to either the total forecast duration or the start of the last window that falls outside the forecast
        """
        next_charge_start = self.forecast_minutes + self.minutes_now
        if charge_window:
            for window_n in range(len(charge_window)):
                if charge_limit[window_n] > 0 and charge_window[window_n]["average"] <= best_price:
                    next_charge_start = charge_window[window_n]["start"]
                    if next_charge_start < self.minutes_now:
                        next_charge_start = charge_window[window_n]["end"]
                    break

        end_record_min = self.minutes_now + self.forecast_plan_hours * 60
        end_record = min(self.forecast_plan_hours * 60 + next_charge_start, self.forecast_minutes + self.minutes_now)

        # Align to next window
        max_windows = self.max_charge_windows(end_record, charge_window)
        if len(charge_window) > max_windows:
            end_record = min(end_record, charge_window[max_windows]["start"])
            # If we are within this window then push to the end of it
            if end_record < self.minutes_now:
                end_record = min(charge_window[max_windows]["end"], self.forecast_minutes + self.minutes_now)

        # avoid too short a plan, find the next charge window if its short
        if end_record < end_record_min:
            # If we are within this window then push to
            max_windows += 1
            if len(charge_window) > max_windows:
                end_record = min(max(end_record, charge_window[max_windows]["start"]), self.forecast_minutes + self.minutes_now)
            # Final check to avoid too short a plan
            end_record = max(end_record, end_record_min)

        self.log("Calculated end_record as {} based on best_price {}{}, next_charge_start {}, max_windows {}".format(self.time_abs_str(end_record), best_price, self.currency_symbols[1], self.time_abs_str(next_charge_start), max_windows))
        return end_record - self.minutes_now

    def max_charge_windows(self, end_record_abs, charge_window):
        """
        Work out how many charge windows the time period covers
        """
        charge_windows = 0
        window_n = 0
        for window in charge_window:
            if end_record_abs >= window["end"]:
                charge_windows = window_n + 1
            window_n += 1
        return charge_windows

    def hit_charge_window(self, charge_window, start, end):
        """
        Determines if the given start and end time falls within any of the charge windows.

        Parameters:
        charge_window (list): A list of dictionaries representing charge windows, each containing "start" and "end" keys.
        start (int): The start time of the interval to check.
        end (int): The end time of the interval to check.

        Returns:
        int: The index of the charge window that the interval falls within, or -1 if it doesn't fall within any charge window.
        """
        window_n = 0
        for window in charge_window:
            if end > window["start"] and start < window["end"]:
                return window_n
            window_n += 1
        return -1

    def run_prediction_metric(self, charge_limit_best, charge_window_best, export_window_best, export_limits_best, end_record=None, save=None, nominal_only=False):
        """
        Run a single datapoint for PV and PV10 (and PV90, when enabled) and return the metric

        Every optimiser comparison (optimise_charge_limit_price_threads, optimise_charge_limit,
        optimise_export) that this baseline is measured against now blends in a pv90 term whenever
        pv_metric90_weight > 0, so this baseline must too - otherwise the two sides of every
        `n_best_metric < best_metric` comparison sit on different scales.

        With nominal_only=True only the nominal (50%) scenario is simulated - one simulation instead
        of two or three. The nominal results are mirrored into the pv10 slots so compute_metric's
        pv10 adjustment cancels to zero and pv90 is skipped, giving a pure central-forecast metric.
        Used by prune_dead_plan_slots, where each trial only asks whether the nominal outcome moved.
        """

        if end_record is None:
            end_record = self.forecast_minutes

        # Run pv90 first (when active), and the nominal scenario last. Plan.run_prediction
        # unconditionally overwrites self.predict_soc, self.car_charging_soc_next, self.iboost_next
        # and self.iboost_running* on every call - so whichever scenario runs last is the one those
        # attributes are left holding. Every traced consumer only reads them after this function's
        # final (nominal, save=save) run anyway, so this ordering does not change any output; it just
        # removes the latent trap of pv90 being the one left behind when pv_metric90_weight > 0.
        soc90 = None
        cost90 = None
        final_iboost90 = 0.0
        if self.pv_metric90_weight > 0 and not nominal_only:
            (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = self.run_prediction(
                charge_limit_best,
                charge_window_best,
                export_window_best,
                export_limits_best,
                PV_SCENARIO_PV90,
                end_record=end_record,
            )

        if not nominal_only:
            (
                cost10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
            ) = self.run_prediction(
                charge_limit_best,
                charge_window_best,
                export_window_best,
                export_limits_best,
                True,
                end_record=end_record,
            )
        # Run new plan
        (
            cost,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
        ) = self.run_prediction(
            charge_limit_best,
            charge_window_best,
            export_window_best,
            export_limits_best,
            False,
            end_record=end_record,
            save=save,
        )
        if nominal_only:
            # Mirror the nominal outputs into the pv10 slots so the pv10 adjustment cancels to zero
            cost10 = cost
            soc10 = soc
            final_iboost10 = final_iboost
        metric, battery_value = self.compute_metric(
            self.end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90
        )
        return metric, battery_value, cost, metric_keep, battery_cycle, final_carbon_g, import_kwh_battery + import_kwh_house, export_kwh

    def in_charge_window(self, charge_window, minute_abs):
        """
        Work out if this minute is within the a charge window

        Parameters:
        charge_window (list): A sorted list of dictionaries representing charge windows.
                                Each dictionary should have "start" and "end" keys
                                representing the start and end minutes of the window.

        minute_abs (int): The absolute minute value to check.

        Returns:
        int: The index of the charge window if the minute is within a window,
                otherwise -1.
        """
        window_n = 0
        for window in charge_window:
            if minute_abs >= window["start"] and minute_abs < window["end"]:
                return window_n
            elif window["start"] > minute_abs:
                # As windows are sorted, we can stop searching once we've passed the minute
                break
            window_n += 1
        return -1

    def plan_fragmentation(self, charge_window, charge_limit, export_window, export_limits):
        """Count the contiguous active (charge/export) segments in a plan.

        A slot is active if it discharges the battery (export limit < 99, i.e. not freeze/off) or charges it
        (charge target above the reserve floor). Time-adjacent active slots of the same mode form one segment; a
        time gap or a change of mode (charge<->export) starts a new segment. A cleaner plan has fewer segments,
        so this is used as a tie-break to prefer a single export block over a fragmented staircase when the cost
        is otherwise equal.
        """
        intervals = []
        for window, limit in zip(export_window, export_limits):
            if limit < 99:
                intervals.append((window["start"], window["end"], "export"))
        for window, limit in zip(charge_window, charge_limit):
            if limit > self.reserve:
                intervals.append((window["start"], window["end"], "charge"))

        intervals.sort(key=lambda item: item[0])

        segments = 0
        prev_end = None
        prev_mode = None
        for start, end, mode in intervals:
            if prev_end is None or start > prev_end or mode != prev_mode:
                segments += 1
            prev_end = end if prev_end is None else max(prev_end, end)
            prev_mode = mode
        return segments

    def should_replace_plan(self, metric_prev, metric_new, fragmentation_prev, fragmentation_new):
        """Decide whether to adopt the freshly optimised plan over the incumbent.

        The new plan is adopted when it is better by at least metric_min_improvement_plan (the existing
        anti-jitter behaviour). On a near-tie within that band it is also adopted when it is no worse on cost
        and strictly less fragmented, so a cleaner single export block can replace a locked-in split schedule
        without churning the plan for tiny cost changes. Lower metric is better, so improvement is prev - new.
        """
        improvement = metric_prev - metric_new
        if improvement >= self.metric_min_improvement_plan:
            return True
        if improvement >= 0 and fragmentation_new < fragmentation_prev:
            return True
        return False

    def plan_window_snapshot(self, typ, window_n):
        """Copy the single window an optimisation pass can modify, so the change can be undone."""
        if typ == "c":
            return self.charge_limit_best[window_n]
        return self.export_limits_best[window_n], self.export_window_best[window_n].copy()

    def plan_window_restore(self, typ, window_n, snapshot):
        """Put a window back as it was when plan_window_snapshot() captured it."""
        if typ == "c":
            self.charge_limit_best[window_n] = snapshot
        else:
            self.export_limits_best[window_n], self.export_window_best[window_n] = snapshot

    def plan_metric_now(self, end_record):
        """Measure the plan currently held as (metric, cost, keep, cycle, carbon, import)."""
        metric, battery_value, cost, metric_keep, battery_cycle, final_carbon_g, import_kwh, export_kwh = self.run_prediction_metric(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=end_record)
        return metric, cost, metric_keep, battery_cycle, final_carbon_g, import_kwh

    def keep_window_change_if_improved(self, baseline, candidate, typ, window_n, snapshot):
        """Undo a pending single-window plan change unless it improved the whole-plan metric.

        optimise_charge_limit/optimise_export rank their candidates against the window being turned off rather
        than the setting the plan already holds, and on a score carrying adjustments the plan metric does not,
        so a pass that writes their result back unconditionally can replace a better setting chosen by an
        earlier pass. Reverting on a regression keeps such a pass monotonic - notably it stops an in-progress
        forced export being cancelled part way through a price peak.

        candidate is the metric of the plan as just written back, taken from the optimiser rather than
        re-simulated: the optimiser scores each option by simulating the whole plan with this one window
        changed, so the unadjusted metric of the option it selected already describes the plan we now hold.

        A change that ties is kept, matching optimise_swap_export: only a genuine regression is reverted.
        Reverting ties instead leaves a differently shaped plan of equal value, and the passes that run after
        this one amplify that into a real difference.

        The comparison carries no commitment bonus, so an in-progress export that is worse on the plan metric
        than the setting already held is reverted even when optimise_export preferred it. Lower metric is
        better.

        Returns the metric tuple to carry forward: the candidate when the change is kept, the baseline when not.
        """
        if candidate[0] <= baseline[0]:
            return candidate
        if self.debug_enable:
            self.log("Reverted change to {} window {} as metric {} did not improve on {}".format(typ, window_n, dp2(candidate[0]), dp2(baseline[0])))
        self.plan_window_restore(typ, window_n, snapshot)
        return baseline

    def plan_scoring_pair(self, plan_new, plan_prev, preclip_new, preclip_prev):
        """Return the (new, previous) plans that selection should be scored on.

        Plans are (charge_limit, charge_window, export_window, export_limits) tuples.

        Clipping sets the charge/export percentage actually shown on the plan and sent to the inverter, so it
        has to stay. It is a no-op in the expected case - the mid-case cost is unchanged - but it moves the
        metric through the PV10 branch by an amount that depends on plan shape. Scoring the clipped plans
        therefore decides between them partly on a difference clipping invented rather than one the plans
        really have, and by then the slot is usually hours away and will be re-planned many times before it
        executes.

        Both sides fall back together when either snapshot is missing (the first recompute after a restart):
        scoring an un-taxed new plan against a taxed incumbent would favour the new plan on the tax alone.
        """
        if preclip_new is not None and preclip_prev is not None:
            return preclip_new, preclip_prev
        return plan_new, plan_prev

    @staticmethod
    def pv_series_signature(series):
        """Return a cheap content and coverage signature for a per-minute PV series.

        The tuple is (minute count, total kWh, first minute, last minute) - enough to notice that a
        series has been swapped or rewritten between plan runs, and to tell how much of the plan
        horizon it spans. It is never used as a value in its own right, so a content collision costs
        nothing more than a redundant (or skipped) refresh of the fallback p90 copy.
        """
        if not series:
            return (0, 0.0, None, None)
        return (len(series), round(sum(series.values()), 6), min(series), max(series))

    def refresh_pv_forecast_minute90(self):
        """Keep the pv90 (upside) PV forecast series in step with the p50 series it sits beside.

        ``fetch.py`` always refreshes ``pv_forecast_minute90`` alongside ``pv_forecast_minute``, falling
        back to a copy of the p50 when no forecast90 data is published, so in production the pair is
        always consistent. But callers that assign ``pv_forecast_minute`` directly - ``annual.py``'s
        year-long sweep, which reuses ONE PredBat instance across every sampled day, replayed debug
        dumps with no forecast90 sensor, and unit tests sharing a fixture - can leave the p90 empty or,
        far worse, holding a series belonging to a completely different p50.

        A stale p90 is not a harmless approximation: it silently turns pv90, which exists to be the
        UPSIDE case, into a severe downside one. January's p50 held against July's makes the "upside"
        scenario carry a quarter of nominal PV - exactly the inversion already ruled out for
        ``load_scaling90``, arriving by another route. Emptiness is therefore not a sufficient trigger.

        Two independent tests must both pass for the p90 in hand to be used:

        1. Coverage. The p90 must span the part of the plan horizon that the p50 spans. Missing minutes
           read back as zero from ``step_data_history``, so a p90 that stops short of the horizon makes
           pv90 a zero-PV downside case over the rest of it. In production this always holds - fetch.py
           builds all three series over one shared minute range - so this only ever fires on a p90 that
           came from somewhere else.
        2. Not left behind. If the p50 changed since the previous call while the p90 did not, the p90
           cannot belong to the p50 in hand. A p90 that moved with its p50 - the normal case for a real
           fetched forecast90 - is kept untouched however far it diverges, because that divergence is
           the entire point of having a real p90.

        Failing either test re-derives the p90 from the p50. That costs only the accuracy of the upside
        case (pv90 collapses to nominal PV, an inert scenario), whereas using a mismatched one silently
        inverts what the feature means.
        """
        p50_signature = self.pv_series_signature(self.pv_forecast_minute)
        p90_signature = self.pv_series_signature(self.pv_forecast_minute90)
        previous = self.pv_forecast_minute90_signatures

        if not self.pv_forecast_minute:
            # Nothing to plan against - the only consistent p90 is an equally empty one
            covers_horizon = not self.pv_forecast_minute90
        else:
            first_needed = max(p50_signature[2], self.minutes_now)
            last_needed = min(p50_signature[3], self.minutes_now + self.forecast_minutes)
            covers_horizon = bool(self.pv_forecast_minute90) and p90_signature[2] <= first_needed and p90_signature[3] >= last_needed
        left_behind = previous is not None and previous[0] != p50_signature and previous[1] == p90_signature

        if not covers_horizon or left_behind:
            self.pv_forecast_minute90 = dict(self.pv_forecast_minute)
            p90_signature = p50_signature
        self.pv_forecast_minute90_signatures = (p50_signature, p90_signature)

    def calculate_plan(self, recompute=True, debug_mode=False, publish=True):
        """
        Calculate the new plan (best)

        sets:
           self.charge_window_best
           self.charge_limit_best
           self.export_window_best
           self.export_limits_best
        """
        curr = self.currency_symbols[1]

        plan_start_time = time.time()

        # Re-compute plan due to time wrap
        if self.plan_last_updated_minutes > self.minutes_now:
            self.log("Force recompute due to start of day")
            recompute = True
            self.plan_valid = False

        # Shift onto next charge window if required
        while self.charge_window_best:
            window = self.charge_window_best[0]
            if window["end"] <= self.minutes_now:
                del self.charge_window_best[0]
                del self.charge_limit_best[0]
                self.log("Current charge window has expired, removing it")
            else:
                break

        # Shift onto next export window if required
        while self.export_window_best:
            window = self.export_window_best[0]
            if window["end"] <= self.minutes_now:
                del self.export_window_best[0]
                del self.export_limits_best[0]
                self.log("Current export window has expired, removing it")
            else:
                break

        # Recompute?
        if recompute:
            # Obtain previous plan data for comparison
            if self.plan_valid:
                charge_limit_best_prev = self.charge_limit_best.copy()
                charge_window_best_prev = clone_windows(self.charge_window_best)
                export_window_best_prev = clone_windows(self.export_window_best)
                export_limits_best_prev = self.export_limits_best.copy()
                preclip_prev = self.plan_preclip
                self.log("Recompute is saving previous plan...")
            else:
                charge_limit_best_prev = None
                charge_window_best_prev = None
                export_window_best_prev = None
                export_limits_best_prev = None
                preclip_prev = None
                self.log("Recompute, previous plan is invalid...")

            self.plan_valid = False  # In case of crash, plan is now invalid

            # Calculate best charge windows
            if self.low_rates and self.calculate_best_charge and self.set_charge_window:
                # If we are using calculated windows directly then save them
                self.charge_window_best = clone_windows(self.low_rates)
            else:
                # Default best charge window as this one
                self.charge_window_best = clone_windows(self.charge_window)

            # Calculate best export windows
            if self.calculate_best_export and self.set_export_window:
                self.export_window_best = clone_windows(self.high_export_rates)
            else:
                self.export_window_best = clone_windows(self.export_window)

            # Pre-fill best charge limit with the current charge limit
            self.charge_limit_best = [self.current_charge_limit * self.soc_max / 100.0 for i in range(len(self.charge_window_best))]

            # Pre-fill best export enable with Off
            self.export_limits_best = [100.0 for i in range(len(self.export_window_best))]

            self.end_record = self.forecast_minutes
        # Show best windows
        self.log("Best charge window {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max))))
        self.log("Best export window {}".format(self.window_as_text(self.export_window_best, self.export_limits_best)))

        # Created optimised step data
        self.metric_cloud_coverage = self.get_cloud_factor(self.minutes_now, self.pv_forecast_minute, self.pv_forecast_minute10)
        self.metric_load_divergence = self.get_load_divergence(self.minutes_now, self.load_minutes)

        # Clamp the three load scalings so load_scaling90 <= load_scaling <= load_scaling10 always
        # holds: the PV90 case can never end up with more load than the central case, and the PV10
        # case can never end up with less. Without this, any load_scaling below load_scaling90 turns
        # PV90 into a second, harsher downside case rather than the upside one it exists to model.
        # This lives here rather than in fetch_config_options because it is an invariant of the
        # scenarios, not of config reading - callers that set the scalings directly (the annual
        # report, the random scenario harness, compare) never read config and would otherwise plan
        # with the scenarios inverted. Sequential evaluation is deliberate: the clamped load_scaling90
        # can never be the maximum of the three, so the second line reduces to max of the other two.
        # These are locals so the configured values stay visible in Home Assistant and the logs.
        load_scaling90 = min(self.load_scaling90, self.load_scaling10, self.load_scaling)
        load_scaling10 = max(load_scaling90, self.load_scaling, self.load_scaling10)
        if load_scaling90 != self.load_scaling90:
            self.log(
                "Warn: load_scaling90 {} exceeds load_scaling ({}) or load_scaling10 ({}) so the PV90 scenario would have more load than the central case - using {} for this plan".format(
                    self.load_scaling90, self.load_scaling, self.load_scaling10, load_scaling90
                )
            )
        if load_scaling10 != self.load_scaling10:
            self.log("Warn: load_scaling10 {} is below load_scaling ({}) so the PV10 scenario would have less load than the central case - using {} for this plan".format(self.load_scaling10, self.load_scaling, load_scaling10))
        load_minutes_step = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=self.load_scaling,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=self.metric_load_divergence,
            load_adjust=self.manual_load_adjust,
            load_baseline=self.dynamic_load_baseline,
        )
        load_minutes_step10 = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=load_scaling10,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=min(self.metric_load_divergence + 0.5, 1.0) if self.metric_load_divergence else None,
            load_adjust=self.manual_load_adjust,
            load_baseline=self.dynamic_load_baseline,
        )
        # load_scaling90 is an ABSOLUTE multiplier of the historical load, exactly like load_scaling10
        # above - it does not compose with load_scaling. fetch_config_options() (CHANGE 4) clamps
        # load_scaling90 <= load_scaling <= load_scaling10 immediately after reading all three from
        # config, so by the time calculate_plan() runs load_scaling90 can never exceed load_scaling
        # here - the pv90-load-inverts-into-a-second-downside-case failure mode CHANGE 3 used to warn
        # about from this call site is now structurally unreachable, and the warning has been removed
        # (see fetch_config_options' clamp-changed-a-value log instead, which fires there once per
        # config read rather than here once per plan).
        load_minutes_step90 = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=load_scaling90,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=self.metric_load_divergence,
            load_adjust=self.manual_load_adjust,
            load_baseline=self.dynamic_load_baseline,
        )
        pv_forecast_minute_step = self.step_data_history(self.pv_forecast_minute, self.minutes_now, forward=True, cloud_factor=self.metric_cloud_coverage)
        pv_forecast_minute10_step = self.step_data_history(self.pv_forecast_minute10, self.minutes_now, forward=True, cloud_factor=min(self.metric_cloud_coverage + 0.2, 1.0) if self.metric_cloud_coverage else None, flip=True)
        self.refresh_pv_forecast_minute90()
        pv_forecast_minute90_step = self.step_data_history(self.pv_forecast_minute90, self.minutes_now, forward=True, cloud_factor=self.metric_cloud_coverage)

        # Save step data for debug
        self.load_minutes_step = load_minutes_step
        self.load_minutes_step10 = load_minutes_step10
        self.load_minutes_step90 = load_minutes_step90
        self.pv_forecast_minute_step = pv_forecast_minute_step
        self.pv_forecast_minute10_step = pv_forecast_minute10_step
        self.pv_forecast_minute90_step = pv_forecast_minute90_step

        # Yesterday data
        if recompute and self.calculate_savings and publish:
            self.calculate_yesterday()

        # Creation prediction object
        self.prediction = Prediction(self, pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, pv_forecast_minute90_step, load_minutes_step90)
        # The kernel spreads one batched fan-out across threads with the GIL released for the whole
        # call, so these are real cores - unlike a Python ThreadPool, which peaked at 1.15x on two
        # threads and then degraded below serial (perf/threadpool-prototype).
        self.prediction.batch_threads = resolve_batch_threads(self.get_arg("threads", "auto"), cpu_count())
        self.log("Prediction batch using {} kernel thread(s)".format(self.prediction.batch_threads))
        kernel_message, kernel_is_warning = kernel_status_summary(self.prediction)
        self.log("{}Prediction kernel: {}".format("Warn: " if kernel_is_warning else "", kernel_message))

        # Check if LoadML is active - it used to force the process pool off, which no longer exists;
        # the kernel's threads are C++ threads with no fork and no NumPy involvement
        load_ml_comp = self.components.get_component("load_ml") if self.components else None
        if load_ml_comp:
            self.log("LoadML is_calculating {}".format(load_ml_comp.is_calculating()))

        # Simulate current settings to get initial data
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            self.charge_limit, self.charge_window, self.export_window, self.export_limits, False, end_record=self.end_record
        )

        # Try different battery SoC's to get the best result
        if recompute:
            self.rate_best_cost_threshold_charge = None
            self.rate_best_cost_threshold_export = None

        if self.calculate_best and recompute:
            # Recomputing the plan
            self.log_option_best()

            # Full plan
            self.optimise_all_windows(metric, metric_keep, debug_mode)

            # Update target values, will be refined via clipping
            self.update_target_values()

            # Remove charge windows that overlap with export windows
            self.charge_limit_best, self.charge_window_best = remove_intersecting_windows(self.charge_limit_best, self.charge_window_best, self.export_limits_best, self.export_window_best)

            # Snapshot the plan as optimised, before clipping adjusts the percentages for execution
            preclip_new = (self.charge_limit_best.copy(), clone_windows(self.charge_window_best), clone_windows(self.export_window_best), self.export_limits_best.copy())

            # Model-based clipping: drop slots that do nothing in the central forecast. Runs after the
            # scoring snapshot so plan selection still compares plans as optimised (#4403).
            self.prune_dead_plan_slots()

            # Filter out any unused export windows
            if self.calculate_best_export and self.export_window_best:
                # Filter out the windows we disabled
                self.export_limits_best, self.export_window_best = self.discard_unused_export_slots(self.export_limits_best, self.export_window_best)

                # Clipping windows
                if self.export_window_best:
                    # Re-run prediction to get data for clipping
                    (
                        best_metric,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ) = self.run_prediction(
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.export_window_best,
                        self.export_limits_best,
                        False,
                        end_record=self.end_record,
                    )

                    # Work out record windows
                    record_export_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.export_window_best), 1)

                    # Export slot clipping
                    self.export_window_best, self.export_limits_best = self.clip_export_slots(self.minutes_now, self.predict_soc, self.export_window_best, self.export_limits_best, record_export_windows, PREDICT_STEP)

                    # Filter out the windows we disabled during clipping
                    self.export_limits_best, self.export_window_best = self.discard_unused_export_slots(self.export_limits_best, self.export_window_best)
                self.log("Export windows filtered {}".format(self.window_as_text(self.export_window_best, self.export_limits_best)))

            # Filter out any unused charge slots
            if self.calculate_best_charge and self.charge_window_best:
                # Re-run prediction to get data for clipping
                (
                    best_metric,
                    import_kwh_battery,
                    import_kwh_house,
                    export_kwh,
                    soc_min,
                    soc,
                    soc_min_minute,
                    battery_cycle,
                    metric_keep,
                    final_iboost,
                    final_carbon_g,
                ) = self.run_prediction(
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.export_window_best,
                    self.export_limits_best,
                    False,
                    end_record=self.end_record,
                )
                self.log("Raw charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))

                # Initial charge slot filter
                if self.set_charge_window:
                    record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
                    self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)

                # Charge slot clipping
                record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
                self.log("Unclipped charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))
                self.charge_window_best, self.charge_limit_best = self.clip_charge_slots(self.minutes_now, self.predict_soc, self.charge_window_best, self.charge_limit_best, record_charge_windows, PREDICT_STEP)

                if self.set_charge_window:
                    # Filter out the windows we disabled during clipping
                    self.log("Unfiltered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))
                    self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)
                    self.log("Filtered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))
                else:
                    self.log("Unfiltered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))

            # Plan comparison
            if charge_window_best_prev is not None and not debug_mode:
                # Score the plans as optimised rather than as clipped - see plan_scoring_pair()
                score_new, score_prev = self.plan_scoring_pair(
                    (self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best),
                    (charge_limit_best_prev, charge_window_best_prev, export_window_best_prev, export_limits_best_prev),
                    preclip_new,
                    preclip_prev,
                )
                metric, battery_value, cost, metric_keep, battery_cycle, final_carbon_g, import_kwh, export_kwh = self.run_prediction_metric(score_new[0], score_new[1], score_new[2], score_new[3], end_record=self.end_record)
                metric_prev, battery_value_prev, cost_prev, metric_keep_prev, battery_cycle_prev, final_carbon_g_prev, import_kwh_prev, export_kwh_prev = self.run_prediction_metric(
                    score_prev[0], score_prev[1], score_prev[2], score_prev[3], end_record=self.end_record
                )

                self.log("Previous plan best metric is {} (cost {}) and new plan best metric is {} (cost {})".format(dp2(metric_prev), dp2(cost_prev), dp2(metric), dp2(cost)))
                fragmentation_prev = self.plan_fragmentation(score_prev[1], score_prev[0], score_prev[2], score_prev[3])
                fragmentation_new = self.plan_fragmentation(score_new[1], score_new[0], score_new[2], score_new[3])
                if not self.should_replace_plan(metric_prev, metric, fragmentation_prev, fragmentation_new):
                    self.log("New plan metric is not significantly better (metric_min_improvement_plan {}) than previous plan, using previous plan".format(self.metric_min_improvement_plan))
                    self.charge_window_best = clone_windows(charge_window_best_prev)
                    self.charge_limit_best = charge_limit_best_prev.copy()
                    self.export_window_best = clone_windows(export_window_best_prev)
                    self.export_limits_best = export_limits_best_prev.copy()
                    # Keeping the incumbent keeps its pre-clip snapshot too, so the next cycle still compares
                    # like for like
                    preclip_new = preclip_prev
                elif (metric_prev - metric) >= self.metric_min_improvement_plan:
                    self.log("New plan metric is significantly better from previous plan, using new plan")
                else:
                    self.log("New plan is a cost-neutral improvement but less fragmented ({} vs {} segments), using new plan".format(fragmentation_new, fragmentation_prev))

            # Carry the pre-clip snapshot of whichever plan we kept into the next cycle
            self.plan_preclip = preclip_new

            # Plan is now valid
            self.log("Plan valid is now true after recompute was {}".format(self.plan_valid))
            if not self.update_pending:
                self.plan_valid = True
                metrics().plan_valid.set(1)
            else:
                self.log("Plan is not valid as update is pending, will re-compute on next run...")
                self.plan_valid = False
                metrics().plan_valid.set(0)
            self.plan_last_updated = self.now_utc
            self.plan_last_updated_minutes = self.minutes_now

        # Final simulation of base
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            self.charge_limit, self.charge_window, self.export_window, self.export_limits, False, save="base" if publish else None, end_record=self.end_record
        )
        # And base 10
        (
            metricb10,
            import_kwh_batteryb10,
            import_kwh_houseb10,
            export_kwhb10,
            soc_minb10,
            socb10,
            soc_min_minuteb10,
            battery_cycle10,
            metric_keep10,
            final_iboost10,
            final_carbon_g10,
        ) = self.run_prediction(
            self.charge_limit,
            self.charge_window,
            self.export_window,
            self.export_limits,
            True,
            save="base10" if publish else None,
            end_record=self.end_record,
        )

        if self.calculate_best:
            # Final simulation of best, do 10% and normal scenario
            (
                best_metric10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.export_window_best,
                self.export_limits_best,
                True,
                save="best10" if publish else None,
                end_record=self.end_record,
            )
            (
                best_metric,
                import_kwh_battery,
                import_kwh_house,
                export_kwh,
                soc_min,
                soc,
                soc_min_minute,
                battery_cycle,
                metric_keep,
                final_iboost,
                final_carbon_g,
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.export_window_best,
                self.export_limits_best,
                False,
                save="best" if publish else None,
                end_record=self.end_record,
            )
            # round charge_limit_best (kWh) to 3 decimal places
            self.charge_limit_best = [dp3(elem) for elem in self.charge_limit_best]

            self.log(
                "Best charging limit SoC's {}kWh, export {}kWh gives import battery {}kWh, house {}kWh, export {}kWh, metric {}{}, metric10 {}{}".format(
                    self.charge_limit_best, self.export_limits_best, dp2(import_kwh_battery), dp2(import_kwh_house), dp2(export_kwh), dp2(best_metric), curr, dp2(best_metric10), curr
                )
            )

            # Publish charge and export window best
            if publish:
                self.publish_charge_limit(self.charge_limit_best, self.charge_window_best, best=True, soc=self.predict_soc_best)
                self.publish_export_limit(self.export_window_best, self.export_limits_best, best=True)

                # Compute marginal energy cost matrix (what-if extra load scenarios)
                self.calculate_marginal_costs()

                # HTML data
                text = self.short_textual_plan(soc_min, soc_min_minute, pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, self.end_record)
                text_lines = text.split("\n")
                for line in text_lines:
                    self.log(line)
                self.publish_html_plan(pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, self.end_record)

        # Record planning duration for SLO metrics
        self.plan_last_duration_seconds = time.time() - plan_start_time
        metrics().planning_duration_seconds.observe(self.plan_last_duration_seconds)
        self.log("Plan calculation took {:.2f} seconds".format(self.plan_last_duration_seconds))

        # Return if we recomputed or not
        return recompute

    def battery_value_rate(self, minute):
        """
        Forward value of one kWh left in the battery at the given absolute minute, in p/kWh

        The value is what it would cost to replace that energy: the cheapest import rate available
        from `minute` to the end of the forecast, grossed up by the charging losses. It is capped at
        the highest import rate (reduced by losses) so a flat tariff cannot value stored energy above
        what the grid would ever charge for it, and floored at the export arbitrage margin and at
        1p/kWh.

        Both the cap and the export recovery ratio read the base tariff (rate_max_base,
        rate_export_max_forward), captured before saving sessions and overrides are layered on. A
        session is a one-off event, not evidence about what the tariff charges for a kWh or pays for a
        surplus one, and letting one set either term values stored energy above what discharging it can
        realise - which the planner spends as profit by freeze charging every window up to end_record.
        Both fall back to their whole-horizon equivalents when the base data is absent, so replaying an
        older debug file behaves as it did before.

        Note `rate_export_min` here is not the export rate - it is the export earnings less the
        replacement cost, so it only raises the value when exporting beats re-importing. It can
        never lower it, which is why a zero export rate does not reduce the credit.

        This is the single definition used by the planner's metric, the dashboard's value_per_kwh
        attributes and the savings report, so all three agree on what a stored kWh is worth.
        """
        rate_min_raw = self.rate_min_forward.get(minute, self.rate_min)
        rate_max = self.rate_max_base or self.rate_max
        rate_min = rate_min_raw / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
        rate_min = max(min(rate_min, rate_max * self.inverter_loss * self.battery_loss - self.metric_battery_cycle), 0)

        # Replacement cost assumes the energy can always be redeployed. That holds while surplus can
        # be sold, but if export pays less than it cost to import then anything the house cannot use
        # is a partial loss, so the stored energy is worth less than replacement. Scale the value by
        # how much of the cheapest import price export would actually recover: full value when export
        # matches or beats it, down to metric_battery_value_export_scaling when export is worthless.
        # Setting that to 1.0 disables this entirely.
        if rate_min_raw > 0 and self.metric_battery_value_export_scaling < 1.0:
            # Forward-looking like rate_min_raw, so an export price that has already passed stops
            # counting once it has - a saving session ending in ten minutes says nothing about what
            # the energy still in the battery can be sold for over the rest of the plan.
            export_max = self.rate_export_max_forward.get(minute, 0.0) if self.rate_export_max_forward else self.rate_export_max
            recovery = min(max(export_max / rate_min_raw, 0.0), 1.0)
            rate_min *= self.metric_battery_value_export_scaling + (1.0 - self.metric_battery_value_export_scaling) * recovery

        rate_export_min = self.rate_export_min * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle - rate_min
        return max(rate_min, 1.0, rate_export_min)

    def compute_metric(self, end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=None, cost90=None, final_iboost90=0.0):
        """
        Compute the metric by blending the nominal, PV10 and PV90 scenarios

        cost90 is the switch for the PV90 term - when it is None the scenario was not simulated and
        its weight collapses into the nominal weight.
        """
        # Store simulated mid value
        metric = cost
        metric10 = cost10
        metric90 = cost90

        # Balancing payment to account for battery left over
        # ie. how much extra battery is worth to us in future, assume it's the same as low rate
        value_rate = self.battery_value_rate(self.minutes_now + end_record)
        battery_value = (soc * self.metric_battery_value_scaling + final_iboost * self.iboost_value_scaling) * value_rate
        battery_value10 = (soc10 * self.metric_battery_value_scaling + final_iboost10 * self.iboost_value_scaling) * value_rate
        metric -= battery_value
        metric10 -= battery_value10
        if metric90 is not None:
            battery_value90 = ((soc90 or 0) * self.metric_battery_value_scaling + final_iboost90 * self.iboost_value_scaling) * value_rate
            metric90 -= battery_value90

        # Signed weighted average across the simulated scenarios. Unlike the previous downside-only
        # clamp this lets a better-than-nominal scenario pull the metric down, which is what gives
        # PV90 a gradient at all - PV90 is nearly always cheaper than nominal.
        weight10 = self.pv_metric10_weight
        weight90 = self.pv_metric90_weight if metric90 is not None else 0.0
        weight_total = weight10 + weight90
        if weight_total > 1.0:
            weight10 = weight10 / weight_total
            weight90 = weight90 / weight_total
        metric = (1.0 - weight10 - weight90) * metric + weight10 * metric10 + weight90 * (metric90 if metric90 is not None else 0.0)

        # Carbon metric
        if self.carbon_enable:
            metric += (final_carbon_g / 1000) * self.carbon_metric

        # Self sufficiency metric
        metric += (import_kwh_house + import_kwh_battery) * self.metric_self_sufficiency

        # Adjustment for battery cycles metric
        metric += battery_cycle * self.metric_battery_cycle + metric_keep

        return dp4(metric), dp4(battery_value)

    def optimise_charge_limit(self, window_n, record_charge_windows, charge_limit, charge_window, export_window, export_limits, all_n=None, end_record=None, freeze_only=False, allow_freeze=True):
        """
        Optimise a single charging window for best SoC
        """
        loop_soc = self.soc_max
        best_soc = self.soc_max
        best_soc_min = 0
        best_soc_min_minute = 0
        best_metric = 9999999
        best_metric_first = best_metric
        best_metric_plan = 9999999
        best_cost = 0
        best_import = 0
        best_soc_step = self.best_soc_step
        best_keep = 0
        best_cycle = 0
        best_carbon = 0
        all_max_soc = self.soc_max
        all_min_soc = 0
        try_charge_limit = list(charge_limit)
        resultmid = {}
        result10 = {}
        result90 = {}
        run_pv90 = self.pv_metric90_weight > 0

        if not self.set_charge_freeze:
            allow_freeze = False

        if all_n:
            window_n = all_n[0]

        min_improvement_scaled = self.metric_min_improvement
        if not all_n:
            start = charge_window[window_n]["start"]
            end = charge_window[window_n]["end"]
            window_size = end - start
            if window_size <= self.plan_interval_minutes:
                best_soc_step = best_soc_step * 2
            min_improvement_scaled = self.metric_min_improvement * window_size / float(self.plan_interval_minutes)

        # Start the loop at the max soc setting
        if self.best_soc_max > 0:
            loop_soc = min(loop_soc, self.best_soc_max)

        # Create min/max SoC to avoid simulating SoC that are not going have any impact
        # Can't do this for anything but a single window as the winder SoC impact isn't known
        if not all_n and not freeze_only:
            hans = []
            all_max_soc = 0
            all_min_soc = self.soc_max
            hans.append(self.launch_run_prediction_charge_min_max(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, all_n, end_record))
            hans.append(self.launch_run_prediction_charge_min_max(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, all_n, end_record))
            hans.append(self.launch_run_prediction_charge_min_max(best_soc_min, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, all_n, end_record))
            hans.append(self.launch_run_prediction_charge_min_max(best_soc_min, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, all_n, end_record))
            # These two candidates (full-charge loop_soc and best_soc_min) are pre-simulated here and
            # never re-launched by the results/results10/results90 block below (they are excluded there
            # by the `if try_soc not in resultmid` gate), so they must get a pv90 result of their own
            # here too - otherwise they are scored with cost90=None while every other candidate in the
            # same ranking loop gets the three-scenario blend, systematically advantaging these two
            # extreme candidates (see task-7 fix round 1, Finding 2).
            #
            # These two extra launches also feed the same `hans` loop that computes all_min_soc/
            # all_max_soc below, so at run_pv90 the SoC-pruning envelope is built from three scenarios
            # instead of two. This only ever widens the envelope (min/max over a superset can only move
            # outward or stay put, never inward), so it can only relax the pruning below, never discard
            # a candidate that would otherwise have been tried - it cannot silently drop a viable SoC.
            if run_pv90:
                hans.append(self.launch_run_prediction_charge_min_max(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV90, all_n, end_record))
                hans.append(self.launch_run_prediction_charge_min_max(best_soc_min, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV90, all_n, end_record))
            id = 0
            for han in hans:
                (
                    cost,
                    import_kwh_battery,
                    import_kwh_house,
                    export_kwh,
                    soc_min,
                    soc,
                    soc_min_minute,
                    battery_cycle,
                    metric_keep,
                    final_iboost,
                    final_carbon_g,
                    min_soc,
                    max_soc,
                ) = han.get()
                all_min_soc = min(all_min_soc, min_soc)
                all_max_soc = max(all_max_soc, max_soc)
                if id == 0:
                    resultmid[loop_soc] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ]
                elif id == 1:
                    result10[loop_soc] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ]
                elif id == 2:
                    resultmid[best_soc_min] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ]
                elif id == 3:
                    result10[best_soc_min] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ]
                elif id == 4:
                    result90[loop_soc] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ]
                elif id == 5:
                    result90[best_soc_min] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ]
                id += 1

        # Assemble list of SoC's to try
        try_socs = [loop_soc]
        loop_step = max(best_soc_step, 0.1)
        best_soc_min_setting = self.best_soc_min
        if best_soc_min_setting > 0:
            best_soc_min_setting = max(self.reserve, best_soc_min_setting)

        while loop_soc > self.reserve and not freeze_only:
            skip = False
            try_soc = max(best_soc_min, loop_soc)
            try_soc = dp2(min(try_soc, self.soc_max))
            if try_soc > (all_max_soc + loop_step):
                skip = True
            if (try_soc > self.reserve) and (try_soc > self.best_soc_min) and (try_soc < (all_min_soc - loop_step)):
                skip = True
            # Keep those we already simulated
            if try_soc in resultmid:
                skip = False
            # Keep the current setting if different from the selected ones
            if not all_n and try_soc == charge_limit[window_n]:
                skip = False
            # All to the list
            if not skip and (try_soc not in try_socs) and (try_soc != self.reserve):
                try_socs.append(dp2(try_soc))
            loop_soc -= loop_step

        if freeze_only:
            try_socs = [charge_limit[window_n]]
            if allow_freeze and self.reserve not in try_socs:
                try_socs.append(self.reserve)
        else:
            # Give priority to off to avoid spurious charge freezes
            if best_soc_min_setting not in try_socs:
                try_socs.append(best_soc_min_setting)
            if allow_freeze and (self.reserve not in try_socs):
                try_socs.append(self.reserve)
            if not allow_freeze and (self.reserve in try_socs):
                try_socs.remove(self.reserve)

        # Run the simulations in parallel
        results = []
        results10 = []
        results90 = []
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, all_n, end_record)
                results.append(hanres)
                hanres10 = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, all_n, end_record)
                results10.append(hanres10)
                if run_pv90:
                    results90.append(self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV90, all_n, end_record))

        # Get results from sims if we simulated them
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = results.pop(0)
                hanres10 = results10.pop(0)
                resultmid[try_soc] = hanres.get()
                result10[try_soc] = hanres10.get()
                if run_pv90:
                    result90[try_soc] = results90.pop(0).get()

        window_results = {}
        # Now we have all the results, we can pick the best SoC
        # Note the first result is full charge, so metric min improvement will work against that
        first = True
        for try_soc in try_socs:
            window = charge_window[window_n]

            # Store try value into the window, either all or just this one
            if all_n:
                for window_id in all_n:
                    try_charge_limit[window_id] = try_soc
            else:
                try_charge_limit[window_n] = try_soc

            # Simulate with medium PV
            (cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g) = resultmid[try_soc]
            (cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10) = result10[try_soc]
            soc90 = None
            cost90 = None
            final_iboost90 = 0.0
            if try_soc in result90:
                (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = result90[try_soc]

            # Compute the metric from simulation results
            metric, battery_value = self.compute_metric(
                end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90
            )

            # Keep the unadjusted metric: the adjustments below are ranking hints for this function only, so a
            # caller checking whether the plan actually improved has to compare on this instead
            metric_plan = metric

            # Metric adjustment based on current charge limit when inside the window
            # to try to avoid constant small changes to SoC target by forcing to keep the current % during a charge period
            # if changing it has little impact
            if not all_n and self.isCharging and (window_n == self.in_charge_window(charge_window, self.minutes_now)):
                if calc_percent_limit(self.isCharging_Target, self.soc_max) == calc_percent_limit(try_soc, self.soc_max):
                    metric -= max(0.1, self.metric_min_improvement)

            if try_soc == best_soc_min_setting:
                # Minor weighting to 0%
                metric -= 0.003
            elif try_soc == self.soc_max:
                # Minor weighting to 100%
                metric -= 0.002
            elif self.set_charge_freeze and try_soc == self.reserve:
                # Minor weighting to freeze
                metric -= 0.001

            # Round metric to 4 DP
            metric = dp4(metric)

            if self.debug_enable:
                self.log(
                    "Sim: SoC {} soc_min {} @ {} window {} metric {} cost {} cost10 {} soc {} soc10 {} final_iboost {} final_iboost10 {} final_carbon_g {} metric_keep {} cycle {} carbon {} import {} export {} battery_value {}".format(
                        try_soc,
                        dp1(soc_min),
                        self.time_abs_str(soc_min_minute),
                        window_n,
                        dp2(metric),
                        dp2(cost),
                        dp2(cost10),
                        dp1(soc),
                        dp1(soc10),
                        dp2(final_iboost),
                        dp2(final_iboost10),
                        dp0(final_carbon_g),
                        dp2(metric_keep),
                        dp2(battery_cycle),
                        dp0(final_carbon_g),
                        dp2(import_kwh_battery + import_kwh_house),
                        dp2(export_kwh),
                        dp2(battery_value),
                    )
                )

            window_results[try_soc] = metric

            # Only select the lower SoC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold
            if (metric + min_improvement_scaled) <= best_metric_first and metric <= best_metric:
                best_metric = metric
                best_metric_plan = metric_plan
                best_soc = try_soc
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
                best_keep = metric_keep
                best_cycle = battery_cycle
                best_carbon = final_carbon_g
                best_import = import_kwh_battery + import_kwh_house

            if first:
                best_metric_first = metric
                first = False

        # Add margin last
        best_soc = min(best_soc, self.soc_max)

        if self.debug_enable:
            if not all_n:
                self.log(
                    "Try optimising charge window(s)    {}: {} - {} price {} cost {} metric {} keep {} cycle {} carbon {} import {} export {} selected {} was {} results {}".format(
                        window_n,
                        self.time_abs_str(window["start"]),
                        self.time_abs_str(window["end"]),
                        charge_window[window_n]["average"],
                        dp4(best_cost),
                        dp4(best_metric),
                        dp4(best_keep),
                        dp4(best_cycle),
                        dp0(best_carbon),
                        dp4(import_kwh_battery + import_kwh_house),
                        dp4(export_kwh),
                        best_soc,
                        charge_limit[window_n],
                        window_results,
                    )
                )
            else:
                self.log(
                    "Try optimising charge window(s)    {}: price {} cost {} metric {} keep {} cycle {} carbon {} import {} selected {} was {} results {}".format(
                        all_n,
                        charge_window[window_n]["average"],
                        dp2(best_cost),
                        dp2(best_metric),
                        dp2(best_keep),
                        dp2(best_cycle),
                        dp0(best_carbon),
                        dp0(best_import),
                        best_soc,
                        charge_limit[window_n],
                        window_results,
                    )
                )
        return best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import, best_metric_plan

    def optimise_export(self, window_n, record_charge_windows, try_charge_limit, charge_window, export_window, export_limit, all_n=None, end_record=None, freeze_only=False, allow_freeze=True):
        """
        Optimise a single export window for best export %
        """
        best_export = False
        best_metric = 9999999
        best_metric_plan = 9999999
        off_metric = 9999999
        off_cost = 9999999
        best_cost = 0
        best_soc_min = 0
        best_soc_min_minute = 0
        best_keep = 0
        best_cycle = 0
        best_import = 0
        best_carbon = 0
        this_export_limit = 100.0
        window = export_window[window_n]
        # A shallow copy is enough: nothing here writes to a window dict, and the one write that does
        # happen downstream - the trial start - is applied copy-on-write by _prepare_export, which
        # takes its own list and replaces that single window with dict(window, start=start). The list
        # is still copied so a caller cannot reorder it underneath a batch that has not flushed yet.
        # Deep-copying every window dict on entry was the largest block of copying in a plan.
        try_export_window = list(export_window)
        try_export = list(export_limit)
        best_start = window["start"]
        best_size = window["end"] - best_start
        export_step = 5
        export_step_large = 15

        if not self.set_export_freeze:
            allow_freeze = False

        # loop on each export option
        if allow_freeze and (freeze_only or self.set_export_freeze_only):
            loop_options = [100.0, 99.0]
        elif allow_freeze and not self.set_export_freeze_only:
            # If we support freeze, try a 99% option which will freeze at any SoC level below this
            loop_options = [100.0, 99.0, 0.0]
            if self.set_export_low_power:
                loop_options.extend([0.3, 0.5, 0.7])
        else:
            loop_options = [100.0, 0.0]
            if self.set_export_low_power:
                loop_options.extend([0.3, 0.5, 0.7])

        # Collect all options
        results = []
        results10 = []
        results90 = []
        run_pv90 = self.pv_metric90_weight > 0
        try_options = []
        for loop_limit in loop_options:
            # Loop on window size
            loop_start = window["end"] - 5  # Minimum export window size 5 minutes
            while loop_start >= window["start"]:
                this_export_limit = loop_limit
                start = loop_start

                # Move the loop start back to full size
                current_len = window["end"] - start
                if current_len >= 120 and (current_len % export_step_large) == 0:
                    # Large window, step back by 15 minutes
                    loop_start -= export_step_large
                else:
                    loop_start -= export_step

                # Can't optimise all window start slot
                if all_n and (start != window["start"]):
                    continue

                # Don't allow slow export for small windows
                if this_export_limit > int(this_export_limit) and (try_export_window[window_n]["end"] - start) < 15:
                    continue

                # Don't optimise start of disabled windows or freeze only windows, just for export ones
                if (this_export_limit in [100.0, 99.0]) and (start != window["start"]):
                    continue

                # Never go below the minimum level
                this_export_limit = max(calc_percent_limit(self.best_soc_min, self.soc_max), int(this_export_limit))
                this_export_limit = this_export_limit + loop_limit - int(loop_limit)
                try_options.append([start, this_export_limit])

                results.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, PV_SCENARIO_NOMINAL, all_n, end_record))
                results10.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, PV_SCENARIO_PV10, all_n, end_record))
                if run_pv90:
                    results90.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, PV_SCENARIO_PV90, all_n, end_record))

        # Get results from sims
        try_results = []
        for try_option in try_options:
            hanres = results.pop(0)
            hanres10 = results10.pop(0)
            result = hanres.get()
            result10 = hanres10.get()
            result90 = results90.pop(0).get() if run_pv90 else None
            try_results.append(try_option + [result, result10, result90])

        window_results = {}
        for try_option in try_results:
            start, this_export_limit, hanres, hanres10, hanres90 = try_option

            # Simulate with medium PV
            cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = hanres
            (
                cost10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
            ) = hanres10
            soc90 = None
            cost90 = None
            final_iboost90 = 0.0
            if hanres90 is not None:
                (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = hanres90

            # Compute the metric from simulation results
            metric, battery_value = self.compute_metric(
                end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90
            )

            # Keep the unadjusted metric: the adjustments below are ranking hints for this function only, so a
            # caller checking whether the plan actually improved has to compare on this instead
            metric_plan = metric

            if this_export_limit == 100.0:
                # Minor weighting to off
                metric -= 0.002
            elif this_export_limit == 0:
                # Minor weighting to 0%
                metric -= 0.001

            # Adjust to try to keep existing windows
            keep_export = False
            if window_n < 2 and this_export_limit < 99.0 and self.export_window and self.isExporting:
                pwindow = export_window[window_n]
                dwindow = self.export_window[0]
                if self.minutes_now >= pwindow["start"] and self.minutes_now < pwindow["end"] and ((self.minutes_now >= dwindow["start"] and self.minutes_now < dwindow["end"]) or (dwindow["end"] == pwindow["start"])):
                    # Only reward an option that actually covers the current minute. The option start is varied
                    # during optimisation, so a future-starting option is not the in-progress export and must
                    # receive neither the metric bonus nor the cost-gate commitment - giving both options the
                    # same bonus cancels it out and lets "stop now, restart later in this window" win, which is
                    # the export flapping this commitment exists to prevent.
                    if start <= self.minutes_now:
                        metric -= max(0.5, self.metric_min_improvement_export)
                        keep_export = True

            # Round metric to 4 DP
            metric = dp4(metric)

            if self.debug_enable:
                self.log(
                    "Sim: Export {} window {} start {} end {}, import {} export {} min_soc {} @ {} soc {} soc10 {} cost {} cost10 {} metric {} cycle {} iboost {} iboost10 {} carbon {} keep {} battery_value {} end_record {}".format(
                        this_export_limit,
                        window_n,
                        self.time_abs_str(start),
                        self.time_abs_str(try_export_window[window_n]["end"]),
                        dp2(import_kwh_battery + import_kwh_house),
                        dp2(export_kwh),
                        dp1(soc_min),
                        self.time_abs_str(soc_min_minute),
                        dp1(soc),
                        dp1(soc10),
                        dp2(cost),
                        dp2(cost10),
                        dp2(metric),
                        dp2(battery_cycle * self.metric_battery_cycle),
                        dp2(final_iboost),
                        dp2(final_iboost10),
                        dp0(final_carbon_g),
                        dp2(metric_keep),
                        dp2(battery_value),
                        end_record,
                    )
                )

            window_size = try_export_window[window_n]["end"] - start
            window_key = str(dp2(this_export_limit)) + "_" + str(window_size)
            window_results[window_key] = [metric, cost]

            # Only select an export if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # Scale back in the case of freeze export as improvements will be smaller
            rate_scale = 1 - (this_export_limit - int(this_export_limit))

            if this_export_limit == 99:
                min_improvement_scaled = self.metric_min_improvement_export_freeze
            elif all_n:
                min_improvement_scaled = self.metric_min_improvement_export * rate_scale * len(all_n)
            else:
                min_improvement_scaled = self.metric_min_improvement_export * window_size * rate_scale / float(self.plan_interval_minutes)

            # When already exporting within this window keep the export going across planning cycles by
            # relaxing the cost gate (not just the metric above). This only sustains an in-progress forced
            # export (it never opens a new one) so it cannot reintroduce the metric_keep gaming of issue #2984.
            # It prevents export oscillation on near-flat multi-slot price peaks where exporting now versus
            # holding and exporting the adjacent equal-priced slot is otherwise a cost coin-toss each cycle.
            if keep_export:
                min_improvement_scaled = min(min_improvement_scaled, -max(0.5, self.metric_min_improvement_export))

            # Only select an export if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # Also require cost improvement to prevent exports that only game metric_keep without actual savings (issue #2984)
            if (metric <= off_metric) and (metric <= best_metric) and ((cost + min_improvement_scaled) <= off_cost):
                best_metric = metric
                best_metric_plan = metric_plan
                best_export = this_export_limit
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
                best_start = start
                best_size = window_size
                best_keep = metric_keep
                best_cycle = battery_cycle
                best_carbon = final_carbon_g
                best_import = import_kwh_battery + import_kwh_house

            # Store the metric and cost for export off
            if off_metric == 9999999:
                off_metric = metric
                off_cost = cost

        if self.debug_enable:
            if not all_n:
                self.log(
                    "Try optimising export window(s) {}: {} - {} price {} cost {} metric {} carbon {} import {} keep {} selected {}% size {} was {}% results {}".format(
                        window_n,
                        self.time_abs_str(window["start"]),
                        self.time_abs_str(window["end"]),
                        window["average"],
                        dp2(best_cost),
                        dp2(best_metric),
                        dp2(best_carbon),
                        dp2(best_import),
                        dp2(best_keep),
                        best_export,
                        best_size,
                        export_limit[window_n],
                        window_results,
                    )
                )
            else:
                self.log(
                    "Try optimising export window(s) {} price {} selected {}% size {} cost {} metric {} carbon {} import {} keep {} results {}".format(
                        all_n,
                        window["average"],
                        best_export,
                        best_size,
                        dp2(best_cost),
                        dp2(best_metric),
                        dp2(best_carbon),
                        dp2(best_import),
                        dp2(best_keep),
                        window_results,
                    )
                )

        return best_export, best_start, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import, best_metric_plan

    def window_sort_func(self, window):
        """
        Helper sort index function
        """
        return float(window["key"])

    def window_sort_func_start(self, window):
        """
        Helper sort index function
        """
        return float(window["start"])

    def sort_window_by_time(self, windows):
        """
        Sort windows in start time order, return a new list of windows
        """
        window_sorted = clone_windows(windows)
        window_sorted.sort(key=self.window_sort_func_start)
        return window_sorted

    def _io_run_starts(self, windows):
        """
        Map each io_adjusted (Octopus Intelligent) window start to the start minute of its
        contiguous IOG run.

        A run is a maximal sequence of io_adjusted windows that are contiguous in time
        (each window's start equal to the previous window's end). A firm window or a time
        gap breaks the run. Firm windows are not included in the result. Input windows may
        arrive in any order.
        """
        io_windows = sorted((w for w in windows if self.io_adjusted.get(w["start"], False)), key=lambda w: w["start"])
        run_starts = {}
        run_start = None
        prev_end = None
        for window in io_windows:
            start = window["start"]
            if run_start is None or start != prev_end:
                run_start = start
            run_starts[start] = run_start
            prev_end = window["end"]
        return run_starts

    def _io_rate_adjustment(self, window_start, run_start):
        """
        Return the signed rate adjustment (pence) for an io_adjusted window.

        Earliest slots in the run are discounted (negative) so they rank below equally-priced
        firm slots and are filled first; latest slots are penalised (positive) so distant IOG
        slots are not relied upon. The discount is only applied to imminent slots (starting
        within IO_ADJUST_DISCOUNT_HORIZON_HOURS of now); the penalty side always applies.
        """
        hours_in = (window_start - run_start) / 60.0
        hours_ahead = max(window_start - self.minutes_now, 0) / 60.0
        gradient = (hours_in - IO_ADJUST_PIVOT_HOURS) * IO_ADJUST_SLOPE
        gradient = max(-IO_ADJUST_MAX_DISCOUNT, min(IO_ADJUST_MAX_PENALTY, gradient))
        if hours_ahead > IO_ADJUST_DISCOUNT_HORIZON_HOURS:
            # Distant period: suppress the discount but keep any penalty
            gradient = max(gradient, 0.0)
        return gradient

    def sort_window_by_price_combined(self, charge_windows, export_windows, calculate_import_low_export=False, calculate_export_high_import=False):
        """
        Sort windows into price sets
        """
        window_sort = []
        window_links = {}
        price_set = []
        price_links = {}

        pv_forecast_minute_step = self.prediction.pv_forecast_minute_step

        # Add charge windows
        charge_io_run_starts = self._io_run_starts(charge_windows)
        if self.calculate_best_charge:
            id = 0
            for window in charge_windows:
                # Account for losses in average rate as it makes import higher
                average = window["average"] / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
                if self.carbon_enable:
                    carbon_intensity = self.carbon_intensity.get(max(window["start"] - self.minutes_now, 0), 0)
                    average += dp1(carbon_intensity * self.carbon_metric / 1000.0)
                if window["start"] in charge_io_run_starts:
                    # IOG (planned-dispatch) slot: apply the earlier-charge skew gradient
                    average += self._io_rate_adjustment(window["start"], charge_io_run_starts[window["start"]])
                average += self.metric_self_sufficiency
                average = dp2(average)  # Round to nearest 0.01 penny to avoid too many bands
                if calculate_import_low_export:
                    average_export = dp2((self.rate_export.get(window["start"], 0) + self.rate_export.get(window["end"] - PREDICT_STEP, 0)) / 2)
                else:
                    average_export = 0
                window_start = window["start"]
                sort_key = "%04.2f_%04.2f_%04d_c%02d" % (5000 - average, 5000 - average_export, 9999 - window_start, id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "c"
                window_links[sort_key]["id"] = id
                window_links[sort_key]["average"] = dp1(average / 2) * 2  # Round to nearest 0.2 penny to avoid too many bands
                window_links[sort_key]["average_secondary"] = dp1(average_export)  # Round to nearest 0.1 penny to avoid too many bands

                if self.set_charge_freeze:
                    average = window["average"]
                    if self.carbon_enable:
                        carbon_intensity = self.carbon_intensity.get(window["start"] - self.minutes_now, 0)
                        average += dp1(carbon_intensity * self.carbon_metric / 1000.0)
                    average += self.metric_self_sufficiency
                    average = dp2(average)  # Round to nearest 0.01 penny to avoid too many bands
                    sort_key = "%04.2f_%04.2f_%04d_cf%02d" % (5000 - average, 5000 - average_export, 9999 - window_start, id)
                    window_sort.append(sort_key)
                    window_links[sort_key] = {}
                    window_links[sort_key]["type"] = "cf"
                    window_links[sort_key]["id"] = id
                    window_links[sort_key]["average"] = dp1(average / 2) * 2  # Round to nearest 0.2 penny to avoid too many bands
                    window_links[sort_key]["average_secondary"] = dp1(average_export)  # Round to nearest 0.1 penny to avoid too many bands

                id += 1

        # Add export windows
        export_io_run_starts = self._io_run_starts(export_windows)
        if self.calculate_best_export:
            id = 0
            for window in export_windows:
                # Account for losses in average rate as it makes export value lower
                average = window["average"] * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle
                if self.carbon_enable:
                    carbon_intensity = self.carbon_intensity.get(max(window["start"] - self.minutes_now, 0), 0)
                    average += dp1(carbon_intensity * self.carbon_metric / 1000.0)
                average = dp1(average)  # Round to nearest 0.01 penny to avoid too many bands
                if calculate_export_high_import:
                    average_import = dp2((self.rate_import.get(window["start"], 0) + self.rate_import.get(window["end"] - PREDICT_STEP, 0)) / 2)
                    if window["start"] in export_io_run_starts:
                        # IOG (planned-dispatch) slot on the import side: apply the earlier-charge skew gradient
                        average_import += self._io_rate_adjustment(window["start"], export_io_run_starts[window["start"]])
                else:
                    average_import = 0
                window_start = window["start"]
                sort_key = "%04.2f_%04.2f_%04d_d%02d" % (5000 - average, 5000 - average_import, 9999 - window_start, id)
                if not self.calculate_export_first:
                    # Push export last if first is not set
                    sort_key = "zz_" + sort_key
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "d"
                window_links[sort_key]["id"] = id
                window_links[sort_key]["average"] = dp1(average / 2) * 2  # Round to nearest 0.2 penny to avoid too many bands
                window_links[sort_key]["average_secondary"] = dp1(average_import)  # Round to nearest 0.1 penny to avoid too many bands

                if self.set_export_freeze:
                    pv_period = 0
                    for minute in range(window_start - self.minutes_now, window["end"] - self.minutes_now, PREDICT_STEP):
                        pv_period += pv_forecast_minute_step.get(minute, 0)

                    if pv_period >= 0.1:
                        average = window["average"]
                        if self.carbon_enable:
                            carbon_intensity = self.carbon_intensity.get(window["start"] - self.minutes_now, 0)
                            average += dp1(carbon_intensity * self.carbon_metric / 1000.0)
                        average = dp2(average)  # Round to nearest 0.01 penny to avoid too many bands
                        sort_key = "%04.2f_%04.2f_%04d_df%02d" % (5000 - average, 5000 - average_import, 9999 - window_start, id)
                        if not self.calculate_export_first:
                            # Push export last if first is not set
                            sort_key = "zz_" + sort_key
                        window_sort.append(sort_key)
                        window_links[sort_key] = {}
                        window_links[sort_key]["type"] = "df"
                        window_links[sort_key]["id"] = id
                        window_links[sort_key]["average"] = dp1(average / 2) * 2  # Round to nearest 0.2 penny to avoid too many bands
                        window_links[sort_key]["average_secondary"] = dp1(average_import)  # Round to nearest 0.1 penny to avoid too many bands

                id += 1

        if window_sort:
            window_sort.sort()

        # Create price ordered links by set
        for key in window_sort:
            average = window_links[key]["average"]
            if average not in price_set:
                price_set.append(average)
                price_links[average] = []
            price_links[average].append(key)

        return window_sort, window_links, price_set, price_links

    def sort_window_by_time_combined(self, charge_windows, export_windows):
        window_sort = []
        window_links = {}

        # Add charge windows
        if self.calculate_best_charge:
            id = 0
            for window in charge_windows:
                sort_key = "%04d_%03d_c" % (window["start"], id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "c"
                window_links[sort_key]["id"] = id
                id += 1

        # Add export windows
        if self.calculate_best_export:
            id = 0
            for window in export_windows:
                sort_key = "%04d_%03d_d" % (window["start"], id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "d"
                window_links[sort_key]["id"] = id
                id += 1

        if window_sort:
            window_sort.sort()

        return window_sort, window_links

    def sort_window_by_price(self, windows, reverse_time=False):
        """
        Sort the charge windows by highest price first, return a list of window IDs
        """
        window_with_id = clone_windows(windows)
        wid = 0
        for window in window_with_id:
            window["id"] = wid
            if reverse_time:
                window["key"] = "%04.2f%02d" % (5000 - window["average"], 999 - window["id"])
            else:
                window["key"] = "%04.2f%02d" % (5000 - window["average"], window["id"])
            wid += 1
        window_with_id.sort(key=self.window_sort_func)
        id_list = []
        for window in window_with_id:
            id_list.append(window["id"])
        return id_list

    def discard_unused_charge_slots(self, charge_limit_best, charge_window_best, reserve):
        """
        Filter out unused charge slots (those set at 0)
        """
        new_limit_best = []
        new_window_best = []

        max_slots = len(charge_limit_best)

        for window_n in range(max_slots):
            # Only keep slots > than reserve, or keep the last one so we don't have zero slots
            # Also keep a slot if we are already inside it and charging is enabled
            window = charge_window_best[window_n].copy()
            start = window["start"]
            end = window["end"]
            limit = charge_limit_best[window_n]

            predict_minute_start = max(int((start - self.minutes_now) / 5) * 5, 0)
            predict_minute_end = int((end - self.minutes_now) / 5) * 5
            start_soc = self.predict_soc.get(predict_minute_start, limit)
            end_soc = self.predict_soc.get(predict_minute_end, limit)
            if limit <= start_soc and limit <= end_soc:
                window["target"] = limit
            else:
                window["target"] = end_soc

            if (
                new_window_best
                and (start == new_window_best[-1]["end"])
                and (limit == new_limit_best[-1])
                and (start not in self.manual_all_times)
                and (start not in self.all_active_keep)
                and (new_window_best[-1]["start"] not in self.manual_all_times)
                and (new_window_best[-1]["start"] not in self.all_active_keep)
                and (new_window_best[-1]["average"] >= window["average"] or not self.set_charge_low_power or limit == self.reserve)
            ):
                # Combine two windows of the same charge target provided the rates are the same or low power mode is off (low power mode can skew the charge into the more expensive slot)
                new_window_best[-1]["end"] = end
                new_window_best[-1]["target"] = window.get("target", limit)
                new_window_best[-1]["average"] = dp2((new_window_best[-1]["average"] + window["average"]) / 2)
                if self.debug_enable:
                    self.log("Combine charge slot {} with previous (same target) - target soc {} kWh slot {} start {} end {} limit {}".format(window_n, new_limit_best[-1], new_window_best[-1], start, end, limit))
            elif (
                new_window_best
                and (start == new_window_best[-1]["end"])
                and (limit >= new_limit_best[-1])
                and not (limit != self.reserve and new_limit_best[-1] == self.reserve)
                and (start not in self.manual_all_times)
                and (start not in self.all_active_keep)
                and (new_window_best[-1]["start"] not in self.manual_all_times)
                and (new_window_best[-1]["start"] not in self.all_active_keep)
                and new_window_best[-1]["average"] == window["average"]
                and (new_window_best[-1]["target"] < new_limit_best[-1])
            ):
                # Combine two windows of the same price, provided the second charge limit is greater than the first
                # and the old charge never reaches it defined limit
                new_window_best[-1]["end"] = end
                new_window_best[-1]["target"] = window.get("target", limit)
                new_limit_best[-1] = limit
                if self.debug_enable:
                    self.log("Combine charge slot {} with previous (same price) - target soc {} kWh slot {} start {} end {} limit {}".format(window_n, new_limit_best[-1], new_window_best[-1], start, end, limit))
            elif limit > 0:
                new_limit_best.append(limit)
                new_window_best.append(window)
                if self.debug_enable:
                    self.log("Keep charge slot {} - target soc {} kWh slot {} start {} end {} limit {}".format(window_n, limit, window, start, end, limit))
            else:
                if self.debug_enable:
                    self.log("Clip off charge slot window {} limit {}".format(window_n, limit))
        return new_limit_best, new_window_best

    def find_spare_energy(self, predict_soc, predict_export, step, first_charge):
        """
        Find spare energy and set triggers
        """
        triggers = self.args.get("export_triggers", [])
        if not isinstance(triggers, list):
            return

        # Only run if we have export data
        if not predict_export:
            return

        # Check each trigger
        for trigger in triggers:
            total_energy = 0
            name = trigger.get("name", "trigger")
            minutes = trigger.get("minutes", 60.0)
            minutes = min(max(minutes, 0), first_charge)
            energy = trigger.get("energy", 1.0)
            try:
                energy = float(energy)
            except (ValueError, TypeError):
                energy = 0.0
                self.log("Warn: Bad energy value {} provided via trigger {}".format(energy, name))
                self.record_status("Error: Bad energy value {} provided via trigger {}".format(energy, name), had_errors=True)

            for minute in range(0, minutes, step):
                total_energy += predict_export[minute]
            sensor_name = "binary_sensor." + self.prefix + "_export_trigger_" + name
            if total_energy >= energy:
                state = "on"
            else:
                state = "off"
            self.log("Evaluate trigger {} results {} total_energy {}".format(trigger, state, dp2(total_energy)))
            self.dashboard_item(
                sensor_name,
                state=state,
                attributes={
                    "friendly_name": "Predbat export trigger " + name,
                    "required": energy,
                    "available": dp2(total_energy),
                    "minutes": minutes,
                    "icon": "mdi:clock-start",
                },
            )

    def prune_dead_plan_slots(self):
        """Model-based clipping: remove plan slots that do nothing in the central forecast.

        Each active charge/export slot inside the record window is trialled in turn: the slot is
        removed (export -> 100, charge -> 0) and the whole plan re-simulated in the nominal (50%)
        scenario only - one simulation per trial via run_prediction_metric(nominal_only=True). The
        removal is kept when the nominal metric does not get worse, so slots whose value exists only
        in the pv10/pv90 branches (or nowhere at all - phantom exports, dead freezes) are dropped.
        If the pessimistic scenario materialises in reality, the next plan recompute re-creates a
        genuine slot from actual state, so nothing is permanently lost.

        Runs after the pre-clip scoring snapshot (see calculate_plan), so plan selection still
        compares plans as optimised (#4403). Manual windows are preserved. Later removals are
        compared against the running baseline, so an earlier accepted removal cannot make a later
        one look free.

        A window covering the current minute is trialled like any other - it is the slot being
        executed right now, so a dead one left in place is precisely the spurious command that
        reaches the inverter. This does not reopen #4402: that regression came from writing back a
        window change scored on optimise_export's adjusted metric (commitment bonus and tie-break
        weightings), and the fix was to gate on the unadjusted whole-plan metric, which is what this
        trial uses. An in-progress export worth anything at all fails the gate and is kept.
        """
        eps = 0.02
        record_limit = self.end_record + self.minutes_now
        baseline = None
        start_metric = None
        pruned = 0
        trials = 0
        for typ, windows, limits, off_value in (("export", self.export_window_best, self.export_limits_best, 100.0), ("charge", self.charge_window_best, self.charge_limit_best, 0)):
            for window_n, window in enumerate(windows):
                limit = limits[window_n]
                active = (limit < 100.0) if typ == "export" else (limit > 0)
                if not active:
                    continue
                if window["end"] <= self.minutes_now or window["start"] >= record_limit:
                    continue
                if window["start"] in self.manual_all_times:
                    continue
                if baseline is None:
                    baseline = self.run_prediction_metric(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record, nominal_only=True)[0]
                    start_metric = baseline
                limits[window_n] = off_value
                trial = self.run_prediction_metric(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record, nominal_only=True)[0]
                trials += 1
                if trial <= baseline + eps:
                    if self.debug_enable:
                        self.log("Prune dead {} slot {} {}-{} limit {} - nominal metric {} vs baseline {}".format(typ, window_n, self.time_abs_str(window["start"]), self.time_abs_str(window["end"]), limit, dp2(trial), dp2(baseline)))
                    baseline = trial
                    pruned += 1
                else:
                    limits[window_n] = limit
        if pruned:
            # The trials run on the nominal scenario only, so report the full metric and cost of the
            # pruned plan rather than the nominal figure the trials compared on
            metric, battery_value, cost, metric_keep, battery_cycle, final_carbon_g, import_kwh, export_kwh = self.run_prediction_metric(
                self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
            )
            curr = self.currency_symbols[1]
            self.log(
                "Pruned {} dead plan slot(s) in {} trial(s), nominal metric {}{} -> {}{}, plan now metric {}{}, cost {}{}, cycle {}kWh, import {}kWh".format(
                    pruned, trials, dp2(start_metric), curr, dp2(baseline), curr, dp2(metric), curr, dp2(cost), curr, dp2(battery_cycle), dp2(import_kwh)
                )
            )
        return pruned

    def clip_charge_slots(self, minutes_now, predict_soc, charge_window_best, charge_limit_best, record_charge_windows, step):
        """
        Clip charge slots that are useless as they don't charge at all
        set the 'target' field in the charge window for HTML reporting

        Both clip-up branches raise the limit to the full battery so that adjacent windows share a limit and can
        be merged, which is only sound when the limit had no influence on the simulated charge. The achieved SoC
        can land just under the limit even when the limit never clamped it (e.g. charge loss/rounding), and can dip
        a hair below its own peak for the same reason, so both tests need a margin of one charge step to tell a real effect from rounding.
        """
        charge_step = self.battery_rate_max_charge * self.battery_rate_max_scaling * step
        for window_n in range(min(record_charge_windows, len(charge_window_best))):
            window = charge_window_best[window_n]
            limit = charge_limit_best[window_n]
            window_start = max(window["start"], minutes_now)
            window_end = max(window["end"], minutes_now)
            window_length = window_end - window_start
            window["target"] = limit

            if limit <= 0.0:
                # Ignore disabled windows
                pass
            elif window_length > 0:
                predict_minute_start = max(int((window_start - minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window_end - minutes_now) / 5) * 5
                predict_minute_end_m1 = max(predict_minute_end - 5, predict_minute_start)

                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    # Work out min/max soc
                    soc_min = self.soc_max
                    soc_max = 0
                    for minute in range(predict_minute_start, predict_minute_end + 5, 5):
                        if minute in predict_soc:
                            soc_min = min(soc_min, predict_soc[minute])
                            soc_max = max(soc_max, predict_soc[minute])

                    soc_m1 = predict_soc[predict_minute_end_m1]

                    if self.debug_enable:
                        self.log("Examine charge window {} from {} - {} (minute {}) limit {} - min soc {} max soc {} soc_m1 {}".format(window_n, window_start, window_end, predict_minute_start, limit, soc_min, soc_max, soc_m1))

                    # Removing a charge window that never charges is prune_dead_plan_slots' job - it asks the
                    # model whether the nominal plan changes without the slot, which subsumes the old
                    # never-reaches-limit and freeze-at-100% removal branches. What is left here narrows the
                    # limit to what the window can actually achieve, so adjacent windows share a limit and merge.
                    if soc_max < (limit - charge_step):
                        # Work out what can be achieved in the window and set the target to match that
                        window["target"] = soc_max
                        charge_limit_best[window_n] = self.soc_max
                        if self.debug_enable:
                            self.log("Clip up charge window {} from {} - {} from limit {} to new limit {} target set to {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n], window["target"]))
                    elif (soc_max > (soc_m1 + charge_step)) and soc_max == limit:
                        window["target"] = soc_max
                        charge_limit_best[window_n] = self.soc_max
                        if self.debug_enable:
                            self.log("Clip up charge window {} from {} - {} from limit {} to new limit {} target set to {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n], window["target"]))
                    elif limit == self.reserve and (dp1(soc_min) == dp1(self.soc_max)) and (dp1(soc_max) == dp1(self.soc_max)):
                        # Reserve slot, so set to 100% if we are already at 100%
                        window["target"] = soc_max
                        charge_limit_best[window_n] = self.soc_max
                        if self.debug_enable:
                            self.log(
                                "Change freeze charge into charge, already at 100% - window {} from {} - {} from limit {} to new limit {} target set to {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n], window["target"])
                            )

            else:
                self.log("Warn: Clip charge window {} as it's already passed".format(window_n))
                charge_limit_best[window_n] = 0
                window["target"] = 0
        return charge_window_best, charge_limit_best

    def clip_export_slots(self, minutes_now, predict_soc, export_window_best, export_limits_best, record_export_windows, step):
        """
        Clip export slots to the right length
        """
        for window_n in range(min(record_export_windows, len(export_window_best))):
            window = export_window_best[window_n]
            limit = export_limits_best[window_n]
            limit_soc = self.soc_max * limit / 100.0
            window_start = max(window["start"], minutes_now)
            window_end = max(window["end"], minutes_now)
            window_length = window_end - window_start
            window["target"] = limit

            if limit == 100:
                # Ignore disabled windows
                pass
            elif window_length > 0:
                predict_minute_start = max(int((window_start - minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window_end - minutes_now) / 5) * 5
                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    soc_min = self.soc_max
                    soc_max = 0
                    for minute in range(predict_minute_start, predict_minute_end + 5, 5):
                        if minute in predict_soc:
                            soc_min = min(soc_min, predict_soc[minute])
                            soc_max = max(soc_max, predict_soc[minute])

                    if self.debug_enable:
                        self.log("Examine export window {} from {} - {} (minute {}) limit {} - starting soc {} ending soc {}".format(window_n, window_start, window_end, predict_minute_start, limit, soc_min, soc_max))

                    # Export level adjustment: narrow the requested limit towards what the simulation says is
                    # actually achievable, so the target sent to the inverter matches the simulated plan.
                    #
                    # Removing a window that achieves nothing is prune_dead_plan_slots' job - it asks the model
                    # directly (does the nominal plan change without this slot?) instead of inferring it from the
                    # SoC trace, and subsumes the removal branches that used to live here: freeze-at-100%,
                    # no-SoC-above-reserve (#4171/#4434), phantom export (#4453/#4487) and target-unreachable.
                    # That includes the window covering the current minute, so a dead slot is never left
                    # commanding the inverter.
                    if limit != 99.0 and soc_min > limit_soc:
                        # Give it 10 minute margin
                        target_soc = max(limit_soc, soc_min)
                        limit_soc = max(limit_soc, soc_min - 10 * self.battery_rate_max_discharge * self.battery_rate_max_scaling_discharge)
                        window["target"] = calc_percent_limit(target_soc, self.soc_max)
                        export_limits_best[window_n] = calc_percent_limit(limit_soc, self.soc_max) + (limit - int(limit))
                        if limit != export_limits_best[window_n] and self.debug_enable:
                            self.log("Clip up export window {} from {} - {} from limit {} to new limit {} target set to {}".format(window_n, window_start, window_end, limit, export_limits_best[window_n], window["target"]))
            else:
                self.log("Warn: Clip export window {} as it's already passed".format(window_n))
                export_limits_best[window_n] = 100.0
        return export_window_best, export_limits_best

    def discard_unused_export_slots(self, export_limits_best, export_window_best):
        """
        Filter out the windows we disabled
        """
        new_best = []
        new_enable = []
        for window_n in range(len(export_limits_best)):
            if export_limits_best[window_n] < 100.0:
                # Also merge contiguous enabled windows
                if (
                    new_best
                    and (export_window_best[window_n]["start"] == new_best[-1]["end"])
                    and (export_limits_best[window_n] == new_enable[-1])
                    and (export_window_best[window_n]["start"] not in self.manual_all_times)
                    and (new_best[-1]["start"] not in self.manual_all_times)
                ):
                    new_best[-1]["end"] = export_window_best[window_n]["end"]
                    new_best[-1]["target"] = export_window_best[window_n].get("target", export_limits_best[window_n])
                    if self.debug_enable:
                        self.log("Combine export slot {} with previous - percent {} slot {}".format(window_n, new_enable[-1], new_best[-1]))
                else:
                    new_best.append(export_window_best[window_n].copy())
                    new_enable.append(export_limits_best[window_n])

        return new_enable, new_best

    def update_target_values(self):
        """
        Update target values for HTML plan
        """
        for window_n in range(len(self.export_limits_best)):
            self.export_window_best[window_n]["target"] = self.export_limits_best[window_n]
        for window_n in range(len(self.charge_limit_best)):
            self.charge_window_best[window_n]["target"] = self.charge_limit_best[window_n]

    def optimise_plan_pass(self, end_record, budget=0, debug_mode=False):
        """Re-optimise each charge and export window of the settled plan, in time order.

        This is the pass that runs after the levels and detailed passes have chosen the plan's shape.
        Each window is re-optimised against the whole plan and the change is kept only when it improves
        on the plan we were handed, so the pass is monotonic.

        budget caps how many windows are visited; 0 visits every window in the record. The cheap default
        exists because the near-term windows are the ones about to be executed, but the cap is what makes
        the fast path miss value further out - see calculate_second_pass, which runs unbudgeted.

        The metric is measured from the plan in hand rather than accepted from the caller, so it cannot
        be handed a stale one - passes ahead of this can mutate the plan without their return value being
        threaded through, and the swap passes that follow re-baseline for the same reason.

        Each export window's start is reset to start_orig before it is re-optimised. Without that reset a
        window trimmed by an earlier pass can only ever be trimmed further, so the pass cannot recover a
        window it narrowed on a plan that has since changed underneath it.
        """
        record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
        record_export_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.export_window_best), 1)
        selected = self.plan_metric_now(end_record)
        curr = self.currency_symbols[1]
        self.log("Plan pass optimisation started metric {}{}, cost {}{}, budget {}".format(dp2(selected[0]), curr, dp2(selected[1]), curr, budget if budget else "unlimited"))
        count = 0
        window_sorted, window_index = self.sort_window_by_time_combined(self.charge_window_best[:record_charge_windows], self.export_window_best[:record_export_windows])
        for key in window_sorted:
            typ = window_index[key]["type"]
            window_n = window_index[key]["id"]
            if typ == "c":
                # Don't optimise a charge window that hits an export window if this is disallowed
                if not self.allow_this_charge_window(window_n):
                    continue

                snapshot = self.plan_window_snapshot(typ, window_n)
                best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import, best_metric_plan = self.optimise_charge_limit(
                    window_n,
                    record_charge_windows,
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.export_window_best,
                    self.export_limits_best,
                    end_record=end_record,
                )
                self.charge_limit_best[window_n] = best_soc
                candidate = (best_metric_plan, best_cost, best_keep, best_cycle, best_carbon, best_import)
                selected = self.keep_window_change_if_improved(selected, candidate, typ, window_n, snapshot)
            elif typ == "d":
                if not self.allow_this_export_window(window_n):
                    continue

                snapshot = self.plan_window_snapshot(typ, window_n)
                set_window_start(self.export_window_best[window_n], self.export_window_best[window_n].get("start_orig", self.export_window_best[window_n]["start"]))
                best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import, best_metric_plan = self.optimise_export(
                    window_n,
                    record_export_windows,
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.export_window_best,
                    self.export_limits_best,
                    end_record=end_record,
                )
                self.export_limits_best[window_n] = best_soc
                self.export_window_best[window_n]["start_orig"] = self.export_window_best[window_n].get("start_orig", self.export_window_best[window_n]["start"])
                set_window_start(self.export_window_best[window_n], best_start)
                candidate = (best_metric_plan, best_cost, best_keep, best_cycle, best_carbon, best_import)
                selected = self.keep_window_change_if_improved(selected, candidate, typ, window_n, snapshot)
            if (count % 16) == 0 and self.debug_enable:
                log_metric, log_cost, log_keep, log_cycle, log_carbon, log_import = selected
                self.log("Plan pass type {} window {} metric {} metric_keep {} carbon {} import {} cost {}".format(typ, window_n, log_metric, dp2(log_keep), dp0(log_carbon), dp2(log_import), dp2(log_cost)))
            count += 1
            if budget and count >= budget:
                break

        best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import = selected
        self.log(
            "Plan pass optimisation finished metric {}{}, cost {}{}, metric_keep {}kWh, cycle {}kWh, carbon {}kg, import {}kWh, visited {} window(s)".format(
                dp2(best_metric), curr, dp2(best_cost), curr, dp2(best_keep), dp2(best_cycle), dp0(best_carbon), dp2(best_import), count
            )
        )
        self.plan_write_debug(debug_mode, "plan_pass.html", self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, end_record)
        return best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import

    def plan_write_debug(self, debug_mode, name, pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, end_record, test=False, prediction=None):
        """
        Write debug plan to file
        """
        if debug_mode:
            orig_charge_limit_best = self.charge_limit_best.copy()
            orig_charge_window_best = clone_windows(self.charge_window_best)
            self.charge_limit_best, self.charge_window_best = remove_intersecting_windows(self.charge_limit_best, self.charge_window_best, self.export_limits_best, self.export_window_best)

            (
                cost10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.export_window_best,
                self.export_limits_best,
                True,
                end_record=end_record,
                save="best10" if name else "yesterday10",
            )
            self.update_target_values()

            if name:
                html_data, json_data = self.publish_html_plan(pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, end_record, publish=False, prediction=prediction)
                open(name + "_10.html", "w").write(html_data)

            best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = self.run_prediction_metric(
                self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=end_record, save="best" if name else "yesterday"
            )

            self.update_target_values()
            html_data, json_data = self.publish_html_plan(pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, end_record, publish=False, prediction=prediction)

            if name:
                open(name, "w").write(html_data)
                print("Wrote plan to {} - metric {} cost {} battery_value {} keep {} import {} (self {})".format(name, best_metric, best_cost, best_battery_value, best_keep, best_import, best_import * self.metric_self_sufficiency))

            if test:
                best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = self.run_prediction_metric(
                    self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=end_record, save="test"
                )

            self.charge_window_best = orig_charge_window_best
            self.charge_limit_best = orig_charge_limit_best
            return html_data, json_data

    def optimise_solar(self, best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import, record_export_windows, debug_mode=False):
        """
        Export more solar optimisation.

        Enables freeze export (export limit 99) on every currently idle export window that has
        predicted PV generation, then simulates the resulting plan. The new plan is only kept if it
        does not increase the overall metric by more than export_more_solar_threshold, otherwise the
        original export limits are restored. This is an optimiser only pass - execution is unchanged
        as the resulting freeze export slots are handled by the normal freeze export logic.

        Returns the (possibly updated) metric tuple so the caller stays consistent with the plan.
        """
        curr = self.currency_symbols[1]

        # Freeze export slots only have an effect when export freeze is enabled
        if not self.calculate_best_export or not self.set_export_freeze or not self.export_window_best:
            return best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import

        pv_forecast_minute_step = self.prediction.pv_forecast_minute_step

        # Work out which calendar days are covered by the record export windows. Each day is
        # considered independently as exporting more solar may be worthwhile on one day but not another.
        days = []
        for window_n in range(record_export_windows):
            window_start = self.export_window_best[window_n]["start"]
            if window_start >= (self.minutes_now + self.end_record):
                continue
            day = window_start // (24 * 60)
            if day not in days:
                days.append(day)
        days.sort()

        for day in days:
            # Snapshot the current plan so we can revert this day if it costs too much. The window
            # list is snapshotted too as re-optimising force exports can move window start times.
            orig_export_limits_best = self.export_limits_best.copy()
            orig_export_window_best = clone_windows(self.export_window_best)

            day_start = day * (24 * 60)
            day_end = day_start + (24 * 60)

            # Find the first minute of solar generation on this day (cannot act in the past)
            first_solar_minute = None
            for minute_absolute in range(max(day_start, self.minutes_now), day_end, PREDICT_STEP):
                if pv_forecast_minute_step.get(minute_absolute - self.minutes_now, 0) > 0:
                    first_solar_minute = minute_absolute
                    break

            # No solar on this day means there is nothing extra to export
            if first_solar_minute is None:
                continue

            added = 0
            for window_n in range(record_export_windows):
                window_start = self.export_window_best[window_n]["start"]
                window_end = self.export_window_best[window_n]["end"]

                # Only consider idle windows on this calendar day within the record period
                if window_start // (24 * 60) != day:
                    continue
                if window_start >= (self.minutes_now + self.end_record):
                    continue
                if window_start in self.manual_all_times:
                    continue

                # An existing freeze export slot may have been trimmed earlier (start moved later) -
                # restore it to its original full size so it covers the whole solar period
                if self.export_limits_best[window_n] == 99.0:
                    start_orig = self.export_window_best[window_n].get("start_orig", window_start)
                    if start_orig < window_start:
                        set_window_start(self.export_window_best[window_n], start_orig)
                    continue

                # Only enable currently idle (disabled) export windows
                if self.export_limits_best[window_n] != 100.0:
                    continue

                # Don't freeze export where a charge is already planned - we can't charge the battery
                # and freeze export (which disables charging) at the same time
                hit_charge = self.hit_charge_window(self.charge_window_best, window_start, window_end)
                if hit_charge >= 0 and self.charge_limit_best[hit_charge] != 0:
                    continue

                # Don't freeze export over a car charging slot - freeze export disables charging so the
                # battery can't be topped up for the car, unless the car is allowed to charge from the battery
                if not self.car_charging_from_battery and self.hit_car_window(window_start, window_end):
                    continue

                # Only enable windows where we expect to generate any solar to export
                pv_period = 0
                for minute in range(window_start - self.minutes_now, window_end - self.minutes_now, PREDICT_STEP):
                    pv_period += pv_forecast_minute_step.get(minute, 0)
                if pv_period < 0.01:
                    continue

                self.export_limits_best[window_n] = 99.0
                added += 1

            if not added:
                continue

            # Now we are exporting more solar, re-optimise any force export slots later in the day as
            # they may need their export level reducing given the battery now holds less stored solar.
            re_optimised = 0
            for window_n in range(record_export_windows):
                window_start = self.export_window_best[window_n]["start"]

                # Only force export slots (limit below the freeze sentinel) on this day, after first solar
                if window_start // (24 * 60) != day:
                    continue
                if window_start >= (self.minutes_now + self.end_record):
                    continue
                if window_start in self.manual_all_times:
                    continue
                if self.export_limits_best[window_n] >= 99.0:
                    continue
                if window_start <= first_solar_minute:
                    continue
                if not self.allow_this_export_window(window_n):
                    continue

                new_soc, new_start, new_metric, new_cost, new_soc_min, new_soc_min_minute, new_keep, new_cycle, new_carbon, new_import, new_metric_plan = self.optimise_export(
                    window_n, record_export_windows, self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
                )
                self.export_limits_best[window_n] = new_soc
                self.export_window_best[window_n]["start_orig"] = self.export_window_best[window_n].get("start_orig", self.export_window_best[window_n]["start"])
                set_window_start(self.export_window_best[window_n], new_start)
                re_optimised += 1

            # Simulate the final plan for this day on top of any days already kept and decide on the
            # final metric after the force exports have been re-optimised.
            new_metric, new_battery_value, new_cost, new_keep, new_cycle, new_carbon, new_import, new_export = self.run_prediction_metric(
                self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
            )
            self.plan_write_debug(debug_mode, "plan_more_solar_{}.html".format(day), self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)

            if (new_metric - best_metric) <= self.export_more_solar_threshold:
                self.log(
                    "Export more solar day {} enabled freeze export on {} idle solar window(s), re-optimised {} force export(s), metric {}{} (was {}{}, threshold {}{}) - keeping".format(
                        day, added, re_optimised, dp2(new_metric), curr, dp2(best_metric), curr, self.export_more_solar_threshold, curr
                    )
                )
                best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import = new_metric, new_cost, new_keep, new_cycle, new_carbon, new_import
            else:
                self.log("Export more solar day {} rejected - metric {}{} exceeds {}{} by more than threshold {}{}, reverting".format(day, dp2(new_metric), curr, dp2(best_metric), curr, self.export_more_solar_threshold, curr))
                self.export_limits_best = orig_export_limits_best
                self.export_window_best = orig_export_window_best

        return best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import

    def optimise_swap_export(self, record_charge_windows, record_export_windows, drop=False, debug_mode=False):
        """
        Swap optimisation tries to move export windows later
        """
        swapped_target = {}
        curr = self.currency_symbols[1]
        first = True

        if self.calculate_best_export and record_export_windows >= 2:
            swapped = True
            while swapped:
                selected_metric, selected_battery_value, selected_cost, selected_keep, selected_cycle, selected_carbon, selected_import, select_export = self.run_prediction_metric(
                    self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
                )
                if first:
                    self.log(
                        "Swap export optimisation started metric {}{}, cost {}{}, battery_value {}kWh, min_improvement_swap {}{}".format(
                            dp2(selected_metric), curr, dp2(selected_cost), curr, dp2(selected_battery_value), self.metric_min_improvement_swap, curr
                        )
                    )
                first = False
                swapped = False

                for window_n_target in range(record_export_windows - 1, 0, -1):
                    previous_end_target = 0
                    if window_n_target > 0:
                        previous_end_target = self.export_window_best[window_n_target - 1]["end"]
                    window_start_target = self.export_window_best[window_n_target]["start"]
                    orig_start_target = self.export_window_best[window_n_target].get("start_orig", window_start_target)
                    window_length_target = self.export_window_best[window_n_target]["end"] - window_start_target
                    orig_length_target = self.export_window_best[window_n_target]["end"] - orig_start_target
                    export_limit_target = self.export_limits_best[window_n_target]
                    target_day = self.export_window_best[window_n_target]["start"] // 1440

                    if window_start_target in self.manual_all_times:
                        continue
                    if swapped_target.get(window_n_target, False):
                        # Skip if we already swapped this window
                        continue

                    # Can not swap into car charging slot
                    if not self.car_charging_from_battery and self.hit_car_window(window_start_target, self.export_window_best[window_n_target]["end"]):
                        continue

                    # Try to drop the target
                    if drop and export_limit_target < 100:
                        self.export_limits_best[window_n_target] = 100.0
                        best_metric_drop, best_battery_value_drop, best_cost_drop, best_keep_drop, best_cycle_drop, best_carbon_drop, best_import_drop, best_export_drop = self.run_prediction_metric(
                            self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
                        )
                        if best_metric_drop <= selected_metric:
                            if self.debug_enable:
                                self.log(
                                    "Drop export window {}, limit {} {}-{}, metric {}{}, cost {}{}, keep {}kWh, cycle {}kWh, carbon {}kg, import {}kWh".format(
                                        window_n_target,
                                        export_limit_target,
                                        self.time_abs_str(self.export_window_best[window_n_target]["start"]),
                                        self.time_abs_str(self.export_window_best[window_n_target]["end"]),
                                        best_metric_drop,
                                        curr,
                                        dp2(best_cost_drop),
                                        curr,
                                        dp2(best_keep_drop),
                                        dp2(best_cycle_drop),
                                        dp0(best_carbon_drop),
                                        dp2(best_import_drop),
                                    )
                                )
                            selected_metric = best_metric_drop
                            selected_battery_value = best_battery_value_drop
                            selected_cost = best_cost_drop
                            selected_keep = best_keep_drop
                            selected_cycle = best_cycle_drop
                            selected_carbon = best_carbon_drop
                            selected_import = best_import_drop
                            swapped = True
                            export_limit_target = 100.0
                        else:
                            self.export_limits_best[window_n_target] = export_limit_target

                    if self.debug_enable:
                        self.log(
                            "Try target window {} {}-{} limit {} length {} orig_length {}".format(
                                window_n_target,
                                self.time_abs_str(self.export_window_best[window_n_target]["start"]),
                                self.time_abs_str(self.export_window_best[window_n_target]["end"]),
                                export_limit_target,
                                window_length_target,
                                orig_length_target,
                            )
                        )
                    # Try to swap into the target slot
                    for window_n in range(max(window_n_target - 32, 0), max(window_n_target, 0), 1):
                        previous_end = 0
                        if window_n > 0:
                            previous_end = self.export_window_best[window_n - 1]["end"]
                        window_start = self.export_window_best[window_n]["start"]
                        window_start_orig = self.export_window_best[window_n].get("start_orig", window_start)
                        window_start_from_now = max(window_start, self.minutes_now)
                        window_length = self.export_window_best[window_n]["end"] - window_start_from_now
                        window_length_orig = self.export_window_best[window_n]["end"] - window_start_orig
                        export_limit = self.export_limits_best[window_n]
                        window_day = self.export_window_best[window_n]["start"] // 1440

                        if window_start in self.manual_all_times:
                            continue

                        if target_day != window_day:
                            # Don't swap windows on different days
                            continue

                        if export_limit == export_limit_target and window_length == window_length_target:
                            # Don't swap if the windows are the same
                            continue

                        if export_limit < 99 and window_length <= orig_length_target:
                            # Don't optimise a charge window that hits an export window if this is disallowed
                            if not self.allow_this_export_window(window_n_target):
                                continue

                            is_combined = False
                            if export_limit_target < 99 and (window_length_target + window_length) <= orig_length_target:
                                # Full combine
                                self.export_limits_best[window_n] = 100
                                set_window_start(self.export_window_best[window_n], window_start_orig)
                                self.export_limits_best[window_n_target] = export_limit
                                set_window_start(self.export_window_best[window_n_target], self.export_window_best[window_n_target]["end"] - (window_length + window_length_target))
                                is_combined = True
                            elif export_limit_target < 99 and window_length_target < orig_length_target:
                                # Partial combine
                                amount_to_move = min(orig_length_target - window_length_target, window_length)
                                window_length_target_new = amount_to_move + window_length_target
                                window_length_new = amount_to_move + window_length
                                self.export_limits_best[window_n] = min(export_limit, export_limit_target)
                                set_window_start(self.export_window_best[window_n], self.export_window_best[window_n]["end"] - window_length_new)
                                set_window_start(self.export_window_best[window_n_target], self.export_window_best[window_n_target]["end"] - window_length_target_new)
                                self.export_limits_best[window_n_target] = min(export_limit, export_limit_target)
                                is_combined = True
                            else:
                                # Swap
                                if export_limit_target < 100 and window_length < window_length_target:
                                    # Don't swap if we move a smaller window later
                                    continue

                                # Set the current window to off and optimise the swap window
                                self.export_limits_best[window_n] = export_limit_target
                                set_window_start(self.export_window_best[window_n], max(self.export_window_best[window_n]["end"] - window_length_target, previous_end))
                                self.export_limits_best[window_n_target] = export_limit
                                set_window_start(self.export_window_best[window_n_target], max(self.export_window_best[window_n_target]["end"] - window_length, previous_end_target))

                            best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = self.run_prediction_metric(
                                self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
                            )

                            if self.debug_enable:
                                self.log(
                                    "Try to swap export combine {} window {} {}-{} previous end {} window_length {} limit {} with {} => {}-{} previous_end_target {} length_target {} limit_target {} metric {} (current best {}) cost {} keep {} cycle {} carbon {} import {}".format(
                                        is_combined,
                                        window_n,
                                        self.time_abs_str(self.export_window_best[window_n]["start"]),
                                        self.time_abs_str(self.export_window_best[window_n]["end"]),
                                        self.time_abs_str(previous_end),
                                        window_length,
                                        export_limit,
                                        window_n_target,
                                        self.time_abs_str(self.export_window_best[window_n_target]["start"]),
                                        self.time_abs_str(self.export_window_best[window_n_target]["end"]),
                                        self.time_abs_str(previous_end_target),
                                        window_length_target,
                                        export_limit_target,
                                        best_metric,
                                        dp2(selected_metric),
                                        dp2(best_cost),
                                        dp2(best_keep),
                                        dp2(best_cycle),
                                        dp0(best_carbon),
                                        dp2(best_import),
                                    )
                                )

                            if ((selected_metric - best_metric) >= self.metric_min_improvement_swap) and (best_metric <= selected_metric or ((export_limit_target == 100.0 or is_combined))):
                                if self.debug_enable:
                                    self.log(
                                        "Swap export window {} {}-{} limit {} with {} => {}-{} metric {}{}, selected_metric {}{}, min_improvement_swap {}, cost {}{}, keep {}kWh, cycle {}kWh, carbon {}kg, import {}kWh".format(
                                            window_n,
                                            self.time_abs_str(self.export_window_best[window_n]["start"]),
                                            self.time_abs_str(self.export_window_best[window_n]["end"]),
                                            export_limit,
                                            window_n_target,
                                            self.time_abs_str(self.export_window_best[window_n_target]["start"]),
                                            self.time_abs_str(self.export_window_best[window_n_target]["end"]),
                                            best_metric,
                                            curr,
                                            selected_metric,
                                            curr,
                                            self.metric_min_improvement_swap,
                                            dp2(best_cost),
                                            curr,
                                            dp2(best_keep),
                                            dp2(best_cycle),
                                            dp0(best_carbon),
                                            dp2(best_import),
                                        )
                                    )

                                # Update best
                                selected_metric = best_metric
                                selected_battery_value = best_battery_value
                                selected_cost = best_cost
                                selected_keep = best_keep
                                selected_cycle = best_cycle
                                selected_carbon = best_carbon
                                selected_import = best_import
                                swapped = True
                                swapped_target[window_n_target] = True
                                break
                            else:
                                # Revert the change
                                self.export_limits_best[window_n] = export_limit
                                set_window_start(self.export_window_best[window_n], window_start)
                                self.export_limits_best[window_n_target] = export_limit_target
                                set_window_start(self.export_window_best[window_n_target], window_start_target)

            self.log(
                "Swap export optimisation finished metric {}{}, cost {}{}, metric_keep {}kWh, cycle {}kWh, carbon {}kg, import {}kWh".format(
                    dp2(selected_metric), curr, dp2(selected_cost), curr, dp2(selected_keep), dp2(selected_cycle), dp0(selected_carbon), dp2(selected_import)
                )
            )

    def optimise_swap_charge(self, record_charge_windows, debug_mode=False):
        """
        Swap optimisation for charge windows.

        Single-window coordinate descent (optimise_charge_limit) cannot perform a move that
        turns one charge window off and another on when neither change improves the metric on
        its own - it only ever sees one window changing against a fixed background. This pass
        evaluates such pairwise moves directly: it exchanges the charge limits of two charge
        windows on the same day and keeps the swap only if it strictly lowers the overall
        metric. This lets the plan escape a local optimum where the same charge is better
        placed in a different (equal or similar priced) window - for example charging later,
        closer to when the energy is needed, or in a slot that interacts better with solar and
        export windows.

        Only strictly improving swaps are kept so the pass converges monotonically and can
        never make the plan worse.
        """
        curr = self.currency_symbols[1]

        if not (self.calculate_best_charge and record_charge_windows >= 2):
            return

        # Require a real improvement to keep a swap. The export swap's metric_min_improvement_swap
        # defaults negative (it deliberately breaks ties towards later windows) which for a
        # symmetric charge swap would let the metric increase and cause churn, so use a small
        # positive gate here: a swap is only kept if it lowers the metric by at least this amount.
        min_improvement_swap = 0.1

        swapped_target = {}
        swapped = True
        first = True
        while swapped:
            selected_metric, selected_battery_value, selected_cost, selected_keep, selected_cycle, selected_carbon, selected_import, selected_export = self.run_prediction_metric(
                self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
            )
            if first:
                self.log("Swap charge optimisation started metric {}{}, cost {}{}, min_improvement_swap {}{}".format(dp2(selected_metric), curr, dp2(selected_cost), curr, min_improvement_swap, curr))
            swapped = False
            first = False

            for window_n_target in range(record_charge_windows - 1, 0, -1):
                window_start_target = self.charge_window_best[window_n_target]["start"]
                charge_limit_target = self.charge_limit_best[window_n_target]
                target_day = window_start_target // 1440

                if window_start_target in self.manual_all_times:
                    continue
                if swapped_target.get(window_n_target, False):
                    # Only swap each target once per pass to avoid churn
                    continue
                if not self.allow_this_charge_window(window_n_target):
                    continue

                # Try swapping the charge limit with an earlier window on the same day
                for window_n in range(max(window_n_target - 32, 0), window_n_target, 1):
                    window_start = self.charge_window_best[window_n]["start"]
                    charge_limit = self.charge_limit_best[window_n]
                    window_day = window_start // 1440

                    if window_start in self.manual_all_times:
                        continue
                    if target_day != window_day:
                        # Don't swap windows on different days
                        continue
                    if charge_limit == charge_limit_target:
                        # Nothing to move between these two windows
                        continue
                    if not self.allow_this_charge_window(window_n):
                        continue

                    # Perform the swap and evaluate the resulting plan
                    self.charge_limit_best[window_n] = charge_limit_target
                    self.charge_limit_best[window_n_target] = charge_limit

                    best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = self.run_prediction_metric(
                        self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
                    )

                    if self.debug_enable:
                        self.log(
                            "Try to swap charge window {} limit {} with window {} limit {} => metric {} (current best {}) cost {}".format(window_n, charge_limit, window_n_target, charge_limit_target, dp2(best_metric), dp2(selected_metric), dp2(best_cost))
                        )

                    if best_metric < (selected_metric - min_improvement_swap):
                        if self.debug_enable:
                            self.log(
                                "Swap charge window {} limit {} with window {} limit {} metric {}{} (was {}{}), cost {}{}".format(
                                    window_n, charge_limit, window_n_target, charge_limit_target, dp2(best_metric), curr, dp2(selected_metric), curr, dp2(best_cost), curr
                                )
                            )
                        selected_metric = best_metric
                        selected_cost = best_cost
                        swapped = True
                        swapped_target[window_n_target] = True
                        break
                    else:
                        # Revert the change
                        self.charge_limit_best[window_n] = charge_limit
                        self.charge_limit_best[window_n_target] = charge_limit_target

            self.log("Swap charge optimisation finished metric {}{}, cost {}{}".format(dp2(selected_metric), curr, dp2(selected_cost), curr))

    def allow_this_charge_window(self, charge_window_n):
        """
        Allowed to optimise this charge window?
        """
        window_start = self.charge_window_best[charge_window_n]["start"]
        if self.calculate_best_charge and (window_start not in self.manual_all_times):
            if not self.calculate_export_oncharge:
                hit_export = self.hit_charge_window(self.export_window_best, self.charge_window_best[charge_window_n]["start"], self.charge_window_best[charge_window_n]["end"])
                if hit_export >= 0 and self.export_limits_best[hit_export] < 100:
                    return False
            return True
        return False

    def allow_this_export_window(self, export_window_n):
        """
        Allowed to optimise this export window?
        """
        window_start = self.export_window_best[export_window_n]["start"]
        if self.calculate_best_export and (window_start not in self.manual_all_times):
            if not self.calculate_export_oncharge:
                hit_charge = self.hit_charge_window(self.charge_window_best, self.export_window_best[export_window_n]["start"], self.export_window_best[export_window_n]["end"])
                if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                    # Check exact alignment of start and end times, as we allow export to replace entire charge window
                    if not ((self.export_window_best[export_window_n]["start"] <= self.charge_window_best[hit_charge]["start"]) and (self.export_window_best[export_window_n]["end"] >= self.charge_window_best[hit_charge]["end"])):
                        return False
            if not self.car_charging_from_battery and self.hit_car_window(self.export_window_best[export_window_n]["start"], self.export_window_best[export_window_n]["end"]):
                return False
            if not self.iboost_on_export and self.iboost_enable and self.iboost_plan and (self.hit_charge_window(self.iboost_plan, self.export_window_best[export_window_n]["start"], self.export_window_best[export_window_n]["end"]) >= 0):
                return False
            return True
        return False

    def optimise_detailed_pass(
        self,
        best_price_charge,
        best_price_export,
        best_price_charge_level,
        best_price_export_level,
        best_metric,
        best_cost,
        best_keep,
        best_soc_min,
        best_cycle,
        best_carbon,
        best_import,
        best_battery_value,
        record_charge_windows,
        record_export_windows,
        debug_mode=False,
    ):
        """
        Detailed optimisation of the charge and export windows
        """
        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(
            self.charge_window_best[:record_charge_windows], self.export_window_best[:record_export_windows], calculate_import_low_export=self.calculate_import_low_export, calculate_export_high_import=self.calculate_export_high_import
        )

        # Work out the lowest rate we charge at from the first pass
        lowest_price_charge = best_price_charge
        for price_key in price_set:
            links = price_links[price_key]
            for key in links:
                typ = window_index[key]["type"]
                window_n = window_index[key]["id"]
                if typ == "c" and (self.charge_limit_best[window_n] > self.reserve):
                    lowest_price_charge = min(self.charge_window_best[window_n]["average"], lowest_price_charge)

        # Optimise individual windows in the price band for charge/export
        # First optimise those at or below threshold highest to lowest (to turn down values)
        # then optimise those above the threshold lowest to highest (to turn up values)
        # Do the opposite for export.
        best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = self.run_prediction_metric(
            self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=self.end_record
        )
        curr = self.currency_symbols[1]
        self.log(
            "Starting detailed optimisation end_record {}, best_price_charge {}{}, best_price_export {}{}. lowest_price_charge {}{} with charge limits {}kWh and export limits {}kWh".format(
                self.time_abs_str(self.end_record + self.minutes_now), best_price_charge, curr, best_price_export, curr, lowest_price_charge, curr, self.charge_limit_best, self.export_limits_best
            )
        )

        # Run the fixed schedule once, then repeat just the trim/normal refinement pair until it
        # makes no change, capped at a few iterations. The one-shot "freeze" and "low" sub-passes are
        # structural and do not need repeating, but a normal/trim change can open up a further
        # improvement in an earlier window that a single sweep never revisits - repeating the pair
        # lets those cascade. Acceptance only ever keeps equal-or-better limits so the metric stays
        # monotonic non-increasing and the extra iterations cannot make the plan worse.
        base_sequence = ["trim_export", "trim_import", "freeze", "normal", "trim_export", "trim_import", "low"]
        refine_sequence = ["trim_export", "trim_import", "normal"]
        max_refine_iterations = 3
        base_len = len(base_sequence)
        refine_len = len(refine_sequence)
        changed_this_iteration = False

        for idx, pass_type in enumerate(base_sequence + refine_sequence * max_refine_iterations):
            # Stop early once a refinement pair changes nothing. Each pass derives its own iteration order
            # from price_set (see ordered_price_set below) rather than mutating it, so no restore is needed.
            if idx >= base_len and ((idx - base_len) % refine_len) == 0:
                if idx > base_len and not changed_this_iteration:
                    break
                changed_this_iteration = False

            start_at_low = False
            if pass_type in ["low", "trim_export"]:
                # Export trim sheds the least valuable (cheapest) export first, so walk price bands low to
                # high - the high-priced peak is only reduced if the cheaper slots cannot absorb the excess.
                ordered_price_set = list(reversed(price_set))
                start_at_low = True
            else:
                ordered_price_set = list(price_set)

            for price_key in ordered_price_set:
                links = price_links[price_key].copy()

                # Freeze/Trim pass should be done in time order (newest first)
                if pass_type in ["freeze", "trim_export", "trim_import"]:
                    links.reverse()

                printed_set = False

                for key in links:
                    typ = window_index[key]["type"]
                    window_n = window_index[key]["id"]

                    if typ in ["c", "cf"]:
                        # Store price set with window
                        self.charge_window_best[window_n]["set"] = price_key
                        window_start = self.charge_window_best[window_n]["start"]
                        price = self.charge_window_best[window_n]["average"]

                        # Freeze pass is just export freeze; the export trim pass does not touch charge
                        if pass_type in ["freeze", "trim_export"]:
                            continue

                        # Don't trim a window that is already off
                        if pass_type in ["trim_import"] and (self.charge_limit_best[window_n] == 0):
                            continue

                        # In normal don't do trimming of charge
                        if pass_type in ["normal"] and (self.charge_limit_best[window_n] == 100):
                            continue

                        # Don't allow charging if the price is above the threshold and not already selected during levelling
                        if (price_key > best_price_charge_level) and (self.charge_limit_best[window_n] == 0) and pass_type == "normal":
                            if self.debug_enable:
                                self.log("Skip high window {}, best limit {}, price_set {}, price {}{}, level {}{}".format(window_n, self.charge_limit_best[window_n], price_key, price, curr, best_price_charge_level, curr))
                            continue

                        if self.calculate_best_charge and (window_start not in self.manual_all_times):
                            if not printed_set and self.debug_enable:
                                self.log(
                                    "Optimise price set {}{}, pass {}, price {}{}, start_at_low {}, best_price_charge {}{}, best_metric {}{}, best_cost {}{}, best_cycle {}kWh, best_carbon {}kg, best_import {}kWh".format(
                                        price_key,
                                        curr,
                                        pass_type,
                                        price,
                                        curr,
                                        start_at_low,
                                        best_price_charge,
                                        curr,
                                        dp2(best_metric),
                                        curr,
                                        dp2(best_cost),
                                        curr,
                                        dp2(best_cycle),
                                        dp0(best_carbon),
                                        dp2(best_import),
                                    )
                                )
                                printed_set = True
                            average = self.charge_window_best[window_n]["average"]

                            # Don't optimise a charge window that hits an export window if this is disallowed
                            if not self.allow_this_charge_window(window_n):
                                continue

                            if (price_key > best_price_charge_level) and (self.charge_limit_best[window_n] == 0) and not pass_type == "low":
                                continue

                            n_best_soc, n_best_metric, n_best_cost, n_soc_min, n_soc_min_minute, n_best_keep, n_best_cycle, n_best_carbon, n_best_import, n_best_metric_plan = self.optimise_charge_limit(
                                window_n,
                                record_charge_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.export_window_best,
                                self.export_limits_best,
                                end_record=self.end_record,
                                freeze_only=(typ == "cf"),
                                allow_freeze=True,
                            )
                            # The import trim pass may only reduce charge (charge to a lower SoC), never add it
                            trim_import_ok = pass_type != "trim_import" or n_best_soc < self.charge_limit_best[window_n]
                            if n_best_metric < best_metric and n_best_soc != self.charge_limit_best[window_n] and trim_import_ok:
                                # Only a strict improvement drives another full iteration. Equal-metric
                                # limit flips are still applied once (as before) but must not keep the
                                # iteration alive - re-running to chase them over-optimises the
                                # end_record proxy and can diverge from the post-clipping final metric.
                                changed_this_iteration = True
                                best_metric = n_best_metric
                                best_cost = n_best_cost
                                best_soc = n_best_soc
                                best_keep = n_best_keep
                                best_cycle = n_best_cycle
                                best_carbon = n_best_carbon
                                best_import = n_best_import
                                best_soc_min = n_soc_min
                                best_soc_min_minute = n_soc_min_minute
                                self.charge_limit_best[window_n] = best_soc
                                self.plan_write_debug(debug_mode, "plan_{}_charge_{}.html".format(pass_type, window_n), self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)

                                if self.debug_enable:
                                    self.log(
                                        "Best charge limit pass {}, window {}, time {} - {}, cost {}{}, charge_limit {}kWh, (adjusted) min {} @ {} (min {} max {}) with metric {}{}, cost {}{}, cycle {}kWh, carbon {}kg, import {}kWh, windows {}".format(
                                            pass_type,
                                            window_n,
                                            self.time_abs_str(self.charge_window_best[window_n]["start"]),
                                            self.time_abs_str(self.charge_window_best[window_n]["end"]),
                                            average,
                                            curr,
                                            dp2(best_soc),
                                            dp2(best_soc_min),
                                            self.time_abs_str(best_soc_min_minute),
                                            self.best_soc_min,
                                            self.best_soc_max,
                                            dp2(best_metric),
                                            curr,
                                            dp2(best_cost),
                                            curr,
                                            dp2(best_cycle),
                                            dp0(best_carbon),
                                            dp2(best_import),
                                            calc_percent_limit(self.charge_limit_best, self.soc_max),
                                        )
                                    )
                    if typ in ["d", "df"]:
                        # Store price set with window
                        self.export_window_best[window_n]["set"] = price_key
                        window_start = self.export_window_best[window_n]["start"]
                        price = self.export_window_best[window_n]["average"]

                        # The import trim pass does not touch export windows
                        if pass_type in ["trim_import"]:
                            continue

                        # Ignore freeze pass if export freeze disabled
                        if not self.set_export_freeze and pass_type == "freeze":
                            continue

                        # Don't remove exports during freeze pass
                        if pass_type == "freeze" and self.export_limits_best[window_n] == 0:
                            continue

                        # Don't trim a window that is already off
                        if pass_type in ["trim_export"] and (self.export_limits_best[window_n] == 100):
                            continue

                        # In normal don't do trimming of export
                        if pass_type in ["normal"] and (self.export_limits_best[window_n] == 0):
                            continue

                        # Do highest price first
                        # Second pass to tune down any excess exports only
                        if pass_type == "low" and (self.export_limits_best[window_n] == 100):
                            continue

                        # Don't trim freeze, that can be done in the freeze pass
                        if pass_type == "trim_export" and self.export_limits_best[window_n] == 99:
                            continue

                        # Ignore prices below the threshold if not already selected during levelling
                        if (price_key < best_price_export_level) and (self.export_limits_best[window_n] == 100):
                            if self.debug_enable:
                                self.log("Skip low window {} best limit {} price_set {} price {} level {}".format(window_n, self.export_limits_best[window_n], price_key, price, best_price_export_level))
                            continue

                        if self.allow_this_export_window(window_n):
                            if not printed_set and self.debug_enable:
                                self.log(
                                    "Optimise price set {}{}, pass {}, price {}{}, start_at_low {}, best_price_export {}{}, level {}{}, best_metric {}{}, best_cost {}{}, best_cycle {}kWh, best_carbon {}kg, best_import {}kWh".format(
                                        price_key,
                                        curr,
                                        pass_type,
                                        price,
                                        curr,
                                        start_at_low,
                                        best_price_export,
                                        curr,
                                        best_price_export_level,
                                        curr,
                                        dp2(best_metric),
                                        curr,
                                        dp2(best_cost),
                                        curr,
                                        dp2(best_cycle),
                                        dp0(best_carbon),
                                        dp2(best_import),
                                    )
                                )
                                printed_set = True

                            if self.debug_enable:
                                self.log(
                                    "Optimise export window {} end_record {} best_price_charge {} best_price_export {} lowest_price_charge {} with charge limits {} export limits {}".format(
                                        window_n, self.time_abs_str(self.end_record + self.minutes_now), best_price_charge, best_price_export, lowest_price_charge, self.charge_limit_best, self.export_limits_best
                                    )
                                )
                            # Try to optimise the export window
                            keep_start = self.export_window_best[window_n]["start"]
                            set_window_start(self.export_window_best[window_n], self.export_window_best[window_n].get("start_orig", self.export_window_best[window_n]["start"]))
                            n_best_soc, n_best_start, n_best_metric, n_best_cost, n_soc_min, n_soc_min_minute, n_best_keep, n_best_cycle, n_best_carbon, n_best_import, n_best_metric_plan = self.optimise_export(
                                window_n,
                                record_export_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.export_window_best,
                                self.export_limits_best,
                                end_record=self.end_record,
                                freeze_only=(typ == "df") or pass_type == "freeze",
                                allow_freeze=True,
                            )
                            set_window_start(self.export_window_best[window_n], keep_start)
                            # The export trim pass may only reduce export, never add it, so the cheapest slots
                            # shed any levels over-export before the high-priced peak is touched. A reduction is
                            # a shallower discharge (higher SoC limit) and/or a smaller window (later start) -
                            # never a deeper discharge nor an earlier start (a bigger window exports more, even
                            # when the SoC limit rises). Off/freeze (limit >= 99) export no battery and force the
                            # start back to the full window, so they are exempt from the earlier-start check.
                            trim_export_ok = pass_type != "trim_export" or (n_best_soc >= self.export_limits_best[window_n] and (n_best_soc >= 99 or n_best_start >= keep_start))
                            if n_best_metric < best_metric and (n_best_soc != self.export_limits_best[window_n] or n_best_start != self.export_window_best[window_n]["start"]) and trim_export_ok:
                                # Only a strict improvement drives another refinement iteration (see
                                # the charge block above for why equal-metric flips must not).
                                changed_this_iteration = True
                                best_metric = n_best_metric
                                best_cost = n_best_cost
                                best_soc = n_best_soc
                                best_start = n_best_start
                                best_keep = n_best_keep
                                best_cycle = n_best_cycle
                                best_carbon = n_best_carbon
                                best_import = n_best_import
                                best_soc_min = n_soc_min
                                best_soc_min_minute = n_soc_min_minute
                                self.export_limits_best[window_n] = best_soc
                                self.export_window_best[window_n]["start_orig"] = self.export_window_best[window_n].get("start_orig", self.export_window_best[window_n]["start"])
                                set_window_start(self.export_window_best[window_n], best_start)

                                self.plan_write_debug(debug_mode, "plan_{}_export_{}.html".format(pass_type, window_n), self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)

                                if self.debug_enable:
                                    self.log(
                                        "Best export limit window {}, time {} - {}, cost {}{}. export_limit {}kWh, (adjusted) min {}kWh @ {} (min {}kWh) with metric {}{}, cost {}{}, cycle {}kWh, carbon {}kg, import {}kWh".format(
                                            window_n,
                                            self.time_abs_str(self.export_window_best[window_n]["start"]),
                                            self.time_abs_str(self.export_window_best[window_n]["end"]),
                                            price,
                                            curr,
                                            best_soc,
                                            dp2(best_soc_min),
                                            self.time_abs_str(best_soc_min_minute),
                                            self.best_soc_min,
                                            dp2(best_metric),
                                            curr,
                                            dp2(best_cost),
                                            curr,
                                            dp2(best_cycle),
                                            dp0(best_carbon),
                                            dp2(best_import),
                                        )
                                    )
            # Log set of charge and export windows - the full window list is long and repeats after
            # every pass, so it is debug only
            if self.calculate_best_charge and self.debug_enable:
                self.log(
                    "Best charge windows best_metric {}{}, best_cost {}{}, best_carbon {}kg, best_import {}kWh, metric_keep {}kWh, end_record {}, windows {}".format(
                        dp2(best_metric),
                        curr,
                        dp2(best_cost),
                        curr,
                        dp0(best_carbon),
                        dp2(best_import),
                        dp2(best_keep),
                        self.time_abs_str(self.end_record + self.minutes_now),
                        self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max), ignore_min=True),
                    )
                )

            if self.calculate_best_export and self.debug_enable:
                self.log(
                    "Best export windows best_metric {}{}, best_cost {}{}, best_carbon {}kg, best_import {}kWh, metric_keep {}kWh, end_record {}, windows {}".format(
                        dp2(best_metric),
                        curr,
                        dp2(best_cost),
                        curr,
                        dp0(best_carbon),
                        dp2(best_import),
                        dp2(best_keep),
                        self.time_abs_str(self.end_record + self.minutes_now),
                        self.window_as_text(self.export_window_best, self.export_limits_best, ignore_max=True),
                    )
                )

        # Update price levels for final plan
        best_price_charge, best_price_export, best_price_charge_level, best_price_export_level = self.find_price_levels(price_set, price_links, window_index, self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best)
        self.rate_best_cost_threshold_charge = best_price_charge
        self.rate_best_cost_threshold_export = best_price_export

        self.plan_write_debug(debug_mode, "plan_main_first.html", self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
        return best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, best_import, best_battery_value

    def optimise_levels_pass(self, best_metric, metric_keep, debug_mode=False):
        """
        Select the charge and export price levels and create the high level plan
        """
        curr = self.currency_symbols[1]
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_export_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.export_window_best), 1)

        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(
            self.charge_window_best[:record_charge_windows], self.export_window_best[:record_export_windows], calculate_import_low_export=self.calculate_import_low_export, calculate_export_high_import=self.calculate_export_high_import
        )

        best_soc = self.soc_max
        best_cost = best_metric
        best_keep = metric_keep
        best_cycle = 0
        best_carbon = 0
        best_import = 0
        best_soc_min = 0
        best_battery_value = 0
        fast_mode = self.get_arg("enable_fast_mode_levels", True)
        enable_coarse_fine = self.get_arg("enable_coarse_fine_levels", True)
        tried_list = {}  # Track tried combinations

        start_time = time.time()
        self.log("Optimise levels pass started at {}".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))))

        # Optimise all windows by picking a price threshold default
        if price_set and self.calculate_best_charge and self.charge_window_best:
            self.log("Optimise all windows, total charge {} export {}".format(record_charge_windows, record_export_windows))
            self.optimise_charge_windows_reset(reset_all=True)
            self.optimise_charge_windows_manual()
            (
                self.charge_limit_best,
                self.export_limits_best,
                best_metric,
                best_cost,
                best_keep,
                best_soc_min,
                best_cycle,
                best_carbon,
                best_import,
                best_battery_value,
                tried_list,
                levels_score,
                best_max_charge_slots,
                best_max_export_slots,
            ) = self.optimise_charge_limit_price_threads(
                price_set,
                price_links,
                window_index,
                record_charge_windows,
                record_export_windows,
                self.charge_limit_best,
                self.charge_window_best,
                self.export_window_best,
                self.export_limits_best,
                end_record=self.end_record,
                fast=fast_mode,
                quiet=False if debug_mode else True,
                enable_coarse_fine=enable_coarse_fine,
            )

            self.plan_write_debug(debug_mode, "plan_pre_levels.html", self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)

            if self.calculate_regions:
                region_size = int(16 * 60)
                min_region_size = int(120)
                while region_size >= min_region_size:
                    # step_size = int(max(region_size / 2, min_region_size))
                    step_size = region_size
                    fast_mode = not (region_size == min_region_size)
                    for region in range(0, self.end_record + self.minutes_now, step_size):
                        region_start = max(self.end_record + self.minutes_now - region - region_size, 0)
                        region_end = min(region_start + region_size, self.end_record + self.minutes_now)

                        if region_end < self.minutes_now:
                            continue

                        (
                            self.charge_limit_best,
                            self.export_limits_best,
                            best_metric,
                            best_cost,
                            best_keep,
                            best_soc_min,
                            best_cycle,
                            best_carbon,
                            best_import,
                            best_battery_value,
                            tried_list,
                            levels_score,
                            best_max_charge_slots,
                            best_max_export_slots,
                        ) = self.optimise_charge_limit_price_threads(
                            price_set,
                            price_links,
                            window_index,
                            record_charge_windows,
                            record_export_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.export_window_best,
                            self.export_limits_best,
                            end_record=self.end_record,
                            region_start=region_start,
                            region_end=region_end,
                            fast=fast_mode,
                            quiet=True,
                            best_metric=best_metric,
                            best_cost=best_cost,
                            best_keep=best_keep,
                            best_soc_min=best_soc_min,
                            best_cycle=best_cycle,
                            best_import=best_import,
                            best_carbon=best_carbon,
                            best_battery_value=best_battery_value,
                            tried_list=tried_list,
                            levels_score=levels_score,
                            enable_coarse_fine=enable_coarse_fine,
                            best_max_charge_slots=best_max_charge_slots,
                            best_max_export_slots=best_max_export_slots,
                        )
                        # Reached the end of the window
                        if self.end_record + self.minutes_now - region - region_size < 0:
                            break

                    self.log(">> Region optimisation pass width {} gives best_metric {}{}, best_cost {}{}, best_cycle {}kWh, best_import {}kWh".format(region_size, dp2(best_metric), curr, dp2(best_cost), curr, dp2(best_cycle), dp2(best_import)))
                    self.plan_write_debug(debug_mode, "plan_levels_{}.html".format(region_size), self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
                    region_size = int(region_size / 2)

        best_price_charge, best_price_export, best_price_charge_level, best_price_export_level = self.find_price_levels(price_set, price_links, window_index, self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best)
        self.rate_best_cost_threshold_charge = best_price_charge
        self.rate_best_cost_threshold_export = best_price_export

        # Set the new end record and blackout period based on the levelling
        self.end_record = self.record_length(self.charge_window_best, self.charge_limit_best, best_price_charge)

        self.log("Optimise levels pass ended at {}, duration {:.1f} seconds tried {} combinations".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())), time.time() - start_time, len(tried_list)))

        self.log(
            "Set best_price_charge_level {}{}, best_price_export_level {}{}, best_price_charge {}{}, best_cost_export {}{}, best_metric {}{}, best_keep {}kWh, best_cycle {}kWh, best_carbon {}kg, best_import {}kWh".format(
                dp2(best_price_charge_level), curr, dp2(best_price_export_level), curr, dp2(best_price_charge), curr, dp2(best_price_export), curr, dp2(best_metric), curr, dp2(best_keep), dp2(best_cycle), dp0(best_carbon), dp2(best_import)
            )
        )
        self.plan_write_debug(debug_mode, "plan_levels.html", self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
        return best_price_charge, best_price_export, best_price_charge_level, best_price_export_level, best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, best_import, best_battery_value

    def optimise_all_windows(self, best_metric, metric_keep, debug_mode=False):
        """
        Optimise all windows, both charge and export in rate order
        """

        # Create levels
        best_price_charge, best_price_export, best_price_charge_level, best_price_export_level, best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, best_import, best_battery_value = self.optimise_levels_pass(
            best_metric, metric_keep, debug_mode
        )

        # Clear out windows not inside record and apply manual overrides
        self.optimise_charge_windows_reset(reset_all=False)
        self.optimise_charge_windows_manual()
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_export_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.export_window_best), 1)

        # Swaps
        # self.optimise_swap_export(record_charge_windows, record_export_windows, debug_mode=debug_mode, drop=False)
        # self.plan_write_debug(debug_mode, "plan_swap_levels.html", self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)

        # Perform detailed optimisation
        best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, best_import, best_battery_value = self.optimise_detailed_pass(
            best_price_charge,
            best_price_export,
            best_price_charge_level,
            best_price_export_level,
            best_metric,
            best_cost,
            best_keep,
            best_soc_min,
            best_cycle,
            best_carbon,
            best_import,
            best_battery_value,
            record_charge_windows,
            record_export_windows,
            debug_mode=debug_mode,
        )
        # Re-optimise each window of the settled plan. calculate_second_pass lifts the window budget so
        # every window in the record is revisited rather than just the near-term ones (slower).
        best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import = self.optimise_plan_pass(self.end_record, budget=0 if self.calculate_second_pass else PLAN_PASS_WINDOW_BUDGET, debug_mode=debug_mode)

        # Export more solar - enable freeze export on idle solar windows if it doesn't cost too much
        if self.export_more_solar:
            best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import = self.optimise_solar(best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import, record_export_windows, debug_mode=debug_mode)

        # Swaps run once all other passes have settled. The export swap can only defer an export that
        # already exists when it runs, and the plan pass and solar pass both turn exports on - on the
        # budgeted plan pass the windows it reaches are the near-term ones, so the exports it adds are at
        # the front, exactly the ones the swap exists to push back. Running the swap before them left
        # those pinned in place (#4478). The charge swap follows for the mirror-image reason: a
        # strictly-improving pairwise charge move must not be subsequently undone by a non-monotonic pass.
        self.optimise_swap_export(record_charge_windows, record_export_windows, debug_mode=debug_mode)
        self.plan_write_debug(debug_mode, "plan_swap_final.html", self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
        self.optimise_swap_charge(record_charge_windows, debug_mode=debug_mode)

        self.plan_write_debug(debug_mode, "plan_raw.html", self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)

        # Return
        return best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import

    def optimise_charge_windows_manual(self):
        """
        Apply manual window overrides to the plan
        """
        if self.charge_window_best and self.calculate_best_charge:
            for window_n in range(len(self.charge_window_best)):
                if self.charge_window_best[window_n]["start"] in self.manual_demand_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_export_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_freeze_export_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_charge_times:
                    self.charge_limit_best[window_n] = self.soc_max
                elif self.charge_window_best[window_n]["start"] in self.manual_freeze_charge_times:
                    self.charge_limit_best[window_n] = self.reserve

        if self.export_window_best and self.calculate_best_export:
            for window_n in range(len(self.export_window_best)):
                if self.export_window_best[window_n]["start"] in self.manual_demand_times:
                    self.export_limits_best[window_n] = 100.0
                elif self.export_window_best[window_n]["start"] in self.manual_export_times:
                    self.export_limits_best[window_n] = 0.0
                elif self.export_window_best[window_n]["start"] in self.manual_freeze_export_times:
                    self.export_limits_best[window_n] = 99.0

    def optimise_charge_windows_reset(self, reset_all):
        """
        Reset the charge windows to min

        Parameters:
        - reset_all (bool): If True, reset all charge windows. If False, reset only the charge windows that are in the record window.

        Returns:
        None
        """
        if self.charge_window_best and self.calculate_best_charge:
            # Set all to max
            for window_n in range(len(self.charge_window_best)):
                if self.charge_window_best[window_n]["start"] < (self.minutes_now + self.end_record):
                    if reset_all:
                        self.charge_limit_best[window_n] = 0.0
                else:
                    self.charge_limit_best[window_n] = self.soc_max

        if self.export_window_best and self.calculate_best_export:
            # Set all to max
            for window_n in range(len(self.export_window_best)):
                if self.export_window_best[window_n]["start"] < (self.minutes_now + self.end_record):
                    if reset_all:
                        self.export_limits_best[window_n] = 100.0
                else:
                    self.export_limits_best[window_n] = 100.0

    def run_prediction(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, save=None, step=PREDICT_STEP):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """

        # Call the prediction model
        pred = self.prediction
        (
            final_metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            final_battery_cycle,
            final_metric_keep,
            final_iboost_kwh,
            final_carbon_g,
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        ) = pred.run_prediction(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, save, step)
        self.predict_soc = predict_soc
        self.car_charging_soc_next = car_charging_soc_next
        self.iboost_next = iboost_next
        self.iboost_running = iboost_running
        self.iboost_running_solar = iboost_running_solar
        self.iboost_running_full = iboost_running_full
        if save or pred.debug_enable:
            predict_soc_time = pred.predict_soc_time
            first_charge = pred.first_charge
            first_charge_soc = pred.first_charge_soc
            predict_car_soc_time = pred.predict_car_soc_time
            predict_battery_power = pred.predict_battery_power
            predict_state = pred.predict_state
            predict_battery_cycle = pred.predict_battery_cycle
            predict_pv_power = pred.predict_pv_power
            predict_grid_power = pred.predict_grid_power
            predict_load_power = pred.predict_load_power
            final_export_kwh = pred.final_export_kwh
            export_kwh_h0 = pred.export_kwh_h0
            final_load_kwh = pred.final_load_kwh
            load_kwh_h0 = pred.load_kwh_h0
            metric_time = pred.metric_time
            record_time = pred.record_time
            predict_iboost = pred.predict_iboost
            predict_carbon_g = pred.predict_carbon_g
            load_kwh_time = pred.load_kwh_time
            pv_kwh_time = pred.pv_kwh_time
            import_kwh_time = pred.import_kwh_time
            export_kwh_time = pred.export_kwh_time
            final_pv_kwh = pred.final_pv_kwh
            export_to_first_charge = pred.export_to_first_charge
            pv_kwh_h0 = pred.pv_kwh_h0
            final_import_kwh = pred.final_import_kwh
            final_import_kwh_house = pred.final_import_kwh_house
            final_import_kwh_battery = pred.final_import_kwh_battery
            hours_left = pred.hours_left
            final_car_soc = pred.final_car_soc
            import_kwh_h0 = pred.import_kwh_h0
            predict_export = pred.predict_export

            if save == "best" or save == "compare" or save == "yesterday":
                self.predict_soc_best = pred.predict_soc_best
                self.predict_iboost_best = pred.predict_iboost_best
                self.predict_metric_best = pred.predict_metric_best
                self.predict_carbon_best = pred.predict_carbon_best
                self.predict_clipped_best = pred.predict_clipped_best

            if save:
                self.log(
                    "predict {} end_record {}, final soc {}kWh, metric {}{}, metric_keep {}kWh, min_soc {}kWh @ {}, load {}kWh, PV {}kWh".format(
                        save,
                        self.time_abs_str(end_record + self.minutes_now),
                        round(final_soc, 2),
                        round(final_metric, 2),
                        self.currency_symbols[1],
                        round(final_metric_keep, 2),
                        round(soc_min, 2),
                        self.time_abs_str(soc_min_minute),
                        round(final_load_kwh, 2),
                        round(final_pv_kwh, 2),
                    )
                )
                if self.debug_enable:
                    self.log("   Slot: [{}]".format(self.scenario_summary_title(record_time)))
                    self.log("    SoC: [{}] kWh".format(self.scenario_summary(record_time, predict_soc_time)))
                    self.log("    BAT: [{}] kWh".format(self.scenario_summary(record_time, predict_state)))
                    self.log("   LOAD: [{}] kWh".format(self.scenario_summary(record_time, load_kwh_time)))
                    self.log("     PV: [{}] kWh".format(self.scenario_summary(record_time, pv_kwh_time)))
                    self.log(" IMPORT: [{}] kWh".format(self.scenario_summary(record_time, import_kwh_time)))
                    self.log(" EXPORT: [{}] kWh".format(self.scenario_summary(record_time, export_kwh_time)))
                    if self.iboost_enable:
                        self.log(" IBOOST: [{}] kWh".format(self.scenario_summary(record_time, predict_iboost)))
                    if self.carbon_enable:
                        self.log(" CARBON: [{}] kg".format(self.scenario_summary(record_time, predict_carbon_g)))
                    for car_n in range(self.num_cars):
                        self.log("   CAR{}: [{}] kWh".format(car_n, self.scenario_summary(record_time, predict_car_soc_time[car_n])))
                    self.log(" METRIC: [{}] {}".format(self.scenario_summary(record_time, metric_time), self.currency_symbols[1]))
                    if save == "best":
                        self.log(" STATE:  [{}]".format(self.scenario_summary_state(record_time)))

            # Save data to HA state
            if save and save == "base":
                self.dashboard_item(
                    self.prefix + ".battery_hours_left",
                    state=dp2(hours_left),
                    attributes={"friendly_name": "Predicted Battery Hours left", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
                )
                postfix = ""
                for car_n in range(self.num_cars):
                    if car_n > 0:
                        postfix = "_" + str(car_n)
                    self.dashboard_item(
                        self.prefix + ".car_soc" + postfix,
                        state=dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                        attributes={
                            "results": self.filtered_times(predict_car_soc_time[car_n]),
                            "today": self.filtered_today(predict_car_soc_time[car_n]),
                            "friendly_name": "Car " + str(car_n) + " Battery SoC",
                            "state_class": "measurement",
                            "unit_of_measurement": "%",
                            "icon": "mdi:battery",
                        },
                    )
                self.dashboard_item(
                    self.prefix + ".soc_kw_h0",
                    state=dp3(self.predict_soc[0]),
                    attributes={"friendly_name": "Current SoC kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Predicted SoC kWh",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "first_charge_kwh": first_charge_soc,
                        "icon": "mdi:battery",
                        "soc_now": dp3(self.soc_kw),
                        "soc_max": dp3(self.soc_max),
                        "soc_now_percent": dp2(calc_percent_limit(self.soc_kw, self.soc_max)),
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_cycle",
                    state=dp3(final_battery_cycle),
                    attributes={
                        "results": self.filtered_times(predict_battery_cycle),
                        "today": self.filtered_today(predict_battery_cycle),
                        "friendly_name": "Predicted Battery Cycle",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".soc_min_kwh",
                    state=dp3(soc_min),
                    attributes={
                        "time": self.time_abs_str(soc_min_minute),
                        "friendly_name": "Predicted minimum SoC",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-arrow-down-outline",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".export_energy_h0",
                    state=dp3(export_kwh_h0),
                    attributes={"friendly_name": "Current export", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-export"},
                )
                self.dashboard_item(
                    self.prefix + ".load_energy",
                    state=dp3(final_load_kwh),
                    attributes={
                        "results": self.filtered_times(load_kwh_time),
                        "today": self.filtered_today(load_kwh_time),
                        "friendly_name": "Predicted load",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:home-lightning-bolt",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_energy_h0",
                    state=dp3(load_kwh_h0),
                    attributes={"friendly_name": "Current load", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
                )
                self.dashboard_item(
                    self.prefix + ".pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={"results": pv_kwh_time, "friendly_name": "Predicted PV", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"},
                )
                self.dashboard_item(
                    self.prefix + ".pv_energy_h0",
                    state=dp3(pv_kwh_h0),
                    attributes={"friendly_name": "Current PV", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"},
                )
                self.dashboard_item(
                    self.prefix + ".import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".import_energy_h0",
                    state=dp3(import_kwh_h0),
                    attributes={"friendly_name": "Current import kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
                )
                self.dashboard_item(
                    self.prefix + ".import_energy_battery",
                    state=dp3(final_import_kwh_battery),
                    attributes={
                        "friendly_name": "Predicted import to battery",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".import_energy_house",
                    state=dp3(final_import_kwh_house),
                    attributes={"friendly_name": "Predicted import to house", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
                )
                self.log("Battery has {} hours left - now at {}kWh".format(dp2(hours_left), dp2(self.soc_kw)))
                self.dashboard_item(
                    self.prefix + ".metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".duration",
                    state=dp2(end_record / 60),
                    attributes={"friendly_name": "Prediction duration", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:arrow-split-vertical"},
                )
                if self.carbon_enable:
                    self.dashboard_item(
                        self.prefix + ".carbon",
                        state=dp2(final_carbon_g),
                        attributes={
                            "results": self.filtered_times(predict_carbon_g),
                            "today": self.filtered_today(predict_carbon_g),
                            "friendly_name": "Predicted Carbon energy",
                            "state_class": "measurement",
                            "unit_of_measurement": "g",
                            "icon": "mdi:molecule-co2",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".carbon_now",
                        state=dp2(self.carbon_intensity.get(0, 0)),
                        attributes={
                            "friendly_name": "Grid carbon intensity now",
                            "state_class": "measurement",
                            "unit_of_measurement": "g/kWh",
                            "icon": "mdi:molecule-co2",
                        },
                    )

            if save and save == "best":
                self.log("Saving plan best values to HA entities")
                self.dashboard_item(
                    self.prefix + ".best_battery_hours_left",
                    state=dp2(hours_left),
                    attributes={"friendly_name": "Predicted Battery Hours left best", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
                )
                postfix = ""
                for car_n in range(self.num_cars):
                    if car_n > 0:
                        postfix = "_" + str(car_n)
                    self.dashboard_item(
                        self.prefix + ".car_soc_best" + postfix,
                        state=dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                        attributes={
                            "results": self.filtered_times(predict_car_soc_time[car_n]),
                            "today": self.filtered_today(predict_car_soc_time[car_n]),
                            "friendly_name": "Car " + str(car_n) + " Battery SoC best",
                            "state_class": "measurement",
                            "unit_of_measurement": "%",
                            "icon": "mdi:battery",
                        },
                    )

                # Compute battery value now and at end of plan
                value_kwh_now = self.metric_battery_value_scaling * self.battery_value_rate(self.minutes_now)
                value_kwh_end = self.metric_battery_value_scaling * self.battery_value_rate(self.minutes_now + end_record)

                self.dashboard_item(
                    self.prefix + ".soc_kw_best",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Battery SoC kWh best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "first_charge_kwh": first_charge_soc,
                        "value_per_kwh_now": dp2(value_kwh_now),
                        "value_per_kwh_end": dp2(value_kwh_end),
                        "value_now": dp2(self.soc_kw * value_kwh_now),
                        "value_end": dp2(final_soc * value_kwh_end),
                        "icon": "mdi:battery",
                        "soc_now": dp3(self.soc_kw),
                        "soc_max": dp3(self.soc_max),
                        "soc_now_percent": dp2(calc_percent_limit(self.soc_kw, self.soc_max)),
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_power_best",
                    state=dp3(self.battery_power / 1000.0),
                    attributes={
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
                        "friendly_name": "Predicted Battery Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_cycle_best",
                    state=dp3(final_battery_cycle),
                    attributes={
                        "results": self.filtered_times(predict_battery_cycle),
                        "today": self.filtered_today(predict_battery_cycle),
                        "friendly_name": "Predicted Battery Cycle Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".pv_power_best",
                    state=dp3(self.pv_power / 1000.0),
                    attributes={
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
                        "friendly_name": "Predicted PV Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".grid_power_best",
                    state=dp3(self.grid_power / 1000.0),
                    attributes={
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
                        "friendly_name": "Predicted Grid Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_power_best",
                    state=dp3(self.load_power / 1000.0),
                    attributes={
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
                        "friendly_name": "Predicted Load Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw_best_h1",
                    state=dp3(self.predict_soc[60]),
                    attributes={"friendly_name": "Predicted SoC kWh best + 1h", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw_best_h8",
                    state=dp3(self.predict_soc[60 * 8]),
                    attributes={"friendly_name": "Predicted SoC kWh best + 8h", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw_best_h12",
                    state=dp3(self.predict_soc[60 * 12]),
                    attributes={"friendly_name": "Predicted SoC kWh best + 12h", "state_class": "measurement", "unit _of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".best_soc_min_kwh",
                    state=dp3(soc_min),
                    attributes={
                        "time": self.time_abs_str(soc_min_minute),
                        "friendly_name": "Predicted minimum SoC best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-arrow-down-outline",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_load_energy",
                    state=dp3(final_load_kwh),
                    attributes={
                        "results": self.filtered_times(load_kwh_time),
                        "today": self.filtered_today(load_kwh_time),
                        "friendly_name": "Predicted load best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:home-lightning-bolt",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
                        "friendly_name": "Predicted PV best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_import_energy_battery",
                    state=dp3(final_import_kwh_battery),
                    attributes={
                        "friendly_name": "Predicted import to battery best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_import_energy_house",
                    state=dp3(final_import_kwh_house),
                    attributes={
                        "friendly_name": "Predicted import to house best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted best metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(self.prefix + ".record", state=0.0, attributes={"results": self.filtered_times(record_time), "friendly_name": "Prediction window", "state_class": "measurement"})
                self.dashboard_item(
                    self.prefix + ".iboost_best",
                    state=dp4(final_iboost_kwh),
                    attributes={
                        "results": self.filtered_times(predict_iboost),
                        "today": self.filtered_today(predict_iboost, resetmidnight=True),
                        "friendly_name": "Predicted iBoost energy best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:water-boiler",
                    },
                )
                self.dashboard_item(
                    "binary_sensor." + self.prefix + "_iboost_active",
                    state=self.iboost_running,
                    attributes={"friendly_name": "iBoost active", "icon": "mdi:water-boiler", "solar": self.iboost_running_solar, "full": self.iboost_running_full},
                )
                self.find_spare_energy(self.predict_soc, predict_export, step, first_charge)
                if self.carbon_enable:
                    self.dashboard_item(
                        self.prefix + ".carbon_best",
                        state=dp2(final_carbon_g),
                        attributes={
                            "results": self.filtered_times(predict_carbon_g),
                            "today": self.filtered_today(predict_carbon_g),
                            "friendly_name": "Predicted Carbon energy best",
                            "state_class": "measurement",
                            "unit_of_measurement": "g",
                            "icon": "mdi:molecule-co2",
                        },
                    )

            if save and save == "debug":
                self.dashboard_item(
                    self.prefix + ".pv_power_debug",
                    state=dp3(self.filtered_today(predict_pv_power, stamp=self.now_utc) or 0),
                    attributes={
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
                        "friendly_name": "Predicted PV Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".grid_power_debug",
                    state=dp3(self.filtered_today(predict_grid_power, stamp=self.now_utc) or 0),
                    attributes={
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
                        "friendly_name": "Predicted Grid Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_power_debug",
                    state=dp3(self.filtered_today(predict_load_power, stamp=self.now_utc) or 0),
                    attributes={
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
                        "friendly_name": "Predicted Load Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_power_debug",
                    state=dp3(self.filtered_today(predict_battery_power, stamp=self.now_utc) or 0),
                    attributes={
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
                        "friendly_name": "Predicted Battery Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )

            if save and save == "best10":
                self.dashboard_item(
                    self.prefix + ".soc_kw_best10",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Battery SoC kWh best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "first_charge_kwh": first_charge_soc,
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
                        "friendly_name": "Predicted PV best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted best 10% metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_load_energy",
                    state=dp3(final_load_kwh),
                    attributes={"friendly_name": "Predicted load best 10%", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
                )
                self.dashboard_item(
                    self.prefix + ".best10_import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )

            if save and save == "base10":
                self.dashboard_item(
                    self.prefix + ".soc_kw_base10",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Battery SoC kWh base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
                        "friendly_name": "Predicted PV base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted base 10% metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_load_energy",
                    state=dp3(final_load_kwh),
                    attributes={"friendly_name": "Predicted load base 10%", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
                )
                self.dashboard_item(
                    self.prefix + ".base10_import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )

        return (
            final_metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            final_battery_cycle,
            final_metric_keep,
            final_iboost_kwh,
            final_carbon_g,
        )

    def plan_iboost_smart(self):
        """
        Smart iboost planning
        """
        plan = []
        iboost_today = self.iboost_today
        iboost_max = self.iboost_max_energy
        iboost_power = self.iboost_max_power * 60
        iboost_min_length = max(int((self.iboost_smart_min_length + self.plan_interval_minutes - 1) / self.plan_interval_minutes) * self.plan_interval_minutes, self.plan_interval_minutes)

        self.log("Create iBoost smart plan, max {} kWh, power {} kW, min length {} minutes".format(iboost_max, iboost_power, iboost_min_length))

        low_rates = []
        start_minute = int(self.minutes_now / self.plan_interval_minutes) * self.plan_interval_minutes
        for minute in range(start_minute, start_minute + self.forecast_minutes, self.plan_interval_minutes):
            import_rate = 0
            export_rate = 0
            slot_length = 0
            slot_count = 0
            for slot_start in range(minute, minute + iboost_min_length, self.plan_interval_minutes):
                import_rate += self.rate_import.get(minute, self.rate_min)
                export_rate += self.rate_export.get(minute, 0)
                slot_length += self.plan_interval_minutes
                slot_count += 1
            if slot_count:
                low_rates.append({"start": minute, "end": minute + slot_length, "average": import_rate / slot_count, "export": export_rate / slot_count})

        # Get prices
        if self.iboost_smart:
            price_sorted = self.sort_window_by_price(low_rates, reverse_time=True)
            price_sorted.reverse()
        else:
            price_sorted = [n for n in range(len(low_rates))]

        total_days = int((self.forecast_minutes + self.minutes_now + 24 * 60 - 1) / (24 * 60))
        iboost_soc = [0 for n in range(total_days)]
        iboost_soc[0] = iboost_today

        used_slots = {}
        for window_n in price_sorted:
            window = low_rates[window_n]
            price = window["average"]
            export_price = window["export"]

            length = 0
            kwh = 0

            for day in range(0, total_days):
                day_start_minutes = day * 24 * 60
                day_end_minutes = day_start_minutes + 24 * 60

                slot_start = max(window["start"], self.minutes_now, day_start_minutes)
                slot_end = min(window["end"], day_end_minutes)

                if slot_start < slot_end:
                    rate_okay = True

                    for start in range(slot_start, slot_end, self.plan_interval_minutes):
                        end = min(start + self.plan_interval_minutes, slot_end)

                        # Avoid duplicate slots
                        if minute in used_slots:
                            rate_okay = False

                        # Boost on import/export rate
                        if price > self.iboost_rate_threshold:
                            rate_okay = False
                        if export_price > self.iboost_rate_threshold_export:
                            rate_okay = False

                        # Boost on gas rate vs import price
                        if self.iboost_gas and self.rate_gas:
                            gas_rate = self.rate_gas.get(start, 99) * self.iboost_gas_scale
                            if price > gas_rate:
                                rate_okay = False

                        # Boost on gas rate vs export price
                        if self.iboost_gas_export and self.rate_gas:
                            gas_rate = self.rate_gas.get(start, 99) * self.iboost_gas_scale
                            if export_price > gas_rate:
                                rate_okay = False

                        if not rate_okay:
                            continue

                        # Work out charging amounts
                        length = end - start
                        hours = length / 60
                        kwh = iboost_power * hours
                        iboost_left = max(iboost_max - iboost_soc[day], 0)
                        # Scale down the number of minutes if the value exceeds the max
                        if kwh > iboost_left:
                            percent = iboost_left / kwh
                            length = int(min(round(((length * percent) / 5) + 0.5, 0) * 5, end - start))
                            end = start + length
                            hours = length / 60
                            kwh = min(iboost_power * hours, iboost_left)
                        if kwh > 0:
                            iboost_soc[day] = dp3(iboost_soc[day] + kwh)
                            new_slot = {}
                            new_slot["start"] = start
                            new_slot["end"] = start + length
                            new_slot["kwh"] = dp3(kwh)
                            new_slot["average"] = window["average"]
                            new_slot["cost"] = dp2(new_slot["average"] * kwh)
                            plan.append(new_slot)
                            used_slots[start] = True

        # Return sorted back in time order
        plan = self.sort_window_by_time(plan)
        return plan

    def plan_car_charging(self, car_n, low_rates):
        """
        Plan when the car will charge, taking into account ready time and pricing
        """
        plan = []
        car_soc = self.car_charging_soc[car_n]
        max_price = self.car_charging_plan_max_price[car_n]

        if self.car_charging_plan_smart[car_n]:
            price_sorted = self.sort_window_by_price(low_rates, reverse_time=True)
            price_sorted.reverse()
        else:
            price_sorted = [n for n in range(len(low_rates))]

        try:
            ready_time = datetime.strptime(self.car_charging_plan_time[car_n], "%H:%M:%S")
        except (ValueError, TypeError):
            ready_time = datetime.strptime("07:00:00", "%H:%M:%S")
            self.log("Warn: Car charging plan time for car {} is invalid".format(car_n))

        ready_minutes = ready_time.hour * 60 + ready_time.minute

        # Ready minutes wrap?
        if ready_minutes < self.minutes_now:
            ready_minutes += 24 * 60

        # Car charging now override
        extra_slot = {}
        if self.car_charging_now[car_n]:
            start = int(self.minutes_now / self.plan_interval_minutes) * self.plan_interval_minutes
            end = start + self.plan_interval_minutes
            extra_slot["start"] = start
            extra_slot["end"] = end
            extra_slot["average"] = self.rate_import.get(start, self.rate_min)
            self.log("Car is charging now slot {}".format(extra_slot))

            for window_p in price_sorted:
                window = low_rates[window_p]
                if window["start"] == start:
                    price_sorted.remove(window_p)
                    self.log("Remove old window {}".format(window_p))
                    break

            price_sorted = [-1] + price_sorted

        for window_n in price_sorted:
            if window_n == -1:
                window = extra_slot
            else:
                window = low_rates[window_n]

            start = max(window["start"], self.minutes_now)
            end = min(window["end"], ready_minutes)
            price = window["average"]

            length = 0
            kwh = 0

            # Stop once we have enough charge, allow small margin for rounding
            if (car_soc + 0.1) >= self.car_charging_limit[car_n]:
                break

            # Skip past windows
            if end <= start:
                continue

            # Skip over prices when they are too high
            if (max_price != 0) and price > max_price:
                continue

            # Compute amount of charge
            length = end - start
            hours = length / 60
            kwh = self.car_charging_rate[car_n] * hours

            kwh_add = kwh * self.car_charging_loss
            kwh_left = max(self.car_charging_limit[car_n] - car_soc, 0)

            # Clamp length to required amount (shorten the window)
            if kwh_add > kwh_left:
                percent = kwh_left / kwh_add
                length = int(min(round(((length * percent) / 5) + 0.5, 0) * 5, end - start))
                end = start + length
                hours = length / 60
                kwh = self.car_charging_rate[car_n] * hours
                kwh_add = min(kwh * self.car_charging_loss, kwh_left)
                kwh = kwh_add / self.car_charging_loss

            # Work out charging amounts
            if kwh > 0:
                car_soc = dp3(car_soc + kwh_add)
                new_slot = {}
                new_slot["start"] = start
                new_slot["end"] = end
                new_slot["kwh"] = dp3(kwh)
                new_slot["average"] = window["average"]
                new_slot["cost"] = dp2(new_slot["average"] * kwh)
                new_slot["octopus"] = False
                plan.append(new_slot)

        # Return sorted back in time order
        plan = self.sort_window_by_time(plan)
        return plan

    def car_charge_slot_kwh(self, minute_start, minute_end):
        """
        Work out car charging amount in KWh for given self.plan_interval_minutes-minute slot
        """
        car_charging_kwh = 0.0
        if self.num_cars > 0:
            for car_n in range(self.num_cars):
                for window in self.car_charging_slots[car_n]:
                    start = window["start"]
                    end = window["end"]
                    if start < minute_end and end > minute_start:
                        kwh = 0
                        if end != start:
                            kwh = dp2(window["kwh"]) / (end - start)
                        for minute_offset in range(minute_start, minute_end, PREDICT_STEP):
                            if minute_offset >= start and minute_offset < end:
                                car_charging_kwh += kwh * PREDICT_STEP
            car_charging_kwh = dp2(car_charging_kwh)
        return car_charging_kwh

    def hit_car_window(self, window_start, window_end, cache=None):
        """Does this window intersect a car charging window?

        cache, when given, is a caller-owned dict of (start, end) -> hit. The optimiser asks the same
        question about the same handful of windows millions of times per plan, so the caller keeps a dict
        for as long as car_charging_slots cannot change underneath it and the scan collapses to a lookup.
        Deliberately not held on self: the lifetime then belongs to whoever knows when the slots change.

        The slot scan tests the intersection before dp2(): rounding every slot's kwh up front made this the
        most expensive function in planning for anyone with an EV, since the rounding was being done for
        nearly every slot the overlap test then discarded (6.45us -> 1.08us per call at 48 slots). dp2 is
        pure, so testing it last cannot change the answer.
        """
        if self.num_cars <= 0:
            # No car, no cache work - this is the common case and it has to stay a single test
            return False

        key = (window_start, window_end)
        if cache is not None:
            hit = cache.get(key)
            if hit is not None:
                return hit

        hit = False
        for car_n in range(self.num_cars):
            for window in self.car_charging_slots[car_n]:
                if window["end"] > window_start and window["start"] < window_end and dp2(window["kwh"]) > 0:
                    hit = True
                    break
            if hit:
                break

        if cache is not None:
            cache[key] = hit
        return hit
