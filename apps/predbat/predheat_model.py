"""Pure five-minute PredHeat thermal simulation, with Celsius, watts and kWh units."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

PREDICT_STEP = 5
WATTS_TO_DEGREES = 1.16


def _finite(name, value):
    """Reject missing, boolean and non-finite numeric model inputs."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError("{} must be a finite number".format(name))


@dataclass(frozen=True)
class HeatingConfig:
    """Immutable existing PredHeat coefficients; lookup tables are snapshots, not live mappings.

    Powers are watts, water volume is litres and temperatures are Celsius.
    ``watt_per_degree`` preserves PredHeat's existing thermal-capacity coefficient.
    Tables contain integer temperature keys and numeric values, already interpolated
    exactly as configured by PredHeat. Empty tables retain the legacy fallback.
    """

    mode: str = "pump"
    flow_temp: float = 40.0
    flow_difference_target: float = 5.0
    heat_loss_watts: float = 100.0
    heat_gain_static: float = 0.0
    watt_per_degree: float = 5000.0
    heat_output: float = 7000.0
    heat_volume: float = 200.0
    heat_max_power: float = 7000.0
    heat_min_power: float = 2000.0
    heat_cop: float = 4.0
    hysteresis_on: float = 0.5
    hysteresis_off: float = 0.1
    heat_pump_efficiency_max: float = 1.0
    heat_pump_efficiency: tuple = ()
    gas_efficiency: tuple = ()
    delta_correction: tuple = ()
    weather_compensation: tuple = ()
    weather_compensation_enabled: bool = False

    def __post_init__(self):
        """Validate coefficients and freeze caller-owned lookup table data."""
        if self.mode not in ("pump", "gas"):
            raise ValueError("mode must be pump or gas")
        positive = ("flow_difference_target", "watt_per_degree", "heat_volume", "heat_cop", "heat_pump_efficiency_max")
        nonnegative = ("heat_loss_watts", "heat_output", "heat_max_power", "heat_min_power", "hysteresis_on", "hysteresis_off")
        for name in positive + nonnegative + ("flow_temp", "heat_gain_static"):
            value = getattr(self, name)
            _finite(name, value)
            if (name in positive and value <= 0) or (name in nonnegative and value < 0):
                raise ValueError("{} is outside its physical range".format(name))
        if self.heat_min_power > self.heat_max_power:
            raise ValueError("heat_min_power exceeds heat_max_power")
        for name in ("heat_pump_efficiency", "gas_efficiency", "delta_correction", "weather_compensation"):
            source = getattr(self, name)
            try:
                entries = tuple((key, value) for key, value in source)
            except (TypeError, ValueError) as error:
                raise ValueError("{} must contain key/value pairs".format(name)) from error
            keys = set()
            for key, value in entries:
                if isinstance(key, bool) or not isinstance(key, int) or key in keys:
                    raise ValueError("{} keys must be unique integer temperatures".format(name))
                keys.add(key)
                _finite(name, value)
                if name in ("heat_pump_efficiency", "gas_efficiency") and value <= 0:
                    raise ValueError("efficiency must be positive")
                if name == "delta_correction" and value < 0:
                    raise ValueError("radiator correction must be nonnegative")
            object.__setattr__(self, name, entries)


@dataclass(frozen=True)
class HeatingState:
    """Room and water temperatures in Celsius and thermostat demand at a boundary."""

    room_temperature_c: float
    water_temperature_c: float
    heating_on: bool

    def __post_init__(self):
        """Validate the initial or resulting thermal state."""
        _finite("room_temperature_c", self.room_temperature_c)
        _finite("water_temperature_c", self.water_temperature_c)
        if not isinstance(self.heating_on, bool):
            raise ValueError("heating_on must be boolean")


@dataclass(frozen=True)
class HeatingInput:
    """Complete five-minute schedules, snapshotted without history or network reads.

    Each fraction is a cap on the existing modulated generator output, expressed
    as a fraction of configured maximum thermal watts. Zero prevents generation;
    it does not discard heat already present in the water or change the thermostat.
    The first interval honours ``initial_state.heating_on`` like the legacy runner.
    """

    config: HeatingConfig
    initial_state: HeatingState
    target_temperature_c: tuple
    outdoor_temperature_c: tuple
    heat_output_fraction: tuple | None = None

    def __post_init__(self):
        """Reject incomplete schedules and freeze all caller-owned sequences."""
        if not isinstance(self.config, HeatingConfig) or not isinstance(self.initial_state, HeatingState):
            raise ValueError("config and initial_state must be typed model inputs")
        for name in ("target_temperature_c", "outdoor_temperature_c", "heat_output_fraction"):
            values = getattr(self, name)
            if name == "heat_output_fraction" and values is None:
                continue
            try:
                values = tuple(values)
            except TypeError as error:
                raise ValueError("{} must be a complete sequence".format(name)) from error
            for value in values:
                _finite(name, value)
                if name == "heat_output_fraction" and not 0 <= value <= 1:
                    raise ValueError("heat_output_fraction must be between zero and one")
            object.__setattr__(self, name, values)
        if not self.target_temperature_c or len(self.target_temperature_c) != len(self.outdoor_temperature_c):
            raise ValueError("temperature schedules must be nonempty and have equal lengths")
        if self.heat_output_fraction is not None and len(self.heat_output_fraction) != len(self.target_temperature_c):
            raise ValueError("heat_output_fraction must cover every interval")


@dataclass(frozen=True)
class HeatingInterval:
    """One five-minute interval, unrounded energies and end-of-interval Celsius state."""

    minute: int
    room_temperature_c: float
    water_temperature_c: float
    target_temperature_c: float
    outdoor_temperature_c: float
    heating_on: bool
    heat_to_c: float
    electrical_kwh: float
    input_energy_kwh: float
    heat_generated_kwh: float
    heat_delivered_kwh: float

    @property
    def heating_active(self):
        """Return actual heat generation, distinct from thermostat demand when capped."""
        return self.heat_generated_kwh > 0


@dataclass(frozen=True)
class HeatingResult:
    """Immutable interval trajectory and final state, with no publishing side effects."""

    intervals: tuple
    final_state: HeatingState


def simulate_heating(inputs):
    """Run PredHeat's existing water/radiator/room physics for explicit schedules.

    Pump input energy is electrical kWh. Gas input energy is fuel kWh, with zero
    electrical demand, so callers cannot accidentally add boiler fuel to a battery
    load. The existing hysteresis, temperature rounding, modulation and efficiency
    order are preserved; no coefficient is fitted or inferred here.
    """
    if not isinstance(inputs, HeatingInput):
        raise ValueError("inputs must be HeatingInput")
    config = inputs.config
    internal_temp = inputs.initial_state.room_temperature_c
    volume_temp = inputs.initial_state.water_temperature_c
    heating_on = inputs.initial_state.heating_on
    efficiency = dict(config.heat_pump_efficiency)
    gas_efficiency = dict(config.gas_efficiency)
    delta_correction = dict(config.delta_correction)
    weather = dict(config.weather_compensation)
    intervals = []
    for index, (target_temp, external_temp) in enumerate(zip(inputs.target_temperature_c, inputs.outdoor_temperature_c)):
        minute = index * PREDICT_STEP
        temp_diff_outside = internal_temp - external_temp
        temp_diff_inside = target_temp - internal_temp
        if index:
            if heating_on and temp_diff_inside <= -config.hysteresis_on:
                heating_on = False
            elif not heating_on and temp_diff_inside >= config.hysteresis_off:
                heating_on = True
        heat_loss_current = config.heat_loss_watts * temp_diff_outside * PREDICT_STEP / 60.0
        heat_loss_current -= config.heat_gain_static * PREDICT_STEP / 60.0
        heat_to = 0
        heat_power_in = 0
        heat_power_out = 0
        energy_now_in = 0
        if heating_on:
            out_temp = min(max(int(external_temp + 0.5), -20), 20)
            heat_to = target_temp
            if config.weather_compensation_enabled:
                if out_temp not in weather:
                    raise ValueError("weather compensation missing temperature {}".format(out_temp))
                flow_temp = weather[out_temp]
            else:
                flow_temp = config.flow_temp
            if volume_temp < flow_temp:
                flow_temp_diff = min(flow_temp - volume_temp, config.flow_difference_target)
                power_percent = flow_temp_diff / config.flow_difference_target
                heat_power_out = config.heat_max_power * power_percent
                heat_power_out = max(config.heat_min_power, heat_power_out)
                heat_power_out = min(config.heat_max_power, heat_power_out)
            if inputs.heat_output_fraction is not None:
                heat_power_out = min(heat_power_out, config.heat_max_power * inputs.heat_output_fraction[index])
            if config.mode == "gas":
                inlet_temp = min(max(int(volume_temp + 0.5), 0), 100)
                condensing = gas_efficiency.get(inlet_temp, 0.80)
                heat_power_in = heat_power_out / (condensing * config.heat_cop)
            else:
                cop_adjust = efficiency.get(out_temp, config.heat_pump_efficiency_max) / config.heat_pump_efficiency_max
                heat_power_in = heat_power_out / (config.heat_cop * cop_adjust)
            energy_now_in = heat_power_in * PREDICT_STEP / 60.0 / 1000.0
            volume_temp += (heat_power_out / WATTS_TO_DEGREES / config.heat_volume) * PREDICT_STEP / 60.0
        flow_delta = volume_temp - internal_temp
        flow_delta_rounded = min(max(int(flow_delta), 0), 75)
        correction = delta_correction.get(flow_delta_rounded, 1.0)
        heat_output = config.heat_output * correction
        volume_temp -= (heat_output / WATTS_TO_DEGREES / config.heat_volume) * PREDICT_STEP / 60.0
        heat_loss_current -= heat_output * PREDICT_STEP / 60.0
        internal_temp = internal_temp - heat_loss_current / config.watt_per_degree
        _finite("simulated room temperature", internal_temp)
        _finite("simulated water temperature", volume_temp)
        intervals.append(
            HeatingInterval(
                minute, internal_temp, volume_temp, target_temp, external_temp, heating_on, heat_to, energy_now_in if config.mode == "pump" else 0.0, energy_now_in, heat_power_out * PREDICT_STEP / 60.0 / 1000.0, heat_output * PREDICT_STEP / 60.0 / 1000.0
            )
        )
    return HeatingResult(tuple(intervals), HeatingState(internal_temp, volume_temp, heating_on))
