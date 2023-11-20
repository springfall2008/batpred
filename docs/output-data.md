# Output data

## Displaying output data

Each config item has an input_number or switch associated with it, you can find an auto generated dashboard for your configuration in your HA configuration area under the filename **predbat_dashboard.yaml**

You can also create a card using 'dynamic-entities-card.yaml' for a dynamically created list of entities for predbat which groups the entities by type and is collapsed by default to prevent screen clutter. Requires lovelace-collapsable-cards ([https://github.com/RossMcMillan92/lovelace-collapsable-cards](https://github.com/RossMcMillan92/lovelace-collapsable-cards)) and lovelace-auto-entities ([https://github.com/thomasloven/lovelace-auto-entities](https://github.com/thomasloven/lovelace-auto-entities)) to be installed via HACS as well as the stock vertical stack card. Credit @DJBenson for the code.

## Basic status

- predbat.status - Gives the current status & errors and logs any adjustments made to your inverter

## Baseline data

What your battery is expected to do with no changes made by Predbat:

- predbat.battery_hours_left - The number of hours left until your home battery is predicated to run out (stops at the maximum prediction time)
- predbat.charge_limit - The current charge limit used for the scenario in %
- predbat.charge_limit_kw - The current charge limit used for the scenario in kwH
- predbat.duration - The duration of the prediction maximum in hours
- predbat.load_energy - Predicted load energy in kWh
- predbat.pv_energy - Predicted PV energy in kWh
- predbat.export_energy - Predicted export energy in kWh
- predbat.import_energy - Predicted import energy in kWh
- predbat.import_energy_battery - Predicted import energy to charge your home battery in kWh
- predbat.import_energy_house - Predicted import energy not provided by your home battery (flat battery or above maximum discharge rate
- predbat.soc_kw - Predicted state of charge (in kWh) at the end of the prediction, not very useful in itself, but holds all minute by minute prediction data (in attributes) which can be charted with Apex Charts (or similar)
- predbat.soc_min_kwh - The minimum battery level during the time period in kWh
- predbat.metric - Predicted cost metric for the next simulated period (in pence). Also contains data for charting cost in attributes.
- predbat.battery_power - Predicted battery power per minute, for charting
- predbat.battery_cycle - Predicted battery cycle in kWh (total kWh processed)
- predbat.pv_power - Predicted PV power per minute, for charting
- predbat.grid_power - Predicted Grid power per minute, for charting
- predbat.car_soc - Predicted car battery %

## PV 10% baseline data

The calculated baseline results under PV 10% scenario:

- predbat.soc_kw_base10 - As soc_kw but using the 10% solar forecast, also holds minute by minute data (in attributes) to be charted
- predbat.base10_pv_energy - Predicted PV 10% energy in kWh
- predbat.base10_metric - Predicted cost for PV 10%
- predbat.base10_export_energy- Predicted export energy for PV 10%
- predbat.base10_load_energy - Predicted load energy for PV 10%
- predbat.base10_import_energy- Predicted import energy for PV 10%

## Best

When calculate_best is enabled a second set of entities are created for the simulation based on the best battery charge percentage:

- predbat.best_battery_hours_left - Number of hours left under best plan
- predbat.best_export_energy - Predicted exports under best plan
- predbat_best_import_energy - Predicted imports under best plan
- predbat_best_load - Predicted best load energy
- predbat.best_pv_energy - Predicted Best PV energy in kWh
- predbat_best_import_energy_battery - Predicted imports to the battery under best SOC setting
- predbat_best_import_energy_house - Predicted imports to the house under best SOC setting
- predbat_soc_kw_best - Predicted best final state of charge (in kWh), holds minute by minute prediction data (in attributes) to be charted
- predbat.soc_kw_best_h1 - Single data point for the predicted state of charge in 1 hours time (useful for calibration charts, predicted vs actual)
- predbat.soc_kw_best_h8 - Single data point for the predicted state of charge in 8 hours time (useful for calibration charts, predicted vs actual)
- predbat.soc_kw_best_h12 - Single data point for hte predicted state of charge in 12 hours time (useful for calibration charts, predicted vs actual)
- predbat_best_metric - The predicted cost if the proposed SOC % charge target is selected. Also contains data for charting cost in attributes.
- predbat.best_charge_limit - Predicted best battery charge limit in percent
- predbat.best_charge_limit_kw - Predicted best battery charge limit in kwH
- predbat.best_discharge_limit - Predicted best battery discharge limit in percent (will be 0% when discharging or 100% when not)
- predbat.best_discharge_limit_kw - Predicted best battery discharge limit in kwH
- predbat.battery_power_best  - Predicted best battery power per minute, for charting
- predbat.battery_cycle_best - Predicted best battery cycle in kWh (total kWh processed)
- predbat.pv_power_best - Predicted best PV power per minute, for charting
- predbat.grid_power - Predicted best Grid power per minute, for charting
- predbat.car_soc_best - Predicated car battery % in  best plan
- predbat.iboost_best - Gives the predicted energy going into the iBoost - for charter
- input_number.iboost_today - Gives the amount of energy modelled into the diverter today, resets at 11:30pm each night. Increments in the day.

## Best PV 10%

The calculated best results under PV 10% scenario:

- predbat.soc_kw_best10 - As soc_kw_best but using the 10% solar forecast, also holds minute by minute data (in attributes) to be charted
- predbat.best10_pv_energy - Predicted best PV 10% energy in kWh
- predbat.best10_metric - Predicted best cost for PV 10%
- predbat.best10_export_energy- Predicted best export energy for PV 10%
- predbat.best10_load_energy - Predicted best load energy for PV 10%
- predbat.best10_import_energy- Predicted best import energy for PV 10%

## Energy rate data

### Low import rate entities

- predbat.low_rate_cost - The lowest import rate cost in P
- predbat.low_rate_start - Start time of the next low import rate
- predbat.low_rate_end - End time of the next low import rate
- predbat.low_rate_cost_2, predbat.low_rate_start_2, predbat.low_rate_end_2 - The following low import rate slot
- binary_sensor.predbat_low_rate_slot - A sensor that indicates which there is a low energy rate slot active

### High export rate entities

- predbat.high_export_rate_cost - The highest rate cost in P
- predbat.high_export_rate_start - Start time of the next high export rate
- predbat.high_export_rate_end - End time of the next high export rate
- predbat.high_export_rate_cost_2, predbat.high_export_rate_start_2, predbat.high_export_rate_end_2 - The following high export rate slot
- binary_sensor.predbat_high_export_rate_slot - A sensor that indicates which there is a high export rate slot active

### Other rate entities

- predbat.rates - The current energy rates in P (also can be charted)
- predbat.rates_export - The current energy export rates in P (also be be charted)
- predbat.cost_today - The total cost of energy so far today (since midnight)
- predbat.car_soc - The expected charge level of your car at the end of the simulation. Can also be charted.
- predbat.car_soc_best - The expected charge level of your car at the end of the simulation using the proposed SOC%/Window. Can also be charted.

## Car data

- binary_sensor.predbat_car_charging_slot - A binary sensor suggesting when to charge your car (if the car planning is enabled)

Example data out:

![image](https://github.com/springfall2008/batpred/assets/48591903/5c73cd6e-3110-4ecd-af42-7e6d156af4b2)
