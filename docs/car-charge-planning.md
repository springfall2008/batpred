# Car charging planning

You will firstly need to configure the [Car charging settings in apps.yaml](apps-yaml.md#car-charging-integration)
and have installed the appropriate Home Assistant integration for your car charger.
As a bare minimum a HA-controllable smart plug with a granny charger could be used,
but do consider there could be an electrical spike to the car if the smart plug is turned off when the car is charging. A proper car charger and HA integration is preferable.

There are two ways that Predbat can plan the slots for charging your car:

- If you have Intelligent Octopus import tariff, have completed enrollment of your car/charger to Intelligent Octopus (requires a compatible charger or car),
and you have installed the Octopus Energy integration - in which case Predbat will use the car charging slots allocated by Octopus Energy in battery prediction.
The [Octopus Energy integration supports Octopus Intelligent](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/),
and through that Predbat gets most of the information it needs.
    - **octopus_intelligent_slot** in `apps.yaml` is pre-configured with a regular expression to point to the Intelligent Slot sensor in the Octopus Energy integration.
You should not need to change this, but its worth checking the [Predbat logfile](output-data.md#predbat-logfile) to confirm that it has found your Octopus account details
    - Set **switch.predbat_octopus_intelligent_charging** to True
    - Information about the car's battery size will be automatically extracted from the Octopus Energy integration
    - You should set the cars current soc sensor, **car_charging_soc** in `apps.yaml` to point to a Home Assistant sensor
    that specifies the car's current % charge level to have accurate results. This should normally be a sensor provided by your car charger.
    If you don't have this available for your charger then Predbat will assume the charge level is 0%.
    - If you set **car_charging_limit** in `apps.yaml` then Predbat can also know if the car's limit is set lower than in Intelligent Octopus.
    If you don't set this Predbat will default to 100%.
    - You can use **car_charging_now** as a workaround to indicate your car is charging but the Intelligent API hasn't reported it.
    - Let the Octopus app control when your car charges.

- Predbat-led charging - Here Predbat plans and can initiate the car charging based on the upcoming low rate slots
    - Ensure **car_charging_limit**, **car_charging_soc** and **car_charging_planned** are set correctly in `apps.yaml`.
    - Set **select.predbat_car_charging_plan_time** in Home Assistant to the time you want the car to be ready by.
    - Enable **switch.predbat_car_charging_plan_smart** if you want to use the cheapest slots only.
    - You can set **car_charging_plan_max_price** if you want to set a maximum price per kWh to charge your car (e.g. 10p)
    If you leave this disabled then all low rate slots will be used. This may mean you need to use expert mode and change your low rate
    threshold to configure which slots should be considered if you have a tariff with more than 2 import rates (e.g. flux)
    - Predbat will set **binary_sensor.predbat_car_charging_slot** when it determines the car can be charged;
    you will need to write a Home Assistant automation based on this sensor to control when your car charges.<BR>
    A sample automation to start/stop car charging using a Zappi car charger and the [MyEnergi Zappi integration](https://github.com/CJNE/ha-myenergi) is as follows,
    this should be adapted for your own charger type and how it controls starting/stopping car charging:
    - _WARNING: Do not set **car_charging_now** or you will create a circular dependency._

```yaml
alias: Car charging
description: "Start/stop car charging based on Predbat determined slots"
trigger:
  - platform: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
    to: "on"
    id: start_charge
  - platform: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
    to: "off"
    id: end_charge
action:
  - choose:
      - conditions:
          - condition: trigger
            id:
              - start_charge
        sequence:
          - service: select.select_option
            data:
              option: Eco+
            target:
              entity_id: select.myenergi_zappi_charge_mode
      - conditions:
          - condition: trigger
            id:
              - end_charge
        sequence:
          - service: select.select_option
            data:
              option: Stopped
            target:
              entity_id: select.myenergi_zappi_charge_mode
  mode: single
```

NOTE: Multiple cars can be planned with Predbat.

**Example of automation using the cheapest Octopus Agile charging slots and how to set an EV and Charger with no/limited Homeassistant Integration**

 MG4 EV Vehicle with a Hypervolt Car Charger. There is no 3rd party integration with the MG, and the Hypervolt car charger doesn't understand when an EV is plugged in.

Yet it can be stopped and started with a 3rd party integration.

In Homeassistant, make the two examples below in Settings - Helpers - Number.

- EV Max Charge - input_number.car_max_charge
- EV Current SOC in kWh - input_number.predbat_car_charging_manual_soc_kwh

Again, in Settings - Helpers - Dropdown, enter a name with the two options True and False.

- Car Charger Plugged in -  input_select.car_charger_plugged_in

Browse to the Predbat `apps.yaml` configuration file. Please stay in `apps.yaml` until instructed.
Within `apps.yaml` find the line for **car_charger_battery_size** and enter the Car Battery Size in kWh.

**Example**

```yaml
  car_charging_battery_size:
    - 61.7
```

Within the first steps, we created the EV Max Charge helper. We want to specify the Car Charging Limit input.

```yaml
  car_charging_limit:
    - 're:(input_number.car_max_charge)'
```

Find **car_charging_planned** and add **input_select.car_charger_plugged_in** to the end of the line.

**Example**

```yaml
  car_charging_planned:
    - 're:(sensor.wallbox_portal_status_description|sensor.myenergi_zappi_[0-9a-z]+_plug_status|input_select.car_charger_plugged_in)'
```

Find **car_charging_planned_response** and add
**'true'** to the list.

**Example**

```yaml
  car_charging_planned_response:
    - 'yes'
    - 'on'
    - 'true'
```

If possible, add your entity keeping track of the kWh used for car charging. Please look into [Integration - Riemann sum integral](URL) to convert KW into kWh.
Your charging device doesn't keep track of kWh.

**Example**

```yaml
car_charging_energy: 're:(sensor.myenergi_zappi_[0-9a-z]+_charge_added_session|sensor.wallbox_portal_added_energy|sensor.mixergy_electricity_used|**sensor.car_energy_left**|sensor.pvd_immersion_load_total_diverted)'
```

Car Charging now must be hashed out.

```yaml
  #car_charging_now:
  #  - off
```

Please save the `apps.yaml` file and exit.

In Homeassistant, turn **on** the Predbat creates switches.

**switch.predbat_car_charging_hold**
**switch.predbat_car_charging_manual_soc**
**switch.predbat_car_charging_plan_smart**

Turn **off** the predbat-created switch.

**switch.predbat_octopus_intelligent_charging**

**HA Charging Slot Automation**

In Homeassistant - Settings - Automation, you must create an automation to monitor the charging slot.
Below is an example that monitors the state of the charging slot, turning the charger on and off according to the plan.

```yaml
alias: Car Charging Slot
description: ""
trigger:
  - platform: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
condition: []
action:
  - if:
      - condition: state
        entity_id: binary_sensor.predbat_car_charging_slot
        state: "off"
    then:
      - type: turn_off
        device_id: ac8b06952c7fe838314e
        entity_id: f6de2df0758744aba60f6b5f
        domain: switch
  - if:
      - condition: state
        entity_id: binary_sensor.predbat_car_charging_slot
        state: "on"
    then:
      - type: turn_on
        device_id: ac8b06952c7fe838314e
        entity_id: f6de2df0758744aba60f6b5f
        domain: switch
mode: single
```

Finally, for simplicity, add the below to your HA Dashboard. Once the charger is switched to  **true** and your Target SOC % is higher than the kWh currently in the car,
Annoyingly, you have to calculate the kWh your vehicle has in total by taking the Percentage left in the car / 100 * Total Capacity of Battery.

**Example**

```text
65/100*61.7=40.1
```

Enter 40.1 into the EV Current SOC in kWh. 80% in Max car charge.
This will update the Predbat plan  with the cheapest times to charge the EV in line with the number of KW that needed to charge the EV.

- EV Max Charge - input_number.car_max_charge
- EV Current SOC in kWh - input_number.predbat_car_charging_manual_soc_kwh
- Car Charger Plugged in -  input_select.car_charger_plugged_in

---

See [Car charging filtering](apps-yaml.md#car-charging-filtering) and [Planned car charging](apps-yaml.md#planned-car-charging)
in the [apps.yaml settings](apps-yaml.md) section of the documentation.
