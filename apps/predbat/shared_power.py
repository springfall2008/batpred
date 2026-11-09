# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string

"""Configuration and prediction helpers for shared EV/battery conversion capacity."""


def normalise_shared_power_groups(value, num_cars, num_inverters):
    """Validate and normalise the optional shared conversion-capacity configuration."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("shared_power_groups must be a list")

    groups = []
    used_cars = set()
    used_inverters = set()
    used_ids = set()
    for group_n, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("shared_power_groups entry {} must be a dictionary".format(group_n))
        group_id = item.get("id", "shared_power_{}".format(group_n))
        if not isinstance(group_id, str) or not group_id.strip():
            raise ValueError("shared_power_groups entry {} has an invalid id".format(group_n))
        if group_id in used_ids:
            raise ValueError("shared_power_groups id '{}' is duplicated".format(group_id))
        used_ids.add(group_id)

        priority = item.get("priority", "ev_first")
        if priority != "ev_first":
            raise ValueError("shared_power_groups '{}' priority must be ev_first".format(group_id))
        try:
            limit_kw = float(item["limit_kw"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("shared_power_groups '{}' limit_kw must be a number".format(group_id)) from error
        if limit_kw <= 0:
            raise ValueError("shared_power_groups '{}' limit_kw must be greater than zero".format(group_id))

        cars = item.get("cars")
        inverters = item.get("inverters")
        if not isinstance(cars, list) or not cars:
            raise ValueError("shared_power_groups '{}' cars must be a non-empty list".format(group_id))
        if not isinstance(inverters, list) or not inverters:
            raise ValueError("shared_power_groups '{}' inverters must be a non-empty list".format(group_id))
        if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= num_cars for index in cars):
            raise ValueError("shared_power_groups '{}' contains a car index outside 0..{}".format(group_id, max(num_cars - 1, 0)))
        if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= num_inverters for index in inverters):
            raise ValueError("shared_power_groups '{}' contains an inverter index outside 0..{}".format(group_id, max(num_inverters - 1, 0)))
        if len(set(cars)) != len(cars) or used_cars.intersection(cars):
            raise ValueError("shared_power_groups '{}' assigns a car more than once".format(group_id))
        if len(set(inverters)) != len(inverters) or used_inverters.intersection(inverters):
            raise ValueError("shared_power_groups '{}' assigns an inverter more than once".format(group_id))
        used_cars.update(cars)
        used_inverters.update(inverters)
        groups.append({"id": group_id, "limit_kw": limit_kw, "priority": priority, "cars": list(cars), "inverters": list(inverters)})
    return groups


def build_shared_power_model(groups, inverter_objects, aggregate_battery_rate, num_cars, num_inverters):
    """Build compact per-group rates and a car-to-group map for the prediction hot loop."""
    if not groups:
        return [], [], [-1] * num_cars, 0.0

    inverter_rates = []
    rates_available = len(inverter_objects) >= num_inverters
    for inverter_n in range(num_inverters):
        rate = getattr(inverter_objects[inverter_n], "battery_rate_max_charge", None) if inverter_n < len(inverter_objects) else None
        if rate is None:
            rates_available = False
            break
        inverter_rates.append(float(rate))
    if not rates_available:
        # Prediction fixtures and old debug dumps may not retain per-inverter rates. The aggregate
        # is still exact; divide it evenly so group membership remains useful and deterministic.
        inverter_rates = [aggregate_battery_rate / max(num_inverters, 1)] * num_inverters

    limits = []
    battery_rates = []
    car_groups = [-1] * num_cars
    grouped_battery_rate = 0.0
    for group_n, group in enumerate(groups):
        limits.append(group["limit_kw"] / 60.0)
        battery_rate = sum(inverter_rates[inverter_n] for inverter_n in group["inverters"])
        battery_rates.append(battery_rate)
        grouped_battery_rate += battery_rate
        for car_n in group["cars"]:
            car_groups[car_n] = group_n
    unconstrained_rate = max(aggregate_battery_rate - grouped_battery_rate, 0.0)
    return limits, battery_rates, car_groups, unconstrained_rate


def car_slot_average(minute, step, car_n, car_charging_slots):
    """Return average input kW and premium rate across a prediction interval for one car."""
    energy = 0.0
    premium_cost = 0.0
    interval_end = minute + step
    slots = car_charging_slots[car_n] if car_n < len(car_charging_slots) else []
    for slot in slots:
        start = slot["start"]
        end = slot["end"]
        overlap = max(min(interval_end, end) - max(minute, start), 0)
        duration = end - start
        if overlap <= 0 or duration <= 0:
            continue
        power = abs(slot["kwh"] / (duration / 60.0))
        slot_energy = power * overlap / 60.0
        energy += slot_energy
        if slot.get("octopus", False):
            premium_cost += slot_energy * slot.get("average", 0)
    average_power = energy * 60.0 / step if step > 0 else 0.0
    average_rate = premium_cost / energy if energy > 0 else 0.0
    return average_power, average_rate


def shared_battery_charge_limit(step, group_limits, group_battery_rates, group_ev_energy, unconstrained_rate):
    """Return battery-side grid charging energy available after EV-first allocation."""
    available = unconstrained_rate * step
    for group_n, limit in enumerate(group_limits):
        group_available = max(limit * step - group_ev_energy[group_n], 0.0)
        available += min(group_battery_rates[group_n] * step, group_available)
    return available
