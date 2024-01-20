# Customisation

These are the Predbat configuration items in Home Assistant that you can modify to fit your needs, you can configure these in Home Assistant directly.

See [Displaying output data](output-data.md#displayng-output-data)
for information on how to view and edit these entities within
Home Assistant.

## Saving and restoring Predbat settings

The selector **select.predbat_saverestore** can be used to save you current settings to a yaml file (kept in /config/predbat_save/) and to
restore the settings from one of these files.

Selecting **save current** will cause the settings to be save to a date/time stamped file. You can rename this file yourself in the HA filesystem
to give it a more human readable name or delete it if you no longer want it. This is normally best done in the SSH window or via a Samba mount.

Selecting **restore default** will put all your settings back to the Predbat defaults.
Before the the restore the current settings will be saved as **previous.yaml** should you have made a mistake you can restore them quickly again.

Selecting any of the .yaml files you have created will restore your settings from this file.
Before the the restore the current settings will be saved as **previous.yaml** should you have made a mistake you can restore them quickly again.

![image](https://github.com/springfall2008/batpred/assets/48591903/209442c1-bd4d-4812-84e2-c5a81794bd1d)

## Predbat mode

The mode that Predbat operates in will change the operation, this can be configured with **select.predbat_mode** drop down menu as follows:

- Monitor
- Control SOC Only
- Control charge
- Control charge & discharge

If the **switch.predbat_set_read_only** is set to True then this prevents Predbat from making modifications to the inverter settings (regardless of the configuration).
Predbat will continue making and updating its prediction plan every 5 minutes, but no inverter changes will be made.
This is useful if you want to over-ride what predbat is planning to do (e.g. your own automation), or whilst you are learning how Predbat works prior to turning it on 'in anger'.

_NOTE: Changing the Predbat mode or the read only switch will cause Predbat to reset the inverter settings to default, this will disable
both charge and discharge, reset charge and discharge rates to full power and reset the reserve to the default setting_

![image](https://github.com/springfall2008/batpred/assets/48591903/43faa962-6b8a-495a-88f8-f762aa1d55b8)

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

If you have performance problems turn **switch.predbat_calculate_fast_plan** (_expert mode_) On to help
reduce your CPU load.

## Battery loss options

**input_number.battery_loss** accounts for energy lost charging the battery, default 0.05 is 5%

**input_number.battery_loss_discharge** accounts for energy lost discharging the battery, default 0.05 is 5%

**input_number.inverter_loss** accounts for energy loss during going from DC to AC or AC to DC, default is 0% for
legacy reasons but please adjust.

**switch.inverter_hybrid** When True you have a hybrid inverter so no inverter losses for DC charging. When false
you have inverter losses as it's AC coupled battery.

**input_number.metric_battery_cycle** (_expert mode_) Sets the cost in pence per kWh of using your battery for charging and discharging.
Higher numbers will reduce battery cycles at the expense of using higher energy costs.
Figures of around 1p-5p are recommended, the default is 1p per kWh.

**input_number.predbat_metric_battery_value_scaling** (_expert mode_) Can be used to scale the value of the energy
in the battery at the end of the plan. The battery value is accounted for in the optimisations at the lowest future
import rate including charging and inverter losses. A value of 1.0 means no change to this, while lower than 1.0
means to value future battery levels less, greater than 1.0 will value it more (and hence hold more charge at the end of the plan).

## Scaling and weight options

**input_number.battery_rate_max_scaling** adjusts your maximum charge/discharge rate from that reported by GivTCP
e.g. a value of 1.1 would simulate a 10% faster charge/discharge than reported by the inverter

**switch.predbat_battery_capacity_nominal** - When enabled Predbat uses the reported battery size from the Nominal field rather than from the normal GivTCP
reported size. If your battery size is reported wrongly maybe try turning this on and see if it helps.

**input_number.load_scaling** is a Scaling factor applied to historical load, tune up if you want to be more pessimistic on future consumption
Use 1.0 to use exactly previous load data (1.1 would add 10% to load)

**input_number.load_scaling10** is a Scaling factor applied to historical load only for the PV10% scenario (this is in addition to load_scaling).
This can  be used to make the 10% scenario take into account extra load usage and hence be more pessimistic while leaving the central
scenario unchanged. The default is 1.1 meaning an extra 10% load is added. This will only have an impact if the PV 10% weighting is non-zero.

**input_number.pv_scaling** is a scaling factor applied to PV data, tune down if you want to be more pessimistic on PV production vs Solcast
Use 1.0 to use exactly the Solcast data (0.9 would remove 10% from forecast)

**input_number.pv_metric10_weight** is the weighting given to the 10% PV scenario. Use 0.0 to disable this.
A value of 0.1 assumes that 1:10 times we get the 10% scenario and hence to count this in the metric benefit/cost.
A value of 0.15 is recommended.

## Historical load data

The historical load data is taken from the load sensor as configured in `apps.yaml` and the days are selected
using **days_previous** and weighted using ***days_previous_weight** in the same configuration file

**switch.predbat_load_filter_modal** (_expert mode_) when enabled will automatically discard the lowest daily consumption
day from the list of days to use (provided you have more than 1 day selected in days_previous). This can be used to ignore
a single low usage day in your average calculation. By default is feature is enabled but can be disabled only in expert mode.

## Car Charging

### Car charging hold options

Car charging hold is a feature where you try to filter out previous car charging from your historical data so that
future predictions are more accurate.

When **car_charging_hold** is enabled loads of above the power threshold **car_charging_threshold** then you are
assumed to be charging the car and **car_charging_rate** will be subtracted from the historical load data.

For more accurate results can you use an incrementing energy sensor set with **car_charging_energy** in the apps.yml
then historical data will be subtracted from the load data instead.

**car_charging_energy_scale** Is used to scale the **car_charging_energy** sensor, the default units are kWh so
if you had a sensor in watts you might use 0.001 instead.

- **input_number.car_charging_rate** - Set to the car's charging rate in kW per hour (normally 7.5 for 7.5kWh),
but will be pulled automatically from Octopus Energy integration if enabled for Octopus Intelligent.

**car_charging_loss** gives the amount of energy lost when charging the car (load in the home vs energy added to the battery). A good setting is 0.08 which is 8%.

### Car charging plan options

Car charging planning - is only used if Intelligent Octopus isn't enabled and car_charging_planned is connected correctly.

This feature allows Predbat to create a plan for when you car will charge, but you will have to create an automation
to trigger your car to charge using **binary_sensor.predbat_car_charging_slot** if you want it to match the plan.

- **car_charging_plan_time** - When using Predbat-led planning set this to the time you want the car to be charged by

- **car_charging_plan_smart** - When enabled (True) allows Predbat to allocate car charging slots to the cheapest times,
when disabled (False) all low rate slots will be used in time order.

**switch.predbat_octopus_intelligent_charging** when true enables the Intelligent Octopus charging feature
which will make Predbat create a car charging plan which is taken from the Intelligent Octopus plan
you must have set **octopus_intelligent_slot** sensor in apps.yaml to enable this feature.

If Octopus Intelligent Charging is enabled the switch **switch.predbat_octopus_intelligent_ignore_unplugged** (_expert mode_)
can be used to prevent Predbat from assuming the car will be charging when the car is unplugged. This will only work correctly
if **car_charging_planned** is set correctly in apps.yaml to detect your car being plugged in.

Control how your battery behaves during car charging:

- **car_charging_from_battery** - When True the car can drain the home battery, Predbat will manage the correct level of battery accordingly.
When False home battery discharge will be prevented when your car charges, all load from the car and home will be from the grid. This is achieved
by setting the discharge rate to 0 during car charging and to the maximum otherwise, hence if you turn this switch Off you won't be able to change
your discharge rate outside Predbat. The home battery can still charge from the grid/solar in either case. Only use this if Predbat knows your car
charging plan, e.g. you are using Intelligent Octopus or you use the car slots in Predbat to control your car charging.

If your car does not have an SOC sensor and you are not using Octopus Intelligent you can set **switch.predbat_car_charging_manual_soc**
to have Predbat create **input_number.predbat_car_charging_manual_soc_kwh** which will hold the cars current state of charge (soc)
in kWh. You will need to manually set this to the cars current charge level before charging, Predbat will increment it during
charging sessions but will not reset it automatically.

## Calculation options

See the Predbat mode setting as above for basic calculation options

**switch.predbat_calculate_regions** (_expert mode_) When True the a second pass of the initial thresholds is
calculated in 4 hour regions before forming the detailed plan. Is True by default but can be turned off in expert
mode.

**switch.predbat_calculate_fast_plan** (_expert mode_) When True the plan is calculated with a limited number of
windows to make calculations faster. When False (default) all windows are considered but planning will take a little
longer but be more accurate.
The end result is unlikely to change in fast mode as the next 8 windows are always considered in the plan, but the
longer term plan will be less accurate.

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

**input_number.best_soc_keep** is the minimum battery level in kWh that Predbat will to try to keep above during the whole period of the simulation time.
This is a soft constraint only so it is possible for your SoC to drop below this - use **input_number.best_soc_min** for hard SoC constraint that will always be maintained.
It's usually good to have best_soc_keep set to a value above 0 to allow some margin
in case you use more energy than planned between charge slots.

**input_number.best_soc_min** (_expert mode_) sets the minimum charge level (in kWh) for charging during each slot and the
minimum discharge level also (set to 0 if you want to skip some slots). If you set this to a non-zero value you will need
to use the low rate threshold to control which slots you charge from or you may charge all the time.

**input_number.best_soc_max** (_expert mode_) sets the maximum charge level (in kWh) for charging during each slot.
A value of 0 disables this feature.

**switch.combine_charge_slots** Controls if charge slots of > 30 minutes can be combined. When disabled they will be split up,
increasing run times but potentially more accurate for planning. Turn this off if you want to enable ad-hoc import
during long periods of higher rates but you wouldn't charge normally in that period (e.g. pre-charge at day rate before
a saving session). The default is enable (True)

**switch.combine_discharge_slots** (_expert mode_) Controls if discharge slots of > 30 minute can be combined. When disabled
they will be split up, increasing run times but potentially more accurate for planning. The default is disabled (False)

**input_number.metric_min_improvement** (_expert mode_) sets the minimum cost improvement that it's worth lowering the battery SOC % for.
If it's 0 then this is disabled and the battery will be charged less if it's cost neutral.
If you use **pv_metric10_weight** then you probably don't need to enable this as the 10% forecast does the same thing better
Do not use if you have multiple charge windows in a given period as it won't lead to good results (e.g. Agile)
You could even go to something like -0.1 to say you would charge less even if it cost up to 0.1p more (best used with metric10)

**input_number.metric_min_improvement_discharge** (_expert mode_) Sets the minimum pence cost improvement it's worth doing a forced discharge (and export) for.
A value of 0.1 is the default which prevents any marginal discharges. If you increase this value (e.g. you only want to discharge/forced export if definitely very profitable),
then discharges will become less common and shorter.

**input_number.rate_low_threshold** (_expert mode_) When set to 0 (the default) Predbat will automatically look at the future import rates in the plan
and determine the import rate threshold below which a slot will be considered to be a potential charging slot.<BR>
If rate_low_threshold is set to a non zero value this will set the threshold below future average import rates as the minimum to consider for a charge window,
e.g. setting to 0.8 = 80% of average rate.<BR>
If you set this too low you might not get enough charge slots. If it's too high you might get too many in the
24-hour period which makes optimisation harder.

**input_number.rate_high_threshold** (_expert mode_) When set to 0 (the default) Predbat will automatically look at the future export rates in the plan
and determine the threshold above which a slot can be considered a potential exporting slot.<BR>
If rate_high_threshold is set to a non zero value this will set the threshold above future average export rates as the minimum export rate to consider exporting for,
e.g. setting to 1.2 = 20% above average rate.<BR>
If you set this too high you might not get any export slots. If it's too low you might get too many in the 24-hour period.

**input_number.metric_future_rate_offset_import** (_expert mode_) Sets an offset to apply to future import energy rates that are
not yet published, best used for variable rate tariffs such as Agile import where the rates are not published until 4pm.
If you set this to a positive value then Predbat will assume unpublished import rates are higher by the given amount.

**input_number.metric_future_rate_offset_export** (_expert mode_) Sets an offset to apply to future export energy rates that are
not yet published, best used for variable rate tariffs such as Agile export where the rates are not published until 4pm.
If you set this to a negative value then Predbat will assume unpublished export rates are lower by the given amount.

**switch.predbat_calculate_inday_adjustment** (_expert mode_) Enabled by default with damping of 0.95. When enabled will
calculate the difference between today's actual load and today's predicated load and adjust the rest of the days usage
prediction accordingly. A scale factor can be set with **input_number.predbat_metric_inday_adjust_damping** (_expert mode_)
to either scale up or down the impact of the in-day adjustment (lower numbers scale down its impact). The in-day adjustment
factor can be see in **predbat.load_inday_adjustment** and charted with the In Day Adjustment chart (template can be found
in the charts template in Github).

## Inverter control options

**switch.set_status_notify** Enables mobile notification about changes to the Predbat state (e.g. Charge, Discharge etc). On by default.

**switch.set_inverter_notify** Enables mobile notification about all changes to inverter registers (e.g. setting window, turning discharge on/off).
Off by default.

**switch.predbat_set_charge_low_power** Enables low power charging mode where the max charge rate will be limited to the
lowest possible to meet the charge target. Only really effective for charge windows >30 minutes.
Off by default.

**switch.set_reserve_enable** (_expert_mode_) When enabled the reserve setting is used to hold the battery charge level
once it has been reached or to protect against discharging beyond the set limit. Enabled by default.

**switch.set_charge_freeze** (_expert mode_) When enabled will allow Predbat to hold the current battery level while drawing
from the grid/solar as an alternative to charging. Enabled by default.

**switch.set_discharge_freeze_only** (_expert mode_) When enabled forced discharge is prevented, but discharge freeze can be used
(if enabled) to export excess solar rather than charging the battery. This is useful with tariffs that pay you for
solar exports but don't allow forced export (brown energy).

If you have **switch.inverter_hybrid** set to False then if **switch.inverter_soc_reset** (_expert mode_) is set to True then the
target SOC % will be reset to 100% outside a charge window. This may be required for AIO inverter to ensure it charges from solar.

**input_number.set_reserve_min** Defines the reserve percentage to reset the reserve to when not in use, a value of 4 is the minimum and recommended to make use of the full battery.<BR>
If you want to pre-prepare the battery to retain extra charge in the event of a high likelihood of a grid power outage such as storms predicted,
you can increase set_reserve_min to 100%, and then change it back afterwards.<BR>
(Obviously this is only any use if your inverter is wired to act as an Emergency Power Supply or whole-home backup 'island mode' on the GivEnergy AIO).

**switch.inverter_soc_reset**  (_expert mode_) When enabled the target SOC for the inverter(s) will be reset to 100%
when a charge slot is not active, this can be used to workaround some firmware issues where the SOC target is
used for solar charging as well as grid charging. When disabled the SOC % will not be changed after a charge slot.
This is disabled by default.

## Balance Inverters

When you have two or more inverters it's possible they get out of sync so they are at different charge levels or they start to cross-charge (one discharges into another).
When enabled, balance inverters tries to recover this situation by disabling either charging or discharging from one of the batteries until they re-align.

The apps.yaml contains a setting **balance_inverters_seconds** which defines how often to run the balancing, 30 seconds is recommended if your
machine is fast enough, but the default is 60 seconds.

Enable the **switch.predbat_balance_inverters_enable** switch in Home Assistant to enable this feature.

- **switch.predbat_balance_inverters_charge** - Is used to toggle on/off balancing while the batteries are charging
- **switch.predbat_balance_inverters_discharge** - Is used to toggle on/off balancing while the batteries are discharging
- **switch.predbat_balance_inverters_crosscharge** - Is used to toggle on/off balancing when the batteries are cross charging
- **input_number.predbat_balance_inverters_threshold_charge** - Sets the minimum percentage divergence of SOC during charge before balancing, default is 1%
- **input_number.predbat_balance_inverters_threshold_discharge** - Sets the minimum percentage divergence of SOC during discharge before balancing, default is 1%

## iBoost model options

iBoost model, when enabled with **switch.iboost_enable** tries to model excess solar energy being used to heat
hot water (or similar). The predicted output from the iBoost model is returned in **iboost_best**.

The following entities are only available when you turn on iboost enable:

**iboost_solar** When enabled assumes iBoost will use solar power to boost.

**iboost_min_soc** sets the minimum home battery soc % to enable iboost solar on, default 0

**iboost_gas** When enabled assumes IBoost will operate when electric rates are lower than gas rates.
Note: Gas rates have to be configured in apps.yaml with **metric_octopus_gas**

**iboost_gas_scale** Sets the scaling of the gas rates used before comparing with electric rates, to account for losses

**iboost_charging** Assume IBoost operates when the battery is charging (can be combined with iboost_gas or not)

**iboost_max_energy** Sets the max energy sets the number of kwh that iBoost can consume during a day before turning off - default 3kWh

**iboost_max_power** Sets the maximum power in watts to consume - default 2400

**iboost_min_power** Sets the minimum power in watts to consume - default 500

You will see **predbat.iboost_today** entity which tracks the estimated amount consumed during the day, and resets at night

The **binary_sensor.iboost_active** entity will be enabled when IBoost should be active, can be used for automations to trigger boost

If you have an incrementing Sensor that tracks iBoost energy usage then you should set **iboost_energy_today** sensor in
apps.yaml to point to it and optionally set **iboost_energy_scaling** if the sensor isn't in kWh.

## Holiday mode

When you go away you are likely to use less electricity and so the previous load data will be quite pessimistic.

Using the Home Assistant entity **input_number.predbat_holiday_days_left** you can set the number of full days that
you will be away for (including today). The number will count down by 1 day at midnight until it gets back to zero.
Whilst holiday days left is non-zero, Predbat's 'holiday mode' is active.

When Predbat's 'holiday mode' is active the historical load data will be taken from yesterday's data (1 day ago) rather than from the **days_previous** setting in apps.yaml.
This means Predbat will adjust more quickly to the new usage pattern.

If you have been away for a longer period of time (more than your normal days_previous setting) then obviously it's going
to take longer for the historical data to catch up, you could then enable holiday mode for another 7 days after your return.

In summary:

- For short holidays set holiday_days_left to the number of full days you are away, including today but excluding the return day
- For longer holidays set holiday_days_left to the number of days you are away plus another 7 days until the data catches back up

## Manual control

In some cases you may want to override Predbat behaviour and make a decision yourself. One way to achieve this is to put Predbat into
read-only mode using **switch.predbat_set_read_only**. When going to read only mode the inverter will be put back to the default settings and then you should
control it yourself using GivTCP or the App. 

A better alternative in some cases is to tell Predbat what you want it to do using the manual force features:

Can you force a charge within a 30 minute slot by using the **select.predbat_manual_charge** selector. Pick the 30 minute slot you wish
to charge in and this will be actioned. You can select multiple slots by using the drop down menu more than once, when Predbat updates
you will see the slots picked in the current value of this selector and in the HTML plan (upside down F symbol). 

You can cancel a force slot by selecting the time again (it will be shown in square brackets to indicate its already selected).

![image](https://github.com/springfall2008/batpred/assets/48591903/aa668cc3-60fc-4956-8619-822f09f601dd)

The **select.predbat_manual_discharge** selector can be used to manually force a discharge within a 30 minute slot in the same way as the
manual force charge feature. The force discharge takes priority over force charging.

The **select.predbat_manual_idle** selector is used to force Predbat to be idle during a 30 minute slot, this implies no charging or discharging and thus the
battery will cover the house load (if there is enough charge).

When you use the manual override features you can only select times in the next 12 hours, the overrides will be removed once their time
slot expires (they do not repeat).

_CAUTION: If you leave Predbat turned off for a long period of time then the override timeslots could end up repeating when you restart_

![image](https://github.com/springfall2008/batpred/assets/48591903/7e69730f-a379-483a-8281-f72de0cc6e97)


## Debug

**switch.debug_enable** when on prints lots of debug, leave off by default

**switch.plan_debug** (_expert mode_) when enabled adds some extra debug to the Predbat HTML plan - see [Predbat Plan debug mode](predbat-plan-card.md#debug-mode-for-predbat-plan)
for more details.
