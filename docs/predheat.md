# predheat

Predheat attempts to model water based central heating systems based on a boiler or a heat pump.

## Operation

The app runs every 5 minutes and it will automatically update its prediction for the heating system for the next period, up to a maximum of 48 hours.

The inputs are as follows

- An external temperature sensor, can be a real one or one created by an Internet service
- An internal temperature sensor, ideally from your home thermostat.
- The target temperature sensor, this is what your home thermostat is set to.
- A heating energy sensor in kWh (not strictly required but needed to plot historical usage and calibrate)
- The flow temperature setting of your heating, can be static or a sensor
- Your current energy rates, either from the Octopus Energy plugin or hand typed into the configuration
- Some data about your home that you have to figure out for yourself and calibrate

The outputs are:

- Prediction of the internal house temperature going forward, including times when the heating will be active.
- Your predicted energy usage and costs. The energy usage, if electric, can also be connected into Predbat to help you project your home battery usage.

Future versions will also offer Predbat to run in master mode, controlling your homes heating in the same way as a smart thermostat (e.g. Nest)

## Installation guide

Predheat is now part of Predbat, first you will need to configure it using apps.yaml and then enable it by turning on **switch.predbat_predheat_enable**

### Openweather install

See: <https://www.home-assistant.io/integrations/openweathermap>

First create an OpenWeather account and then register for a "One Call by Call" subscription plan. This does need a credit/debit card but won't cost anything.
You get 1000 API calls a day for free, so edit your limit in the account to 1000 to avoid ever being charged.

Then add in the Home Assistant service and connect up your API key to obtain hourly weather data. Use the v3.0 API and ensure you have a 2024 version of Home Assistant.

### Apex Charts install

Use HACS to install Apex Charts (Lovelace frontend add-on) - <https://github.com/RomRider/apexcharts-card>

There is a template for the Predheat charts in: https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/templates/example_chart_predbat.yaml_template

Create a new Apex chart for each chart in this template and copy the YAML code into the chart.

## Configuration guide

First you need to edit apps.yaml to configure your system.

Copy the following template into The Predbat apps.yaml and edit the settings:

```yaml
predheat:
    # Days forward
    forecast_days: 2

    # Days previous is the number of days back to find historical load data
    # Recommended is 7 to capture day of the week but 1 can also be used
    # if you have more history you could use 7 and 14 (in a list) but the standard data in HA only lasts 10 days
    days_previous:
      - 7

    # Gas or heatpump mode ('gas' or 'pump')
    mode: gas

    # External temperature sensor
    # You may need to create a template sensor for this one
    external_temperature: sensor.external_temperature

    # Internal temperature sensor(s)
    internal_temperature:
      - sensor.living_room_temperature

    # Weather data
    weather: weather.openweathermap

    # Sensor with history that monitors the thermostat setting in the house
    target_temperature: sensor.living_room_target

    # When true models a smart thermostat that turns the heating ahead of the target temperature so it reaches it just in time
    smart_thermostat: True

    # Past energy consumption of heating in KWh, scaled with heating_energy_scaling
    heating_energy: sensor.boiler_nrgheat
    heating_energy_scaling: 1.4

    # Heating is turned on history
    heating_active: binary_sensor.boiler_heatingactive

    # House heat loss in watts per degree temp difference
    heat_loss_watts: 120

    # Static heat sources in the house (e.g. people/equipment)
    heat_gain_static: 200

    # House heat loss in degrees per hour per degree temp difference
    heat_loss_degrees: 0.030

    # Heating max output (of the radiators), in Watts at delta 50 (BTU / 3.41)
    # https://www.bestheating.com/milano-kent-straight-chrome-heated-towel-rail-various-sizes-91119
    # https://www.bestheating.com/milano-compact-type-11-single-panel-radiator-multi-sizes-available-74174
    # https://www.bestheating.com/milano-compact-type-22-double-panel-radiator-multi-sizes-available-74176
    # https://www.bestheating.com/milano-compact-type-21-double-panel-plus-radiator-multi-sizes-available-74175
    heat_output: 17000

    # Add up radiator volume + any pipework or expansion vessel
    heat_volume: 75

    # Heating max power in Watts
    heat_max_power: 30000
    heat_min_power: 7000

    # Heating cop is the maximum efficiency and will be scaled down based on temperatures
    # put 1.0 for condensing gas boilers, or around 4.0 for heat pumps
    heat_cop: 1.0

    # Current flow temperature setting
    flow_temp: number.boiler_heatingtemp
    flow_difference_target: 40
```

Set the mode (**mode**) to 'gas' or 'pump' depending on if you have a gas boiler or heat pump
Set the external temperature sensor (**external_temperature**) either to a real sensor or create one from the open weather map by adding this sensor to your configuration.yaml file for HA:

```yaml
template:
  - sensor:
    - name: "external_temperature"
      unit_of_measurement: 'c'
      state_class: measurement
      state: >
        {{ state_attr('weather.openweathermap', 'temperature') }}
```

Set **internal_temperature** to point to one or more internal temperature sensors, if you have a heating thermostat then ideally link it to this or to a sensor at least in a similar area of the house.

The **weather** configuration points to the Open Weather Map sensor by default so should work as-is.

Set the **target_temperature** to point to a sensor that indicates what your boiler thermostat is set to, or manually enter the temperature setting here.

Set **smart_thermostat** to True if your thermostat starts the boiler ahead of time for the new target temperature or False for regular options.

Set **heating_energy** To point to a sensor that indicates the energy consumed by your boiler/heat-pump in kWh. If the sensor isn't accurate then using **heating_energy_scaling** to adjust it to the actually energy consumed.

Now you need to make a list of all your radiators in the house, measure them and look up their BTU output at Delta 50 and their volume in Litres. The links below maybe useful for various standard radiators:

- <https://www.bestheating.com/milano-kent-straight-chrome-heated-towel-rail-various-sizes-91119>
- <https://www.bestheating.com/milano-compact-type-11-single-panel-radiator-multi-sizes-available-74174>
- <https://www.bestheating.com/milano-compact-type-22-double-panel-radiator-multi-sizes-available-74176>
- <https://www.bestheating.com/milano-compact-type-21-double-panel-plus-radiator-multi-sizes-available-74175>

Add up all the BTUs and divide by 3.41 to gain the heat output in Watts and set that in **heat_output** configuration option.
Add up all the litres of water, add in some extra for the piping and an expansion vessel if present (e.g. 5-10 litres) and set **heat_volume** accordingly.

Set the **heat_max_power** and **heat_min_power** to the minimum and maximum power output of your boiler/heat-pump in watts.

Set **heating_cop** to the nominal COP of your system. For a gas boiler use 1.0 (as the efficiency will be based on flow temperature) or for a heat pump set it to the best value which is likely around 4.0 (it will be scaled down for cold weather).

Set **flow_temp** To the target flow temperature of your system, either via a sensor or as a fixed value. E.g. gas boilers are often set to say 60 or 70 degrees while heat pumps are much lower e.g. 30 or 40.

Set **flow_difference_target** to be the difference in flow temperature (in vs out) where your heating system will run at full power if it is above. e.g. for gas boilers this maybe something around 40 while on a heat pump it could be much lower e.g. 10.

For your energy rates either have **metric_octopus_import** point to the current energy rate sensor (gas or electric) or comment it out and enter your rate(s) using **rates_import**

If you want to account for standing charge set **metric_standing_charge** to a sensor or enter it manually, if not comment it out.

Now comes the tricky part, we need to calculate the heat loss for your house:

What will help here is historical temperature data, find a time period in the last few weeks when your heating was turned off (for a few hours beforehand) and the house is cooling down.
Measure the number of degrees the house drops by in a given time period. Divide that figure (e.g. 1.5 degrees) by the time period e.g. (3 hours) and then again divide it by the
average difference between the inside and outside temperature
(e.g. 19 degrees inside, 9 degrees outside, so a temperature difference of 4 degrees) = 1.5 degrees / 3 hours / 10 degrees difference = 0.05. Set that figure to **heat_loss_degrees**.
It maybe best to compute this when it's cold out and if you have your heating turned off overnight.

_Note in future versions of Predheat I might make this calculation automatic._

Next we need to work out the number of watts of heat loss in the house, this can be done by looking at the energy consumed when the heating comes on. Pick a period of heating,
ideally from the time the temperature starts increasing for a complete hour of increase, looking at the increase in temperature in degrees,
add to that static heat loss which is  heat_loss_degrees _(internal temp - external temp)_ 1 hours to get the total degrees accounted for.
Now divide that by the external temperature difference again / (internal_temp - external_temp) and multiply the final figure by the energy your system consumed in Watts
during that period (can be found either from your sensor or just by looking at your energy bill for the same 1 hour period).

The final figure should be the number of watts your house loses per 1 degree of external temperature difference and be set to **heat_loss_watts**

Then you can set **heat_gain_static** to be the static heat output of other things in your house eg. computers and people. You can figure this out by looking at how many degrees of
temperature difference your house can maintain without any heating and multiply up your heat loss watts figure by this.
