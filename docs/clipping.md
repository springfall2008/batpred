# Solar Clipping Buffer

The **Clipping Buffer** is a native feature in Predbat designed to prevent solar energy loss (clipping) on days where PV generation exceeds the inverter's AC capacity.

## The Problem
Predbat's cost-optimization engine often plans to fill your home battery to 100% during cheap overnight grid rates. While this is economically sound for grid imports, it creates a physical problem during the day:
1.  **Inverter AC Limit**: Hybrid inverters can only export or provide house load up to their AC limit (e.g., 5kW).
2.  **DC Spikes**: On sunny or scattered-cloud days, DC solar generation can spike significantly above this limit (e.g., 7kW).
3.  **No Storage**: If the battery is already full from grid charging, there is nowhere for the excess 2kW of DC power to go. It cannot be stored, and it cannot be exported because the inverter is already at its AC limit.
4.  **Lost Energy**: This energy is simply "clipped" and lost.

## The Native Solution
Instead of using external Home Assistant automations to force battery levels, Predbat natively reserves a **clipping buffer** in the battery specifically for this excess solar.

### 1. Multi-Forecast Safety Margin
Standard solar forecasts are often "smoothed" or averaged, meaning they might miss the short, high-intensity spikes that cause clipping. Predbat solves this by allowing you to choose from five different forecast types for the buffer calculation:
- **Main Forecast (`pv_estimate`)**: The standard calibrated forecast.
- **Worst Case (`pv_estimate10`)**: 10th percentile (conservative).
- **Best Case (`pv_estimate90`)**: 90th percentile (likely for sunny days).
- **Clear Sky (`pv_clearsky`)**: **Recommended.** Theoretical maximum based on site orientation. This provides a "physics-based" upper bound that won't change with the weather.
- **Historical Max (`pv_historical`)**: Calculated based on your site's absolute peak production in the last 7 days. Best for adapting to local environmental factors (like shade or seasonal variations).

### 2. Buffer Calculation
Predbat calculates the `clipping_buffer_kwh` by integrating the area of your chosen safety forecast that exceeds your **Effective Clipping Limit**.
*   **Dynamic Sizing**: If the forecast shows 2 hours of 1kW clipping, Predbat reserves a 2kWh buffer.
*   **Proactive Risk Management**: Predbat uses a two-stage approach to manage unpredictable spikes on high-generation days:
    *   **The Trigger (`risk_threshold`)**: If the forecast comes close to your limit (default >80% of capacity), Predbat marks the period as a "risk window".
    *   **The Payload (`clipping_buffer_safety_margin`)**: Once a risk is detected, Predbat proactively reserves an additional safety margin (default 5% of the total forecasted solar in that window), even if hard clipping isn't explicitly forecast. This ensures there is always a buffer available for un-forecasted cloud-edge spikes.
*   **48-Hour Planning**: The buffer is aware of Predbat's full 48-hour planning window. If clipping is forecast for tomorrow, Predbat will automatically begin reserving space (by capping overnight grid charging) at midnight of that day.
*   **Dynamic Decay**: As your solar panels produce energy and the peak of the day passes, the reserved buffer dynamically shrinks. This ensures that you don't unnecessarily limit your battery usage in the late afternoon or evening after the risk of clipping has passed.

### 3. Proactive Reservation (Grid-Charge Capping)
The core of the implementation is inside the simulation engine. Predbat enforces the buffer by restricting **Grid Charging** only:
*   Any grid charge window is proactively capped at `soc_max - Buffer_Needed`.
*   **PV Priority**: Solar generation *below* the AC limit is prioritized for load or export, ensuring you get the full financial benefit of your solar while keeping the buffer empty for spikes.
*   **Active Mitigation**: During the clipping window, any solar production *above* the AC limit is diverted into the reserved buffer.
*   **Buffer Protection**: The battery is always allowed to charge to 100% using solar power, but only the "clipping" portion is allowed to fill the reserved buffer space during the peak.

### Advanced Configuration

| Setting | Description |
| ------- | ----------- |
| `clipping_buffer_enable` | Master toggle to enable/disable the feature. |
| `clipping_buffer_forecast` | Which solar curve to use for calculating the buffer. **Recommended: `pv_clearsky`** for maximum safety. |
| `clipping_buffer_min_kwh` | The minimum floor for the buffer. Setting this equal to `max_kwh` creates a **fixed manual buffer**. |
| `clipping_buffer_max_kwh` | A hard cap on the buffer size to prevent leaving the battery too empty on over-optimistic forecasts. |
| `clipping_buffer_safety_margin` | **(Default: `0.05`)** The percentage of total window solar to reserve as a safety margin when a risk is detected. |
| `clipping_buffer_risk_threshold` | **(Default: `0.80`)** How close the forecast must get to your limit (as a factor) to trigger the safety margin logic. |
| `clipping_buffer_can_discharge` | **Optional (Default: `Cost Optimal`).** Controls how aggressively Predbat creates the buffer.<br>• `None`: Only stops grid charging.<br>• `Cost Optimal`: **(Recommended)** Automatically chooses to discharge early if the financial value of the saved solar (valued at the current export rate) outweighs the costs of discharging now.<br>• `Always`: Forces a discharge to ensure the buffer is physically available before clipping begins. |
| `clipping_buffer_fallback_window` | **Optional (Default: `2.0`).** The duration (in hours) of the clipping window centered around solar noon for days when the sun does not naturally exceed your hardware limits (e.g. winter). Set to `0` to disable the buffer entirely on those days. |
| `clipping_buffer_window_offset` | **Optional (Default: `15`).** The safety padding (in minutes) added to the start and end of the auto-detected clipping window. |
| `clipping_buffer_start_time` | **Optional.** Manually override the start of the clipping window (e.g., `11:00:00`). |
| `clipping_buffer_end_time` | **Optional.** Manually override the end of the clipping window (e.g., `15:00:00`). |
| `clipping_buffer_limit_override` | **Optional.** Manually set the power threshold (in Watts) above which clipping is considered active. |

### How the Clipping Limit is Determined
Predbat automatically calculates the **Effective Clipping Limit** by choosing the *most restrictive* constraint on your system:
1.  **Manual Override:** If `clipping_buffer_limit_override` is set, this is used exclusively.
2.  **DNO Export Limit:** If your `export_limit` is configured and is lower than your hardware capacity, Predbat will reserve space to prevent export throttling.
3.  **Battery Charge Capacity:** For AC-coupled systems, Predbat limits the "absorbable" PV to the sum of your battery charge rate, house load, and grid export limit.
4.  **Physical Inverter AC Capacity:** The maximum AC power your inverters can convert from DC solar.
5.  **PV AC Capacity:** For non-hybrid systems, the rated limit of your separate PV inverters (e.g., microinverters).

### Understanding 'Cost Optimal' Mode
The default `Cost Optimal` mode allows the Clipping Buffer to work seamlessly with Predbat's primary goal: saving you money.
In this mode, Predbat treats any "Clipped Solar" as a direct financial loss equivalent to your current export rate. This allows the optimizer to make a smart decision: *"Is it cheaper to force-export some battery power now (at a low rate) to ensure I can capture this high-value solar spike later?"*

If you want the buffer to be created regardless of immediate profit (e.g., to maximize self-consumption at all costs), use the `Always` mode.

You can check the active constraint in Home Assistant via the `clipping_mode` attribute on the `sensor.predbat_clipping_status` entity.


## Visualization
You can monitor the buffer in two ways:
1.  **Predbat Web UI**: Use the new **Clipping** chart to see your actual PV power overlaid with your chosen safety forecast. The chart now includes a **Clipping Remaining** line showing how the reservation decays throughout the day.
2.  **Home Assistant**: Add the `sensor.predbat_clipping_buffer_kwh` to your dashboard to see exactly how much space is being reserved in real-time.
