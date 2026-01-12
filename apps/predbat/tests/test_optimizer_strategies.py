# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Table-Driven Tests for Optimizer Strategies
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

import pytest
import time
from pathlib import Path
from typing import List

# Import optimizer components
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from optimizer.base import OptimizerResult, OptimizerStrategy
from optimizer.benchmark import OptimizerBenchmark, create_mock_predict_fn
from optimizer.strategies import (
    BaselineStrategy,
    CoarseToFineStrategy,
    NumpyVectorizedStrategy,
    NumbaJITStrategy,
)

# All strategies to test - add new strategies here as they're implemented
def get_all_strategies() -> List[OptimizerStrategy]:
    """Return all registered optimizer strategies"""
    strategies = [
        BaselineStrategy(),
        CoarseToFineStrategy(),
        NumpyVectorizedStrategy(),
    ]
    # Add Numba strategy if available (requires numba package)
    try:
        strategy = NumbaJITStrategy(warmup=True)
        strategies.append(strategy)
    except Exception:
        pass
    return strategies


# Test fixtures
FIXTURES = [
    "standard_tariff_simple",
    "iog_moderate",
]


@pytest.fixture
def benchmark_harness():
    """Create benchmark harness with mock prediction"""
    fixtures_dir = Path(__file__).parent / "fixtures" / "optimizer"
    benchmark = OptimizerBenchmark(data_dir=fixtures_dir)
    benchmark.set_predict_fn(create_mock_predict_fn())
    return benchmark


@pytest.fixture(params=get_all_strategies(), ids=lambda s: s.name)
def strategy(request):
    """Parametrized fixture for all strategies"""
    return request.param


@pytest.fixture(params=FIXTURES)
def fixture_name(request):
    """Parametrized fixture for all test fixtures"""
    return request.param


class TestOptimizerBase:
    """Basic tests for optimizer infrastructure"""

    def test_load_fixture(self, benchmark_harness):
        """Can load test fixtures"""
        inputs = benchmark_harness.load_fixture("standard_tariff_simple")
        assert inputs is not None
        assert len(inputs.price_set) > 0
        assert len(inputs.charge_windows) > 0

    def test_fixture_serialization(self, benchmark_harness, tmp_path):
        """Fixtures can be saved and loaded"""
        inputs = benchmark_harness.load_fixture("standard_tariff_simple")

        # Save to temp location
        temp_benchmark = OptimizerBenchmark(data_dir=tmp_path)
        temp_benchmark.save_fixture("test_save", inputs)

        # Reload
        loaded = temp_benchmark.load_fixture("test_save")

        assert loaded.price_set == inputs.price_set
        assert len(loaded.charge_windows) == len(inputs.charge_windows)

    def test_list_fixtures(self, benchmark_harness):
        """Can list available fixtures"""
        fixtures = benchmark_harness.list_fixtures()
        assert "standard_tariff_simple" in fixtures
        assert "iog_moderate" in fixtures


class TestOptimizerStrategies:
    """Table-driven tests for all optimizer strategies"""

    def test_produces_result(self, strategy, fixture_name, benchmark_harness):
        """Each strategy produces a valid result structure"""
        inputs = benchmark_harness.load_fixture(fixture_name)
        predict_fn = create_mock_predict_fn()

        result = strategy.optimize(inputs, predict_fn)

        assert isinstance(result, OptimizerResult)
        assert result.best_metric is not None
        assert result.iterations > 0
        assert len(result.charge_limits) == len(inputs.charge_limits)
        assert len(result.export_limits) == len(inputs.export_limits)

    def test_result_within_tolerance(self, strategy, fixture_name, benchmark_harness):
        """Strategy results are within tolerance of baseline"""
        if strategy.name == "baseline":
            pytest.skip("Baseline is the reference")

        inputs = benchmark_harness.load_fixture(fixture_name)
        predict_fn = create_mock_predict_fn()

        # Get baseline result
        baseline = BaselineStrategy()
        baseline_result = baseline.optimize(inputs, predict_fn)

        # Get strategy result
        result = strategy.optimize(inputs, predict_fn)

        # Within 1% of baseline metric (or both near zero)
        if abs(baseline_result.best_metric) < 0.01:
            assert abs(result.best_metric) < 0.1
        else:
            diff = abs(result.best_metric - baseline_result.best_metric) / abs(baseline_result.best_metric)
            assert diff < 0.01, f"Metric {result.best_metric} differs from baseline {baseline_result.best_metric} by {diff*100:.2f}%"

    def test_not_significantly_slower(self, strategy, fixture_name, benchmark_harness):
        """Strategies should not be significantly slower than baseline"""
        if strategy.name == "baseline":
            pytest.skip("Baseline is the reference")

        inputs = benchmark_harness.load_fixture(fixture_name)
        predict_fn = create_mock_predict_fn()

        # Time baseline
        baseline = BaselineStrategy()
        start = time.perf_counter()
        baseline.optimize(inputs, predict_fn)
        baseline_time = time.perf_counter() - start

        # Time strategy
        start = time.perf_counter()
        strategy.optimize(inputs, predict_fn)
        strategy_time = time.perf_counter() - start

        # Should not be more than 20% slower (allowing for variance)
        if baseline_time > 0.01:  # Only check if baseline takes meaningful time
            slowdown = strategy_time / baseline_time
            assert slowdown < 1.2, f"Strategy is {slowdown:.2f}x slower than baseline"


class TestBaselineStrategy:
    """Specific tests for baseline strategy"""

    def test_iterates_all_prices(self, benchmark_harness):
        """Baseline iterates through all price levels"""
        inputs = benchmark_harness.load_fixture("standard_tariff_simple")
        predict_fn = create_mock_predict_fn()

        strategy = BaselineStrategy()
        result = strategy.optimize(inputs, predict_fn)

        # With 2 price levels and 576 combos each (minus deduplication),
        # should have significant iterations
        assert result.iterations > 100

    def test_iog_has_more_iterations(self, benchmark_harness):
        """IOG fixture requires more iterations than simple tariff"""
        predict_fn = create_mock_predict_fn()
        strategy = BaselineStrategy()

        simple_inputs = benchmark_harness.load_fixture("standard_tariff_simple")
        simple_result = strategy.optimize(simple_inputs, predict_fn)

        iog_inputs = benchmark_harness.load_fixture("iog_moderate")
        iog_result = strategy.optimize(iog_inputs, predict_fn)

        # IOG should have more iterations due to more price levels
        assert iog_result.iterations > simple_result.iterations


class TestBenchmarkHarness:
    """Tests for the benchmark harness itself"""

    def test_run_benchmark(self, benchmark_harness):
        """Can run benchmark with all strategies"""
        benchmark_harness.register(BaselineStrategy())

        results = benchmark_harness.run_benchmark("standard_tariff_simple", iterations=1)

        assert "baseline" in results
        assert results["baseline"]["mean_time"] > 0
        assert results["baseline"]["result"].iterations > 0

    def test_validate_results(self, benchmark_harness):
        """Benchmark validates results against baseline"""
        benchmark_harness.register(BaselineStrategy())

        results = benchmark_harness.run_benchmark(
            "standard_tariff_simple",
            iterations=1,
            validate_results=True
        )

        # Baseline should validate against itself
        assert results["baseline"]["valid"] is True

    def test_speedup_calculation(self, benchmark_harness):
        """Speedup is calculated relative to baseline"""
        benchmark_harness.register(BaselineStrategy())

        results = benchmark_harness.run_benchmark("standard_tariff_simple", iterations=1)

        # Baseline speedup should be 1.0
        assert abs(results["baseline"]["speedup"] - 1.0) < 0.01


# Performance benchmark tests (marked for optional execution)
@pytest.mark.benchmark
class TestPerformanceBenchmarks:
    """Performance benchmarks - run with pytest -m benchmark"""

    def test_standard_tariff_performance(self, benchmark_harness):
        """Benchmark standard tariff performance"""
        benchmark_harness.register(BaselineStrategy())

        results = benchmark_harness.run_benchmark("standard_tariff_simple", iterations=3)
        benchmark_harness.print_results(results, "standard_tariff_simple")

        # Should complete in reasonable time (< 1 second for simple case)
        assert results["baseline"]["mean_time"] < 1.0

    def test_iog_moderate_performance(self, benchmark_harness):
        """Benchmark IOG moderate performance"""
        benchmark_harness.register(BaselineStrategy())

        results = benchmark_harness.run_benchmark("iog_moderate", iterations=3)
        benchmark_harness.print_results(results, "iog_moderate")

        # IOG will be slower, but should still be bounded
        assert results["baseline"]["mean_time"] < 30.0


def run_shootout():
    """
    Run a full shootout of all strategies against all fixtures.

    Usage: python -c "from tests.test_optimizer_strategies import run_shootout; run_shootout()"
    """
    fixtures_dir = Path(__file__).parent / "fixtures" / "optimizer"
    benchmark = OptimizerBenchmark(data_dir=fixtures_dir)
    benchmark.set_predict_fn(create_mock_predict_fn())

    # Register all strategies
    for strategy in get_all_strategies():
        benchmark.register(strategy)

    # Run benchmarks
    print("\n" + "=" * 80)
    print("OPTIMIZER SHOOTOUT")
    print("=" * 80)

    all_results = benchmark.run_all_fixtures(iterations=3)

    # Summary
    print("\n" + "=" * 80)
    print("SHOOTOUT SUMMARY")
    print("=" * 80)

    for fixture_name, results in all_results.items():
        if "error" in results:
            print(f"\n{fixture_name}: ERROR - {results['error']}")
            continue

        fastest = min(results.items(), key=lambda x: x[1]['mean_time'])
        print(f"\n{fixture_name}:")
        print(f"  Fastest: {fastest[0]} ({fastest[1]['mean_time']:.3f}s)")
        if 'baseline' in results and fastest[0] != 'baseline':
            speedup = results['baseline']['mean_time'] / fastest[1]['mean_time']
            print(f"  Speedup vs baseline: {speedup:.1f}x")


if __name__ == "__main__":
    run_shootout()
