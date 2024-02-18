# Support devices

## Wallbox Pulsar

https://www.home-assistant.io/integrations/wallbox/

Can be used both for Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in

```yaml
  car_charging_energy: 're:sensor.wallbox_portal_added_energy'
  car_charging_planned:
    - 're:sensor.wallbox_portal_status_description'
```

Wallbox works with Octopus Intelligent GO, and can be triggered via Octopus themselves or via a HA automation linked to the Predbat slot sensor

## Zappi

https://github.com/CJNE/ha-myenergi

Can be used both for Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in

```yaml
  car_charging_energy: 're:sensor.myenergi_zappi_[0-9a-z]+_charge_added_session'
  car_charging_planned:
    - 're:sensor.myenergi_zappi_[0-9a-z]+_plug_status)'
```

## Tesla

https://github.com/alandtse/tesla

Can be used to extract the cars current SOC and Charge limit. Also can be used to control the cars charging with an automation linked to the Predbat slot sensor

```yaml
  car_charging_limit:
    - 're:number.xxx_charge_limit'
  car_charging_soc:
    - 're:sensor.xxx_battery'
```

## Ohme

https://github.com/dan-r/HomeAssistant-Ohme

Can be used both for Car Charging Hold feature (to filter out previous car charging) and to determine if the car is plugged in.
Also can be used with Octopus Intelligent GO to map out the cars charging slots into Predbat


**car charging energy**
```yaml
  car_charging_energy: 'sensor.ohme_session_energy'
```

**Octopus Intelligent GO**

```yaml
  octopus_intelligent_slot: 'binary_sensor.ohme_slot_active'
  octopus_ready_time: 'time.ohme_target_time'
  octopus_charge_limit: 'number.ohme_target_percent'
```

**Determine if the car is charging now**

Normally not recommended if you are on Intelligent GO, but can be useful for ad-hoc charging not planned via Predbat

```yaml
car_charging_now:
   - 'binary_sensor.ohme_car_charging'
```

## Octopus Energy

https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy

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
