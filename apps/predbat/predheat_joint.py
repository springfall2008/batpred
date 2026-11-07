"""Joint thermal and battery planning using PredHeat physics and PredBat economics.

The search changes heating targets and re-optimises the real battery for every
feasible candidate. It is bounded local search, not a global optimality claim.
No device services or live-plan adoption occur in this module.
"""

from dataclasses import dataclass, replace
from math import isfinite
from time import monotonic

from predheat_battery import BatteryHeatEvaluator
from predheat_model import HeatingInput, HeatingResult, PREDICT_STEP, simulate_heating


def _number(value):
    """Recognise finite real numbers without treating booleans as measurements."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def replace_heating_load(total, embedded, proposed):
    """Replace a known same-horizon heating contribution exactly once, in interval kWh.

    This requires a provenance-qualified embedded forecast, not an unrelated
    historical meter series. An unknown contribution must not be guessed.
    """
    total, embedded, proposed = tuple(total), tuple(embedded), tuple(proposed)
    if not total or len(total) != len(embedded) or len(total) != len(proposed):
        raise ValueError("Heating replacement requires matching nonempty intervals")
    if any(not _number(value) or value < 0 for values in (total, embedded, proposed) for value in values):
        raise ValueError("Heating replacement requires finite nonnegative interval kWh")
    if any(heat > load + 1e-9 for load, heat in zip(total, embedded)):
        raise ValueError("Embedded heating exceeds household demand")
    return tuple(max(0.0, load - heat) + new for load, heat, new in zip(total, embedded, proposed))


@dataclass(frozen=True)
class JointHeatingRequest:
    """Explicit model and comfort policy over the battery's five-minute horizon.

    The caller must attest that all three prepared load scenarios exclude heating.
    Room and water terminal reserves account for both stores of leftover heat.
    """

    thermal_input: HeatingInput
    minimum_temperature_c: tuple
    maximum_temperature_c: tuple
    terminal_minimum_c: float
    load_is_non_heating: bool
    max_evaluations: int = 32
    terminal_water_minimum_c: float | None = None
    block_minutes: int = 30
    max_seconds: float = 30

    def __post_init__(self):
        """Freeze schedules and reject incomplete, inconsistent or unqualified inputs."""
        if not isinstance(self.thermal_input, HeatingInput) or self.thermal_input.config.mode != "pump":
            raise ValueError("Joint battery planning requires an electric PredHeat model")
        if self.load_is_non_heating is not True:
            raise ValueError("Joint planning requires an explicitly non-heating load forecast")
        for name in ("minimum_temperature_c", "maximum_temperature_c"):
            try:
                values = tuple(getattr(self, name))
            except TypeError as error:
                raise ValueError("Comfort schedules must be complete sequences") from error
            if len(values) != len(self.thermal_input.target_temperature_c) or any(not _number(value) or not -10 <= value <= 40 for value in values):
                raise ValueError("Comfort schedules must cover every interval in Celsius")
            object.__setattr__(self, name, values)
        if any(low > high for low, high in zip(self.minimum_temperature_c, self.maximum_temperature_c)):
            raise ValueError("Minimum comfort exceeds maximum comfort")
        if not _number(self.terminal_minimum_c):
            raise ValueError("A finite terminal room temperature reserve is required")
        water = self.terminal_water_minimum_c
        if water is None:
            water = self.thermal_input.initial_state.water_temperature_c
            object.__setattr__(self, "terminal_water_minimum_c", water)
        if not _number(water):
            raise ValueError("A finite terminal water temperature reserve is required")
        if isinstance(self.max_evaluations, bool) or not isinstance(self.max_evaluations, int) or not 1 <= self.max_evaluations <= 256:
            raise ValueError("max_evaluations must be between 1 and 256")
        if not _number(self.max_seconds) or not 1 <= self.max_seconds <= 300:
            raise ValueError("max_seconds must be between 1 and 300")
        if isinstance(self.block_minutes, bool) or not isinstance(self.block_minutes, int) or self.block_minutes < PREDICT_STEP or self.block_minutes % PREDICT_STEP:
            raise ValueError("Planning blocks must be positive five-minute multiples")


@dataclass(frozen=True)
class JointHeatingCandidate:
    """One feasible thermal trajectory and its fully re-optimised battery plan."""

    target_temperature_c: tuple
    simulation: HeatingResult
    battery: object


@dataclass(frozen=True)
class JointHeatingResult:
    """Best candidate, comparable baseline and reproducible bounded-search diagnostics."""

    best: JointHeatingCandidate | None
    baseline: JointHeatingCandidate | None
    evaluations: int
    reason: str | None
    battery_evaluations: int = 0
    termination: str = "search_complete"


def _feasible(request, simulation):
    """Enforce comfort at both boundaries and retain room and water reserves."""
    temperature = request.thermal_input.initial_state.room_temperature_c
    for index, interval in enumerate(simulation.intervals):
        low, high = request.minimum_temperature_c[index], request.maximum_temperature_c[index]
        if temperature < low - 1e-7 or temperature > high + 1e-7 or interval.room_temperature_c < low - 1e-7 or interval.room_temperature_c > high + 1e-7:
            return False
        temperature = interval.room_temperature_c
    return temperature >= request.terminal_minimum_c - 1e-7 and simulation.final_state.water_temperature_c >= request.terminal_water_minimum_c - 1e-7


def _seed_targets(base, request, rates, upper):
    """Generate thermostat-advance, cheap-rate and solar-surplus starting schedules."""
    original = request.thermal_input.target_temperature_c
    steps = len(original)
    for lead_minutes in (30, 60, 90, 120, 180):
        lead = lead_minutes // PREDICT_STEP
        yield tuple(min(upper[index], max(original[index : index + lead + 1])) for index in range(steps))
    cheap = sorted(rates)[max(0, steps // 4 - 1)]
    for rise in (0.5, 1.0, 2.0):
        yield tuple(min(upper[index], original[index] + rise) if rates[index] <= cheap else original[index] for index in range(steps))
        yield tuple(min(upper[index], original[index] + rise) if base.pv_forecast_minute_step.get(index * PREDICT_STEP, 0) > base.load_minutes_step[index * PREDICT_STEP] else original[index] for index in range(steps))


def plan_joint_heating(base, request):
    """Search heating targets against the existing battery optimiser's common metric.

    Every accepted heating trial gets a fresh battery model and caches. The input
    base remains untouched, including on invalid/infeasible model inputs or errors.
    """
    steps = len(request.thermal_input.target_temperature_c)
    if base.forecast_minutes != steps * PREDICT_STEP:
        raise ValueError("Thermal and battery horizons must match exactly")
    rates = tuple(base.rate_import.get(base.minutes_now + index * PREDICT_STEP) for index in range(steps))
    exports = tuple(base.rate_export.get(base.minutes_now + index * PREDICT_STEP, 0 if not base.rate_export else None) for index in range(steps))
    if any(not _number(rate) for rate in rates + exports):
        raise ValueError("Joint planning requires complete import and export prices")
    evaluator = BatteryHeatEvaluator(base, base.load_minutes_step, base.load_minutes_step10, base.load_minutes_step90)
    original = request.thermal_input.target_temperature_c
    # Reserve space below the ceiling for the existing thermostat hysteresis.
    upper = tuple(high - request.thermal_input.config.hysteresis_on for high in request.maximum_temperature_c)
    deadline = monotonic() + request.max_seconds
    termination = "search_complete"
    seen = set()
    best = None
    baseline = None
    evaluations = 0
    battery_evaluations = 0

    def consider(targets):
        """Simulate one distinct schedule and retain only physically feasible trials."""
        nonlocal best, baseline, evaluations, battery_evaluations, termination
        targets = tuple(targets)
        if targets in seen:
            return
        if monotonic() >= deadline:
            termination = "time_budget"
            return
        if evaluations >= request.max_evaluations:
            termination = "evaluation_budget"
            return
        seen.add(targets)
        evaluations += 1
        simulation = simulate_heating(replace(request.thermal_input, target_temperature_c=targets))
        if not _feasible(request, simulation):
            return
        battery = evaluator.evaluate(tuple(row.electrical_kwh for row in simulation.intervals))
        battery_evaluations += 1
        candidate = JointHeatingCandidate(targets, simulation, battery)
        if targets == original:
            baseline = candidate
        if best is None or battery.metric < best.battery.metric - 1e-7 or (abs(battery.metric - best.battery.metric) <= 1e-7 and battery.cost < best.battery.cost):
            best = candidate

    consider(original)
    # Tariffs only seed the search; the battery metric determines acceptance.
    for targets in _seed_targets(base, request, rates, upper):
        consider(targets)
    # Coordinate refinement starts from the best JOINT plan, so the battery's
    # opportunity cost, export value and retained SOC determine the next trials.
    block = request.block_minutes // PREDICT_STEP
    starts = sorted(range(0, steps, block), key=lambda index: (rates[index], index))
    for start in starts:
        if evaluations >= request.max_evaluations or monotonic() >= deadline:
            termination = "evaluation_budget" if evaluations >= request.max_evaluations else "time_budget"
            break
        incumbent = best.target_temperature_c if best else original
        for delta in (-0.5, 0.5):
            targets = list(incumbent)
            for index in range(start, min(start + block, steps)):
                targets[index] = max(request.minimum_temperature_c[index], min(upper[index], targets[index] + delta))
            consider(targets)
    return JointHeatingResult(best, baseline, evaluations, None if best else "no_feasible_schedule_in_search", battery_evaluations, termination)
