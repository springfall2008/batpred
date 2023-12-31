# apps.yaml settings

The basic configuration for Predbat is configured in the *apps.yaml* file that's normally stored in the '/config/appdaemon/apps/batpred/config/' directory.
You will need to use a file editor within Home Assistant (e.g. either the File Editor or Studio Code Server add-on's) to edit this file.

This section of the documentation describes what the different configuration items in apps.yaml do.

When you edit apps.yaml, AppDaemon will automatically detect the change and Predbat will be reloaded with the updated file.
You don't need to restart the AppDaemon add-on for your edits to take effect.

## Templates

You can find template configurations in the following locations:

| Template | Link |
| ---------- | ----------------------------------------- |
| GivEnergy | [apps.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/apps/predbat/config/apps.yaml) |
| SolisX | [apps.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/ginlong_solis.yaml) |
| SolarEdge | [apps.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solaredge.yaml) |

The GivEnergy template will be installed by default but if you are using another inverter please copy the correct template into your
*/config/appdaemon/apps/batpred/config/* directory and modify it from there.

## Basics

Basic configuration items

- **prefix** - Set to the prefix name to be used for all entities that predbat creates in Home Assistant. Default 'predbat'. Unlikely that you will need to change this.
- **timezone** - Set to your local timezone, default is Europe/London. It must be set to a
[valid Python time zone for your location](https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568)
- **template** - Initially set to True, this is used to stop Predbat from operating until you have finished configuring your apps.yaml.
Once you have made all other required changes to apps.yaml this line should be deleted or commented out.
- **notify_devices** - A list of device names to notify when Predbat sends a notification. The default is just 'notify' which contacts all mobile devices
- **days_previous** - A list (one entry per line) of the number of days of historical house load to be used to predict your future daily load.<BR>
It's recommended that you set days_previous so Predbat uses sufficient days' history so that 'unusual' load activity (e.g. saving sessions, "big washing day", etc) get averaged out.<BR>
Typical settings could be 1, 7 or 7, 14, or 2, 3, 4, 5, 6, 7, 8.<BR>
Do keep in mind that Home Assistant only keeps 10 days history by default, so you might need to increase the number of days history kept in HA before its purged
by editing and adding the following to the /homeassistant/configuration.yaml configuration file and restarting Home Assistant afterwards:

```yaml
    recorder:
      purge_keep_days: 14
```

- **days_previous_weight** - A list (one entry per line) of weightings to be applied to each of the days in days_previous. Default value is 1, that all history days are equally weighted.
- **forecast_hours** - the number of hours to that Predbat will forecast ahead, 48 is the suggested amount, although other values can be used
such as 30 or 36 if you have a small battery and thus don't need to forecast 2 days ahead.

## Inverter information

The template apps.yaml comes pre-configured with regular expressions that should auto-discover the GivTCP Home Assistant entity names.
If you have more than one inverter or entity names are non-standard then you will need to edit apps.yaml for your inverter entities.
For other inverter brands, see [Other Inverters](other-inverters.md)

- **num_inverters** - The number of inverters you have. If you increase this above 1 you must provide multiple of each of the inverter entities
- **geserial** - This is a helper regular expression to find your serial number, if it doesn't work edit it manually or change individual entities to match.

## Historical data

Predbat can either get historical data (house load, import, export and PV generation) directly from GivTCP or it can obtain it from the GivEnergy cloud.
Unless you have a specific reason to not use the GivTCP data (e.g. you've lost your GivTCP data), its recommended to use GivTCP.

### Data from GivTCP

The following configuration entries in apps.yaml are pre-configured to automatically use the appropriate GivTCP sensors.

If you have a 3-phase electricity supply and one inverter (and battery) on each phase then you will need to add one line for the load, import, export and PV sensors
for each of the 3 phases.

If you have a single phase electricity supply and multiple inverters on the phase then you will need to add one line for each of the load and PV sensors.
You don't need multiple lines for the import or export sensors as each inverter will give the total import or export information.

Edit if necessary if you have non-standard GivTCP sensor names:

- **load_today** - GivTCP Entity name for the house load in kWh today (must be incrementing)
- **import_today** - GivTCP Imported energy today in kWh (incrementing)
- **export_today** - GivTCP Exported energy today in kWh (incrementing)
- **pv_today** - GivTCP PV energy today in kWh (incrementing). If you have multiple inverters, enter each inverter PV sensor on a separate line.<BR>
If you have an AC-coupled GivEnergy inverter then enter the Home Assistant sensor for your PV inverter.<BR>
If you don't have any PV panels, comment or delete this line out of apps.yaml.

See the [Workarounds](#workarounds) section below for configuration settings for scaling these if required.

If you have multiple inverters then you may find that the load_today figures from GivTCP are incorrect as the inverters share the house load between them.
In this circumstance one solution is to create a Home Assistant template helper to calculate house load from {pv generation}+{battery discharge}-{battery charge}+{import}-{export}.

e.g.

```yaml
{{ states('sensor.givtcp_XXX_pv_energy_today_kwh')|float(0) + <inverter 2>...
+ states('sensor.givtcp_XXX_battery_discharge_energy_today_kwh')|float(0) + <inverter 2>...
- states('sensor.givtcp_XXX_battery_charge_energy_today_kwh')|float(0) - <inverter 2>...
+ states('sensor.givtcp_XXX_import_energy_today_kwh')|float(0)
- states('sensor.givtcp_XXX_export_energy_today_kwh')|float(0) }}
```

### GivEnergy Cloud Data

If you have an issue with the GivTCP data, Predbat can get the required historical data from the GivEnergy cloud instead. This data is updated every 30 minutes.
Obviously connecting to the cloud is less efficient and means that Predbat will be dependent upon your internet connection and the GivEnergy cloud to operate.

- **ge_cloud_data** - When True Predbat will use the GE Cloud for data rather than load_today, import_today and export_today
- **ge_cloud_serial** - Set the inverter serial number to use for the Cloud data
- **ge_cloud_key** - Set to your API Key for the GE Cloud (long string)

## Load filtering

By default if Predbat sees a gap in the historical load data it will fill it with average data. This is to help in the cases of small amounts of lost data.
For entire lost days you should change **days_previous** to point to different days(s) or include 3 or more days and if you set **switch.predbat_load_filter_modal** to true,
the lowest day's historical load will be discarded.

- **load_filter_threshold** - Sets the number of minutes of zero load data to be considered a gap (that's filled with average data), the default is 30.
To disable, set it to 1440.

## Inverter control configurations

- **inverter_limit** - One per inverter. When set defines the maximum watts of AC output power for your inverter (e.g. 3600).
This will help to emulate clipping when your solar produces more than the inverter can handle, but it won't be that accurate as the source of the data isn't minute by minute.
If you have a separate Solar inverter as well then add the solar inverter limit to the battery inverter limit to give one total amount.

- **export_limit** - One per inverter (optional). When set defines the maximum watts of AC power your inverter can export to the grid at (e.g. 2500).
This will emulate the software export limit setting in the Inverter that you will have if your G98/G99
approval was lower than your maximum inverter power (check your install information for details).
If you do not set an export limit then it's the same as the inverter limit.

- **inverter_limit_charge** and **inverter_limit_discharge** - One per inverter (optional). When set in watts, overrides the maximum
charge/discharge rate settings used when controlling the inverter.
This can be used if you need to cap your inverter battery rate (e.g. charge overnight at a slower rate to reduce inverter/battery heating) as Predbat
will normally configure all timed charges or discharges to be at the inverter's maximum rate.

## Controlling the Inverter

There are two ways that Predbat can control GivTCP to control the inverter, either via REST API calls (preferred) or via the GivTCP inverter controls in Home Assistant.

### REST Interface inverter control

- **givtcp_rest** - One per Inverter, sets the GivTCP REST API URL ([http://homeassistant.local:6345](http://homeassistant.local:6345)
is the normal one for the first inverter and :6346 for the second inverter).
When enabled the Control per inverter below isn't used and instead communication from Predbat to GivTCP is directly via REST and thus bypasses some issues with MQTT.
If using Docker then change homeassistant.local to the Docker IP address.

To check your REST is working open up the readData API point in a Web browser e.g: [http://homeassistant.local:6345/readData](http://homeassistant.local:6345/readData)

If you get a bunch of inverter information back then it's working!

It's recommended you enable 'Output Raw Register Values' in GivTCP (via Add-on's / GivTCP / configuration tab) for added monitoring:

![image](https://github.com/springfall2008/batpred/assets/48591903/e6cf0304-57f3-4259-8354-95a7c4f9b77f)

### Home Assistant inverter control

As an alternative to REST control, Predbat can control the GivEnergy inverters via GivTCP controls in Home Assistant.
The template apps.yaml is pre-configured with regular expressions for the following configuration items that should auto-discover the GivTCP controls,
but may need changing if you have multiple inverters or non-standard GivTCP entity names.

The **givtcp_rest** line should be commented out/deleted in order for Predbat to use the direct GivTCP Home Assistant controls.

- **charge_rate** - GivTCP battery charge rate entity in watts
- **discharge_rate** - GivTCP battery discharge max rate entity in watts
- **battery_power** - GivTCP current battery power in watts
- **pv_power** - GivTCP current PV power in watts
- **load_power** - GivTCP current load power in watts
- **soc_kw** - GivTCP Entity name of the battery SOC in kWh, should be the inverter one not an individual battery
- **soc_max** - GivTCP Entity name for the maximum charge level for the battery
- **reserve** - GivTCP sensor name for the reserve setting in %
- **inverter_mode** - GivTCP inverter mode control
- **inverter_time** - GivTCP inverter timestamp
- **charge_start_time** - GivTCP battery charge start time entity
- **charge_end_time** - GivTCP battery charge end time entity
- **charge_limit** - GivTCP Entity name for used to set the SOC target for the battery in percentage
- **scheduled_charge_enable** - GivTCP Scheduled charge enable config
- **scheduled_discharge_enable** - GivTCP Scheduled discharge enable config
- **discharge_start_time** - GivTCP scheduled discharge slot_1 start time
- **discharge_end_time** - GivTCP scheduled discharge slot_1 end time

If you are using REST control the above GivTCP configuration items can be deleted or commented out of apps.yaml.

## Solcast Solar Forecast

As described in the [Predbat installation instructions](install.md#solcast-install), Predbat needs a solar forecast
in order to predict solar generation and battery charging which can be provided by the Solcast integration.

The template apps.yaml is pre-configured with regular expressions for the following configuration items that should auto-discover the Solcast entity names.
They are unlikely to need changing although a few people have reported their entity names don't contain 'solcast' so worth checking, or edit if you have non-standard names:

- **pv_forecast_today** - Entity name for today's Solcast forecast
- **pv_forecast_tomorrow** - Entity name for tomorrow's Solcast's forecast
- **pv_forecast_d3** - Entity name for Solcast's forecast for day 3
- **pv_forecast_d4** - Entity name for Solcast's forecast for day 4 (also d5, d6 & d7 are supported, but not that useful)

If you do not have a PV array then comment out or delete these Solcast lines from apps.yaml.

If you have multiple PV arrays connected to GivEnergy Hybrid inverters or you have GivEnergy AC-coupled inverters, then ensure your PV configuration in Solcast covers all arrays.

If however you have a mixed PV array setup with some PV that does not feed into your GivEnergy inverters
(e.g. hybrid GE inverters but a separate older FIT array that directly feeds AC into the house),
then it's recommended that Solcast is only configured for the PV connected to the GivEnergy inverters.

## Energy Rates

There are a number of configuration items in apps.yaml for telling Predbat what your import and export rates are.

These are described in detail in [Energy Rates](energy-rates.md) and are listed here just for completeness:

- **metric_octopus_import** - Import rates from the Octopus Energy integration
- **metric_octopus_export** - Export rates from the Octopus Energy integration
- **metric_octopus_gas** - Gas rates from the Octopus Energy integration
- **octopus_intelligent_slot** - Octopus Intelligent GO slot sensor from the Octopus Energy integration
- **octopus_saving_session** - Energy saving sessions sensor from the Octopus Energy integration
- **octopus_saving_session_octopoints_per_penny** - Sets the Octopoints per pence
- **rates_import_octopus_url** - Octopus pricing URL (over-rides metric_octopus_import)
- **rates_export_octopus_url** - Octopus export pricing URL (over-rides metric_octopus_export)
- **metric_standing_charge** - Standing charge in pounds
- **rates_import** - Import rates over a 24-hour period with start and end times
- **rates_export** - Export rates over a 24-hour period with start and end times
- **rates_import_override** - Over-ride import rate for specific date and time range, e.g. Octopus Power-up events
- **rates_export_override** - Over-ride export rate for specific date and time range

## Car Charging Integration

Predbat is able to include electric vehicle charging in its plan and manage the battery activity so that the battery isn't discharged into your car when the car is charging
(although you can over-ride this by setting **switch.predbat_car_charging_from_battery** to True in Home Assistant).

The following configuration items (Home Assistant entities and apps.yaml configuration items) are used to plan car charging activity within Predbat.

- **num_cars** should be set in apps.yaml to the number of cars you want Predbat to plan for.
Set to 0 if you don't have an EV (and the remaining car sensors in apps.yaml can be commented out or deleted).

### Car charging filtering

You might want to remove your electric car charging data from the historical house load data so as to not bias the calculations, otherwise you will get
high charge levels when the car was charged previously (e.g. last week).

- **switch.car_charging_hold** - A Home Assistant switch that when turned on (True) tells Predbat to remove car charging data from the simulation.

- **car_charging_energy** - Set in apps.yaml to point to a Home Assistant entity which is the incrementing kWh data for the car charger.
This is pre-defined to a regular expression to auto-detect the appropriate Wallbox and Zappi car charger sensors, or edit as necessary for your charger sensor.

- **input_number.car_charging_energy_scale** - A Home Assistant entity used to define a scaling factor (in the range 0.1 to 1.0)
to multiply the car_charging_energy data by (e.g. set to 0.001 to convert Watts to kW).

If you do not have a suitable car charging kWh sensor in Home Assistant then comment the car_charging_energy line out of apps.yaml and configure the following Home Assistant entity:

- **input_number.car_charging_threshold** - Sets the threshold above which home consumption is assumed to be car charging and ignore (default 6 = 6kW).

### Planned car charging

These features allow Predbat to know when you plan to charge your car. If you have Intelligent Octopus setup then you won't need to change
these as it's done automatically via the Octopus app and the Octopus Energy integration in Home Assistant.

- **switch.predbat_octopus_intelligent_charging** - When this Home Assistant switch is enabled, Predbat will plan charging around the Intelligent Octopus slots, taking
it into account for battery load and generating the slot information

If you don't use Intelligent Octopus then there are a number of other apps.yaml configuration items that should be set:

- **car_charging_planned** - Optional, can be set to a Home Assistant sensor which lets Predbat know the car is plugged in and planned to charge during low rate slots.
Or manually set it to 'False' to disable this feature, or 'True' to always enable

- **car_charging_planned_response** - An array of values in from the above car_charging_planned sensor which indicate that the car is plugged in and will charge
in the next low rate slot. Customise for your car charger sensor if it sets sensor values that are not in the pre-defined list.

- **car_charging_now** - For some cases planning of car charging is difficult to obtain (e.g. Ohme with Intelligent doesn't report slots).<BR>
The car_charging_now configuration item can be set to point to a sensor that tells you that the car is currently charging.
Predbat will then assume this 30 minute slot is used for charging regardless of the plan.<BR>
If Octopus Intelligent Charging is enabled and car_charging_now indicates the car is charging then Predbat will also assume that this is a
low rate slot for the car/house (and might therefore start charging the battery), otherwise electricity import rates are taken from the normal rate data.

- **car_charging_now_response** - Set to the range of positive responses for car_charging_now, useful if you have a sensor for your car charger that isn't binary.

To make planned car charging more accurate, configure the following items in apps.yaml:

- **car_charging_battery_size** - Set to the car's battery size in kWh, defaults to 100. It will be used to predict car charging stops.

- **car_charging_limit** - Set to point to a sensor that specifies the % limit the car is set to charge to. Default is 100%

- **car_charging_soc** - Set to point to a sensor that specifies the car's current % charge level. Default is 0%

### Multiple Electric Cars

Multiple cars can be planned with Predbat, in which case you should set **num_cars** in apps.yaml to the number of cars you want to plan

- **car_charging_limit**, **car_charging_planned**, **car_charging_battery_size** and **car_charging_soc** must then be a list of values (i.e. 2 entries for 2 cars)
  
- If you have Intelligent Octopus then Car 0 will be managed by the Octopus Energy integration, if its enabled
  
- Each car will have it's own Home Assistant slot sensor created e.g. **binary_sensor.predbat_car_charging_slot_1**,
SOC planning sensor e.g **predbat.car_soc_1** and **predbat.car_soc_best_1** for car 1

## Workarounds

- **switch.predbat_set_read_only** - When set prevents Predbat from making modifications to the inverter settings (regardless of the configuration).
Predbat will continue making and updating its prediction plan every 5 minutes, but no inverter changes will be made.
This is useful if you want to over-ride what predbat is planning to do, or whilst you are learning how Predbat works prior to turning it on 'in anger'.
- **battery_scaling** - Scales the battery reported SOC kWh e.g. if you set 0.8 your battery is only 80% of reported capacity. If you are going
to chart this you may want to use **predbat.soc_kw_h0** as your current status rather than the GivTCP entity so everything lines up
- **import_export_scaling** - Scaling the import & export data from GivTCP - used for workarounds
- **inverter_clock_skew_start**, **inverter_clock_skew_end** - Skews the setting of the charge slot registers vs the predicted start time (see apps.yml)
- **inverter_clock_skew_discharge_start**, **inverter_clock_skew_discharge_end** - Skews the setting of the discharge slot registers vs the predicted start time (see apps.yml)
- **clock_skew** - Skews the local time that Predbat uses (from AppDaemon), will change when real-time actions happen e.g. triggering a discharge.
- **predbat_battery_capacity_nominal** - When enabled Predbat uses the reported battery size from the Nominal field rather than from the normal GivTCP
reported size. If your battery size is reported wrongly maybe try turning this on and see if it helps.
- **car_charging_now** - Can be used to workaround Ohme issue with Intelligent where the plan is not published, see [Planned car charging](#planned-car-charging)

- **inverter_battery_rate_min** - One per inverter (optional), set in watts, when set models a "bug" in the inverter firmware
in some models where if charge or discharge rates are set to 0 you actually get a small amount of charge or discharge.
Recommended setting is 200 for Gen 1 hybrids with this issue.

- **inverter_reserve_max** - Global, sets the maximum reserve % that maybe set to the inverter, the default is 98 as some Gen 2 inverters and
AIO firmware versions refuse to be set to 100.  Comment the line out or set to 100 if your inverter allows setting to 100%.

- **battery_charge_power_curve** - Some batteries tail off their charge rate at high soc% and this optional configuration item enables you to model this in Predbat.
Enter the charging curve as a series of steps of % of max charge rate for each soc percentage.
The default is 1.0 (full power) charge all the way to 100%.
Example from GE 9.5kWh battery with latest firmware and Gen 1 inverter:

```yaml
  battery_charge_power_curve:
    91 : 0.91
    92 : 0.81
    93 : 0.71
    94 : 0.62
    95 : 0.52
    96 : 0.43
    97 : 0.33
    98 : 0.24
    99 : 0.24
    100 : 0.24
```

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

## Triggers

The trigger feature is useful to help trigger your own automation based on Predbat determining that you have spare solar energy or battery that you would otherwise export

The triggers count export energy until the next active charge slot only.

For each trigger give a name, the minutes of export needed and the energy required in that time
Multiple triggers can be set at once so in total you could use too much energy if all run
Each trigger create an entity called 'binary_sensor.predbat_export_trigger_*name*' which will be turned on when the condition is valid
connect this to your automation to start whatever you want to trigger.

Set the name for each trigger, the number of minutes of solar export you need, and the amount of energy in kWh you will need available during that time period in apps.yaml:

For example:

```yaml
export_triggers:
  - name: "large"
    minutes: 60
    energy: 1.0
  - name: "small"
    minutes: 15
    energy: 0.25
```

If you wish to trigger based on charging or discharging the battery rather than spare solar energy you can instead use the following binary sensors

- **binary_sensor.predbat_charging** - Will be True when the home battery is inside a charge slot (either being charged or being held at a level).
Note that this does include charge freeze slots where the discharge rate is set to zero without charging the battery.
- **binary_sensor.predbat_discharging** - Will be True when the home battery is inside a force discharge slot. This does not include
discharge freeze slots where the charge rate is set to zero to export excess solar only.

## Holiday mode

When you go away you are likely to use less electricity and so the previous load data will be quite pessimistic. Using the
configuration item **input_number.predbat_holiday_days_left** in Home assistant you can set the number of full days that
you will be away for (including today). The number will count down by 1 day at midnight until it gets back to zero. When
holiday days left are non-zero, the holiday mode is active.

When holiday mode is active the historical load data will be taken from yesterday's data (1 day ago) rather than from the **days_previous**
setting in apps.yaml. This means Predbat will adjust more quickly to the new usage pattern.

If you have been away for a longer period of time (more than your normal days_previous setting) then obviously it's going
to take longer for the historical data to catch up, you could then enable holiday mode for another 7 days after your return.

In summary:

- For short holidays set holiday_days_left to the number of full days you are away, including today but excluding the return day
- For longer holidays set holiday_days_left to the number of days you are away plus another 7 days until the data catches back up
