# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Optimizer Benchmark Harness
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from .base import OptimizerStrategy, OptimizerInput, OptimizerResult, PredictFn


class OptimizerBenchmark:
    """
    Benchmark harness for optimizer strategies.

    Allows comparing multiple optimization strategies against test fixtures
    captured from real customer data.

    Usage:
        benchmark = OptimizerBenchmark()
        benchmark.register(BaselineStrategy())
        benchmark.register(NumpyVectorizedStrategy())

        results = benchmark.run_benchmark("iog_heavy", iterations=3)
        benchmark.print_results(results)
    """

    def __init__(self, data_dir: Optional[Path] = None):
        if data_dir is None:
            # Default to tests/fixtures/optimizer relative to this file
            data_dir = Path(__file__).parent.parent / "tests" / "fixtures" / "optimizer"
        self.data_dir = Path(data_dir)
        self.strategies: List[OptimizerStrategy] = []
        self._predict_fn: Optional[PredictFn] = None

    def register(self, strategy: OptimizerStrategy):
        """Register a strategy for benchmarking"""
        self.strategies.append(strategy)

    def set_predict_fn(self, predict_fn: PredictFn):
        """Set the prediction function to use for benchmarks"""
        self._predict_fn = predict_fn

    def load_fixture(self, name: str) -> OptimizerInput:
        """Load a test fixture from JSON"""
        path = self.data_dir / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Fixture not found: {path}")
        with open(path) as f:
            data = json.load(f)
        return OptimizerInput.from_dict(data)

    def save_fixture(self, name: str, inputs: OptimizerInput):
        """Save optimizer inputs as a test fixture"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / f"{name}.json"
        with open(path, 'w') as f:
            json.dump(inputs.to_dict(), f, indent=2, default=str)
        return path

    def list_fixtures(self) -> List[str]:
        """List available fixtures"""
        if not self.data_dir.exists():
            return []
        return [p.stem for p in self.data_dir.glob("*.json")]

    def run_single(
        self,
        strategy: OptimizerStrategy,
        inputs: OptimizerInput,
        predict_fn: Optional[PredictFn] = None,
    ) -> OptimizerResult:
        """Run a single strategy on inputs"""
        fn = predict_fn or self._predict_fn
        if fn is None:
            raise ValueError("No predict_fn provided. Call set_predict_fn() first or pass predict_fn.")
        return strategy._timed_optimize(inputs, fn)

    def run_benchmark(
        self,
        fixture_name: str,
        iterations: int = 3,
        validate_results: bool = True,
        predict_fn: Optional[PredictFn] = None,
    ) -> Dict[str, Dict]:
        """
        Run all registered strategies against a fixture.

        Args:
            fixture_name: Name of the fixture file (without .json)
            iterations: Number of iterations per strategy for timing
            validate_results: Whether to validate results against baseline
            predict_fn: Optional prediction function override

        Returns:
            Dict of {strategy_name: {result, times, mean_time, min_time, max_time, valid, speedup}}
        """
        fn = predict_fn or self._predict_fn
        if fn is None:
            raise ValueError("No predict_fn provided. Call set_predict_fn() first or pass predict_fn.")

        inputs = self.load_fixture(fixture_name)
        results = {}

        # Get baseline result for validation
        baseline_result = None
        baseline_time = None

        for strategy in self.strategies:
            times = []
            strategy_results = []

            for i in range(iterations):
                # Create fresh copy of inputs for each iteration
                # (in case strategy modifies tried_list, etc.)
                iter_inputs = OptimizerInput(
                    price_set=inputs.price_set.copy(),
                    price_links=inputs.price_links.copy(),
                    window_index=inputs.window_index.copy(),
                    all_prices=inputs.all_prices.copy(),
                    charge_windows=[w.copy() for w in inputs.charge_windows],
                    export_windows=[w.copy() for w in inputs.export_windows],
                    charge_limits=inputs.charge_limits.copy(),
                    export_limits=inputs.export_limits.copy(),
                    rates_import=inputs.rates_import.copy(),
                    rates_export=inputs.rates_export.copy(),
                    pv_forecast=inputs.pv_forecast.copy(),
                    load_forecast=inputs.load_forecast.copy(),
                    battery=inputs.battery,
                    config=inputs.config,
                    region_start=inputs.region_start,
                    region_end=inputs.region_end,
                    end_record=inputs.end_record,
                    tried_list={},
                    levels_score={},
                )

                start = time.perf_counter()
                result = strategy.optimize(iter_inputs, fn)
                elapsed = time.perf_counter() - start
                result.elapsed_seconds = elapsed
                result.strategy_name = strategy.name

                times.append(elapsed)
                strategy_results.append(result)

            # Use first result for validation
            result = strategy_results[0]
            mean_time = sum(times) / len(times)

            # Validate against baseline
            valid = True
            if validate_results and baseline_result is not None:
                valid = self._validate_result(result, baseline_result)

            # First strategy becomes baseline
            if baseline_result is None:
                baseline_result = result
                baseline_time = mean_time

            speedup = baseline_time / mean_time if mean_time > 0 else 0

            results[strategy.name] = {
                'result': result,
                'times': times,
                'mean_time': mean_time,
                'min_time': min(times),
                'max_time': max(times),
                'valid': valid,
                'speedup': speedup,
                'iterations': result.iterations,
                'predictions_run': result.predictions_run,
            }

        return results

    def _validate_result(
        self,
        result: OptimizerResult,
        baseline: OptimizerResult,
        metric_tolerance: float = 0.01,  # 1% tolerance
    ) -> bool:
        """Check if result is within tolerance of baseline"""
        if baseline.best_metric == 0:
            return abs(result.best_metric) < 0.01

        metric_diff = abs(result.best_metric - baseline.best_metric) / abs(baseline.best_metric)
        return metric_diff <= metric_tolerance

    def print_results(self, results: Dict[str, Dict], fixture_name: str = ""):
        """Pretty print benchmark results"""
        print("\n" + "=" * 100)
        if fixture_name:
            print(f"OPTIMIZER BENCHMARK RESULTS - {fixture_name}")
        else:
            print("OPTIMIZER BENCHMARK RESULTS")
        print("=" * 100)

        # Table header
        header = f"{'Strategy':<25} {'Mean Time':>12} {'Min Time':>12} {'Speedup':>10} {'Valid':>8} {'Iters':>10} {'Preds':>10} {'Metric':>12}"
        print(header)
        print("-" * 100)

        for name, data in sorted(results.items(), key=lambda x: x[1]['mean_time']):
            result = data['result']
            valid_mark = '\u2713' if data['valid'] else '\u2717'
            print(
                f"{name:<25} "
                f"{data['mean_time']:>10.3f}s "
                f"{data['min_time']:>10.3f}s "
                f"{data['speedup']:>9.1f}x "
                f"{valid_mark:>8} "
                f"{data['iterations']:>10} "
                f"{data['predictions_run']:>10} "
                f"{result.best_metric:>12.2f}"
            )

        print("=" * 100)

    def run_all_fixtures(
        self,
        iterations: int = 3,
        predict_fn: Optional[PredictFn] = None,
    ) -> Dict[str, Dict[str, Dict]]:
        """
        Run benchmarks against all available fixtures.

        Returns:
            Dict of {fixture_name: {strategy_name: results}}
        """
        all_results = {}
        for fixture in self.list_fixtures():
            print(f"\nRunning benchmark: {fixture}")
            try:
                results = self.run_benchmark(fixture, iterations=iterations, predict_fn=predict_fn)
                all_results[fixture] = results
                self.print_results(results, fixture)
            except Exception as e:
                print(f"  Error: {e}")
                all_results[fixture] = {"error": str(e)}

        return all_results


def create_mock_predict_fn() -> PredictFn:
    """
    Create a mock prediction function for testing.

    Returns a simple cost calculation based on charge limits and rates.
    """
    def mock_predict(
        charge_limits: List[float],
        charge_windows: List[Dict],
        export_windows: List[Dict],
        export_limits: List[float],
        pv10: bool,
        end_record: int,
        step: int,
    ):
        # Simplified cost calculation for testing
        import random

        # Simulate some work
        cost = sum(charge_limits) * 0.1
        cost += random.uniform(-0.01, 0.01)  # Add small noise

        # Return tuple matching real prediction output:
        # (metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min,
        #  soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g)
        return (
            cost,           # metric
            10.0,           # import_kwh_battery
            20.0,           # import_kwh_house
            5.0,            # export_kwh
            10.0,           # soc_min
            50.0,           # soc
            0,              # soc_min_minute
            0.5,            # battery_cycle
            0.0,            # metric_keep
            0.0,            # final_iboost
            0.0,            # final_carbon_g
        )

    return mock_predict
