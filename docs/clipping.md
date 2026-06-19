# Automated Solar Clipping Mitigation

In solar setups where your peak PV generation exceeds your inverter's maximum AC limit (for example, a 6.5kW solar array connected to a 5kW inverter), the excess energy is "clipped" and permanently lost if your battery is already fully charged.

Historically, Predbat's standard planning lacked visibility into the precise magnitude of this expected clipped energy, meaning it could not preemptively create room in the battery to absorb the excess yield.

The **Clipping Buffer** feature introduces an intelligent mitigation layer that calculates the exact energy expected to be lost to inverter clipping, and automatically schedules preemptive battery exports to create the necessary buffer before the peak hits.

## Key Features

* **Dynamic Clipping Prediction**: Analyzes the raw PV forecast curve against the user's configured `inverter_limit` to calculate the exact volume of energy (in kWh) that will exceed AC capacity.
* **Preemptive Export Scheduling**: Automatically forces battery export windows prior to peak generation periods, guaranteeing the battery has precisely enough headroom to soak up the excess energy.
* **Clear-Sky Cloud Modeling**: Includes an optional integration for clear-sky data (e.g., `ha_solcast_clearsky`) and an auto-tuning amplification factor. This generates a theoretical maximum generation envelope, ensuring the clipping buffer is sized safely even when standard forecasts fluctuate due to unpredictable cloud cover.

## Configuration Settings

These settings can be found in the Home Assistant Predbat Configuration panel, directly beneath the Cloud Model settings.

* **`clipping_buffer_enable`**: The master switch. Turns the entire clipping buffer feature on or off.
* **`clipping_clearsky_source`**: Determines where Predbat gets its clear-sky (theoretical maximum) data. Options include `auto`, `ha_solcast_clearsky`, `solcast_api`, and `openmeteo`. This piggybacks on your existing Predbat/HA API integrations, requiring no extra YAML configuration.
* **`clipping_use_clearsky_peaks`**: If enabled, Predbat uses the selected Clear-Sky source to size the buffer, rather than just the standard forecast. This is highly recommended to protect against cloud-edge spikes.
* **`clipping_auto_tune`**: Automatically learns the scaling difference between your standard solar forecast and your hardware limits based on past clipping behavior, saving a tracking multiplier to `clipping_auto_tune.json`.
* **`clipping_amplification`**: A manual multiplier (e.g., `1.5x`) to force the standard solar forecast higher to simulate a sunny spike. This is only utilized if Auto-Tune is disabled.
* **`clipping_cost_weight`**: An internal optimizer multiplier (default `1.0`). If Predbat isn't dumping the battery aggressively enough before a peak, increasing this number adds a harsher financial penalty for clipping, forcing the optimizer to prioritize creating headroom.
* **`clipping_limit_override`**: Manually define your inverter's AC ceiling in Watts (e.g., `5000W`). If left at `0`, Predbat auto-detects it from your inverter entity.

### Manual Overrides

If you do not want to use the dynamic cloud-based tracking, you can manually force a static buffer size and time window:

* **`clipping_buffer_max_kwh`**: Manually force the maximum size of the clipping buffer (e.g. `3.0` kWh).
* **`clipping_buffer_start_time`**: The start time for the fixed buffer window.
* **`clipping_buffer_end_time`**: The end time for the fixed buffer window.
