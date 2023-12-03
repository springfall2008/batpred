# apps.yaml settings

## Basics

Basic configuration items

- **timezone** - Set to your local timezone, default is Europe/London
([https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568](https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568))
- **notify_devices** - A list of device names to notify, the default is just 'notify' which contacts all mobile devices
- **user_config_enable** - When True the user configuration is exposed in Home Assistant as input_number and switch, the config file becomes just the defaults to use
- **days_previous** - A list of the the number of days to go back in the history to predict your load, recommended settings
are 1, 7 or both 7 and 14 (if you have enough data). Each list entry is weighted with **days_previous_weight**.
Keep in mind HA default history is only 10 days.
- **days_previous_weight** A list of the weightings to use of the data for each of the days in days_previous.
- **forecast_hours** - the number of hours to forecast ahead, 48 is the suggested amount.
- **input_number.forecast_plan_hours** - the number of hours after the next charge slot to include in the plan, default 24 hours
is the suggested amount (to match energy rate cycles)

## Inverter information

The following are entity names in HA for GivTCP, assuming you only have one inverter and the entity names are standard then it will be auto discovered

- **num_inverters** - If you increase this above 1 you must provide multiple of each of these entities
- **geserial** - This is a helper regular expression to find your serial number, if it doesn't work edit it manually or change individual entities to match.

## Historical data

### Data from GivTCP

  It's recommended you get this data from GivTCP, there are also controls for load_scaling and import_export_scaling if they need scale adjustments

- **load_today**   - GivTCP Entity name for the house load in kWh today (must be incrementing)
- **import_today** - GivTCP Imported energy today in kWh (incrementing)
- **export_today** - GivTCP Exported energy today in kWh (incrementing)
- **pv_today**     - GivTCP PV energy today in kWh (incrementing)

If you have multiple inverters then you may find that the load_today figures from GivTCP are incorrect as the inverters share the house load between them.
In this circumstance one solution is to create a template helper to calculate house load from {pv generation}+{battery discharge}-{battery charge}+{import}-{export}.

e.g.

```yaml
{{ states('sensor.givtcp_XXX_pv_energy_today_kwh')|float(0) + <inverter 2>...
+ states('sensor.givtcp_XXX_battery_discharge_energy_today_kwh')|float(0) + <inverter 2>...
- states('sensor.givtcp_XXX_battery_discharge_energy_today_kwh')|float(0) - <inverter 2>...
+ states('sensor.givtcp_XXX_import_energy_today_kwh')|float(0)
- states('sensor.givtcp_XXX_export_energy_today_kwh')|float(0) }}
```

### GivEnergy Cloud Data

   If you have an issue with the GivTCP data you can get this historical data from the GivEnergy cloud instead. This data is updated every 30 minutes.

- **ge_cloud_data**   - When True use the GE Cloud for data rather than load_today, import_today and export_today
- **ge_cloud_serial** - Set the inverter serial number to use for the Cloud data
- **ge_cloud_key**    - Set to your API Key for GE Cloud (long string)

## Inverter control

- **inverter_limit** - One per inverter, when set defines the maximum watts of AC power for your inverter (e.g. 3600). This will
help to emulate clipping when your solar produces more than the inverter can handle, but it won't be that accurate as the source
of the data isn't minute by minute. If you have a separate Solar inverter as well then add the solar inverter limit to the battery
inverter limit to give one total amount.

- **export_limit** - One per inverter (optional), when set defines the maximum watts of AC power your inverter can export to the
grid at (e.g. 2500). This will emulate the software export limit setting in the Inverter that you will have if your G98/G99
approval was lower than your maximum inverter power (check your install information for details). If not set the export limit is
the same as the inverter limit.

- **inverter_limit_charge** and **inverter_limit_discharge** - One per inverter (optional), when set overrides the maximum
charge/discharge rate register settings used when controlling the inverter. This can be used for workarounds if you need
to cap your inverter battery rate as Predbat overwrites the maximum rate registers when it enables a timed charge or discharge.

- **inverter_battery_rate_min** - One per inverter (optional), set in watts, when set models a "bug" in the inverter firmware
in some models where if charge or discharge rates are set to 0 you actually get a small amount of charge or discharge.
Recommended setting is 200 for gen1 hybrids with this issue.

- **set_discharge_during_charge** - If turned off disables inverter discharge during charge slots, useful for multi-inverter
to avoid cross charging when batteries are out of balance.

- **inverter_reserve_max** - Global, sets the maximum reserve % that maybe set to the inverter, the default is 100. Can be set
to 99 to workaround some gen2 inverters which refuse to be set to 100.

### REST Interface inverter control

- **givtcp_rest** - One per Inverter, sets the REST API URL ([http://homeassistant.local:6345](http://homeassistant.local:6345)
is the normal one for the first inverter). When enabled the Control per inverter below isn't used and instead communication is directly via REST and
thus bypasses some issues with MQTT. If using Docker then change homeassistant.local to the Docker IP address.

To check your REST is working open up the readData API point in a Web browser e.g: [http://homeassistant.local:6345/readData](http://homeassistant.local:6345/readData)

If you get a bunch of inverter information back then it's working!

It's recommended you enable Raw register output in GivTCP for added monitoring:

![image](https://github.com/springfall2008/batpred/assets/48591903/e6cf0304-57f3-4259-8354-95a7c4f9b77f)

### Home-assistant Inverter control

Control per inverter (only used if REST isn't set):

- **soc_kw** - GivTCP Entity name of the battery SOC in kWh, should be the inverter one not an individual battery
- **soc_max** - GivTCP Entity name for the maximum charge level for the battery
- **reserve** - GivTCP sensor name for the reserve setting in %
- **inverter_mode** - GivTCP inverter mode control
- **inverter_time** - GivTCP inverter timestamp
- **charge_enable** - GivTCP charge enable entity - says if the battery will be charged in the time window
- **charge_limit** - GivTCP Entity name for used to set the SOC target for the battery in percentage
- **charge_start_time** - GivTCP battery charge start time entity
- **charge_end_time** - GivTCP battery charge end time entity
- **charge_rate** - GivTCP battery charge rate entity in watts
- **discharge_rate** - GivTCP battery discharge max rate entity in watts
- **battery_power** - GivTCP current battery power in watts
- **scheduled_charge_enable** - GivTCP Scheduled charge enable config
- **scheduled_discharge_enable** - GivTCP Scheduled discharge enable config
- **discharge_start_time** - GivTCP scheduled discharge slot_1 start time
- **discharge_end_time** - GivTCP scheduled discharge slot_1 end time

## Solcast

The following are entity names in Solcast, unlikely to need changing although a few people have reported their entity names don't contain 'solcast' so worth checking:

- **pv_forecast_today** - Entity name for solcast today's forecast
- **pv_forecast_tomorrow** - Entity name for solcast forecast for tomorrow
- **pv_forecast_d3** - Entity name for solcast forecast for day 3
- **pv_forecast_d4** - Entity name for solcast forecast for day 4 (also d5, d6 & d7 are supported but not that useful)

## Octopus Energy

The following are entity names in the Octopus Energy plugin.
They are set to a regular expression and auto-discovered but you can comment out to disable or set them manually.

- **metric_octopus_import** - Import rates from the Octopus plugin, should point to the sensor
- **metric_octopus_export** - Export rates from the Octopus plugin, should point to the sensor

 **CAUTION** To get detailed energy rates needed by Predbat you need to go into Home Assistant and manually enable the following
 events which are disabled by the plugin by default:

 ```yaml
    event.octopus_energy_electricity_xxxxxxxx_previous_day_rates
    event.octopus_energy_electricity_xxxxxxxx_current_day_rates
    event.octopus_energy_electricity_xxxxxxxx_next_day_rates

    event.octopus_energy_electricity_xxxxxxxx_export_previous_day_rates
    event.octopus_energy_electricity_xxxxxxxx_export_current_day_rates
    event.octopus_energy_electricity_xxxxxxxx_export_next_day_rates
 ```  

- **octopus_intelligent_slot** - If you have Intelligent Octopus and the Octopus Energy plugin installed point to the 'slot' sensor
- **octopus_saving_session** - Points to the sensor in the Octopus Energy plugin that publishes saving sessions (binary_sensor.octopus_energy_XXXXX_saving_sessions

- **switch.predbat_octopus_intelligent_charging** - When enabled Predbat will plan charging around the Intelligent Octopus slots, taking
it into account for battery load and generating the slot information
- **input_number.predbat_metric_octopus_saving_rate** - Set the assuming saving rate for an Octopus saving session (in pence)

Or you can override these by manually supplying an octopus pricing URL (expert feature)

- **rates_import_octopus_url**
- **rates_export_octopus_url**

## Standing charge

Predbat also includes the daily standing charge in cost predictions (optional)

- **metric_standing_charge** - Set to the standing charge in pounds e.g. 0.50 is 50p. Can be typed in directly or point to a sensor that
stores this information (e.g. Octopus Plugin).

Delete this line from apps.yaml or set it to zero if you don't want the standing charge (and only have consumption usage) to be included in Predbat charts and output data.

## Manual energy rates

Or manually set your rates in a 24-hour period using these:

```yaml
  rates_import:
    - start : "HH:MM:SS"
      end : "HH:MM:SS"
      rate : pence
  rates_export:
    - start : "HH:MM:SS"
      end : "HH:MM:SS"
      rate : pence
```

**start** and **end** are in the time format of "HH:MM:SS" e.g. "12:30:00" and should be aligned to 30 minute slots normally.
rate is in pence e.g. 4.2

## Manually Over-riding energy rates

You can also override the energy rates (regardless of if they are set manually or via Octopus) using the override feature.
The override is used to set times where rates are different, e.g. an Octopus Power Up session (zero rate for an hour or two)

```yaml
  rates_import_override:
    - start : "HH:MM:SS"
      end : "HH:MM:SS"
      rate : pence
      date : "YYYY-MM-DD"
  rates_export_override:
    - start : "HH:MM:SS"
      end : "HH:MM:SS"
      rate : pence
      date : "YYYY-MM-DD"
```

**date** is in the date format of "YYYY-MM-DD" e.g. "2023-09-09"

## Car charging filtering

You might want to remove your electric car charging data from the historical load so as to not bias the calculations, otherwise you will get
high charge levels when the car was charged previously (e.g. last week).

- **car_charging_hold** - When true car charging data is removed from the simulation (by subtracting car_charging_rate), as you either
charge from the grid or you use the Octopus Energy plugin to predict when it will charge correctly (default 6kw, configure with **car_charging_threshold**)
- **car_charging_threshold** - Sets the threshold above which is assumed to be car charging and ignore (default 6 = 6kw)
- **car_charging_energy** - Set to a HA entity which is incrementing kWh data for the car charger, will be used instead of threshold for
more accurate car charging data to filter out

## Planned car charging

These features allow Predbat to know when you plan to charge your car. If you have Intelligent Octopus setup then you won't need to change
these as it's done automatically via their app and the Octopus Energy plugin.

- **octopus_intelligent_charging** - When enabled Predbat will plan charging around the Intelligent Octopus slots, taking it into account
for battery load and generating the slot information

Only needed if you don't use Intelligent Octopus:

- **car_charging_planned** - Can be set to a sensor which lets Predbat know the car is plugged in and planned to charge during low rate
slots, or False to disable, or True to always enable
- **car_charging_planned_response** - An array of values from the planned sensor which indicate that the car is plugged in and will charge
in the next low rate slot
- **car_charging_rate** - Set to the car's charging rate (normally 7.5 for 7.5kw).
- **car_charging_battery_size** - Indicates the car's battery size in kWh, defaults to 100. It will be used to predict car charging stops.

- **car_charging_now** - When set links to a sensor that tells you that the car is currently charging. Predbat will then assume this 30 minute
slot is used for charging regardless of the plan. If Octopus Intelligent Charging is enabled then it will also assume it's a low rate slot for
the car/house, otherwise rates are taken from the normal rate data.
- **car_charging_now_response** - Sets the range of positive responses for **car_charging_now**, useful if you have a sensor for your car that isn't binary.

- **car_charging_plan_time** - When using Predbat-led planning set this to the time you want the car to be charged by
- **car_charging_plan_smart** - When true the cheapest slots can be used for charging, when False it will be the next low rate slot

Connect to your cars sensors for accurate data:

- **car_charging_limit** - The % limit the car is set to charge to, link to a suitable sensor. Default is 100%
- **car_charging_soc** - The cars current % charge level, link to a suitable sensor. Default is 0%

Control how your battery behaves during car charging:

- **car_charging_from_battery** - When True the car can drain the home battery, Predbat will manage the correct level of battery accordingly.
When False home battery discharge will be prevented when your car charges, all load from the car and home will be from the grid. This is achieved
by setting the discharge rate to 0 during car charging and to the maximum otherwise, hence if you turn this switch Off you won't be able to change
your discharge rate outside Predbat. The home battery can still charge from the grid/solar in either case. Only use this if Predbat knows your car
charging plan, e.g. you are using Intelligent Octopus or you use the car slots in Predbat to control your car charging.
    - CAUTION: If you turn this switch back on during a car charging session you will need to set your battery discharge rate back to maximum manually.

- Multiple cars can be planned with Predbat, in which case you should set **num_cars** in apps.yaml to the number of cars you want to plan
    - **car_charging_limit**, **car_charging_planned**, **car_charging_battery_size** and **car_charging_soc** must then be a list of values (e.g. 2 entries for 2 cars)
    - If you have Intelligent Octopus then Car 0 will be managed by Octopus Energy plugin if enabled
    - Each car will have it's own slot sensor created **predbat_car_charging_slot_1** for car 1
    - Each car will have it's own SOC planning sensor created e.g **predbat.car_soc_1** and **predbat.car_soc_best_1** for car 1

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
- **inverter_battery_rate_min** - Can be set to model the inverter not actually totally stopping discharging or charging the battery (value in watts).
- **inverter_reserve_max** - Global, sets the maximum reserve % that maybe set to the inverter, the default is 100. Can be set to 99 to workaround some
gen2 inverters which refuse to be set to 100.
- **car_charging_now** - Can be used to workaround Ohme issue with Intelligent where the plan is not published, see [Planned car charging](#planned-car-charging)

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

For each trigger give a name, the minutes of export needed and the energy required in that time.

Multiple triggers can be set at once so in total you could use too much energy if all run!

Each trigger create an entity called 'binary_sensor.predbat_export_trigger_[name]' which will be turned On when the condition is valid.

Connect your automation to this binary sensor to start whatever you want to trigger.

Set the name for each trigger, the number of minutes of solar export you need, and the amount of energy in kwH you will need available during that time period in apps.yaml:

For example:

```yaml
 export_triggers:
     - name: 'large'
       minutes: 60
       energy: 1.0
     - name: 'small'
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
