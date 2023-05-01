# batpred
Home battery prediction for Home Assistant with GivTCP

Operation:

The app runs every 5 minutes, it will automatically update its prediction for the home battery levels for the next period, up to a maximum of 48 hours. It uses the solar production forecast from Solcast combined with your historical energy use and your plan charging slots to make a prediction.

The output is a prediction of the battery levels and import and export amounts.

Optionally it can also predict the results based on different target battery charge levels (SOC) and suggest the best option to use. This is based on a rough cost of imports and exports and adjusted with a safety margin. You can also have the target SOC automatically programmed into the inverter for the next day based on the calculation.

To install:

- You must have GivTCP installed and running first
  - You will need at least 24 hours history in HA for this to work correctly, the default is 7 days (but you configure this back 1 day if you need to)
- Install AppDeamon add-on https://github.com/hassio-addons/addon-appdaemon
   - In python packages (in the config) add 'tzlocal'
- Copy predbat.py to 'config/appdeamon/apps' directory in home assistant
- Edit config/appdemon/apps.yml and put into it the contents of apps.yml, but change the entity names to match your own inverter serial number
- Customise any settings needed

The following are entity names in HA and should be set correctly:
  - soc_kw - Entity name of the battery SOC in Kw, should be the inverter one not an individual battery
  - soc_max - Entity name for the maximum charge level for the battery
  - soc_percent - Entity name for used to set the SOC target for the battery in percentage
  - reserve - sensor name for the reserve setting in %
  - pv_forecast_today - Entity name for solcast today's forecast
  - pv_forecast_tomorrow - Entity name for solcast forecast for tomorrow
  - load_today - Entity name for the house load in kwh today (must be incrementing)
  - charge_enable - The charge enable entity - says if the battery will be charged in the time window
  - charge_start_time - The battery charge start time entity
  - charge_end_time - The battery charge end time entity
  - charge_rate - The battery charge rate entity in watts 
  - discharge_rate - The battery discharge max rate entity in watts
  
These are configuration items that you can modify to fit your needs:
  - battery_loss - The percent of energy lost when charing the battery, default is 0.05 (5%)
  - days_previous - sets the number of days to go back in the history to predict your load, recommended settings are 7 or 1 (can't be 0)
  - forecast_hours - the number of hours to forecast ahead, keep in mind Solcast only gives you the next day so much more than 24 won't be that useful
  - debug_enable - option to print lots of debug messages
  - car_charging_hold - When true it's assumed that the car charges from the grid and so any load values above a threshold (default 6kw, configure with car_charging_threshold) are the car and should be ignored in estimates
  - calculate_best - When true the algorithm tries to calculate the target % to charge the battery to overnight
  - best_soc_margin - Sets the number of Kwh of battery margin you want for the best SOC prediction, it's added to battery charge amount for safety
  - best_soc_min - Sets the minimum Soc to propose for the best SOC prediction
  - metric_house - Set to the cost per Kwh of importing energy when you could have used the battery
  - metric_battery - Set to the cost per Kwh of charging the battery
  - metric_export - Set to the price per Kwh you get for exporting
  - set_soc_enable - When true the best SOC Target will be automatically programmed
  - set_soc_minutes - Sets the number of minutes before the charge window to set the SOC Target, between this time and the charge window start the SOC will be auto-updated, and thus if it's changed manually it will be overriden.
  - set_soc_notify - When true a notification is sent with the new SOC target once set
  
- You will find new entities are created in HA:
  - predbat.battery_hours_left - The number of hours left until your home battery is predicated to run out (stops at the maximum prediction time)
  - predbat.export_energy - Predicted export energy in Kwh
  - predbat.import_energy - Predicted import energy in Kwh
  - predbat.import_energy_battery - Predicted import energy to charge your home battery in Kwh
  - predbat.import_energy_house - Predicted import energy not provided by your home battery (flat battery or above maximum discharge rate)
  - predbat.soc_kw - Predicted state of charge (in Kwh) at the end of the prediction, not very useful in itself, but holds all minute by minute prediction data (in attributes) which can be charted with Apex Charts (or similar)
- When calculate_best is enabled a second set of entities are created for the simulation based on the best battery charge percentage: 
  - predbat.best_export_energy
  - predbat_best_import_energy
  - predbat_best_import_energy_battery
  - predbat_best_import_energy_house
  - predbat_best_soc_kw
  
Example data out:

![image](https://user-images.githubusercontent.com/48591903/235373372-a8ed02d9-fdd1-497e-b245-e4e6b0407b6b.png)
  
To create the fancy chart 
- Install apex charts https://github.com/RomRider/apexcharts-card
- Create a new apexcharts card and copy the YML from example_chart.yml into the chart settings, updating the serial number to match your inverter
- Customise as you like

Example chart:

![image](https://user-images.githubusercontent.com/48591903/235373360-a2365956-6c8f-4d34-ac32-b57e4a46c7cd.png)
