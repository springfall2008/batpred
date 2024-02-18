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

## Octopus 



