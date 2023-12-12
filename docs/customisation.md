# Customisation

These are configuration items that you can modify to fit your needs, you can configure these in Home Assistant directly.
Changing the items in apps.yaml will have no effect.

See [Displaying output data](output-data.md#displayng-output-data)
for information on how to view and edit these entities within
Home Assistant.

## Predbat mode

The mode that Predbat operates in will change the operation, this can be configured with **select.predbat_mode** drop down menu as follows:

- Monitor
- Control SOC Only
- Control charge
- Control charge & discharge

If **switch.predbat_set_read_only** is True then the plan will be updated but the inverter controls will not be used, this is useful to pause
Predbat operation while an automation takes over.

_CAUTION: If you use Read only mode while the inverter is in a particular state e.g. with discharge disable, you will need to return it to
the desired state yourself_

### Predbat Monitor mode

In **monitor** mode Predbat will not control charging or discharging, inverter balancing will take place if enabled, the plan will show
just what is expected based on the current inverter configuration alone.

### Predbat Control SOC Only mode

In **Control SOC Only** mode Predbat will adjust the target charge percentage (SOC target) according to the Best plan, but the charge
window will not be modified.  This can be useful if you just have one fixed
charge slot per day and you only want Predbat to control the percentage.

_CAUTION: If the charge window is disabled then no charging will take place._

### Predbat Control Charge mode

In **Control Charge** mode Predbat will set the charge times and charge percentages according to the Best plan, charging can be enabled and
disabled by Predbat.

### Predbat Control Charge & Discharge mode

In **Control Charge and Discharge** mode Predbat will set both charge and discharge times and control charge and discharge percentages.

If you have set the **switch.predbat_set_discharge_freeze_only** to True then forced export won't occur but Predbat can force the export
of solar power to the grid when desired.

## Expert mode

Predbat has a toggle switch called **switch.predbat_expert_mode** which is off by default for new installs (on
by default for upgraded installs). A lot of configuration items will not be available unless expert mode is enabled.
It's recommended for new users to start without expert mode and then maybe enable it later once you become more
confident with the tool.

## Performance related

By default Predbat controls the inverter and updates the plan every 5 minutes, this can however use a lot of CPU power
especially on more complex tariffs like Agile when run on lower power machines such as Raspberry PIs and some thin clients.

You can tweak **input_number.predbat_calculate_plan_every** (_expert mode_) to reduce the frequency of replanning while
keeping the inverter control in the 5 minute slots. E.g. a value of 10 or 15 minutes should also give good results.

If you have performance problems leave **switch.predbat_calculate_second_pass** (_expert mode_) turned Off as it's
quite CPU intensive and provides very little improvement for most systems.

If you have performance problems leave **switch.predbat_calculate_fast_plan** (_expert mode_) turned On to help
reduce your CPU load.

## Battery loss options

**input_number.battery_loss** accounts for energy lost charging the battery, default 0.05 is 5%

**input_number.battery_loss_discharge** accounts for energy lost discharging the battery, default 0.05 is 5%

**input_number.inverter_loss** accounts for energy loss during going from DC to AC or AC to DC, default is 0% for
legacy reasons but please adjust.

**switch.inverter_hybrid** When True you have a hybrid inverter so no inverter losses for DC charging. When false
you have inverter losses as it's AC coupled battery.

**input_number.metric_battery_cycle** (_expert mode_) Sets the cost in pence per kWh of using your battery for
charging and discharging. Higher numbers will reduce battery cycles at the expensive of higher energy costs.
Figures of around 1p-5p are recommended, the default is 0.

**input_number.predbat_metric_battery_value_scaling** (_expert mode_) Can be used to scale the value of the energy
in the battery at the end of the plan. The battery value is accounted for in the optimisations at the lowest future
import rate including charging and inverter losses. A value of 1.0 means no change to this, while lower than 1.0
means to value future battery levels less, greater than 1.0 will value it more (and hence hold more charge at the end of the plan).

## Scaling and weight options

**input_number.battery_rate_max_scaling** adjusts your maximum charge/discharge rate from that reported by GivTCP
e.g. a value of 1.1 would simulate a 10% faster charge/discharge than reported by the inverter

**input_number.load_scaling** is a Scaling factor applied to historical load, tune up if you want to be more pessimistic on future consumption
Use 1.0 to use exactly previous load data (1.1 would add 10% to load)

**input_number.pv_scaling** is a scaling factor applied to pv data, tune down if you want to be more pessimistic on PV production vs Solcast
Use 1.0 to use exactly the Solcast data (0.9 would remove 10% from forecast)

**input_number.pv_metric10_weight** is the weighting given to the 10% PV scenario. Use 0.0 to disable this.
A value of 0.1 assumes that 1:10 times we get the 10% scenario and hence to count this in the metric benefit/cost.
A value of 0.15 is recommended.

## Historical load data

The historical load data is taken from the load sensor as configured in apps.yaml and the days are selected
using **days_previous** and weighted using ***days_previous_weight** in the same configuration file

**switch.predbat_load_filter_modal** (_expert mode_) when enabled will automatically discard the lowest daily consumption
day from the list of days to use (provided you have more than 1 day selected in days_previous). This can be used to ignore
a single low usage day in your average calculation. By default is feature is enabled but can be disabled only in expert mode.

## Car charging hold options

Car charging hold is a feature where you try to filter out previous car charging from your historical data so that
future predictions are more accurate.

When **car_charging_hold** is enabled loads of above the power threshold **car_charging_threshold** then you are
assumed to be charging the car and **car_charging_rate** will be subtracted from the historical load data.

For more accurate results can you use an incrementing energy sensor set with **car_charging_energy** in the apps.yml
then historical data will be subtracted from the load data instead.

**car_charging_energy_scale** Is used to scale the **car_charging_energy** sensor, the default units are kWh so
if you had a sensor in watts you might use 0.001 instead.

**car_charging_rate** sets the rate your car is assumed to charge at, but will be pulled automatically from Octopus Energy plugin if enabled

**car_charging_loss** gives the amount of energy lost when charging the car (load in the home vs energy added to the battery). A good setting is 0.08 which is 8%.

## Car charging plan options

Car charging planning - is only used if Intelligent Octopus isn't enabled and car_charging_planned is connected correctly.

This feature allows Predbat to create a plan for when you car will charge, but you will have to create an automation
to trigger your car to charge using **binary_sensor.predbat_car_charging_slot** if you want it to match the plan.

**car_charging_plan_time** Is set to the time you expect your car to be fully charged by
**car_charging_plan_smart** When enabled allows Predbat to allocated car charging slots to the cheapest times,
when disabled all low rate slots will be used in time order.

**switch.predbat_octopus_intelligent_charging** when true enables the Intelligent Octopus charging feature
which will make Predbat create a car charging plan which is taken from the Intelligent Octopus plan
you must have set **octopus_intelligent_slot** sensor in apps.yaml to enable this feature.

If Octopus Intelligent Charging is enabled the switch **'switch.predbat_octopus_intelligent_ignore_unplugged'**
can be used to prevent Predbat from assuming the car will be charging when the car is unplugged. This will only work correctly
if **car_charging_planned** is set correctly in apps.yaml to detect your car being plugged in.

## Calculation options

See the Predbat mode setting as above for basic calculation options

**switch.predbat_calculate_fast_plan** (_expert mode_) When True (default) the plan is calculated with a limited number of
windows to make calculations faster. When False all windows are considered but planning will take a little longer but be
more accurate. The end result is unlikely to change in fast mode as the next 8 windows are always considered in the plan,
but the longer term plan will be less accurate.

**switch.predbat_calculate_discharge_oncharge** (_expert mode_) When True calculated discharge slots will
disable or move charge slots, allowing them to intermix. When False discharge slots will never be placed into charge slots.

**switch.predbat_calculate_tweak_plan** (_expert mode_) When True causes Predbat to perform a second pass optimisation
across the next 8 charge and discharge windows in time order.

This can help to slightly improve the plan for tariffs like Agile but can make it worse in some fixed rate tariffs which
you want to discharge late.

**switch.predbat_calculate_second_pass** (_expert mode_) When True causes Predbat to perform a second pass optimisation
across all the charge and discharge windows in time order.

NOTE: This feature is quite slow and so may need a higher performance machine

This can help to slightly improve the plan for tariffs like Agile but can make it worse in some fixed rate tariffs which
you want to discharge late.

## Battery margins and metrics options

**best_soc_keep** is minimum battery level to try to keep above during the whole period of the simulation time,
soft constraint only (use min for hard constraint). It's usually good to have this above 0 to allow some margin
in case you use more energy than planned between charge slots.

**best_soc_min** (_expert mode_) sets the minimum charge level (in kWh) for charging during each slot and the
minimum discharge level also (set to 0 if you want to skip some slots). If you set this non-zero you will need
to use the low rate threshold to control which slots you charge from or you may charge all the time.

**best_soc_max** (_expert mode_) sets the maximum charge level (in kWh) for charging during each slot.
A value of 0 disables this feature.

**combine_charge_slots** Controls if charge slots of > 30 minutes can be combined. When disabled they will be split up,
increasing run times but potentially more accurate for planning. Turn this off if you want to enable ad-hoc import
during long periods of higher rates but you wouldn't charge normally in that period (e.g. pre-charge at day rate before
a saving session). The default is enable (True)

**combine_discharge_slots** (_expert mode_) Controls if discharge slots of > 30 minute can be combined. When disabled
they will be split up, increasing run times but potentially more accurate for planning. The default is disabled (False)

**metric_min_improvement** (_expert mode_) sets the minimum cost improvement that it's worth lowering the battery SOC % for.
If it's 0 then this is disabled and the battery will be charged less if it's cost neutral.
If you use **pv_metric10_weight** then you probably don't need to enable this as the 10% forecast does the same thing better
Do not use if you have multiple charge windows in a given period as it won't lead to good results (e.g. Agile)
You could even go to something like -0.1 to say you would charge less even if it cost up to 0.1p more (best used with metric10)

**metric_min_improvement_discharge** (_expert mode_) Sets the minimum cost improvement it's worth discharging for.
A value of 0.1 is the default which prevents any marginal discharges. If you increase this value then discharges will become less common and shorter.

**rate_low_threshold** (_expert mode_) When 0 (default) this is automatic but can be overridden. When non zero it sets
the threshold below average rates as the minimum to consider for a charge window, 0.8 = 80% of average rate
If you set this too low you might not get enough charge slots. If it's too high you might get too many in the
24-hour period which makes optimisation harder.

**rate_high_threshold** (_expert mode_) When 0 (default) this is automatic but can be overridden. When non zero it sets
the threshold above average rates as to the minimum export rate to consider exporting for - 1.2 = 20% above average rate
If you set this too high you might not get any export slots. If it's too low you might get too many in the 24-hour period.

**metric_future_rate_offset_import** (_expert mode_) Sets an offset to apply to future import energy rates that are
not yet published, best used for variable rate tariffs such as Agile import where the rates are not published until 4pm.
If you set this to a positive value then Predbat will assume unpublished import rates are higher by the given amount.

**metric_future_rate_offset_export** (_expert mode_) Sets an offset to apply to future export energy rates that are
not yet published, best used for variable rate tariffs such as Agile export where the rates are not published until 4pm.
If you set this to a negative value then Predbat will assume unpublished export rates are lower by the given amount.

**switch.predbat_calculate_inday_adjustment** (_expert mode_) Enabled by default with damping of 0.95. When enabled will
calculate the difference between today's actual load and today's predicated load and adjust the rest of the days usage
prediction accordingly. A scale factor can be set with **input_number.predbat_metric_inday_adjust_damping** (_expert mode_)
to either scale up or down the impact of the in-day adjustment (lower numbers scale down its impact). The in-day adjustment
factor can be see in **predbat.load_inday_adjustment** and charted with the In Day Adjustment chart (template can be found
in the charts template in Github).

## Inverter control options

**set_status_notify** Enables mobile notification about changes to the Predbat state (e.g. Charge, Discharge etc). On by default.

**set_inverter_notify** Enables mobile notification about all changes to inverter registers (e.g. setting window, turning discharge on/off). Off by default.

**set_reserve_enable** (_expert_mode_) When enabled the reserve setting is used to hold the battery charge level
once it has been reached or to protect against discharging beyond the set limit. Enabled by default.

**set_charge_freeze** (_expert mode_) When enabled will allow Predbat to hold the current battery level while drawing
from the grid/solar as an alternative to charging. Enabled by default.

**set_discharge_freeze_only** (_expert mode_) When enabled forced discharge is prevented, but discharge freeze can be used
(if enabled) to export excess solar rather than charging the battery. This is useful with tariffs that pay you for
solar exports but don't allow forced export (brown energy).

If you have **inverter_hybrid** set to False then if **inverter_soc_reset** (_expert mode_) is set to True then the
target SOC % will be reset to 100% outside a charge window. This may be required for AIO inverter to ensure it charges from solar.

**set_reserve_min** Defines the reserve percentage to reset the reserve to when not in use, a value of 4 is the
minimum and recommended to make use of the full battery

**inverter_soc_reset**  (_expert mode_) When enabled the target SOC for the inverter(s) will be reset to 100%
when a charge slot is not active, this can be used to workaround some firmware issues where the SOC target is
used for solar charging as well as grid charging. When disabled the SOC % will not be changed after a charge slot.
This is disabled by default.

## IBoost model options

IBoost model, when enabled with **iboost_enable** tries to model excess solar energy being used to heat
hot water (or similar). The predicted output from the IBoost model is returned in **iboost_best**.

The following entities are only available when you turn on iboost enable:

**iboost_max_energy** Sets the max energy sets the number of kwh that iBoost can consume during a day before turning off - default 3kWh

**iboost_max_power** Sets the maximum power in watts to consume - default 2400

**iboost_min_power** Sets the minimum power in watts to consume - default 500

**iboost_min_soc** sets the minimum home battery soc % to enable iboost on, default 0

You will see **predbat.iboost_today** entity which tracks the estimated amount consumed during the day, and resets at night

If you have an incrementing Sensor that tracks IBoost energy usage then you should set **iboost_energy_today** sensor in
apps.yaml to point to it and optionally set **iboost_energy_scaling** if the sensor isn't in kWh.

## Debug

**debug_enable** when on prints lots of debug, leave off by default
