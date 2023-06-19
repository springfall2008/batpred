# predbat
Home battery prediction and automatic charging for Home Assistant with GivTCP

Copyright (c) Trefor Southwell May 2023 - All rights reserved
This software maybe used at not cost for personal use only
No warranty is given, either expressed or implied
For support please raise a Github ticket or use the GivTCP Facebook page

**Please note from release v5.0 onwards many configuration options are now inside Home Assistant (if user_config_enable is set to True), for these options changing the apps.yml will have no effect once installed** 

- [predbat](#predbat)
  * [Operation](#operation)
  * [Install](#install)
    + [HACS install](#hacs-install)
    + [Manual install](#manual-install)
  * [Solar forecast](#solar-forecast)
    + [Solcast](#solcast)
  * [Energy rates](#energy-rates)
    + [Octopus Energy Plugin](#octopus-energy-plugin)
    + [Rate bands](#rate-bands)
    + [Octopus Intelligent Plugin](#octopus-intelligent-plugin)
  * [Car charging planning](#car-charging-planning)
  * [FAQ](#faq)
  * [config.yml - details:](#configyml---details-)
    + [Inverter information](#inverter-information)
    + [Historical load](#historical-load)
    + [Import and Export data](#import-and-export-data)
    + [Inverter control](#inverter-control)
      - [REST Interface inverter control](#rest-interface-inverter-control)
      - [Home-assistant Inverter control](#home-assistant-inverter-control)
    + [Solcast](#solcast-1)
    + [Octopus energy](#octopus-energy)
    + [Manual energy rates](#manual-energy-rates)
    + [No energy tariff data (legacy)](#no-energy-tariff-data--legacy-)
    + [Car charging filtering](#car-charging-filtering)
    + [Planned car charging](#planned-car-charging)
    + [Customisation](#customisation)
    + [Controlling the battery charging/discharging](#controlling-the-battery-charging-discharging)
    + [Other config](#other-config)
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

- When **user_config_enable** is set to True a set of input_number and switch configurations are created in Home Assistant which can be used to tune the predictions and charging/discharging

## Install

- You must have GivTCP installed and running first
  - You will need at least 24 hours history in HA for this to work correctly, the default is 7 days (but you configure this back 1 day if you need to)
- Install AppDaemon add-on https://github.com/hassio-addons/addon-appdaemon
   - Set the **time_zone** correctly in appdaemon.yml (e.g. Europe/London)
   - Add **thread_duration_warning_threshold: 30** to the appdaemon.yml file in the appdaemon section

### HACS install

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

- Once installed you will get automatic updates from each release!
- Enable AppDeamon in HACS: https://hacs.xyz/docs/categories/appdaemon_apps/
- Add https://github.com/springfall2008/batpred as a custom repository of type 'AppDaemon'
- Click on the Repo and Download the app

> After an update with HACS you may need to reboot AppDeamon as it sometimes reads the config wrongly during the update

- Edit in Homeassistant config/appdaemon/apps/predbat/config/apps.yml to configure
- Note that future updates will not overwrite apps.yml, but you may need to copy settings for new features across manually

### Manual install

- Copy apps/predbat/predbat.py to 'config/appdaemon/apps/' directory in home assistant
- Copy apps/predbat/apps.yml to 'config/appdaemon/apps' directory in home assistant
- Edit in Homeassistant config/appdaemon/apps/apps.yml to configure

- If you later install with HACS then you must move the apps.yml into config/appdaemon/apps/predbat/config

## Solar forecast

Predbat needs a solar forecast in order to predict battery levels.
If you don't have solar then comment out the Solar forecast part of the config: **pv_forecast_* **

### Solcast

- Make sure Solcast is installed and working (https://github.com/oziee/ha-solcast-solar)
- Note that Predbat does not update Solcast for you, it's recommended that you disable polling (due to the API polling limit) in the Solcast plugin and instead have your own automation that updates the forecast a few times a day (e.g. dawn, dusk and just before your nightly charge slot). 

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

## FAQ

  - I've installed Batbred but I don't see the correct entities:
    - First look at AppDeamon.log (can be found in the list of logfiles in the System/Log area of the GUI). See if any errors are warnings are found. If you see an error it's likely something is configured wrongly, check your entity settings are correct.
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

## config.yml - details:

### Basics

Basic configuration items
  - **timezone** - Set to your local timezone, default is Europe/London (https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568)
  - **notify_devices** - A list of device names to notify, the default is just 'notify' which contacts all mobile devices
  - **run_every** - Set the number of minutes between updates, default is 5 (recommended), must divide into 60 to be aligned correctly (e.g. 10 or 15 is okay)
  - **user_config_enable** - When True the user configuration is exposed in Home Assistant as input_number and switch, the config file becomes just the defaults to use
  - **days_previous** - sets the number of days to go back in the history to predict your load, recommended settings are 7 or 1 (can't be 0). Can also be a list of days which will be averaged. Keep in mind HA default history is only 10 days.
  - **forecast_hours** - the number of hours to forecast ahead, 48 is the suggested amount.
  - **max_windows** - Maximum number of charge and discharge windows, the default is 24.  Larger numbers of windows can increase runtime, but is needed if you decide to use smaller slots (e.g. 5, 10 or 15 minutes).
  
### Inverter information
The following are entity names in HA for GivTCP, assuming you only have one inverter and the entity names are standard then it will be auto discovered
  - **num_inverters** - If you increase this above 1 you must provide multiple of each of these entities
  - **geserial** - This is a helper regular expression to find your serial number, if it doesn't work edit it manually or change individual entities to match:
Data per inverter

### Historical load
  - **load_today** - GivTCP Entity name for the house load in kwh today (must be incrementing)

### Import and Export data 
  - **import_today** - GivTCP Imported energy today in Kwh (incrementing)
  - **export_today** - GivTCP Exported energy today in Kwh (incrementing) 

### Inverter control

  - **inverter_limit** - One per inverter, when set defines the maximum watts of AC power for your inverter (e.g. 3600). This will help to emulate clipping when your solar produces more than the inverter can handle, but it won't be that accurate as the source of the data isn't minute by minute.

#### REST Interface inverter control
  - **givtcp_rest** - One per Inverter, sets the REST API URL (http://homeassistant.local:6345 is the normal one). When enabled the Control per inverter below isn't used and instead communication is directly via REST and thus bypasses some issues with MQTT

#### Home-assistant Inverter control

Control per inverter (only used if REST isn't set):
  - **soc_kw** - GivTCP Entity name of the battery SOC in kwh, should be the inverter one not an individual battery
  - **soc_max** - GivTCP Entity name for the maximum charge level for the battery
  - **reserve** - GivTCP sensor name for the reserve setting in %
  - **inverter_mode** - GivTCP inverter mode control
  - **charge_enable** - GivTCP charge enable entity - says if the battery will be charged in the time window
  - **charge_limit** - GivTCP Entity name for used to set the SOC target for the battery in percentage
  - **charge_start_time** - GivTCP battery charge start time entity
  - **charge_end_time** - GivTCP battery charge end time entity
  - **charge_rate** - GivTCP battery charge rate entity in watts 
  - **discharge_rate** - GivTCP battery discharge max rate entity in watts
  - **scheduled_charge_enable** - GivTCP Scheduled charge enable config
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

### No energy tariff data (legacy)

Or set assumed rates for the house, battery charging and export.
You can't enable automatic charging windows with this option, it only works for a fixed charge time.

  - **metric_house** - Set to the cost per Kwh of importing energy when you could have used the battery
  - **metric_battery** - Set to the cost per Kwh of charging the battery
  - **metric_export** - Set to the price per Kwh you get for exporting

### Car charging filtering

You might want to remove your electric car charging data from the historical load as to not bias the calculations, otherwise you will get high charge levels when the car was charged previously (e.g. last week)

  - **car_charging_hold** - When true car charging data is removed from the simulation (by subtracting car_charging_rate), as you either charge from the grid or you use the intelligent plugin to predict when it will charge correctly (default 6kw, configure with car_charging_threshold)
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

### Customisation 

These are configuration items that you can modify to fit your needs, you can configure these in Home Assistant directly if **user_config_enable** is set to True. Once the user config is enabled then changing the items in apps.yml will have no effect.

  - battery_loss - The percent of energy lost when charging the battery, default is 0.05 (5%)
  - battery_loss_discharge - The percent of energy lost when discharging the battery, default is 0.05 (5%)
  - battery_scaling - Scales the battery reported SOC Kwh e.g. if you set 0.8 your battery is only 80% of reported capacity. If you are going to chart this you may want to use predbat.soc_kw_h0 as your current status rather than the GivTCP entity so everything lines up
  - load_scaling - scales the load by a fixed percentage (default is 1.0, set up e.g. 1.2 if you want to add a % margin to your load)
  - pv_scaling - scales the PV data by a fixed percentage (default is 1.0 for no adjustment, set down e.g. 0.80 if you want to scale back)
  - pv_metric10_weight - adds in a pecentage weighting to the 10% PV forecast, recommended to take into account more worst case scenario (e.g. use 0.15 for 15% weighting)

  - calculate_best_charge:      If set to False then charge windows will not be calculated and the default inverter settings are used
  - calculate_charge_oldest:    If set to True the charge windows are calculated oldest first (in the highest price bracket), when False it's the newest first
  - calculate_charge_all:       When True all charge windows are calculated to a single percentage in a first pass (or only pass if there is only 1 window)
  - calculate_charge_passes:    Sets the number of discharge calculation passes to run (for multi-window only), the default is 2 (more than 2 has no impact)
   
  - calculate_best_discharge:   If set to False then discharge windows will not be calculated - defaults to 'set_discharge_window'
  - calculate_discharge_all:    When True all discharge windows are calculated to a single percentage in a first pass (or only pass if there is only 1 window)
  - calculate_discharge_passes: Sets the number of discharge calculation passes to run (for multi-window only), the default is 1 (more than 2 has no impact)
  - calculate_discharge_oldest: When True calculate from the oldest window (in the highest price bracket) first, when false start from the newest

  - metric_min_improvement           - Set a threshold for reducing the battery charge level, e.g. set to 5 it will only reduce further if it saves at least 5p. Best set to 0 if you use pv_metric10_weight or have multiple slots
  - metric_min_improvement_discharge - Set a threshold for increasing discharge level. Set to 0 if you have multiple discharge slots

  - best_soc_margin - Sets the number of Kwh of battery margin you want for the best SOC prediction, it's added to battery charge amount for safety. Best set to 0 if you use multiple charge slots or pv_metric10_weight.
  - best_soc_min - Sets the minimum battery level SOC to propose for the prediction (best to disable for variable tariffs like Agile) for charge and discharge. This is hard constraint for all slots.
  - best_soc_keep - Sets the minimum battery level to try to keep above during the whole period of the simulation time (charging levels will be adjusted accordingly). This is a soft constraint (slots will not be forced to charge/discharge based on this number).
  - best_soc_step - Sets the accuracy of calculating the SOC, the larger values run quicker. Recommended 0.5 or 0.25.
  
  - combine_charge_slots -  Control if charge slots of > 30 minutes can be combined. When disabled they will be split up, increasing runtimes but potentially more accurate for planning.
  - charge_slot_split - When combine charge is False discharge slots will be split into the given slot size, recommended 15 or 30 (must be multiple of 5) - default 30
  - combine_discharge_slots - Control if discharge slots of > 30 minute can be combined. When disabled they will be split up, increasing runtimes but potentially more accurate for planning.
  - discharge_slot_split -  When combine discharge is False discharge slots will be split into the given slot size, recommended 15 or 30 (must be multiple of 5) - default 15
  - combine_mixed_rates - When True multiple 30 minute slots can be combined even if they have a different rate, default is False
  
  - rate_low_threshold - Sets the threshold for price per Kwh below average import price where a charge window is identified. Default of 0.8 means 80% of the average to select a charge window.
  - rate_high_threshold - Sets the threshold for price per Kwh above average export price where a discharge window is identified. Default of 1.2 means 20% above the average.
 
  - set_charge_window - When true automatically configure the next charge window in GivTCP, charge windows can also be disabled by Predbat when this is enabled.
  - set_window_minutes - Number of minutes before charging/discharging the window should be configured in GivTCP (default 30 - recommended)
  - set_window_notify - When True notifications about the charge window are raised to HA
 
  - set_discharge_window - When true automatic forced export slots will be calculated and programmed (assuming you have a variable export rate that is worth using).
  - set_discharge_notify - When true notifications for discharge windows are raised to HA
  
  - set_soc_enable - When true the best SOC Target will be automatically programmed
  - set_soc_minutes - Sets the number of minutes before the charge window to set the SOC Target, between this time and the charge window start the SOC will be auto-updated, and thus if it's changed manually it will be overriden.
  - set_soc_notify - When true a notification is sent with the new SOC target once set

  - set_reserve_enable - When true the reserve % will be set to the same as the SOC target % during charging windows and back to the minimum outside these windows
  - set_reserve_notify - When true notification will be sent about reserve % changes
  - set_reserve_min - Must be set to your minimum soc % for your system, the default is 4%. Do not set to zero if this is not allowed (most systems have a non-zero minimum)
  
  - debug_enable - option to print lots of debug messages

## Output data

- You will find new entities are created in HA:
  - predbat.status - Gives the current status and logs any adjustments made to your inverter
  - predbat.battery_hours_left - The number of hours left until your home battery is predicated to run out (stops at the maximum prediction time)
  - predbat_charge_limit - The current charge limit used for the scenario in %
  - predbat_charge_limit_kw - The current charge limit used for the scenario in kwH
  - predbat_duration - The duration of the prediction maximum in hours
  - predbat_load_energy - Predicted best load energy in Kwh
  - predbat.export_energy - Predicted export energy in Kwh
  - predbat.import_energy - Predicted import energy in Kwh
  - predbat.import_energy_battery - Predicted import energy to charge your home battery in Kwh
  - predbat.import_energy_house - Predicted import energy not provided by your home battery (flat battery or above maximum discharge rate)
  - predbat.soc_kw - Predicted state of charge (in Kwh) at the end of the prediction, not very useful in itself, but holds all minute by minute prediction data (in attributes) which can be charted with Apex Charts (or similar)
  - predbat.soc_min_kwh - The minimum battery level during the time period in Kwh
  - predbat.metric - Predicted cost metric for the next simulated period (in pence). Also contains data for charting cost in attributes.
- When calculate_best is enabled a second set of entities are created for the simulation based on the best battery charge percentage: 
  - predbat.best_export_energy - Predicted exports under best SOC setting
  - predbat_best_import_energy - Predicted imports under best SOC setting
  - predbat_best_load - Predicted best load energy
  - predbat_best_import_energy_battery - Predicted imports to the battery under best SOC setting
  - predbat_best_import_energy_house - Predicted imports to the house under best SOC setting
  - predbat_soc_kw_best - Predicted best final state of charge (in Kwh), holds minute by minute prediction data (in attributes) to be charted
  - predbat.soc_kw_best10 - As soc_kw_best but using the 10% solar forecast, also holds minute by minute data (in attributes) to be charted
  - predbat.soc_kw_best_h1 - Single data point for the predicted state of charge in 1 hours time (useful for calibration charts, predicted vs actual)
  - predbat.soc_kw_best_h8 - Single data point for the predicted state of charge in 8 hours time (useful for calibration charts, predicted vs actual)
  - predbat.soc_kw_best_h12 - Single data point for hte predicted state of charge in 12 hours time (useful for calibration charts, predicted vs actual)
  - predbat_best_metric - The predicted cost if the proposed SOC % charge target is selected. Also contains data for charting cost in attributes.
  - predbat.best_charge_limit - Predicted best battery charge limit in percent
  - predbat.best_charge_limit_kw - Predicted best battery charge limit in kwH
  - predbat.best_discharge_limit - Predicted best battery discharge limit in percent (will be 0% when discharging or 100% when not)
  - predbat.best_discharge_limit_kw - Predicted best battery discharge limit in kwH
  - predbat.low_rate_cost - The lowest rate cost in P
  - predbat.low_rate_start - Start time of the next low rate
  - predbat.low_rate_end - End time of the next low rate
  - predbat.rates - The current energy rates in P (also can be charted)
  - predbat.rates_export - The current energy export rates in P (also be be charted)
  - predbat.car_soc - The expected charge level of your car at the end of the simulation. Can also be charted.
  - predbat.car_soc_best - The expected charge level of your car at the end of the simulation using the proposed SOC%/Window. Can also be charted.
  
Example data out:

![image](https://github.com/springfall2008/batpred/assets/48591903/5c73cd6e-3110-4ecd-af42-7e6d156af4b2)

## Creating the charts

To create the fancy chart 
- Install apex charts https://github.com/RomRider/apexcharts-card
- Create a new apexcharts card and copy the YML from example_chart.yml into the chart settings, updating the serial number to match your inverter
- Customise as you like

Example charts:
![image](https://github.com/springfall2008/batpred/assets/48591903/39b6a1d5-8865-4855-9e60-6d8c4a3fbf12)

![image](https://github.com/springfall2008/batpred/assets/48591903/a10e570d-373e-4fce-aebf-dc6463067e3b)

![image](https://user-images.githubusercontent.com/48591903/236629117-8f05e050-d43d-4a52-a2a7-b5e97b961e3c.png)

## Todo list
  - Add the ability to take car charging data from power sensor (rather than just from energy)
  - Improve documentation
  - Consider using MQTT interface to HA  
