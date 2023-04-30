# batpred
Home battery prediction for Home Assistant with GivTCP

To install:

- You must have GivTCP installed and running first
- Install AppDeamon add-on https://github.com/hassio-addons/addon-appdaemon
   - In python packages (in the config) add 'tzlocal'

- Copy predbat.py to 'config/appdeamon/apps' directory in home assistant
- Edit config/appdemon/apps.yml and put into it the contents of apps.yml, but change the entity names to match your own inverter serial number
- Customise any settings needed 
  - days_previous - sets the number of days to go back in the history to predict your load, recommended settings are 7 or 1 (can't be 0)
  - forecast_hours - the number of hours to forecast ahead, keep in mind Solcast only gives you the next day so much more than 24 won't be that useful
  - debug_enable - option to print lots of debug messages
  - car_charging_hold - When true it's assumed that the car charges from the grid and so any load values above a threshold (default 6kw, configure with car_charging_threshold) are the car and should be ignored in estimates
  - soc_kw - this is the battery SOC in Kw sensor name, should be the inverter one not an individual battery
  - soc_max - sensor name for the maximum charge level for the battery
  - reserve - sensor name for the reserve setting in %
  - pv_forecast_today - sensor for solcast today's forecast
  - pv_forecast_tomorrow - sensor for solcast forecast for tomorrow
  - load_today - Sensor for the house load in kwh today (must be incrementing)
  - charge_enable - The charge enable entity - says if the battery will be charged in the time window
  - charge_start_time - The battery charge start time entity
  - charge_end_time - The battery charge end time entity
  - charge_rate - The battery charge rate entity in watts 
  - discharge_rate - The battery discharge max rate entity in watts
- You will find new entities are created in HA:
  - predbat.battery_hours_left - The number of hours left until your home battery is predicated to run out (stops at the maximum prediction time)
  - predbat.export_energy - Predicted export energy in Kwh
  - predbat.import_energy - Predicted import energy in Kwh
  - predbat.import_energy_battery - Predicted import energy to charge your home battery in Kwh
  - predbat.import_energy_house - Predicted import energy not provided by your home battery (flat battery or above maximum discharge rate)
  - predbat.soc_kw - Predicted state of charge (in Kwh) at the end of the prediction, not very useful in itself, but holds all minute by minute prediction data (in attributes) which can be charted with Apex Charts (or similar)

To create the fancy chart 
- Install apex charts https://github.com/RomRider/apexcharts-card
- Create a new apexcharts card and copy the YML from example_chart.yml into the chart settings, updating the serial number to match your inverter
- Customise as you like
