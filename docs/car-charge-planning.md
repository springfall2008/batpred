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
    If you don't have this available for your charger then Predbat will assume the car's current charge level is 0%.
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

      ```yaml
      alias: Car charging
      description: "Start/stop car charging based on Predbat determined slots"
      trigger:
        - platform: state
          entity_id:
            - binary_sensor.predbat_car_charging_slot
      action:
        - choose:
            - conditions:
                - condition: state
                  entity_id: binary_sensor.predbat_car_charging_slot
                  state: "on"
              sequence:
                <commands to turn on your car charger, e.g.>
                - service: select.select_option
                  data:
                    option: Eco+
                  target:
                    entity_id: select.myenergi_zappi_charge_mode
            - conditions:
                - condition: state
                  entity_id: binary_sensor.predbat_car_charging_slot
                  state: "off"
              sequence:
                <commands to turn off your car charger, e.g.>
                - service: select.select_option
                  data:
                    option: Stopped
                  target:
                    entity_id: select.myenergi_zappi_charge_mode
      mode: single
      ```

    - _WARNING: Do not set **car_charging_now** or you will create a circular dependency._

NOTE: Multiple cars can be planned with Predbat.

If you have one charger and multiple cars configured in Predbat then set **car_charging_exclusive** in apps.yaml to True to indicate that only one
car may charge at once (the first car reporting as plugged in will be considered as charging). If you set this to False then it is assumed each car
can charge independently and hence two or more could charge at once

```yaml
car_charging_exclusive:
  - True
  - True
```

See [Car charging filtering](apps-yaml.md#car-charging-filtering) and [Planned car charging](apps-yaml.md#planned-car-charging)
in the [apps.yaml settings](apps-yaml.md) section of the documentation.

**Example EV and charger setup and Predbat automation to use the cheapest charging slots with no/limited Home Assistant Integration**

 MG4 EV Vehicle with a Hypervolt Car Charger. There is no 3rd party integration with the MG, and the Hypervolt car charger doesn't understand when an EV is plugged in.

Yet it can be stopped and started with a 3rd party integration.

In Home Assistant, create two helper entities (Settings / Devices & Services / Helpers) of type 'Number':

- EV Max Charge - input_number.car_max_charge
- EV Current SOC in kWh - input_number.predbat_car_charging_manual_soc_kwh

Create a 'Dropdown' helper entity that has two options 'true' and 'false' (in lower case):

- Car Charger Plugged in - input_select.car_charger_plugged_in

Within the `apps.yaml` configuration file specify the following configuration settings:

Find the line for **car_charger_battery_size** and enter the Car Battery Size in kWh:

**Example**

```yaml
  car_charging_battery_size:
    - 61.7
```

Specify the Car Charging Limit to use the EV Max Charge helper entity created earlier:

```yaml
  car_charging_limit:
    - 're:(input_number.car_max_charge)'
```

Find **car_charging_planned** and add the **input_select.car_charger_plugged_in** dropdown helper entity to the end of the line:

```yaml
  car_charging_planned:
    - 're:(sensor.wallbox_portal_status_description|sensor.myenergi_zappi_[0-9a-z]+_plug_status|input_select.car_charger_plugged_in)'
```

Find **car_charging_planned_response** and add **'true'** to the list:

```yaml
  car_charging_planned_response:
    - 'yes'
    - 'on'
    - 'true'
```

If possible, add an entity keeping track of the kWh used for car charging to **car_charging_energy**.

If your charging device doesn't keep track of kWh but you can measure the power sent to the car charger
(e.g. from the EV charger integration or an energy monitor/smart plug for the EV charger) then you can create another helper entity to convert kW power into kWh:

Create a helper entity (Settings / Devices & Services / Helpers) of type 'Integration - Riemann Sum integral':

- Name : car_energy_used
- Input sensor : _sensor that measures power consumed by the car charger_
- Integration method : Right Riemann sum
- Metric prefix : k (kilo)

Please look into [Integration - Riemann sum integral](URL) to convert kW into kWh.

**Example**

```yaml
car_charging_energy: 're:(sensor.myenergi_zappi_[0-9a-z]+_charge_added_session|sensor.wallbox_portal_added_energy|sensor.car_energy_used)'
```

**car_charging_now** must be commented out (hashed out) in `apps.yaml`:

```yaml
  #car_charging_now:
  #  - off
```

Save the `apps.yaml` file and exit.

In Home Assistant, turn **on** the following Predbat control switches:

- **switch.predbat_car_charging_hold**
- **switch.predbat_car_charging_manual_soc**
- **switch.predbat_car_charging_plan_smart**

And turn **off** the Predbat control switch:

- **switch.predbat_octopus_intelligent_charging**

**HA Charging Slot Automation**

In Home Assistant (Settings / Automation & Scenes), create an automation to monitor the Predbat car charging slot sensor and turn the charger on and off according to the Predbat plan:

```yaml
alias: Car Charging Slot
description: ""
trigger:
  - platform: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
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

Finally, for simplicity, add the below entities to your HA Dashboard:

- EV Max Charge - input_number.car_max_charge
- EV Current SOC in kWh - input_number.predbat_car_charging_manual_soc_kwh
- Car Charger Plugged in - input_select.car_charger_plugged_in

Annoyingly, you have to calculate the kWh your vehicle has in total by taking the Percentage left in the car / 100 * Total EV Battery capacity.<BR>
For example:

```text
65/100*61.7=40.1
```

Enter '40.1' into 'EV Current SOC in kWh' and '80%' into 'EV Max charge'.

Once the charger is switched to **true** and your EV Max charge (target SOC) % is higher than the kWh currently in the car,
Predbat will plan and charge the EV with the kW that are needed to reach the EV target SOC.
