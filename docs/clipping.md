# Solar Clipping Buffer

The **Clipping Buffer** is a native feature in Predbat designed to prevent solar energy loss (clipping) on days where PV generation exceeds the inverter's AC capacity.

## The Problem
Predbat's cost-optimization engine often plans to fill your home battery to 100% during cheap overnight grid rates. While this is economically sound for grid imports, it creates a physical problem during the day:
1.  **Inverter AC Limit**: Hybrid inverters can only export or provide house load up to their AC limit (e.g., 5kW).
2.  **DC Spikes**: On sunny or scattered-cloud days, DC solar generation can spike significantly above this limit (e.g., 7kW).
3.  **No Storage**: If the battery is already full from grid charging, there is nowhere for the excess 2kW of DC power to go. It cannot be stored, and it cannot be exported because the inverter is already at its AC limit.
4.  **Lost Energy**: This energy is simply "clipped" and lost.

## The Native Solution
Instead of using external Home Assistant automations to force battery levels, Predbat natively reserves a "hole" in the battery specifically for this excess solar.

### 1. Multi-Forecast Safety Margin
Standard solar forecasts are often "smoothed" or averaged, meaning they might miss the short, high-intensity spikes that cause clipping. Predbat solves this by allowing you to choose from five different forecast types for the clipping calculation:
- **Main Forecast (`pv_estimate`)**: The standard calibrated forecast.
- **Worst Case (`pv_estimate10`)**: 10th percentile (conservative).
- **Best Case (`pv_estimate90`)**: 90th percentile (likely for sunny days).
- **Clear Sky (`clearsky`)**: **Recommended.** Theoretical maximum based on site orientation. This provides a "physics-based" upper bound that won't change with the weather.
- **Historical Max (`historical`)**: Calculated based on your site's absolute peak production in the last 7 days. Best for adapting to local environmental factors (like shade or seasonal variations).

### 2. The "Hole" Calculation
Predbat calculates the `clipping_buffer_kwh` by integrating the area of your chosen safety forecast that exceeds your `pv_ac_limit`. 
*   **Dynamic Sizing**: If the forecast shows 2 hours of 1kW clipping, Predbat reserves a 2kWh hole.
*   **Time Window**: It identifies the specific start and end times of the expected clipping (e.g., 11:00 to 14:00).

### 3. Proactive Reservation (Grid-Charge Capping)
The core of the implementation is inside the simulation engine. Predbat enforces the buffer by restricting **Grid Charging** only:
*   Any grid charge window planned to end before the clipping window is proactively capped at `soc_max - clipping_buffer_kwh`.
*   **PV is never capped**: The battery is always allowed to charge to 100% using solar power. 
*   **Reasoning**: By holding back the grid charge, we create physical space in the battery. During the daytime, that space is filled by the DC spikes that would have otherwise been clipped.

### 4. Dynamic Release
As the day progresses and the peak solar period passes, the calculated buffer size naturally reduces to zero. This ensures that the battery is allowed to reach 100% (via solar) by the end of the day, and normal grid-charging behavior can resume for the evening if rates drop.

### Advanced Configuration

| Setting | Description |
| ------- | ----------- |
| `clipping_buffer_enable` | Master toggle to enable/disable the feature. |
| `clipping_buffer_forecast` | Which solar curve to use for calculating the buffer. **Recommended: `clearsky`** for maximum safety. |
| `clipping_buffer_min_kwh` | The minimum floor for the buffer. Setting this equal to `max_kwh` creates a **fixed manual hole**. |
| `clipping_buffer_max_kwh` | A hard cap on the buffer size to prevent leaving the battery too empty on over-optimistic forecasts. |
| `clipping_buffer_start_time` | **Optional.** Manually override the start of the clipping window (e.g., `11:00:00`). |
| `clipping_buffer_end_time` | **Optional.** Manually override the end of the clipping window (e.g., `15:00:00`). |
| `clipping_buffer_limit_override` | **Optional.** Manually set the power threshold (in Watts) above which clipping is considered active. |

### How the Clipping Limit is Determined
Predbat automatically calculates the **Effective Clipping Limit** by choosing the *most restrictive* constraint on your system:
1.  **Manual Override:** If `clipping_buffer_limit_override` is set, this is used exclusively.
2.  **DNO Export Limit:** If your `export_limit` is configured and is lower than your hardware capacity, Predbat will reserve space to prevent export throttling.
3.  **Physical Inverter AC Capacity:** The maximum AC power your inverters can convert from DC solar.
4.  **PV AC Capacity:** For non-hybrid systems, the rated limit of your separate PV inverters (e.g., microinverters).

You can check the active constraint in Home Assistant via the `clipping_mode` attribute on the `sensor.predbat_clipping_status` entity.


## Visualization
You can monitor the buffer in two ways:
1.  **Predbat Web UI**: Use the new **Clipping** chart to see your actual PV power overlaid with your chosen safety forecast.
2.  **Home Assistant**: Add the `sensor.predbat_clipping_buffer_kwh` to your dashboard to see exactly how much space is being reserved in real-time.
