# Predheat

Predheat attempts to model water-based central heating systems based on a boiler or a heat pump.

## Operation

The app runs every 5 minutes and it will automatically update its prediction for the heating system for the next period, up to a maximum of 48 hours.

The inputs are as follows

- An external temperature sensor, can be a real one or one created by an Internet service
- An internal temperature sensor, ideally from your home thermostat.
- The target temperature sensor, is what your home thermostat is set to.
- A heating energy sensor in kWh (not strictly required but needed to plot historical usage and calibrate)
- The flow temperature setting of your heating, can be static or a sensor
- Your current energy rates, either from the Octopus Energy plugin or hand-typed into the configuration
- Some data about your home that you have to figure out for yourself and calibrate

The outputs are:

- Prediction of the internal house temperature going forward, including times when the heating will be active.
- Your predicted energy usage and costs. The energy usage, if electric, can also be connected to Predbat to help you project your home battery usage.

Future versions will also offer Predbat to run in master mode, controlling your home's heating in the same way as a smart thermostat (e.g. Nest)

## Installation guide

Predheat is now part of Predbat, you will need to configure it using apps.yaml and then enable it by turning on **switch.predbat_predheat_enable**

### Openweather install

See: <https://www.home-assistant.io/integrations/openweathermap>

Create an OpenWeather account and then register for a "One Call by Call" subscription plan. This does need a credit/debit card but won't cost anything.
You get 1000 API calls a day for free, so edit your limit in the account to 1000 to avoid ever being charged.

Then add in the Home Assistant service and connect up your API key to obtain hourly weather data. Use the v3.0 API and ensure you have a 2024 version of Home Assistant.

### Apex Charts install

Use HACS to install Apex Charts (Lovelace frontend add-on) - <https://github.com/RomRider/apexcharts-card>

There is a template for the Predheat charts in: <https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/templates/example_chart_predbat.yaml_template>

Create a new Apex chart for each chart in this template and copy the YAML code into the chart.

## Configuration guide

You need to edit apps.yaml to configure your system.

Copy the following template into The Predbat apps.yaml and edit the settings: [predheat.yaml](https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/templates/predheat.yaml)

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

Set **heating_energy** To point to a sensor that indicates the energy consumed by your boiler/heat pump in kWh. If the sensor isn't accurate then use **heating_energy_scaling** to adjust it to the actual energy consumed.
You can also comment this line out if you don't have a sensor, but no historical cost information will be produced.

Now you need to make a list of all your radiators in the house, measure them and look up their BTU output at Delta 50 and their volume in Litres. The links below may be useful for various standard radiators:

- <https://www.bestheating.com/milano-kent-straight-chrome-heated-towel-rail-various-sizes-91119>
- <https://www.bestheating.com/milano-compact-type-11-single-panel-radiator-multi-sizes-available-74174>
- <https://www.bestheating.com/milano-compact-type-22-double-panel-radiator-multi-sizes-available-74176>
- <https://www.bestheating.com/milano-compact-type-21-double-panel-plus-radiator-multi-sizes-available-74175>

Add up all the BTUs and divide by 3.41 to gain the heat output in Watts and set that in **heat_output** configuration option.
Add up all the litres of water, add in some extra for the piping and an expansion vessel if present (e.g. 5-10 litres) and set **heat_volume** accordingly.

Set the **heat_max_power** and **heat_min_power** to the minimum and maximum power output of your boiler/heat pump in watts. This should be specified as the maximum output power
and not the maximum input energy. E.g. a heat pump with a COP of 4 might output 7kW but could only consume 1.7kW.

Set **hysteresis** To the amount of hysteresis in degrees applied by your thermostat when turning it on, the default is 0.5
Set **hysteresis_off** To the amount of hysteresis in degrees applied by your thermostat when turning it off, the default is 0.1

Set **heating_cop** to the nominal COP of your system. For a gas boiler use 1.0 (as the efficiency will be based on flow temperature) or for a heat pump set it to the best value which is likely around 4.0 (it will be scaled down for cold weather).

Set **flow_temp** To the target flow temperature of your system, either via a sensor or as a fixed value. E.g. gas boilers are often set to say 60 or 70 degrees while heat pumps are much lower e.g. 30 or 40.

Set **flow_difference_target** to be the difference in flow temperature (in vs out) where your heating system will run at full power if it is above. e.g. 
for gas boilers this maybe something around 40 while on a heat pump, it could be much lower e.g. 10.

Set **volume_temp** If you have a sensor on your radiators which can confirm the water temperature, this must not be near the heat pump/boiler but instead as close to the
interior temperature sensor as possible. If you do not have a sensor then instead PredHeat will calculate the next temperature and store it in **next_volume_temp** for use
in the next calculation cycle.

For energy rates, they will come from the Predbat configuration, ensure you have your electric or gas rates set correctly.

Note you can also change the tables for **gas_efficiency**, **heat_pump_efficiency** and **delta_correction** in the Predheat configuration but the defaults should be fine to get going.

Now comes the tricky part, we need to calculate the heat loss for your house:

What will help here is historical temperature data, find a time period in the last few weeks when your heating was turned off (for a few hours beforehand) and the house is cooling down.
Measure the number of degrees the house drops by in a given time period. Divide that figure (e.g. 1.5 degrees) by the time period e.g. (3 hours) and then again divide it by the
average difference between the inside and outside temperature
(e.g. 19 degrees inside, 9 degrees outside, so a temperature difference of 4 degrees) = 1.5 degrees / 3 hours / 10 degrees difference = 0.05. Set that figure to **heat_loss_degrees**.
It may be best to compute this when it's cold out and if you have your heating turned off overnight.

_Note in future versions of Predheat I might make this calculation automatic._

Next, we need to work out the number of watts of heat loss in the house, this can be done by looking at the energy consumed when the heating comes on. Pick a period of heating,
ideally from the time the temperature starts increasing for a complete hour of increase, looking at the increase in temperature in degrees,
add to that static heat loss which is  heat_loss_degrees _(internal temp - external temp)_ 1 hour to get the total degrees accounted for.
Now divide that by the external temperature difference again / (internal_temp - external_temp) and multiply the final figure by the energy your system consumed in Watts
during that period (can be found either from your sensor or just by looking at your energy bill for the same 1 hour period).

The final figure should be the number of watts your house loses per 1 degree of external temperature difference and be set to **heat_loss_watts**

Then you can set **heat_gain_static** to be the static heat output of other things in your house eg. computers and people. You can figure this out by looking at how many degrees of
temperature difference your house can maintain without any heating and multiply up your heat loss watts figure by this.

### Weather Compensation

If your heat source makes use of weather compensation then add the following to the configuration to map out your heat curve. The example has a flow temp of 45C at -3C outside and 25C at 15C outside:

```yaml   weather_compensation:
      -20: 45.0
      -3: 45.0
      15: 25.0
      20: 25.0
```

Predheat will fill in the gaps between the points provided.
