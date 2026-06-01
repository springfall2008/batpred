# Solar Clipping Research Log

## Detailed Analysis (24 May 2026)

### 1. Simulation-Level Clipping (apps/predbat/prediction.py)
The Prediction.run_prediction method is the heart of the energy simulation. It processes data in 1-minute or 5-minute steps.

#### Inverter Capacity Clipping
- **get_total_inverted**: Calculates the total AC power going through the inverter.
    - If inverter_hybrid is true, it includes pv_ac / inverter_loss.
    - It correctly accounts for efficiency losses (inverter_loss).
- **Main Loop**:
    - Predbat calculates total_inverted based on battery_draw and pv_dc/pv_ac.
    - It then checks: if total_inverted > inverter_limit:.
    - If exceeded, it calculates over_limit = total_inverted - inverter_limit.
    - **Resolution Strategy**:
        1.  First, it tries to reduce battery_draw (discharge) to stay within the limit.
        2.  If battery_draw is already 0 (or is charging) and the limit is still exceeded (due to high PV), it clips the **Solar PV** output.

#### Export Limit Clipping
- Checks if the net export exceeds the grid export_limit.
- Clips pv_ac if the export limit is hit. This represents a software/regulatory cap on how much can be sent to the grid.

### 2. Planner Optimization (apps/predbat/plan.py)
The planner runs multiple prediction passes to optimize the schedule.

#### clip_export_slots / clip_charge_slots
These functions refine windows (when Predbat tells the inverter to charge/discharge).
- They examine each window's predicted SOC (State of Charge) from a prior simulation run.
- **Safety Adjustments**: windows are "clipped" (disabled) if the battery state already satisfies the goal.

## Proposal: Native Clipping Buffer Feature

### 1. The Core Pain Point
Currently, Predbat optimizes for cost by frequently charging the battery to 100% during cheap overnight rates. On days with scattered clouds, solar generation can spike above the inverter's AC capacity. If the battery is full, excess DC energy cannot be stored or exported, resulting in lost energy (clipping).

### 2. Native Solution: The "Clipping Buffer"
To natively solve this, Predbat proactively reserves a dynamic buffer in the battery strictly for expected clipped solar energy.

#### Algorithmic Changes
1. **Pre-Calculation (The "Hole"):** Before optimization, calculate expected_clipping_kwh by integrating the area of the safety forecast above the inverter limit.
2. **Dynamic Grid Charge Limit:** Grid charging windows are capped at `soc_max - expected_clipping_kwh`.
3. **Solar Absorption:** The battery is allowed to charge to 100% using purely PV energy, absorbing the spikes.

## Final Implementation (24 May 2026)

The Native Clipping Buffer has been implemented with the following components:

### 1. Multi-Forecast Solar Data
SolarAPI now fetches and processes five distinct solar forecast types:
- **Main Forecast (`pv_estimate`)**: Standard calibrated forecast.
- **Worst Case (`pv_estimate10`)**: 10th percentile.
- **Best Case (`pv_estimate90`)**: 90th percentile.
- **Clear Sky (`pv_clearsky`)**: Theoretical maximum from Solcast/Open-Meteo.
- **Historical Max (`pv_historical`)**: Scaled historical curve shape.

### 2. Clipping Buffer Calculation (`Plan.calculate_clipping_buffer`)
Calculates the "hole" (kWh) and the "clipping window" (start/end times) for the current day based on the user-selected forecast.

### 3. Proactive Reservation (`Prediction.run_prediction`)
The simulation engine enforces the buffer by capping grid charge windows that end before the clipping period.

### 4. Configuration Options
- `clipping_buffer_enable`: Master toggle.
- `clipping_buffer_forecast`: Selection of forecast source.
- `clipping_buffer_min_kwh`: Optional minimum buffer (fixed manual hole if = max).
- `clipping_buffer_max_kwh`: Optional cap on the reserved space.

### 5. UI and Visualization
- **Web Interface**: Added a new "Clipping" chart to the Predbat web UI.
- **Documentation**: New `docs/clipping.md` created with full reasoning.
- **Recommended Dashboard**: Provided HA YAML configuration.
