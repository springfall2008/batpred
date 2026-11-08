# Predheat

Predheat attempts to model water-based central heating systems based on a boiler or a heat pump.

Further details can be found in the [Predheat YouTube video](https://youtu.be/_-KIQen67zI)

## Operation

The app runs every 5 minutes (by default) and will automatically update its prediction for the heating system for the next period, for `forecast_days` days (48 hours by default; longer forecasts depend on available weather data).

The inputs are as follows

- An external temperature sensor, can be a real one or one created by an Internet service
- An internal temperature sensor, ideally from your home thermostat
- A thermostat target-temperature sensor with historical setpoint data.
- A heating energy sensor in kWh (not strictly required but needed to plot historical usage and calibrate)
- The flow temperature setting of your heating, can be static or a sensor
- Your current energy rates, either from the Octopus Energy plugin or hand-typed into the configuration
- Some data about your home that you have to figure out for yourself and calibrate.

The outputs are:

- Prediction of the internal house temperature going forward, including times when the heating will be active
- Your predicted energy usage and costs. The energy usage, if electric, can also be connected to Predbat to help you project your home battery usage.

Future versions will also offer Predbat to run in master mode, controlling your home's heating in the same way as a smart thermostat (e.g. Nest)

## Installation guide

Predheat is now part of Predbat, you will need to configure it using `apps.yaml` and then enable it by turning on **switch.predbat_predheat_enable**

Configuring the `predheat` section of `apps.yaml` on its own only makes Predheat start up, it does not make it run.
Until the switch is turned on you will see `Predheat: Startup` and `Predheat: Next run time will be ...` in the log
followed by `Predheat: Disabled - turn on switch.predbat_predheat_enable to run it`, and no `predheat.*` entities
will be created. Once the switch is on the log shows `Predheat: Enabled via switch.predbat_predheat_enable ...` and
then a `Predheat: update at ...` line for each run. If you turn the switch on in Home Assistant but the log still
says Predheat is disabled, then Predbat did not receive the switch change - toggle it from Predbat's own web
interface instead and check the log for a `switch_event: switch.predbat_predheat_enable` line.

### Weather install

You will need a weather forecast service available in Home Assistant for Predbat to be able to forecast heating demand based on the weather forecast.

You should be able to use any weather service, the [Met Office integration](https://www.home-assistant.io/integrations/metoffice) is known to work with Predheat as is the [Open Weather Map integration](https://www.home-assistant.io/integrations/openweathermap).

If you are using OpenWeather then you will need to create an OpenWeather account and then register for a [One Call API subscription plan](https://openweathermap.org/price). This does need a credit/debit card but won't cost anything.
You get 1000 API calls a day for free, so edit your limit in the account to 1000 to avoid ever being charged.

Then add in the Home Assistant service and connect up your API key to obtain hourly weather data.

### Apex Charts install

Use HACS to install Apex Charts: <https://github.com/RomRider/apexcharts-card>

There is a template of [example_Predheat charts](https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/templates/example_chart_predheat.yaml_template) you can use.

Create a new Apex chart for each chart in this template and copy the YAML code into the chart.

## Configuration guide

You need to edit your Predbat `apps.yaml` to configure your system.

Copy the following [predheat.yaml](https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/templates/predheat.yaml) template and append it to the end of Predbat's `apps.yaml`, after your existing inverter/battery configuration.

Set the mode (**mode**) to 'gas' or 'pump' depending on if you have a gas boiler or heat pump.

Set the external temperature sensor (**external_temperature**) either to a real sensor or create one from the open weather map by adding this sensor to your `configuration.yaml` file for HA:

```yaml
template:
  - sensor:
    - name: "external_temperature"
      unit_of_measurement: '°C'
      state_class: measurement
      state: >
        {{ state_attr('weather.openweathermap', 'temperature') }}
```

Set **internal_temperature** to point to one or more internal temperature sensors, if you have a heating thermostat then ideally link it to this or to a sensor at least in a similar area of the house.

The **weather** configuration points to the Open Weather Map sensor by default so should work as-is.

Set **target_temperature** to a sensor recording your thermostat setpoint. Predheat forecasts future targets using historical setpoints from **days_previous** (seven days ago by default), rather than reading the actual upcoming thermostat schedule. Manual overrides and recent schedule changes can therefore produce misleading forecasts. The current history-reading code expects an entity ID, not a fixed numerical temperature.

Set **smart_thermostat** to True if your thermostat starts the boiler ahead of time for the new target temperature or False for regular options.
When enabled, Predheat models each rise in the historically inferred target temperature over the forecast as a pre-heat: it works out how long the heating takes to reach the new target, using only the heating period the rise belongs to (until the target next falls below the new value), and raises the target that much earlier.
If the new target is not reached within its heating period the pre-heat starts as early as permitted, which is **smart_thermostat_max_minutes** when set or otherwise the length of that heating period.
A pre-heat never lowers a higher target that is already scheduled, and never starts before the previous rise.

Set **smart_thermostat_max_minutes** to the longest lead time, in minutes, that Predheat will model before a setpoint rise, which should be the longest early start your thermostat makes.
It applies to every pre-heat window. When it is not set, or is not a finite number, the pre-heat lead time is not capped.
Some thermostats only limit how early they pre-heat during the night, in which case set it to cover the longest daytime early start, or leave it unset.
For example, for a thermostat that starts heating up to 60 minutes early:

```yaml
  predheat:
    smart_thermostat: True
    smart_thermostat_max_minutes: 60
```

Set **heating_energy** to a cumulative kWh sensor measuring energy consumed by the boiler or heat pump, rather than instantaneous power or delivered thermal energy. Use **heating_energy_scaling** where a unit conversion or correction is required.
You can also comment this line out if you don't have a sensor, but no historical cost information will be produced.

Now you need to make a list of all your radiators in the house, measure them and look up their BTU output at Delta 50 and their volume in Litres. The links below may be useful for various standard radiators:

- <https://www.bestheating.com/milano-kent-straight-chrome-heated-towel-rail-various-sizes-91119>
- <https://www.bestheating.com/milano-compact-type-11-single-panel-radiator-multi-sizes-available-74174>
- <https://www.bestheating.com/milano-compact-type-22-double-panel-radiator-multi-sizes-available-74176>
- <https://www.bestheating.com/milano-compact-type-21-double-panel-plus-radiator-multi-sizes-available-74175>

Add up all the BTUs and divide by 3.41 to gain the heat output in Watts and set that in **heat_output** configuration option.
Add up all the litres of water, add in some extra for the piping and an expansion vessel if present (e.g. 5-10 litres) and set **heat_volume** accordingly.

Set **heat_min_power** and **heat_max_power** to the minimum and maximum **thermal output** of the boiler or heat pump in watts, not electrical input. For example, a heat pump delivering 7 kW of heat at COP 4 consumes approximately 1.75 kW of electricity.

Set **hysteresis** (default 0.5°C) to the amount above target at which the model turns heating **off**. Set **hysteresis_off** (default 0.1°C) to the amount below target at which heating turns **on**. These names are counterintuitive but reflect their use in the current simulation.

Set **heat_cop** to the maximum COP used by the model. For a gas boiler, use 1.0; gas efficiency is adjusted separately. For a heat pump, use an appropriate best-case COP for your installation. Predheat scales this using **heat_pump_efficiency** according to outdoor temperature. The code default is 0.9, so heat-pump users should set this explicitly.

Set **flow_temp** To the target flow temperature of your system, either via a sensor or as a fixed value. E.g. gas boilers are often set to say 60 or 70 degrees while heat pumps are much lower e.g. 30 or 40.

Set **flow_difference_target** to the difference between the target flow temperature and Predheat's modelled heating-water temperature (`volume_temp`) at which the model reaches full heating power. This is **not** the measured flow-minus-return temperature difference. A boiler might use approximately 40°C, while a heat pump generally needs a smaller value.

Set **volume_temp** If you have a sensor on your radiators which can confirm the water temperature, this must not be near the heat pump/boiler but instead as close to the
interior temperature sensor as possible. If you do not have a sensor then instead PredHeat will calculate the next temperature and store it in **next_volume_temp** for use
in the next calculation cycle.

For energy rates, they will come from the Predbat configuration, ensure you have your electric or gas rates set correctly.

Note you can also change the tables for **gas_efficiency**, **heat_pump_efficiency** and **delta_correction** in the Predheat configuration but the defaults should be fine to get going.

Set **run_every** to how often you want Predheat to update its heat energy prediction. By default this is every 5 minutes.

Now comes the tricky part, we need to calculate the heat loss for your house:

What will help here is historical temperature data, find a time period in the last few weeks when your heating was turned off (for a few hours beforehand) and the house is cooling down.
Measure the number of degrees the house drops by in a given time period. Divide that figure (e.g. 1.5 degrees) by the time period e.g. (3 hours) and then again divide it by the
average difference between the inside and outside temperature
(e.g. 19 degrees inside, 9 degrees outside, so a temperature difference of 10 degrees) = 1.5 degrees / 3 hours / 10 degrees difference = 0.05. Set that figure to **heat_loss_degrees**.
It may be best to compute this when it's cold out and if you have your heating turned off overnight.

_Note in future versions of Predheat I might make this calculation automatic._

Next, estimate **heat_loss_watts**, the house's heat-loss coefficient in
watts per °C of indoor/outdoor temperature difference (W/°C).

A useful starting estimate is to find a period when the indoor temperature is
approximately steady while heating is running. Divide the **thermal power
delivered to the house** by the indoor/outdoor temperature difference, allowing
for other heat gains where possible. For example, 3,000 W of heat maintaining
20°C indoors when it is 10°C outside suggests approximately 300 W/°C,
neglecting other gains.

For heat pumps, electrical consumption is not thermal output: multiply
electrical input by COP, or use a reliable measurement of delivered heat.
If the room temperature is rising or falling, some energy is changing the
building's stored heat, so this simple steady-state estimate is less reliable.
Treat it as an initial calibration and compare predictions against several
heating periods.

Then you can set **heat_gain_static** to be the static heat output of other things in your house eg. computers and people. You can figure this out by looking at how many degrees of
temperature difference your house can maintain without any heating and multiply up your heat loss watts figure by this.

### Weather Compensation

If your heat source makes use of weather compensation then add the following to the configuration to map out your heat curve. The example has a flow temp of 45C at -3C outside and 25C at 15C outside:

```yaml
  weather_compensation:
    -20: 45.0
    -3: 45.0
    15: 25.0
    20: 25.0
```

Predheat will fill in the gaps between the points provided.

IMPORTANT: The weather compensation curve must have a low value of -20 degrees C and a high value of 20 degrees C. You can have whatever other points you want between the upper and lower bounds and Predheat will fill in the gaps, but if you don't have -20 and 20 Predheat will error when it tries to calculate the weather-adjusted flow temperature.

### Link Predheat to Predbat

Add a **load_forecast** entry in `apps.yaml` to configure Predbat to use the [Predheat load forecast](apps-yaml.md#load-forecast):

```yaml
  load_forecast:
    - predheat.heat_energy$external
```

### Exclude Heat Pump load

If your **load_today** sensor in `apps.yaml` already contains your heat pump load then when Predbat forecasts your house load, the heat pump load will be double counted - once from historical house load data, and once from the Predheat forecast.

To resolve this you need to use **car_charging_energy** to [exclude heat pump load](car-charging.md#filtering-car-charging-energy-from-house-load) from the historical house load energy. e.g.:

```yaml
  car_charging_energy:
    - 'sensor.ashp_energy_today'
```
