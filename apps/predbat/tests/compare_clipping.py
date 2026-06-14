# -----------------------------------------------------------------------------
# Clipping Approach Comparison: Cloud-Model Penalty vs Baseline
#
# Runs identical scenarios with and without clipping_peak_enable and compares
# the optimizer metrics, final SoC, and clipping amounts.
#
# Usage: python apps\predbat\tests\compare_clipping.py
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

import sys
import os
import math
import time

# Add parent dirs to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.test_infra import reset_rates, reset_inverter, simple_scenario, Prediction


# ---------------------------------------------------------------------------
# Scenario definitions: hand-crafted clipping challenge cases
# ---------------------------------------------------------------------------

SCENARIOS = [
    {
        "name": "Clear Sky Peak Clipping",
        "description": "7kWp panels, 5kW inverter limit, sunny day. Peak PV exceeds inverter limit. Battery starts at 80%.",
        "pv_kw": 7.0,                    # peak generation in kW (exceeds inverter limit)
        "load_kw": 0.5,                  # constant household load
        "inverter_limit_kw": 5.0,        # AC inverter limit
        "export_limit_kw": 10.0,         # no export restriction
        "battery_size_kwh": 9.5,         # typical UK hybrid battery
        "battery_soc_percent": 80,       # already mostly charged from overnight
        "battery_rate_kw": 3.0,          # charge/discharge rate
        "import_rate_p": 25.0,           # standard import rate
        "export_rate_p": 15.0,           # SEG export rate
        "charge_target_percent": 100,    # optimizer wants to charge to 100%
        "hybrid": True,                  # DC-coupled hybrid inverter
    },
    {
        "name": "Cloudy Day - Intermittent Peaks",
        "description": "6kWp panels, 3.6kW inverter. Cloud model would show spikes. Battery starts at 90% from overnight charge.",
        "pv_kw": 6.0,
        "load_kw": 0.3,
        "inverter_limit_kw": 3.6,
        "export_limit_kw": 10.0,
        "battery_size_kwh": 9.5,
        "battery_soc_percent": 90,
        "battery_rate_kw": 2.6,
        "import_rate_p": 25.0,
        "export_rate_p": 15.0,
        "charge_target_percent": 100,
        "hybrid": True,
    },
    {
        "name": "Export Limited System",
        "description": "12kWp array, 5kW DNO export limit. Large system hitting export cap, not inverter limit.",
        "pv_kw": 4.0,                    # after inverter, 4kW net
        "load_kw": 0.5,
        "inverter_limit_kw": 10.0,       # large inverter
        "export_limit_kw": 3.0,          # tight DNO export limit
        "battery_size_kwh": 13.5,
        "battery_soc_percent": 75,
        "battery_rate_kw": 3.6,
        "import_rate_p": 25.0,
        "export_rate_p": 12.0,
        "charge_target_percent": 100,
        "hybrid": False,                 # AC-coupled
    },
    {
        "name": "Negative Import Rates",
        "description": "Peak PV + negative import rates. Should the optimizer charge despite clipping risk?",
        "pv_kw": 6.0,
        "load_kw": 0.5,
        "inverter_limit_kw": 5.0,
        "export_limit_kw": 10.0,
        "battery_size_kwh": 9.5,
        "battery_soc_percent": 50,
        "battery_rate_kw": 3.0,
        "import_rate_p": -5.0,           # NEGATIVE import rate (paid to consume)
        "export_rate_p": 4.0,            # low export rate
        "charge_target_percent": 100,
        "hybrid": True,
    },
    {
        "name": "Small Battery, Big Array",
        "description": "4.8kWh battery with 10kW array and 5kW inverter. Battery fills quickly, lots of clipping.",
        "pv_kw": 10.0,
        "load_kw": 0.8,
        "inverter_limit_kw": 5.0,
        "export_limit_kw": 10.0,
        "battery_size_kwh": 4.8,
        "battery_soc_percent": 60,
        "battery_rate_kw": 2.4,
        "import_rate_p": 30.0,
        "export_rate_p": 15.0,
        "charge_target_percent": 100,
        "hybrid": True,
    },
    {
        "name": "No Clipping Risk (Control)",
        "description": "3kW panels with 5kW inverter. PV never exceeds limit. Penalty should not affect plan.",
        "pv_kw": 3.0,
        "load_kw": 0.5,
        "inverter_limit_kw": 5.0,
        "export_limit_kw": 10.0,
        "battery_size_kwh": 9.5,
        "battery_soc_percent": 50,
        "battery_rate_kw": 3.0,
        "import_rate_p": 25.0,
        "export_rate_p": 15.0,
        "charge_target_percent": 100,
        "hybrid": True,
    },
]


def run_comparison(my_predbat):
    """Run all clipping scenarios with and without the penalty, and compare."""

    print("\n" + "=" * 100)
    print("CLIPPING APPROACH COMPARISON: Cloud-Model Penalty vs Baseline")
    print("=" * 100)

    results = []

    for scenario in SCENARIOS:
        print("\n" + "-" * 80)
        print("Scenario: {}".format(scenario["name"]))
        print("  {}".format(scenario["description"]))
        print("-" * 80)

        # Common setup
        reset_inverter(my_predbat)
        reset_rates(my_predbat, scenario["import_rate_p"], scenario["export_rate_p"])

        common_args = {
            "pv_amount": scenario["pv_kw"],
            "load_amount": scenario["load_kw"],
            "inverter_limit": scenario["inverter_limit_kw"],
            "export_limit": scenario["export_limit_kw"],
            "battery_size": scenario["battery_size_kwh"],
            "battery_soc": scenario["battery_size_kwh"] * scenario["battery_soc_percent"] / 100.0,
            "battery_rate_max_charge": scenario["battery_rate_kw"],
            "with_battery": True,
            "hybrid": scenario["hybrid"],
            "charge": scenario["battery_size_kwh"] * scenario["charge_target_percent"] / 100.0,
            "save": "best",
            "return_prediction_handle": True,
            "ignore_failed": True,
            "quiet": True,
        }

        # --- Run A: Baseline (no clipping penalty) ---
        t_start = time.perf_counter()
        failed_a, pred_a = simple_scenario(
            scenario["name"] + " [baseline]",
            my_predbat,
            assert_final_metric=0,
            assert_final_soc=0,
            clipping_peak_enable=False,
            **common_args,
        )
        time_a = time.perf_counter() - t_start

        # --- Run B: With clipping penalty ---
        reset_inverter(my_predbat)
        reset_rates(my_predbat, scenario["import_rate_p"], scenario["export_rate_p"])

        t_start = time.perf_counter()
        failed_b, pred_b = simple_scenario(
            scenario["name"] + " [penalty]",
            my_predbat,
            assert_final_metric=0,
            assert_final_soc=0,
            clipping_peak_enable=True,
            clipping_cost_weight=1.0,
            clipping_peak_amplification=1.0,
            **common_args,
        )
        time_b = time.perf_counter() - t_start

        # Extract results
        metric_a = round(pred_a.predict_metric_best[max(pred_a.predict_metric_best.keys())] / 100.0, 4) if pred_a.predict_metric_best else 0
        metric_b = round(pred_b.predict_metric_best[max(pred_b.predict_metric_best.keys())] / 100.0, 4) if pred_b.predict_metric_best else 0
        soc_a = round(list(pred_a.predict_soc.values())[-1], 2) if pred_a.predict_soc else 0
        soc_b = round(list(pred_b.predict_soc.values())[-1], 2) if pred_b.predict_soc else 0
        clipped_a = round(pred_a.predict_clipped_best[max(pred_a.predict_clipped_best.keys())], 2) if pred_a.predict_clipped_best else 0
        clipped_b = round(pred_b.predict_clipped_best[max(pred_b.predict_clipped_best.keys())], 2) if pred_b.predict_clipped_best else 0

        result = {
            "name": scenario["name"],
            "metric_baseline": metric_a,
            "metric_penalty": metric_b,
            "metric_diff": round(metric_b - metric_a, 4),
            "soc_baseline": soc_a,
            "soc_penalty": soc_b,
            "clipped_baseline": clipped_a,
            "clipped_penalty": clipped_b,
            "time_baseline": round(time_a, 3),
            "time_penalty": round(time_b, 3),
        }
        results.append(result)

        print("  {:>20}: {:>10} {:>10} {:>10}".format("", "Baseline", "Penalty", "Diff"))
        print("  {:>20}: {:>10.4f} {:>10.4f} {:>+10.4f}".format("Metric (£)", metric_a, metric_b, metric_b - metric_a))
        print("  {:>20}: {:>10.2f} {:>10.2f} {:>+10.2f}".format("Final SoC (kWh)", soc_a, soc_b, soc_b - soc_a))
        print("  {:>20}: {:>10.2f} {:>10.2f} {:>+10.2f}".format("Clipped (kWh)", clipped_a, clipped_b, clipped_b - clipped_a))
        print("  {:>20}: {:>10.3f}s {:>9.3f}s {:>+9.3f}s".format("Runtime", time_a, time_b, time_b - time_a))

    # Summary table
    print("\n" + "=" * 100)
    print("SUMMARY TABLE")
    print("=" * 100)
    header = "{:<35} {:>10} {:>10} {:>10} {:>10} {:>10} {:>10}".format(
        "Scenario", "Met_Base", "Met_Pen", "Met_Diff", "Clip_Base", "Clip_Pen", "Time_Pen"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print("{:<35} {:>10.4f} {:>10.4f} {:>+10.4f} {:>10.2f} {:>10.2f} {:>9.3f}s".format(
            r["name"][:35],
            r["metric_baseline"],
            r["metric_penalty"],
            r["metric_diff"],
            r["clipped_baseline"],
            r["clipped_penalty"],
            r["time_penalty"],
        ))

    # Analysis
    print("\n" + "=" * 100)
    print("ANALYSIS")
    print("=" * 100)
    penalty_scenarios = [r for r in results if r["metric_diff"] > 0.001]
    neutral_scenarios = [r for r in results if abs(r["metric_diff"]) <= 0.001]

    print("Scenarios where penalty changes metric: {} / {}".format(len(penalty_scenarios), len(results)))
    print("Scenarios where penalty is neutral:     {} / {}".format(len(neutral_scenarios), len(results)))

    if penalty_scenarios:
        avg_diff = sum(r["metric_diff"] for r in penalty_scenarios) / len(penalty_scenarios)
        print("Average metric increase (penalty scenarios): £{:+.4f}".format(avg_diff))
        print("  -> This represents the clipping cost the optimizer now accounts for.")
        print("  -> In a full optimizer run, this would cause it to reduce charge targets.")

    avg_time_diff = sum(r["time_penalty"] - r["time_baseline"] for r in results) / len(results)
    print("\nAverage compute time overhead: {:+.3f}s".format(avg_time_diff))

    return results


if __name__ == "__main__":
    # Bootstrap a minimal predbat instance for testing
    # Run from project root: python apps\predbat\tests\compare_clipping.py
    from unit_test import create_predbat
    my_predbat = create_predbat()
    run_comparison(my_predbat)
