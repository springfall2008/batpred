# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Optimizer Data Capture Utilities
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .base import OptimizerInput, BatteryState, OptimizerConfig


class OptimizerCapture:
    """
    Utility for capturing optimizer inputs from live runs.

    Captures data when optimization is slow (>threshold) or on-demand,
    saving as JSON fixtures for benchmarking.

    Usage in plan.py:
        from optimizer.capture import OptimizerCapture

        capture = OptimizerCapture(
            output_dir="/config/optimizer_captures",
            auto_capture_threshold=60.0  # seconds
        )

        # At start of optimise_charge_limit_price_threads:
        capture.start_capture(locals())

        # At end:
        capture.end_capture(elapsed_time, result)
    """

    def __init__(
        self,
        output_dir: Optional[str] = None,
        auto_capture_threshold: float = 60.0,
        enabled: bool = True,
    ):
        if output_dir is None:
            # Default to /config/optimizer_captures or current dir
            output_dir = os.environ.get("OPTIMIZER_CAPTURE_DIR", "/config/optimizer_captures")

        self.output_dir = Path(output_dir)
        self.auto_capture_threshold = auto_capture_threshold
        self.enabled = enabled
        self._current_capture: Optional[Dict] = None

    def start_capture(
        self,
        price_set: List[float],
        price_links: Dict[float, List[str]],
        window_index: Dict[str, Dict],
        all_prices: List[float],
        charge_windows: List[Dict],
        export_windows: List[Dict],
        charge_limits: List[float],
        export_limits: List[float],
        rates_import: Dict[int, float],
        rates_export: Dict[int, float],
        pv_forecast: Dict[int, float],
        load_forecast: Dict[int, float],
        battery_state: Dict,
        config: Dict,
        region_start: Optional[int] = None,
        region_end: Optional[int] = None,
        end_record: Optional[int] = None,
        user_id: Optional[str] = None,
    ):
        """
        Start capturing optimizer inputs.

        Call this at the beginning of optimization with all relevant parameters.
        """
        if not self.enabled:
            return

        self._current_capture = {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "inputs": {
                "price_set": list(price_set),
                "price_links": {str(k): v for k, v in price_links.items()},
                "window_index": dict(window_index),
                "all_prices": list(all_prices),
                "charge_windows": [dict(w) for w in charge_windows],
                "export_windows": [dict(w) for w in export_windows],
                "charge_limits": list(charge_limits),
                "export_limits": list(export_limits),
                "rates_import": {str(k): v for k, v in rates_import.items()},
                "rates_export": {str(k): v for k, v in rates_export.items()},
                "pv_forecast": {str(k): v for k, v in pv_forecast.items()},
                "load_forecast": {str(k): v for k, v in load_forecast.items()},
                "battery": battery_state,
                "config": config,
                "region_start": region_start,
                "region_end": region_end,
                "end_record": end_record,
            },
        }

    def end_capture(
        self,
        elapsed_seconds: float,
        best_metric: float,
        iterations: int,
        predictions_run: int,
        force_save: bool = False,
    ) -> Optional[Path]:
        """
        End capture and optionally save if threshold exceeded.

        Returns path to saved file if captured, None otherwise.
        """
        if not self.enabled or self._current_capture is None:
            return None

        self._current_capture["result"] = {
            "elapsed_seconds": elapsed_seconds,
            "best_metric": best_metric,
            "iterations": iterations,
            "predictions_run": predictions_run,
        }

        # Save if slow or forced
        if force_save or elapsed_seconds > self.auto_capture_threshold:
            path = self._save_capture()
            self._current_capture = None
            return path

        self._current_capture = None
        return None

    def _save_capture(self) -> Path:
        """Save current capture to file"""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        user_id = self._current_capture.get("user_id", "unknown")[:8]
        elapsed = self._current_capture["result"]["elapsed_seconds"]

        filename = f"capture_{timestamp}_{user_id}_{elapsed:.0f}s.json"
        path = self.output_dir / filename

        with open(path, 'w') as f:
            json.dump(self._current_capture, f, indent=2, default=str)

        return path

    def force_capture(self) -> Optional[Path]:
        """Force save current capture regardless of threshold"""
        if self._current_capture is None:
            return None
        return self._save_capture()


def create_optimizer_input_from_plan(plan_instance) -> OptimizerInput:
    """
    Create OptimizerInput from a Plan instance.

    This extracts all relevant data from the Plan class for use
    with the pluggable optimizer strategies.

    Args:
        plan_instance: Instance of Plan class with populated data

    Returns:
        OptimizerInput ready for optimization
    """
    p = plan_instance  # shorthand

    # Build battery state
    battery = BatteryState(
        soc_kw=p.soc_kw,
        soc_max=p.soc_max,
        reserve=p.reserve,
        charge_rate_max=p.battery_rate_max_charge,
        discharge_rate_max=p.battery_rate_max_discharge,
        loss_charge=p.battery_loss,
        loss_discharge=p.battery_loss_discharge,
        battery_rate_min=p.battery_rate_min,
        inverter_limit=p.inverter_limit,
        inverter_hybrid=p.inverter_hybrid,
    )

    # Build config
    config = OptimizerConfig(
        forecast_minutes=p.forecast_minutes,
        step=p.predict_step,
        minutes_now=p.minutes_now,
        set_charge_freeze=p.set_charge_freeze,
        set_export_freeze=p.set_export_freeze,
        set_discharge_during_charge=p.set_discharge_during_charge,
        debug_enable=p.debug_enable,
        best_soc_keep=p.best_soc_keep,
        best_soc_keep_weight=getattr(p, 'best_soc_keep_weight', 0.5),
        best_soc_min=p.best_soc_min,
        metric_battery_cycle=getattr(p, 'metric_battery_cycle', 0.0),
    )

    return OptimizerInput(
        price_set=list(p.price_set) if hasattr(p, 'price_set') else [],
        price_links=dict(p.price_links) if hasattr(p, 'price_links') else {},
        window_index=dict(p.window_index) if hasattr(p, 'window_index') else {},
        all_prices=list(p.all_prices) if hasattr(p, 'all_prices') else [],
        charge_windows=p.charge_window_best.copy() if hasattr(p, 'charge_window_best') else [],
        export_windows=p.export_window_best.copy() if hasattr(p, 'export_window_best') else [],
        charge_limits=p.charge_limit_best.copy() if hasattr(p, 'charge_limit_best') else [],
        export_limits=p.export_limits_best.copy() if hasattr(p, 'export_limits_best') else [],
        rates_import=dict(p.rate_import) if hasattr(p, 'rate_import') else {},
        rates_export=dict(p.rate_export) if hasattr(p, 'rate_export') else {},
        pv_forecast=dict(p.pv_forecast_minute_step) if hasattr(p, 'pv_forecast_minute_step') else {},
        load_forecast=dict(p.load_minutes_step) if hasattr(p, 'load_minutes_step') else {},
        battery=battery,
        config=config,
    )


def convert_capture_to_fixture(capture_path: str, output_path: Optional[str] = None) -> str:
    """
    Convert a raw capture file to a clean test fixture.

    Args:
        capture_path: Path to capture JSON file
        output_path: Optional output path (defaults to fixtures dir)

    Returns:
        Path to created fixture file
    """
    with open(capture_path) as f:
        capture = json.load(f)

    # Extract just the inputs (drop timestamp, result, etc.)
    inputs = capture["inputs"]

    # Determine output path
    if output_path is None:
        fixtures_dir = Path(__file__).parent.parent / "tests" / "fixtures" / "optimizer"
        fixtures_dir.mkdir(parents=True, exist_ok=True)

        # Generate name from capture metadata
        user_id = capture.get("user_id", "unknown")[:8]
        elapsed = capture.get("result", {}).get("elapsed_seconds", 0)
        output_path = fixtures_dir / f"iog_real_{user_id}_{elapsed:.0f}s.json"

    with open(output_path, 'w') as f:
        json.dump(inputs, f, indent=2)

    return str(output_path)
