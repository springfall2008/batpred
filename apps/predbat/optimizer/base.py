# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Optimizer Base Classes and Contracts
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Callable, Optional, Any
import time


@dataclass(frozen=True)
class BatteryState:
    """Immutable battery state at optimization start"""
    soc_kw: float
    soc_max: float
    reserve: float
    charge_rate_max: float
    discharge_rate_max: float
    loss_charge: float = 1.0
    loss_discharge: float = 1.0
    battery_rate_min: float = 0.0
    inverter_limit: float = 7.5
    inverter_hybrid: bool = True


@dataclass(frozen=True)
class OptimizerConfig:
    """Immutable configuration for optimization"""
    forecast_minutes: int
    step: int  # PREDICT_STEP (usually 5)
    minutes_now: int
    set_charge_freeze: bool = False
    set_export_freeze: bool = False
    set_discharge_during_charge: bool = True
    debug_enable: bool = False
    best_soc_keep: float = 0.0
    best_soc_keep_weight: float = 0.5
    best_soc_min: float = 0.0
    metric_battery_cycle: float = 0.0


@dataclass
class OptimizerInput:
    """
    Input data for optimization.

    This dataclass captures all inputs needed to run an optimization pass.
    Can be serialized to JSON for test fixtures.
    """
    # Price data
    price_set: List[float]
    price_links: Dict[float, List[str]]
    window_index: Dict[str, Dict]
    all_prices: List[float]

    # Windows - list of {"start": int, "end": int, "average": float}
    charge_windows: List[Dict]
    export_windows: List[Dict]

    # Initial limits (one per window)
    charge_limits: List[float]
    export_limits: List[float]

    # Rates (minute_absolute -> price in p/kWh)
    rates_import: Dict[int, float]
    rates_export: Dict[int, float]

    # Forecasts (minute -> kWh per step)
    pv_forecast: Dict[int, float]
    load_forecast: Dict[int, float]

    # State
    battery: BatteryState
    config: OptimizerConfig

    # Region for partial optimization (None = full)
    region_start: Optional[int] = None
    region_end: Optional[int] = None
    end_record: Optional[int] = None

    # Existing state from previous optimization passes
    tried_list: Optional[Dict[int, Any]] = field(default_factory=dict)
    levels_score: Optional[Dict[float, float]] = field(default_factory=dict)

    # Record windows for tracking which windows can be modified
    record_charge_windows: Optional[Dict[int, bool]] = field(default_factory=dict)
    record_export_windows: Optional[Dict[int, bool]] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Serialize to dict for JSON storage"""
        return {
            "price_set": self.price_set,
            "price_links": {str(k): v for k, v in self.price_links.items()},
            "window_index": self.window_index,
            "all_prices": self.all_prices,
            "charge_windows": self.charge_windows,
            "export_windows": self.export_windows,
            "charge_limits": self.charge_limits,
            "export_limits": self.export_limits,
            "rates_import": {str(k): v for k, v in self.rates_import.items()},
            "rates_export": {str(k): v for k, v in self.rates_export.items()},
            "pv_forecast": {str(k): v for k, v in self.pv_forecast.items()},
            "load_forecast": {str(k): v for k, v in self.load_forecast.items()},
            "battery": {
                "soc_kw": self.battery.soc_kw,
                "soc_max": self.battery.soc_max,
                "reserve": self.battery.reserve,
                "charge_rate_max": self.battery.charge_rate_max,
                "discharge_rate_max": self.battery.discharge_rate_max,
                "loss_charge": self.battery.loss_charge,
                "loss_discharge": self.battery.loss_discharge,
            },
            "config": {
                "forecast_minutes": self.config.forecast_minutes,
                "step": self.config.step,
                "minutes_now": self.config.minutes_now,
                "set_charge_freeze": self.config.set_charge_freeze,
                "set_export_freeze": self.config.set_export_freeze,
                "debug_enable": self.config.debug_enable,
            },
            "region_start": self.region_start,
            "region_end": self.region_end,
            "end_record": self.end_record,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "OptimizerInput":
        """Deserialize from dict"""
        battery = BatteryState(**data["battery"])
        config = OptimizerConfig(**data["config"])
        return cls(
            price_set=data["price_set"],
            price_links={float(k): v for k, v in data["price_links"].items()},
            window_index=data["window_index"],
            all_prices=data["all_prices"],
            charge_windows=data["charge_windows"],
            export_windows=data["export_windows"],
            charge_limits=data["charge_limits"],
            export_limits=data["export_limits"],
            rates_import={int(k): v for k, v in data["rates_import"].items()},
            rates_export={int(k): v for k, v in data["rates_export"].items()},
            pv_forecast={int(k): v for k, v in data["pv_forecast"].items()},
            load_forecast={int(k): v for k, v in data["load_forecast"].items()},
            battery=battery,
            config=config,
            region_start=data.get("region_start"),
            region_end=data.get("region_end"),
            end_record=data.get("end_record"),
        )


@dataclass
class OptimizerResult:
    """Output from optimization"""
    best_metric: float
    best_cost: float
    best_keep: float
    best_soc_min: float
    best_soc_min_minute: int
    best_cycle: float
    best_carbon: float
    best_import: float

    # Updated limits
    charge_limits: List[float]
    export_limits: List[float]

    # Telemetry
    iterations: int
    predictions_run: int
    elapsed_seconds: float
    strategy_name: str

    # Optional: the tried_list and levels_score for chaining optimizations
    tried_list: Optional[Dict[int, Any]] = None
    levels_score: Optional[Dict[float, float]] = None


# Type alias for prediction function
PredictFn = Callable[
    [List[float], List[Dict], List[Dict], List[float], bool, int, int],
    Tuple[float, float, float, float, float, float, int, float, float, float, float]
]


class OptimizerStrategy(ABC):
    """
    Abstract base class for optimization strategies.

    Implementations must provide:
    - name: Human-readable identifier
    - description: What the strategy does
    - optimize(): The optimization algorithm
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this strategy (e.g., 'numpy_vectorized')"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of the optimization approach"""
        pass

    @abstractmethod
    def optimize(
        self,
        inputs: OptimizerInput,
        predict_fn: PredictFn,
        launch_predict_fn: Optional[Callable] = None,
    ) -> OptimizerResult:
        """
        Run optimization and return result.

        Args:
            inputs: Immutable optimization inputs
            predict_fn: Synchronous function to run a single prediction.
                       Signature: (charge_limits, charge_windows, export_windows,
                                   export_limits, pv10, end_record, step) -> tuple
            launch_predict_fn: Optional async/parallel prediction launcher.
                              If provided, can be used for parallel evaluation.

        Returns:
            OptimizerResult with best solution found and telemetry
        """
        pass

    def _timed_optimize(
        self,
        inputs: OptimizerInput,
        predict_fn: PredictFn,
        launch_predict_fn: Optional[Callable] = None,
    ) -> OptimizerResult:
        """Wrapper that times the optimization"""
        start = time.perf_counter()
        result = self.optimize(inputs, predict_fn, launch_predict_fn)
        result.elapsed_seconds = time.perf_counter() - start
        result.strategy_name = self.name
        return result


class OptimizerContext:
    """
    Context for running optimizations with a selected strategy.

    Usage:
        ctx = OptimizerContext(strategy=NumpyVectorizedStrategy())
        result = ctx.run(inputs, predict_fn)
    """

    def __init__(self, strategy: OptimizerStrategy):
        self.strategy = strategy

    def run(
        self,
        inputs: OptimizerInput,
        predict_fn: PredictFn,
        launch_predict_fn: Optional[Callable] = None,
    ) -> OptimizerResult:
        """Run optimization using the configured strategy"""
        return self.strategy._timed_optimize(inputs, predict_fn, launch_predict_fn)

    def set_strategy(self, strategy: OptimizerStrategy):
        """Change the optimization strategy"""
        self.strategy = strategy
