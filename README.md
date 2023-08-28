# predbat
Home battery prediction and automatic charging for Home Assistant with GivTCP

Also known by some as Batpred or Batman!

![image](https://github.com/springfall2008/batpred/assets/48591903/e98a0720-d2cf-4b71-94ab-97fe09b3cee1)

```
Copyright (c) Trefor Southwell August 2023 - All rights reserved
This software maybe used at not cost for personal use only
No warranty is given, either expressed or implied
```

For support please raise a Github ticket or use the GivTCP Facebook page: https://www.facebook.com/groups/615579009972782

If you want to buy me a beer then please use Paypal - tdlj@tdlj.net
![image](https://github.com/springfall2008/batpred/assets/48591903/b3a533ef-0862-4e0b-b272-30e254f58467)

**Please note from release v5.0 onwards many configuration options are now inside Home Assistant, for these options changing the apps.yml will have no effect once installed** 

- [predbat](#predbat)
  * [Operation](#operation)
  * [Step by step guide](#step-by-step-guide)
  * [Install](#install)
    + [GivTCP install](#givtcp-install)
    + [AppDaemon install](#appdaemon-install)
    + [HACS install](#hacs-install)
    + [Predbat install](#predbat-install)
    + [Predbat manual install](#predbat-manual-install)
    + [Solcast install](#solcast-install)
  * [Energy rates](#energy-rates)
    + [Octopus Energy Plugin](#octopus-energy-plugin)
    + [Rate bands](#rate-bands)
    + [Octopus Intelligent Plugin](#octopus-intelligent-plugin)
  * [Car charging planning](#car-charging-planning)
  * [config.yml settings](#configyml-settings)
    + [Basics](#basics)
    + [Inverter information](#inverter-information)
    + [Historical data](#historical-data)
      - [Data from GivTCP](#data-from-givtcp)
      - [GE Cloud Data](#ge-cloud-data)
    + [Inverter control](#inverter-control)
      - [REST Interface inverter control](#rest-interface-inverter-control)
      - [Home-assistant Inverter control](#home-assistant-inverter-control)
    + [Solcast](#solcast)
    + [Octopus energy](#octopus-energy)
    + [Manual energy rates](#manual-energy-rates)
    + [Car charging filtering](#car-charging-filtering)
    + [Planned car charging](#planned-car-charging)
    + [Workarounds](#workarounds)
    + [Triggers](#triggers)
  * [Configuration guide](#configuration-guide)
    + [Fixed daily rates](#fixed-daily-rates)
    + [Cheap night rate (e.g. Octopus Go, Intelligent, Economy 7 etc)](#cheap-night-rate-eg-octopus-go-intelligent-economy-7-etc)
    + [Multiple rates for import and export (e.g. Octopus Flux & Cozy)](#multiple-rates-for-import-and-export-eg-octopus-flux-cozy)
    + [Half hourly variable rates (e.g. Octopus Agile)](#half-hourly-variable-rates-eg-octopus-agile)
  * [Video Guides](#video-guides)
  * [FAQ](#faq)
  * [Customisation ](#customisation)
    + [Battery loss options](#battery-loss-options)
    + [Scaling and weight options](#scaling-and-weight-options)
    + [Car charging hold options](#car-charging-hold-options)
    + [Car charging plan options](#car-charging-plan-options)
    + [Calculation options](#calculation-options)
    + [Battery margins and metrics options](#battery-margins-and-metrics-options)
    + [Inverter control options](#inverter-control-options)
    + [IBoost model options](#iboost-model-options)
    + [Debug](#debug)
  * [Output data](#output-data)
  * [Creating the charts](#creating-the-charts)
  * [Todo list](#todo-list)


## Operation

The app runs every N minutes (default 5), it will automatically update its prediction for the home battery levels for the next period, up to a maximum of 48 hours. It will automatically find charging slots (up to 10 slots) and if enabled configure them automatically with GivTCP. It uses the solar production forecast from Solcast combined with your historical energy use and your plan charging slots to make a prediction. When enable it will tune the charging percentage (SOC) for each of the slots to achieve the lowest cost possible.

- The output is a prediction of the battery levels, charging slots and % charge level, costs and import and export amounts.
- Costs are based on energy pricing data, either manually configured (e.g. 7p from 11pm-4pm and 35p otherwise) or by using the Octopus Plugin
   - Both import and export rates are supported.
   - Octopus Intelligent is also supported and takes into account allocated charging slots.  
- The solar forecast used is the central scenario from Solcast but you can also add weighting to the 10% (worst case) scenario, the default is 20% weighting to this.
- The SOC calculation can be adjusted with a safety margin (minimum battery level, extra amount to add and pence threshold). 
- The charging windows and charge level (SOC) can be automatically programmed into the inverter.
- Automatic planning of export slots is also supported, when enabled Batpred can start a forced discharge of the battery if the export rates are high and you have spare capacity.
- Ability to manage reserve % to match SOC % to prevent discharge (if enabled)
- Historical load data is used to predict your consumption, optionally car charging load can be filtered out of this data.

- Multiple inverter support depends on running all inverters in lockstep, that is each will charge at the same time to the same %

## Step by step guide

Please see the sections below for how to achieve each step. This is just a checklist of things:

1. Make sure GivTCP is installed and running - [GivTCP install](#givtcp-install)
2. Install AppDaemon if you haven't already  - [AppDaemon install](#appdaemon-install)
3. Install HACS if you haven't already - [HACS install](#hacs-install)
4. Install Predbat using HACS - [Predbat install](#predbat-install)
5. Install Solcast if you haven't already [Solcast install](#solcast-install)
   - Also check Solcast is being auto-updated a few times a day and that you see the data in Home Assistant
6. If you have Octopus Energy then install the Octopus Energy plugin (if you haven't already)  - [Octopus energy](#octopus-energy)
   - If you have Octopus Intelligent also install the Intelligent plugin
7. Go and edit apps.yml (in config/appdaemon/apps/predbat/config/apps.yml) to match your system - [config.yml settings](#configyml-settings)
   - Inverter settings match the names in GivTCP -  should be automatic but if you have _2 names you will have to edit apps.yml)
     - You have set the right number of inverters (**num_inverters**)
     - Adjust your **inverter_limit** as required
   - You have your energy rates set correctly either using Octopus Plugin or entered manually
   - That the Solcast plugin is matching the configuration correctly - should be automatic
   - If you have a car charging sensor you might want to add that also to help make predictions more accurate
   - Then check the AppDaemon log file and make sure you have no errors or warnings that are unexpected
   - And check **predbat.status** in Home Assistant to check it's now Idle (errors are reported here too)
8. Add the Predbat entities to your dashboard  - [Output data](#output-data)
9. Follow the configuration guide to tune things for your system  - [Configuration guide](#configuration-guide)
10. Set up the Apex Charts so you can check what Predbat is doing - [Creating the charts](#creating-the-charts)
11. Look at the [FAQ](#faq) and [Video Guides](#video-guides) for help

Overview of the key configuration elements:

![image](https://github.com/springfall2008/batpred/assets/48591903/7c9350e0-2b6d-49aa-8f61-93d0547ae6d0)

## Install

### GivTCP install

- You must have GivTCP installed and running first (https://github.com/britkat1980/giv_tcp/tree/main)
  - You will need at least 24 hours history in HA for this to work correctly, the default is 7 days (but you configure this back 1 day if you need to)

### AppDaemon install

- Install AppDaemon add-on https://github.com/hassio-addons/addon-appdaemon
   - Set the **time_zone** correctly in appdaemon.yml (e.g. Europe/London)
   - Add **thread_duration_warning_threshold: 30** to the appdaemon.yml file in the appdaemon section

### HACS install

- Install HACS if you haven't already (https://hacs.xyz/docs/setup/download)
- Enable AppDaemon in HACS: https://hacs.xyz/docs/categories/appdaemon_apps/
- 
### Predbat install

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

- Once installed you will get automatic updates from each release!

- Add https://github.com/springfall2008/batpred as a custom repository of type 'AppDaemon'
- Click on the Repo and Download the app

> After an update with HACS you may need to reboot AppDaemon as it sometimes reads the config wrongly during the update

- Edit in HomeAssistant config/appdaemon/apps/predbat/config/apps.yml to configure
- Note that future updates will not overwrite apps.yml, but you may need to copy settings for new features across manually

### Predbat manual install

**Not recommended if you have HACS**

- Copy apps/predbat/predbat.py to 'config/appdaemon/apps/' directory in home assistant
- Copy apps/predbat/apps.yml to 'config/appdaemon/apps' directory in home assistant
- Edit in HomeAssistant config/appdaemon/apps/apps.yml to configure

- If you later install with HACS then you must move the apps.yml into config/appdaemon/apps/predbat/config

### Solcast install

Predbat needs a solar forecast in order to predict battery levels.

If you don't have solar then comment out the Solar forecast part of the apps.yml: **pv_forecast_* **

- Make sure Solcast is installed and working (https://github.com/oziee/ha-solcast-solar)
 
- Note that Predbat does not update Solcast for you, it's recommended that you disable polling (due to the API polling limit) in the Solcast plugin and instead have your own automation that updates the forecast a few times a day (e.g. dawn, dusk and just before your nightly charge slot).

- Example Solcast update script:

```
alias: Solcast update
description: ""
trigger:
  - platform: time
    at: "23:00:00"
  - platform: time
    at: "12:00:00"
  - platform: time
    at: "04:00:00"
condition: []
action:
  - service: solcast_solar.update_forecasts
    data: {}
mode: single
```

## Energy rates

### Octopus Energy Plugin
- If you want to use real pricing data and have Octopus Energy then ensure you have the Octopus Energy plugin installed and working (https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy/)

### Rate bands

- you can configure your rate bands (assuming they repeat) using rates_import/rates_export (see below)

### Octopus Intelligent Plugin

- If you are on Intelligent and want to include charging slots outside the normal period or account in your predictions for your car charging then use the Octopus Intelligent plugin and ensure it's configured (https://github.com/megakid/ha_octopus_intelligent). 
- Batpred may decide to charge in these slots as well.

## Car charging planning

There are two ways to plan car charging slots
- Enable Octopus Intelligent plugin - in which case Predbat will use the slots allocated by Intelligent in battery prediction
  - Ensure **octopus_intelligent_slot** points to the Intelligent plugin
  - Set **octopus_intelligent_charging** to True
  - Information about the cars battery size will also be extracted from the Intelligent plugin
  - You will need to set the cars current soc sensor, **car_charging_soc** correctly to have accurate results
  - If you set **car_charging_limit** then Batpred can also know if the cars limit is set lower than Intelligent 
  - Let the intelligent app control when your car charges
- Predbat led charging - Here Predbat plans the charging based on the upcoming low rate slots
  - Ensure **car_charging_limit**, **car_charging_soc** and **car_charging_planned** are set correctly
  - Set **car_charging_plan_time** in the config or in HA to the time you want the car ready by
  - Enable **car_charging_plan_smart** if you want to use the cheapest slots only
  - Use an automation based on **binary_sensor.predbat_car_charging_slot** to control when your car charges

## config.yml settings

### Basics

Basic configuration items
  - **timezone** - Set to your local timezone, default is Europe/London (https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568)
  - **notify_devices** - A list of device names to notify, the default is just 'notify' which contacts all mobile devices
  - **run_every** - Set the number of minutes between updates, default is 5 (recommended), must divide into 60 to be aligned correctly (e.g. 10 or 15 is okay)
  - **user_config_enable** - When True the user configuration is exposed in Home Assistant as input_number and switch, the config file becomes just the defaults to use
  - **days_previous** - A list of the the number of days to go back in the history to predict your load, recommended settings are 1, 7 or both 7 and 14 (if you have enough data). Each list entry is weighted with **days_previous_weight**. Keep in mind HA default history is only 10 days.
  - **days_previous_weight** A list of the weightings to use of the data for each of the days in days_previous.
  - **forecast_hours** - the number of hours to forecast ahead, 48 is the suggested amount.
  - **forecast_plan_hours** - the number of hours after the next charge slot to include in the plan, default 24 hours is the suggested amount (to match energy rate cycles)
  - **max_windows** - Maximum number of charge and discharge windows, the default is 32.  Larger numbers of windows can increase runtime, but is needed if you decide to use smaller slots (e.g. 5, 10 or 15 minutes). 
  
### Inverter information
The following are entity names in HA for GivTCP, assuming you only have one inverter and the entity names are standard then it will be auto discovered
  - **num_inverters** - If you increase this above 1 you must provide multiple of each of these entities
  - **geserial** - This is a helper regular expression to find your serial number, if it doesn't work edit it manually or change individual entities to match:

### Historical data

#### Data from GivTCP

  It's recommended you get this data from GivTCP, there are also controls for load_scaling and import_export_scaling if they need scale adjustments
  
  - **load_today** - GivTCP Entity name for the house load in kwh today (must be incrementing)
  - **import_today** - GivTCP Imported energy today in Kwh (incrementing)
  - **export_today** - GivTCP Exported energy today in Kwh (incrementing)

#### GivEnergy Cloud Data

   If you have an issue with the GivTCP data you can get this historical data from the GivEnergy cloud instead. This data is updated every 30 minutes

  - **ge_cloud_data**   - When True use the GE Cloud for data rather than load_today, import_today and export_today
  - **ge_cloud_serial** - Set the inverter serial number to use for the Cloud data
  - **ge_cloud_key**    - Set to your API Key for GE Cloud (long string)

### Inverter control

  - **inverter_limit** - One per inverter, when set defines the maximum watts of AC power for your inverter (e.g. 3600). This will help to emulate clipping when your solar produces more than the inverter can handle, but it won't be that accurate as the source of the data isn't minute by minute. If you have a separate Solar inverter as well then add the solar inverter limit to the battery inverter limit to give one total amount. 

#### REST Interface inverter control
  - **givtcp_rest** - One per Inverter, sets the REST API URL (http://homeassistant.local:6345 is the normal one). When enabled the Control per inverter below isn't used and instead communication is directly via REST and thus bypasses some issues with MQTT. If using Docker then change homeassistant.local to the Docker IP address.

  To check your REST is working open up the readData API point in a Web browser e.g: http://homeassistant.local:6345/readData
 
  If you get a bunch of inverter information back then it's working!

  It's recommended you enable Raw register output in GivTCP for added monitoring:
  
  ![image](https://github.com/springfall2008/batpred/assets/48591903/e6cf0304-57f3-4259-8354-95a7c4f9b77f)

#### Home-assistant Inverter control

Control per inverter (only used if REST isn't set):
  - **soc_kw** - GivTCP Entity name of the battery SOC in kwh, should be the inverter one not an individual battery
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
  - **scheduled_charge_enable** - GivTCP Scheduled charge enable config
  - **scheduled_discharge_enable** - GivTCP Scheduled discharge enable config
  - **discharge_start_time** - GivTCP scheduled discharge slot_1 start time
  - **discharge_end_time** - GivTCP scheduled discharge slot_1 end time

### Solcast

The following are entity names in Solcast, unlikely to need changing although a few people have reported their entity names don't contain 'solcast' so worth checking:  
  - **pv_forecast_today** - Entity name for solcast today's forecast
  - **pv_forecast_tomorrow** - Entity name for solcast forecast for tomorrow
  - **pv_forecast_d3** - Entity name for solcast forecast for day 3
  - **pv_forecast_d4** - Entity name for solcast forecast for day 4 (also d5, d6 & d7 are supported but not that useful)
  
### Octopus energy

The following are entity names in the Octopus Energy plugin and the Octopus Intelligent plugin.
They are set to a regular expression and auto-discovered but you can comment out to disable or set them manually.
  - **metric_octopus_import** - Import rates from the Octopus plugin
  - **metric_octopus_export** - Export rates from the Octopus plugin
  - **octopus_intelligent_slot** - If you have Octopus intelligent and the Intelligent plugin installed point to the 'slot' sensor
  - **octopus_intelligent_charge_rate** - When set to non-zero amount (e.g. 7.5) it's assumed the car charges during intelligent slots using this or the data reported by Octopus
  - **octopus_intelligent_charging** - When enabled Predbat will plan charging around the octopus intelligent slots, taking it into account for battery load and generating the slot information

Or you can override these by manually supplying an octopus pricing URL (expert feature)
  - **rates_import_octopus_url**
  - **rates_export_octopus_url**

### Manual energy rates

Or manually set your rates in a 24-hour period using these:
  - rates_import
    - start
      end
      rate
  - rates_export
    - start
      end
      rate

### Car charging filtering

You might want to remove your electric car charging data from the historical load as to not bias the calculations, otherwise you will get high charge levels when the car was charged previously (e.g. last week)

  - **car_charging_hold** - When true car charging data is removed from the simulation (by subtracting car_charging_rate), as you either charge from the grid or you use the intelligent plugin to predict when it will charge correctly (default 6kw, configure with **car_charging_threshold**)
  - **car_charging_threshold** - Sets the threshold above which is assumed to be car charging and ignore (default 6 = 6kw)
  - **car_charging_energy** - Set to a HA entity which is incrementing kWh data for the car charger, will be used instead of threshold for more accurate car charging data to filter out
 
### Planned car charging

These features allow Predbat to know when you plan to charge your car. If you have Octopus Intelligent set up you won't need to change these as it's done automatically via their app and the Intelligent plugin.

  - **octopus_intelligent_charging** - When enabled Predbat will plan charging around the octopus intelligent slots, taking it into account for battery load and generating the slot information

Only needed if you don't use Intelligent:
  - **car_charging_planned** - Can be set to a sensor which lets Predbat know the car is plugged in and planned to charge during low rate slots, or False to disable or True to always enable
  - **car_charging_planned_response** - An array of values from the planned sensor which indicate that the car is plugged in and will charge in the next low rate slot
  - **car_charging_rate** - Set to the cars charging rate (normally 7.5 for 7.5kw). 
  - **car_charging_battery_size** - Indicates the cars battery size in kwh, defaults to 100. It will be used to predict car charging stops. 

  - **car_charging_plan_time** - When using Batpred led planning set this to the time you want the car to be charged by
  - **car_charging_plan_smart** - When true the cheapest slots can be used for charging, when False it will be the next low rate slot
  
Connect to your cars sensors for accurate data:
  - **car_charging_limit** - The % limit the car is set to charge to, link to a suitable sensor. Default is 100%
  - **car_charging_soc** - The cars current % charge level, link to a suitable sensor. Default is 0%

Control how your battery behaves during car charging:
  - **car_charging_from_battery** - When True the car can drain the home battery, Predbat will manage the correct level of battery accordingly. When False home battery discharge will be prevented when your car charges, all load from the car and home will be from the grid. This is achieved by setting the discharge rate to 0 during car charging and to the maximum otherwise, hence if you turn this switch Off you won't be able to change your discharge rate outside Predbat. The home battery can still charge from the grid/solar in either case. Only use this if Predbat knows your car charging plan, e.g. you are using Octopus Intelligent or you use the car slots in Predbat to control your car charging.

### Workarounds

  - **battery_scaling** - Scales the battery reported SOC Kwh e.g. if you set 0.8 your battery is only 80% of reported capacity. If you are going to chart this you may want to use **predbat.soc_kw_h0** as your current status rather than the GivTCP entity so everything lines up
  - **import_export_scaling** - Scaling the import & export data from GivTCP - used for workarounds
  - **inverter_clock_skew_start**, **inverter_clock_skew_end** - Skews the setting of the charge slot registers vs the predicted start time (see apps.yml)
  - **inverter_clock_skew_discharge_start**, **inverter_clock_skew_discharge_end** - Skews the setting of the discharge slot registers vs the predicted start time (see apps.yml)
  - **clock_skew** - Skews the local time that Predbat uses (from AppDaemon), will change when real-time actions happen e.g. triggering a discharge.
  - **predbat_battery_capacity_nominal** - When enabled Predbat uses the reported battery size from the Nominal field rather than from the normal GivTCP reported size. If your battery size is reported wrongly maybe try turning this on and see if it helps.

### Triggers

The trigger figure is useful to help trigger your own automation based on having spare solar energy that you would otherwise export

For each trigger give a name, the minutes of export needed and the energy required in that time
Multiple triggers can be set at once so in total you could use too much energy if all run
Each trigger create an entity called 'binary_sensor.predbat_export_trigger_<name>' which will be turned On when the condition is valid
connect this to your automation to start whatever you want to trigger.

Set the name for each trigger, the number of minutes of solar export you need and the amount of energy in kwH you will need available during that time period.

For example:

```
 export_triggers:
     - name: 'large'
       minutes: 60
       energy: 1.0
     - name: 'small'
       minutes: 15
       energy: 0.25
```

## Configuration guide

First get the basics set up, ensure you have the inverter controls configured, the historical load data and the solar forecast in place. Make sure your energy rates are configured correctly for import and export.

If you have an EV try to set up the car charging sensor correctly so the tool can tell what part of your historical load is EV charging. You might want to also set to the car charging plan so you can predict when your car is plugged in and how much it will charge.

You should try to tune **inverter_loss**, **battery_loss** and **battery_loss_discharge** to the correct % loss for your system in order to get more accurate predictions. Around 4% for each is good for a hybrid inverter. Also set **inverter_hybrid** to True or False depending on if you have a Hybrid or AC Coupled battery.

### Fixed daily rates
- In this case you will just be predicting the battery levels, no charging or discharging is required although it won't hurt if you leave these options enabled.

### Cheap night rate (e.g. Octopus Go, Intelligent, Economy 7 etc)
- In this scenario you will want to charge overnight based on the next days solar forecast.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

```
set_soc_enable - True              # Allow the tool to configure the charge %
set_reserve_enable - True          # Use the reserve to stop fluctuations in the charge % when charging
set_reserve_hold - True            # Means if you don't need to charge then charging won't be enabled
calculate_best_charge - True       # You want the tool to calculate charging
combine_charge_slots - True        # As you have just one overnight rate then one slot is fine
metric_min_improvement - 0         # Charge less if it's cost neutral 
set_charge_window - True           # You want to have Predbat control the charge window
best_soc_keep - 2.0                # Tweak this to control what battery level you want to keep as a backup in case you use more energy
best_soc_min - 0.0                 # You can also set this to best_soc_keep if you don't want charging to be turned off overnight when it's not required
rate_low_threshold - 0.8           # Consider a 20% reduction in rates or more as a low rate
calculate_discharge_first - False  # You probably only want to discharge any excess 
```

If you have Intelligent then make sure the intelligent plugin is also set up so the tool can see when you plan to charge your car and take this into account.

### Multiple rates for import and export (e.g. Octopus Flux & Cozy)

Follow the instructions from Cheap Night rate above, but also you will want to have automatic discharge when the export rates are profitable.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

```
calculate_best_discharge - True        # Enable discharge calculation
calculate_discharge_first - True       # Give priority to discharge when it's profitable
calculate_discharge_oldest - True      # Make the discharge as late as possible so it has more time to adjust (once you have discharged you can't get it back)
combine_discharge_slots - True         # As these rates have fixed longer periods then a single slot is fine
set_discharge_window - True            # Allow the tool to control the discharge slots
metric_min_improvement - 0             # Charge less if it's cost neutral 
metric_min_improvement_discharge - 0.1 # Make sure discharge only happens if it makes a profit
rate_high_threshold: 1.2               # Rates at least 20% above the average count as export slots
```

### Half hourly variable rates (e.g. Octopus Agile)

Recommended settings - these must be changed in Home Assistant once Predbat is running:

```
calculate_best_discharge - True        # Enable discharge calculation
calculate_discharge_first - True       # Give priority to discharge when it's profitable
calculate_discharge_oldest - True      # Make the discharge as late as possible so it has more time to adjust (once you have discharged you can't get it back)
set_discharge_window - True            # Allow the tool to control the discharge slots
combine_discharge_slots - False        # Split into 30 minute chunks for best optimisation
discharge_slot_split - 30              # 30 minute split matches slots
metric_min_improvement - 0             # Charge less if it's cost neutral 
metric_min_improvement_discharge - 0.1 # Make sure discharge only happens if it makes a profit
max_windows - 128                      # Ensure you have enough slots
rate_low_match_export - False          # Start with this at False but you can try it as True if you want to charge at higher rates to export even more
rate_high_threshold: 1.2               # Consider more valuable export slots only
```

If you have a fixed export rate then follow the above for variable rates but change:

rate_high_threshold: 1.0               # Consider all slots for export

## Video Guides

Many people have been asking for video guides for Predbat so I'm going to start recording some of them.

Basic installation:
   - https://www.loom.com/share/549cc800277b4d39874d9d6a65c0d0aa?sid=580b3293-f65c-4f6b-9c8c-0bef4cb75cc1
   - https://www.loom.com/share/e46e9e0159b04cc89abb05ef21c34f9c?sid=a0fc7e9c-1484-4535-8052-46f6f9721862
AppDaemon log files:
   - https://www.loom.com/share/562e3246c359451ea69428316f58f17f?sid=30bee2e7-86fc-4aca-8081-7c0de255b2e7
Historical data:
   - https://www.loom.com/share/43f3a71e9a6448c4844a26fbc4f19a3d?sid=8fc24279-4a86-4acc-9fad-e4841d0c01b3
Configuring Predbat:
   - https://www.loom.com/share/fa0db1b1fce34db09bb4af76b2e7edef?sid=6456019a-62e3-4e59-95f9-092474a8a5e5
   - https://www.loom.com/share/78e4b4f91fdb45068665e769334a934b?sid=0388e0c3-5cf1-42ee-8dd3-3992683c4c36
Charts:
   - https://www.loom.com/share/e0e312fbb6874f559cd91ca8e292686c?sid=8125a3b0-0321-4583-9038-e252ddbcb038

## FAQ

  - I've installed Batpred but I don't see the correct entities:
    - First look at AppDaemon.log (can be found in the list of log files in the System/Log area of the GUI). See if any errors are warnings are found. If you see an error it's likely something is configured wrongly, check your entity settings are correct.
    - Make sure Solcast is installed and it's auto-updated at least a couple of times a day (see the Solcast instructions). The default solcast sensor names maybe wrong, you might need to update the apps.yml config to match your own names (some people don't have the solcast_ bit in their names)
  - Why is my predicted charge % higher or lower than I might expect?
    - Batpred is based on costing, so it will try to save you money. If you have the PV 10% option enabled it will also take into account the more worse case scenario and how often it might happen, so if the forecast is a bit unreliable it's better to charge more and not risk getting stung importing.
    - Have you checked your energy rates for import and export are correct, maybe check the rates graph and confirm. If you do something like have export>import then Batpred will try to export as much as possible.
    - Have you tuned Solcast to match your output accurately?
    - Have you tuned the **metric_min_improvement**, **best_soc_min** and **best_soc_keep settings**?
    - Do you have predicted car charging during the time period?
    - You can also tune **load_scaling** and **pv_scaling** to adjust predictions up and down a bit
    - Maybe your historical data includes car charging, you might want to filter this out using car_charging_hold (see below)
  - Why didn't the slot actually get configured?
     - make sure **set_charge_window** and **set_soc_enable** is turned on
  - If you are still having trouble feel free to raise a ticket for support to post on the GivTCP facebook group.
  - The charge limit keeps increasing/decreasing in the charge window or is unstable
     - Check you don't have any other automations running that adjust GivTCP settings during this time. Some people had a script that changes the reserve %, this will cause problems - please disable other automations and retry.
  - I changed a config item but it made no difference?
     - If **user_config_enable** is True then many config items are now inside Home Assistant, in which case change it there instead.
  - It's all running but I'm not getting very good results
    - You might want to tune **best_soc_keep** to set a minimum target battery level, e.g. I use 2.0 (for 2kwh, which is just over 20% on a 9.5kwh battery)
    - Have a read of the user configuration guide above depending on your tariff different settings maybe required
    - Check your solar production is well calibrated (you can compare solcast vs actually in home assistant energy tab or on the GivEnergy portal)
    - Make sure your inverter max AC rate has been set correctly
    - If you have an EV that you charge then you will want some sort of car charging sensor or use the basic car charging hold feature or your load predictions maybe unreliable
    - Do you have a solar diverter? If so maybe you want to try using the IBoost model settings.
    - Perhaps set up the calibration chart and let it run for 24 hours to see how things line up
    - If your export slots are too small compared to expected check your inverter_limit is set correctly (see below) 

## Customisation 

These are configuration items that you can modify to fit your needs, you can configure these in Home Assistant directly.
Changing the items in apps.yml will have no effect.

Each config item has an input_number or switch associated with it, see the example dashboard for their exact names (https://github.com/springfall2008/batpred/blob/main/example_dashboard.yml)

### Battery loss options

**battery_loss** accounts for energy lost charging the battery, default 0.05 is 5%

**battery_loss_discharge** accounts for energy lost discharging the battery, default 0.05 is 5%

**inverter_loss** accounts for energy loss during going from DC to AC or AC to DC, default is 0% for legacy reasons but please adjust.

**inverter_hybrid** When True you have a hybrid inverter so no inverter losses for DC charging. When false you have inverter losses as it's AC coupled battery.

### Scaling and weight options

**battery_rate_max_scaling** adjusts your maximum charge/discharge rate from that reported by GivTCP
e.g. a value of 1.1 would simulate a 10% faster charge/discharge than reported by the inverter

**load_scaling** is a Scaling factor applied to historical load, tune up if you want to be more pessimistic on future consumption
Use 1.0 to use exactly previous load data (1.1 would add 10% to load)

**pv_scaling** is a scaling factor applied to pv data, tune down if you want to be more pessimistic on PV production vs Solcast
Use 1.0 to use exactly the solcast data (0.9 would remove 10% from forecast)

**pv_metric10_weight** is the weighting given to the 10% PV scenario. Use 0.0 to disable this.
A value of 0.1 assumes that 1:10 times we get the 10% scenario and hence to count this in the metric benefit/cost. 
A value of 0.15 is recommended.

### Car charging hold options

Car charging hold is a feature where you try to filter out previous car charging from your historical data so that future predictions are more accurate.

When **car_charging_hold** is enabled loads of above the power threshold **car_charging_threshold** then you are assumed to be charging the car and **car_charging_rate** will be subtracted from the historical load data.

For more accurate results can you use an incrementing energy sensor set with **car_charging_energy** in the apps.yml then historical data will be subtracted from the load data instead.

**car_charging_energy_scale** Is used to scale the **car_charging_energy** sensor, the default units are kwh so if you had a sensor in watts you might use 0.001 instead.

**car_charging_rate** sets the rate your car is assumed to charge at, but will be pulled automatically from Octopus Intelligent if enabled

**car_charging_loss** gives the amount of energy lost when charging the car (load in the home vs energy added to the battery). A good setting is 0.08 which is 8%.

### Car charging plan options

Car charging planning - is only used if Octopus intelligent isn't enabled and car_charging_planned is connected correctly. 

This feature allows Predbat to create a plan for when you car will charge, but you will have to create an automation to trigger your car to charge using **binary_sensor.predbat_car_charging_slot** if you want it to match the plan.

**car_charging_plan_time** Is set to the time you expect your car to be fully charged by
**car_charging_plan_smart** When enabled allows Predbat to allocated car charging slots to the cheapest times, when disabled all low rate slots will be used in time order.

**octopus_intelligent_charging** when true enables the octopus intelligent charging feature which will make Predbat create a car charging plan which is taken from the Octopus Intelligent plan
you must have set **octopus_intelligent_slot** sensor in apps.yml to enable this feature.

### Calculation options

**calculate_best** When enables tells Predbat to work out the best battery SOC % based on cost, when disabled no scenarios apart from the default settings are computed. 
This must be enabled to get all the 'best' sensors.

**calculate_best_charge**     If set to False then charge windows will not be calculated and the default inverter settings are used, when True predbat will decide the charge window automatically.

**calculate_charge_oldest**   If set to True the charge windows are calculated oldest first (in the highest price bracket), when False it's the newest first. Recommended to keep disabled.

**calculate_charge_all**      When True all charge windows are calculated to a single percentage in a first pass (or only pass if there is only 1 window). Recommended to keep enabled.

**calculate_charge_passes**   Sets the number of discharge calculation passes to run (for multi-window only), the default is 1 but 2 will increase run-time but might improve the schedule a tiny bit.

**calculate_best_discharge**   If set to False then discharge windows will not be calculated, when True they will be calculated. Default is True.

**calculate_discharge_all**    When True all discharge windows are calculated to a single percentage in a first pass (or only pass if there is only 1 window). Recommended to leave as False.

**calculate_discharge_passes** Sets the number of discharge calculation passes to run (for multi-window only), the default is 1 but 2 will increase run-time but might improve the schedule a tiny bit.

**calculate_discharge_oldest** When True calculate from the oldest window (in the highest price bracket) first, when false start from the newest. Default True (recommended).

**calculate_discharge_first**  When True discharge takes priority over charging (to maximise profit on export), when false charging is optimised first. Default to True

### Battery margins and metrics options

**best_soc margin** is added to the final SOC estimate (in kwh) to set the battery charge level (pushes it up). Recommended to leave this as 0.

**best_soc_min** sets the minimum charge level (in kwh) for charging during each slot and the minimum discharge level also (set to 0 if you want to skip some slots)

**best_soc_max** sets the maximum charge level (in kwh) for charging during each slot. A value of 0 disables this feature.

**best_soc_keep** is minimum battery level to try to keep above during the whole period of the simulation time, soft constraint only (use min for hard constraint). It's usually good to have this above 0 to allow some margin in case you use more energy than planned between charge slots.

**best_soc_step** is the accuracy to calculate the charge levels to, higher values make calculations quicker, smaller ones will take longer (recommended 0.5 or 0.25)

**best_soc_pass_margin** Only used for multiple charge windows, the margin to add to the first pass calculations only (default is 0 - recommended).

**combine_charge_slots** controls if charge slots of > 30 minutes can be combined. When disabled they will be split up, increasing run times but potentially more accurate for planning.
Not recommended to set to False when best_soc_min set to True or all slots will be kept. The default is enabled (True)

**charge_slot_split** When combine charge is False charge slots will be split into the given slot size, recommended 15 or 30 (must be multiple of 5) - default 30

**combine_discharge_slots** Controls if discharge slots of > 30 minute can be combined. When disabled they will be split up, increasing run times but potentially more accurate for planning.

**discharge_slot_split** When combine discharge is False discharge slots will be split into the given slot size, recommended 15 or 30 (must be multiple of 5) - default 30
A value of 15 maybe more useful for discharge planning if the available battery to export isn't that large

**combine_mixed_rates** When True allows mixed rates to be combined into a single average rate charge/discharge window (e.g. Agile windows)
A better plan is achieved leaving this false but it can speed up run time to have it True.

**metric_min_improvement** sets the minimum cost improvement that it's worth lowering the battery SOC % for.
If it's 0 then this is disabled and the battery will be charged less if it's cost neutral.
If you use **pv_metric10_weight** then you probably don't need to enable this as the 10% forecast does the same thing better 
Do not use if you have multiple charge windows in a given period as it won't lead to good results (e.g. Agile)
You could even go to something like -0.1 to say you would charge less even if it cost up to 0.1p more (best used with metric10)

**metric_min_improvement_discharge** Sets the minimum cost improvement it's worth discharging for. A value of 0 or 1 is generally good.

**rate_low_threshold** sets the threshold below average rates as the minimum to consider for a charge window, 0.8 = 80% of average rate
If you set this too low you might not get enough charge slots. If it's too high you might get too many in the 24-hour period.

**rate_low_match_export** - When enabled consider import rates that are lower than the highest export rate (minus any battery losses). 
This is if you want to be really aggressive about importing just to export, default is False (recommended).

**rate_high_threshold** Sets the threshold above average rates as to the minimum export rate to consider exporting for - 1.2 = 20% above average rate
If you set this too high you might not get any export slots. If it's too low you might get too many in the 24-hour period.

### Inverter control options

**set_charge_window** When enabled the next charge window will be automatically configured based on the incoming rates
Only works if the charging time window has been enabled and import rates are configured with the rates_import or using Octopus import
Will also automatically disable charging if not required and re-enable it when required. 
If you turn this off later check that 'GivTCP Enable Charge Schedule' is turned back on.

**set_window_minutes** defines how many minutes before the charge window we should program it (do not set above 30 if you are using Agile or similar)

**set_window_notify** enables mobile notifications about changes to the charge window

**set_discharge_window** When enabled automatically discharge (forced export) for export during high rate periods.

**set_discharge_notify** enables mobile notifications about changes to the discharge window.

**set_soc_enable** When enable automatically set the battery SOC charge amount a defined number of minutes before charging starts
NOTE: it maybe set more than once if things change

**set_soc_minutes** defines how many minutes before the charge window we should program it (do not set above 30 if you are using Agile or similar)
**set_soc_notify** enables mobile notifications about changes to the charge %

**set_reserve_enable** When True the reserve % will be reprogrammed during a charging window or discharging window to match the target SOC/discharge % in order
to prevent discharge and then reset back to minimum % outside the window. Set the set_reserve_min to your minimum reserve % which is often 4%.
The feature applies with **set_soc_enable** or **set_discharge_window** is True 

**set_reserve_min** Defines the reserve percentage to reset the reserve to when not in use, a value of 4 is the minimum and recommended to make use of the full battery

When **set_reserve_hold** is True then if the current charge % is above the target charging will be disabled and the reserve will be used to hold the level (Good for gen3 workaround)

### IBoost model options

IBoost model, when enabled with **iboost_enable** tries to model excess solar energy being used to heat hot water (or similar)
**iboost_max** Sets the max energy sets the number of kwh that iBoost can consume during a day before turning off - default 3kwh

**iboost_max_power** Sets the maximum power in watts to consume - default 2400

**iboost_min_power** Sets the minimum power in watts to consume - default 500

**iboost_min_soc** sets the minimum home battery soc % to enable iboost on, default 0

You will see **predbat.iboost_today** entity which tracks the estimated amount consumed during the day, and resets at night

### Debug

**debug_enable** when on prints lots of debug, leave off by default

## Output data

You can find an example dashboard with all the entities here: https://github.com/springfall2008/batpred/blob/main/example_dashboard.yml

- Basic status:
  - predbat.status - Gives the current status and logs any adjustments made to your inverter
  - predbat.battery_hours_left - The number of hours left until your home battery is predicated to run out (stops at the maximum prediction time)
  - predbat.charge_limit - The current charge limit used for the scenario in %
  - predbat.charge_limit_kw - The current charge limit used for the scenario in kwH
  - predbat.duration - The duration of the prediction maximum in hours
  - predbat.load_energy - Predicted load energy in Kwh
  - predbat.pv_energy - Predicted PV energy in Kwh
  - predbat.export_energy - Predicted export energy in Kwh
  - predbat.import_energy - Predicted import energy in Kwh
  - predbat.import_energy_battery - Predicted import energy to charge your home battery in Kwh
  - predbat.import_energy_house - Predicted import energy not provided by your home battery (flat battery or above maximum discharge rate)
  - predbat.soc_kw - Predicted state of charge (in Kwh) at the end of the prediction, not very useful in itself, but holds all minute by minute prediction data (in attributes) which can be charted with Apex Charts (or similar)
  - predbat.soc_min_kwh - The minimum battery level during the time period in Kwh
  - predbat.metric - Predicted cost metric for the next simulated period (in pence). Also contains data for charting cost in attributes.
  - predbat.battery_power - Predicted battery power per minute, for charting
  - predbat.pv_power - Predicted PV power per minute, for charting
  - predbat.grid_power - Predicted Grid power per minute, for charting
  - predbat.car_soc - Predicted car battery %
    
- When calculate_best is enabled a second set of entities are created for the simulation based on the best battery charge percentage:
  - predbat.best_battery_hours_left - Number of hours left under best plan
  - predbat.best_export_energy - Predicted exports under best plan
  - predbat_best_import_energy - Predicted imports under best plan
  - predbat_best_load - Predicted best load energy
  - predbat.best_pv_energy - Predicted Best PV energy in Kwh
  - predbat_best_import_energy_battery - Predicted imports to the battery under best SOC setting
  - predbat_best_import_energy_house - Predicted imports to the house under best SOC setting
  - predbat_soc_kw_best - Predicted best final state of charge (in Kwh), holds minute by minute prediction data (in attributes) to be charted
  - predbat.soc_kw_best_h1 - Single data point for the predicted state of charge in 1 hours time (useful for calibration charts, predicted vs actual)
  - predbat.soc_kw_best_h8 - Single data point for the predicted state of charge in 8 hours time (useful for calibration charts, predicted vs actual)
  - predbat.soc_kw_best_h12 - Single data point for hte predicted state of charge in 12 hours time (useful for calibration charts, predicted vs actual)
  - predbat_best_metric - The predicted cost if the proposed SOC % charge target is selected. Also contains data for charting cost in attributes.
  - predbat.best_charge_limit - Predicted best battery charge limit in percent
  - predbat.best_charge_limit_kw - Predicted best battery charge limit in kwH
  - predbat.best_discharge_limit - Predicted best battery discharge limit in percent (will be 0% when discharging or 100% when not)
  - predbat.best_discharge_limit_kw - Predicted best battery discharge limit in kwH
  - predbat.battery_power  - Predicted best battery power per minute, for charting
  - predbat.pv_power - Predicted best PV power per minute, for charting
  - predbat.grid_power - Predicted best Grid power per minute, for charting
  - predbat.car_soc_best - Predicated car battery % in  best plan
  - predbat.iboost_best - Gives the predicted energy going into the iBoost - for charter
  - input_number.iboost_today - Gives the amount of energy modelled into the diverter today, resets at 11:30pm each night. Increments in the day.

- The calculated results under PV 10% scenario
  - predbat.soc_kw_best10 - As soc_kw_best but using the 10% solar forecast, also holds minute by minute data (in attributes) to be charted
  - predbat.best10_pv_energy - Predicted PV 10% energy in Kwh
  - predbat.best10_metric - Predicted cost for PV 10%
  - predbat.best10_export_energy- Predicted export energy for PV 10%
  - predbat.best10_load_energy - Predicted load energy for PV 10%
  - predbat.best10_import_energy- Predicted import energy for PV 10%

- Energy rate data:
  - Low import rate entities
    - predbat.low_rate_cost - The lowest import rate cost in P
    - predbat.low_rate_start - Start time of the next low import rate
    - predbat.low_rate_end - End time of the next low import rate
    - predbat.low_rate_cost_2, predbat.low_rate_start_2, predbat.low_rate_end_2 - The following low import rate slot
    - binary_sensor.predbat_low_rate_slot - A sensor that indicates which there is a low energy rate slot active
  - High export rate entities
    - predbat.high_export_rate_cost - The highest rate cost in P
    - predbat.high_export_rate_start - Start time of the next high export rate
    - predbat.high_export_rate_end - End time of the next high export rate
    - predbat.high_export_rate_cost_2, predbat.high_export_rate_start_2, predbat.high_export_rate_end_2 - The following high export rate slot
    - binary_sensor.predbat_high_export_rate_slot - A sensor that indicates which there is a high export rate slot active
  - Other rate entities
    - predbat.rates - The current energy rates in P (also can be charted)
    - predbat.rates_export - The current energy export rates in P (also be be charted)
    - predbat.cost_today - The total cost of energy so far today (since midnight)
    - predbat.car_soc - The expected charge level of your car at the end of the simulation. Can also be charted.
    - predbat.car_soc_best - The expected charge level of your car at the end of the simulation using the proposed SOC%/Window. Can also be charted.

- Car data:
  - binary_sensor.predbat_car_charging_slot - A binary sensor suggesting when to charge your car (if the car planning is enabled)

Example data out:

![image](https://github.com/springfall2008/batpred/assets/48591903/5c73cd6e-3110-4ecd-af42-7e6d156af4b2)

## Creating the charts

To create the fancy chart 
- Install apex charts https://github.com/RomRider/apexcharts-card
- There are multiple charts, for each section of the example file create a new apexcharts card and copy the YAML into it
- Customise as you like

Example charts:

![image](https://github.com/springfall2008/batpred/assets/48591903/28f29756-2502-4079-9c75-398e8a1a0699)

![image](https://github.com/springfall2008/batpred/assets/48591903/4c3df49c-52e5-443f-b9c5-7a673c96b205)

![image](https://github.com/springfall2008/batpred/assets/48591903/5f1f504d-9251-4610-9403-2a5f4d0bf332)

![image](https://github.com/springfall2008/batpred/assets/48591903/c02d65cf-e502-4484-a58d-cff8fb93d0f3)

<img width="1052" alt="image" src="https://github.com/springfall2008/batpred/assets/48591903/a96934d3-753a-49da-800b-925896f87cb6">

## Todo list
  - Add the ability to take car charging data from power sensor (rather than just from energy)
  - Improve documentation
