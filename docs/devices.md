# Support 3rd party devices/integrations

## Wallbox Pulsar

<https://www.home-assistant.io/integrations/wallbox/>

Can be used both for the Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in

```yaml
car_charging_energy: 're:sensor.wallbox_portal_added_energy'
car_charging_planned:
  - 're:sensor.wallbox_portal_status_description'
```

Wallbox works with Octopus Intelligent GO and can be triggered via Octopus themselves or an HA automation linked to the Predbat slot sensor

## Zappi

<https://github.com/CJNE/ha-myenergi>

Can be used both for the Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in

```yaml
car_charging_energy: 're:sensor.myenergi_zappi_[0-9a-z]+_charge_added_session'
car_charging_planned:
  - 're:sensor.myenergi_zappi_[0-9a-z]+_plug_status)'
```

## Tesla

<https://github.com/alandtse/tesla>

Can be used to extract the car's current SoC and Charge limit. Also can be used to control the cars charging with an automation linked to the Predbat slot sensor

```yaml
car_charging_limit:
  - 're:number.xxx_charge_limit'
car_charging_soc:
  - 're:sensor.xxx_battery'
```

## Toyota

<https://github.com/DurgNomis-drol/ha_toyota> - For Toyota EU cars only

Can be used to extract the car's current SoC.

```yaml
car_charging_soc:
  - 'sensor.toyota_XXX_battery_level'
```

Example sensor name for BZ4X - `sensor.toyota_bz4x_battery_level`

## Renault

<https://www.home-assistant.io/integrations/renault>

Can be used to extract the car's current SoC.

```yaml
car_charging_soc:
  - 'sensor.battery'
```

## New Ohme

<https://www.home-assistant.io/integrations/ohme>

Can be used both for the Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in.
Also can be used with Octopus Intelligent GO to map out the cars charging slots into Predbat

**car charging energy**

```yaml
ohme_name = 're:ohme_(.+)_target_time'
car_charging_energy: 'sensor.ohme_{ohme_name}_energy'
```

**Octopus Intelligent GO**

Newer Ohme Integrated into Home Assistant:

The word 'epod_' seems to have been added to some Ohme sensors in the version bundled with Home Assistant:

Replace XXX with your target_config for Ohme which is the device ID.

```yaml
  octopus_intelligent_slot: 'ohme.list_charge_slots'
  octopus_intelligent_slot_action_config: 'XXX'
  octopus_ready_time: 'time.ohme_{ohme_name}_target_time'
  octopus_charge_limit: 'number.ohme_{ohme_name}_target_percentage'
```

Note: You should turn on **switch.predbat_octopus_intelligent_ignore_unplugged** as the Ohme charger does not clear its schedule when unplugged.

**Using Ohme car charging plans on other tariff e.g. Agile**

```yaml
octopus_intelligent_slot: 'binary_sensor.ohme_{ohme_name}_slot_active'
octopus_ready_time: 'time.ohme_{ohme_name}_target_time'
octopus_charge_limit: 'number.ohme_{ohme_name}_target_percentage'
octopus_slot_low_rate: False
```

Note: You should turn on **switch.predbat_octopus_intelligent_ignore_unplugged** as the Ohme charger does not clear its schedule when unplugged.

## Old Ohme

<https://github.com/dan-r/HomeAssistant-Ohme>

Can be used both for the Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in.
Also can be used with Octopus Intelligent GO to map out the cars charging slots into Predbat

**car charging energy**

```yaml
car_charging_energy: 'sensor.ohme_session_energy'
```

**Octopus Intelligent GO**

```yaml
octopus_intelligent_slot: 'binary_sensor.ohme_slot_active'
octopus_ready_time: 'time.ohme_target_time'
octopus_charge_limit: 'number.ohme_target_percentage'
```

Note: You should turn on **switch.predbat_octopus_intelligent_ignore_unplugged** as the Ohme charger does not clear its schedule when unplugged.

**Using Ohme car charging plans on other tariff e.g. Agile**

```yaml
octopus_intelligent_slot: 'binary_sensor.ohme_slot_active'
octopus_ready_time: 'time.ohme_target_time'
octopus_charge_limit: 'number.ohme_target_percent'
octopus_slot_low_rate: False
```

Note: You should turn on **switch.predbat_octopus_intelligent_ignore_unplugged** as the Ohme charger does not clear its schedule when unplugged.

**Determine if the car is charging now**

Normally not recommended if you are on Intelligent GO, but can be useful for ad-hoc charging not planned via Predbat

```yaml
car_charging_now:
  - 'binary_sensor.ohme_car_charging'
```

## PodPoint

<https://github.com/mattrayner/pod-point-home-assistant-component>

Can be used both for the Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in.

```yaml
car_charging_energy: 're:(sensor.psl_[0-9]+_current_energy)'
car_charging_planned:
  - 're:(sensor.psl_[0-9]+_status)'
car_charging_planned_response:
  - 'plugged in'
  - 'connected-waiting-for-schedule'
  - 'suspended-evse'
  - 'pending'
  - 'charging'
car_charging_now:
  - 're:(sensor.psl_[0-9]+_status)'
car_charging_now_response:
  - 'charging'
```

Also can be used to control the cars charging with an automation linked to the Predbat slot sensor.
The device needs to be set to 'Smart' mode in the PodPoint app. Your automation trigger should then set the `switch.psl_XXXXXX_charging_allowed` to on. And off to stop charging. This uses the PodPoint schedule override setting to start/stop the charge.

## Hypervolt

<https://github.com/gndean/home-assistant-hypervolt-charger>

Can be used both for the Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in (only V3 models).

For plugged-in detection on V2 models, see guidance <https://springfall2008.github.io/batpred/car-charging/#example-ev-and-charger-setup>.

```yaml
car_charging_energy: 're:(sensor.hypervolt_session_energy_total_increasing)'
car_charging_planned:
  - 're:(binary_sensor.hypervolt_car_plugged)'
car_charging_planned_response:
  - 'on'
car_charging_now:
  - 're:(sensor.hypervolt_charging_readiness)'
car_charging_now_response:
  - 'charging'
```

Note: **sensor.hypervolt_session_energy_total_increasing** defaults to 'unknown' between charging sessions.

**Agile Tariff**

To automate the schedule charging with Predbat, set up the automation to detect when there is a change to `binary_sensor.predbat_car_charging_slot`.

Ensure that `select.hypervolt_charge_mode` is in 'Boost', when Predbat charging slot is 'on', set `select.hypervolt_activation_mode` to 'Plug and Charge', when it is 'off' set the 'Schedule', this is the recommended method for start/stop charging.

## Octopus Energy

<https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy>

Can be used for energy rates, car charging and saving sessions

**For energy rate**

```yaml
metric_octopus_import: 're:(sensor.(octopus_energy_|)electricity_[0-9a-z]+_[0-9a-z]+_current_rate)'
metric_octopus_export: 're:(sensor.(octopus_energy_|)electricity_[0-9a-z]+_[0-9a-z]+_export_current_rate)'
```

**For Octopus Intelligent GO**

```yaml
octopus_intelligent_slot: 're:(binary_sensor.octopus_energy([0-9a-z_]+|)_intelligent_dispatching)'
octopus_ready_time: 're:(time.octopus_energy([0-9a-z_]+|)_intelligent_ready_time)'
octopus_charge_limit: 're:(number.octopus_energy([0-9a-z_]+|)_intelligent_charge_limit)'
```

**For Octopus Saving sessions**

```yaml
octopus_saving_session: 're:(binary_sensor.octopus_energy([0-9a-z_]+|)_saving_session(s|))'
octopus_saving_session_octopoints_per_penny: 8
```

## Nordpool

### For adjustment to Octopus Intelligent

This is built into Predbat, see the [apps.yaml configuration guide](apps-yaml.md)

### As your energy rates (e.g. for those in Norway)

<https://github.com/custom-components/nordpool/>

Can be linked to Predbat to provide energy rates in your region e.g:

```yaml
metric_octopus_import: 'sensor.nordpool_kwh_oslo_eur_3_10_025'
```

## Solcast

<https://github.com/BJReplay/ha-solcast-solar>

For solar forecast data

```yaml
pv_forecast_today: re:(sensor.(solcast_|)(pv_forecast_|)forecast_today)
pv_forecast_tomorrow: re:(sensor.(solcast_|)(pv_forecast_|)forecast_tomorrow)
pv_forecast_d3: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_3|d3))
pv_forecast_d4: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_4|d4))
```
