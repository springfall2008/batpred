# Customisation

These are configuration items that you can modify to fit your needs, you can configure these in Home Assistant directly.
Changing the items in apps.yml will have no effect.

See [Displaying output data](output-data.md#displayng-output-data)
for information on how to view and edit these entities within
Home Assistant.

## Performance related

By default Predbat controls the inverter and updates the plan every 5 minutes, this can however use a lot of CPU power especially on more complex tariffs like Agile when run on lower power machines such as Raspberry PIs and some thin clients.

You can tweak **input_number.predbat_calculate_plan_every** to reduce the frequency of replanning while keeping the inverter control in the 5 minute slots. E.g. a value of 10 or 15 minutes should also give good results.

You can tune the number of windows that are optimised for using **'input_number.predbat_calculate_max_windows** however a minimum of 32 is recommended, and values of between 40 and 64 will give best results for Agile tariffs.

If you have performance problems leave **switch.predbat_calculate_second_pass** turned off as it's quite CPU intensive and provides very little improvement for most systems.

## Battery loss options

**battery_loss** accounts for energy lost charging the battery, default 0.05 is 5%

**battery_loss_discharge** accounts for energy lost discharging the battery, default 0.05 is 5%

**inverter_loss** accounts for energy loss during going from DC to AC or AC to DC, default is 0% for legacy reasons but please adjust.

**inverter_hybrid** When True you have a hybrid inverter so no inverter losses for DC charging. When false you have inverter losses as it's AC coupled battery.

**input_number.metric_battery_cycle**  Sets the cost in pence per kWh of using your battery for charging and discharging. Higher numbers will reduce battery cycles at the expensive of higher energy costs. Figures of around 1p-5p are recommended, the default is 0.

## Scaling and weight options

**battery_rate_max_scaling** adjusts your maximum charge/discharge rate from that reported by GivTCP
e.g. a value of 1.1 would simulate a 10% faster charge/discharge than reported by the inverter

**load_scaling** is a Scaling factor applied to historical load, tune up if you want to be more pessimistic on future consumption
Use 1.0 to use exactly previous load data (1.1 would add 10% to load)

**pv_scaling** is a scaling factor applied to pv data, tune down if you want to be more pessimistic on PV production vs Solcast
Use 1.0 to use exactly the Solcast data (0.9 would remove 10% from forecast)

**pv_metric10_weight** is the weighting given to the 10% PV scenario. Use 0.0 to disable this.
A value of 0.1 assumes that 1:10 times we get the 10% scenario and hence to count this in the metric benefit/cost.
A value of 0.15 is recommended.

## Historical load data

The historical load data is taken from the load sensor as configured in apps.yaml and the days are selected using **days_previous** and weighted using ***days_previous_weight** in the same configuration file

**switch.predbat_load_filter_modal** when enabled will automatically discard the lowest daily consumption day from the list of days to use (provided you have more than 1 day selected in days_previous). This can be used to ignore a single low usage day in your average calculation.

## Car charging hold options

Car charging hold is a feature where you try to filter out previous car charging from your historical data so that future predictions are more accurate.

When **car_charging_hold** is enabled loads of above the power threshold **car_charging_threshold** then you are assumed to be charging the car and **car_charging_rate** will be subtracted from the historical load data.

For more accurate results can you use an incrementing energy sensor set with **car_charging_energy** in the apps.yml then historical data will be subtracted from the load data instead.

**car_charging_energy_scale** Is used to scale the **car_charging_energy** sensor, the default units are kWh so if you had a sensor in watts you might use 0.001 instead.

**car_charging_rate** sets the rate your car is assumed to charge at, but will be pulled automatically from Octopus Energy plugin if enabled

**car_charging_loss** gives the amount of energy lost when charging the car (load in the home vs energy added to the battery). A good setting is 0.08 which is 8%.

## Car charging plan options

Car charging planning - is only used if Intelligent Octopus isn't enabled and car_charging_planned is connected correctly.

This feature allows Predbat to create a plan for when you car will charge, but you will have to create an automation to trigger your car to charge using **binary_sensor.predbat_car_charging_slot** if you want it to match the plan.

**car_charging_plan_time** Is set to the time you expect your car to be fully charged by
**car_charging_plan_smart** When enabled allows Predbat to allocated car charging slots to the cheapest times, when disabled all low rate slots will be used in time order.

**switch.predbat_octopus_intelligent_charging** when true enables the Intelligent Octopus charging feature which will make Predbat create a car charging plan which is taken from the Intelligent Octopus plan
you must have set **octopus_intelligent_slot** sensor in apps.yml to enable this feature.

## Calculation options

**switch.predbat_calculate_best** When enables tells Predbat to work out the best battery SOC % based on cost, when disabled no scenarios apart from the default settings are computed.
This must be enabled to get all the 'best' sensors.

**switch.predbat_calculate_best_charge** If set to False then charge windows will not be calculated and the default inverter settings are used, when True predbat will decide the charge window automatically.

**switch.predbat_calculate_best_discharge** If set to False then discharge windows will not be calculated, when True they will be calculated. Default is True.

**switch.predbat_calculate_discharge_first** When True discharge takes priority over charging (to maximise profit on export), when false charging is optimised first. Default to True

**switch.predbat_calculate_discharge_oncharge** When True calculated discharge slots will disable or move charge slots, allowing them to intermix. When False discharge slots will never be placed into charge slots.

## Battery margins and metrics options

**best_soc margin** is added to the final SOC estimate (in kWh) to set the battery charge level (pushes it up). Recommended to leave this as 0.

**best_soc_min** sets the minimum charge level (in kWh) for charging during each slot and the minimum discharge level also (set to 0 if you want to skip some slots)

**best_soc_max** sets the maximum charge level (in kWh) for charging during each slot. A value of 0 disables this feature.

**best_soc_keep** is minimum battery level to try to keep above during the whole period of the simulation time, soft constraint only (use min for hard constraint). It's usually good to have this above 0 to allow some margin in case you use more energy than planned between charge slots.

**best_soc_step** is the accuracy to calculate the charge levels to, higher values make calculations quicker, smaller ones will take longer (recommended 0.5 or 0.25)

**combine_charge_slots** controls if charge slots of > 30 minutes can be combined. When disabled they will be split up, increasing run times but potentially more accurate for planning.
Not recommended to set to False when best_soc_min set to True or all slots will be kept. The default is enabled (True)

**combine_discharge_slots** Controls if discharge slots of > 30 minute can be combined. When disabled they will be split up, increasing run times but potentially more accurate for planning.

**combine_mixed_rates** When True allows mixed rates to be combined into a single average rate charge/discharge window (e.g. Agile windows)
A better plan is achieved leaving this false but it can speed up run time to have it True.

**metric_min_improvement** sets the minimum cost improvement that it's worth lowering the battery SOC % for.
If it's 0 then this is disabled and the battery will be charged less if it's cost neutral.
If you use **pv_metric10_weight** then you probably don't need to enable this as the 10% forecast does the same thing better
Do not use if you have multiple charge windows in a given period as it won't lead to good results (e.g. Agile)
You could even go to something like -0.1 to say you would charge less even if it cost up to 0.1p more (best used with metric10)

**metric_min_improvement_discharge** Sets the minimum cost improvement it's worth discharging for. A value of 0 or 1 is generally good.

**rate_low_threshold** sets the threshold below average rates as the minimum to consider for a charge window, 0.8 = 80% of average rate
If you set this too low you might not get enough charge slots. If it's too high you might get too many in the 24-hour period which makes optimisation harder. You can set this to 0 for automatic rate selection when combined with setting **calculate_max_windows** to a lower number e.g. 24 or 32.

**rate_low_match_export** When enabled consider import rates that are lower than the highest export rate (minus any battery losses).
This is if you want to be really aggressive about importing just to export, default is False (recommended).

**rate_high_threshold** Sets the threshold above average rates as to the minimum export rate to consider exporting for - 1.2 = 20% above average rate
If you set this too high you might not get any export slots. If it's too low you might get too many in the 24-hour period. You can set this to 0 for automatic rate selection when combined with setting **calculate_max_windows** to a lower number e.g. 24 or 32.

**calculate_max_windows** - Maximum number of charge and discharge windows, the default is 32. If you system has performance issues you might want to cut this down to something between 16 and 24 to only consider the highest value exports and cheapest imports. The maximum usable value would be 96 which is 48 hours split into 30 minute slots and maybe required for best results with a fixed export rate.

**metric_future_rate_offset_import** Sets an offset to apply to future import energy rates that are not yet published, best used for variable rate tariffs such as Agile import where the rates are not published until 4pm. If you set this to a positive value then Predbat will assume unpublished import rates are higher by the given amount.

**metric_future_rate_offset_export** Sets an offset to apply to future export energy rates that are not yet published, best used for variable rate tariffs such as Agile export where the rates are not published until 4pm. If you set this to a negative value then Predbat will assume unpublished export rates are lower by the given amount.

**switch.predbat_calculate_inday_adjustment** When enabled will calculate the difference between today's actual load and today's predicated load and adjust the rest of the days usage prediction accordingly. A scale factor can be set with **input_number.predbat_metric_inday_adjust_damping** to either scale up or down the impact of the in-day adjustment (lower numbers scale down its impact). The in-day adjustment factor can be see in **predbat.load_inday_adjustment** and charted with the In Day Adjustment chart (template can be found in the charts template in Github).

## Inverter control options

**set_state_notify** enables mobile notification about changes to the Predbat state (e.g. Charge, Discharge etc)

**set_charge_window** When enabled the next charge window will be automatically configured based on the incoming rates
Only works if the charging time window has been enabled and import rates are configured with the rates_import or using Octopus import
Will also automatically disable charging if not required and re-enable it when required.
If you turn this off later check that 'GivTCP Enable Charge Schedule' is turned back on.

**set_window_minutes** defines how many minutes before the charge window we should program it (do not set above 30 if you are using Agile or similar)

**set_window_notify** enables mobile notifications about changes to the charge window

**set_discharge_window** When enabled automatically discharge (forced export) for export during high rate periods.

**set_charge_freeze** When enabled Predbat can hold the current battery level by disabling discharge but not charge the battery, thus drawing the home from the grid. When disabled Predbat can only charge the battery to a target level. The default is enabled.

**set_discharge_freeze** When enabled if a discharge reaches the expected battery level for the discharge slot then charging of the battery will be frozen (charge rate 0) and all non-self consumed solar is exported. When this is disabled the inverter will return to ECO mode. The default is enabled.

**set_discharge_freeze_only** When enabled forced discharge is prevented, but discharge freeze can be used (if enabled) to export excess solar rather than charging the battery. This is useful with tariffs that pay you for solar exports but don't allow forced export (brown energy).

**set_discharge_notify** enables mobile notifications about changes to the discharge window.

**set_soc_enable** When enable automatically set the battery SOC charge amount a defined number of minutes before charging starts
NOTE: it maybe set more than once if things change

If you have **inverter_hybrid** set to False then if **inverter_soc_reset** is set to True then the target SOC % will be reset to 100% outside a charge window. This may be required for AIO inverter to ensure it charges from solar.

**set_soc_minutes** defines how many minutes before the charge window we should program it (do not set above 30 if you are using Agile or similar)
**set_soc_notify** enables mobile notifications about changes to the charge %

**set_reserve_enable** When True the reserve % will be reprogrammed during a charging window or discharging window to match the target SOC/discharge % in order
to prevent discharge and then reset back to minimum % outside the window. Set the set_reserve_min to your minimum reserve % which is often 4%.
The feature applies with **set_soc_enable** or **set_discharge_window** is True

**set_reserve_min** Defines the reserve percentage to reset the reserve to when not in use, a value of 4 is the minimum and recommended to make use of the full battery

When **set_reserve_hold** is True then if the current charge % is above the target charging will be disabled and the reserve will be used to hold the level (Good for gen3 workaround)

## IBoost model options

IBoost model, when enabled with **iboost_enable** tries to model excess solar energy being used to heat hot water (or similar). The predicted output from the IBoost model is returned in **iboost_best**.

**iboost_max_energy** Sets the max energy sets the number of kwh that iBoost can consume during a day before turning off - default 3kWh

**iboost_max_power** Sets the maximum power in watts to consume - default 2400

**iboost_min_power** Sets the minimum power in watts to consume - default 500

**iboost_min_soc** sets the minimum home battery soc % to enable iboost on, default 0

You will see **predbat.iboost_today** entity which tracks the estimated amount consumed during the day, and resets at night

If you have an incrementing Sensor that tracks IBoost energy usage then you should set **iboost_energy_today** sensor in apps.yaml to point to it and optionally set **iboost_energy_scaling** if the sensor isn't in kWh.

## Debug

**debug_enable** when on prints lots of debug, leave off by default
