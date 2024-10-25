# Car charging

As a bare minimum a HA-controllable smart plug with a granny charger could be used,
but do consider there could be an electrical spike to the car if the smart plug is turned off when the car is charging. A proper car charger and HA integration is preferable.

You will firstly need to have installed the appropriate Home Assistant integration for your car charger.

## Configure apps.yaml for your car charging

Start by configuring the [Car charging settings in apps.yaml](apps-yaml.md#car-charging-integration).

## Car Charging Planning

There are two ways that Predbat can plan the slots for charging your car:

- If you have the Intelligent Octopus import tariff, have completed enrollment of your car/charger to Intelligent Octopus (requires a compatible charger or car),
and you have installed the Octopus Energy integration - in which case Predbat will use the car charging slots allocated by Octopus Energy in battery prediction.
The [Octopus Energy integration supports Octopus Intelligent](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/),
and through that Predbat gets most of the information it needs.
    - **octopus_intelligent_slot** in `apps.yaml` is pre-configured with a regular expression to point to the Intelligent Slot sensor in the Octopus Energy integration.
    You should not need to change this, but its worth checking the [Predbat logfile](output-data.md#predbat-logfile) to confirm that it has found your Octopus account details
    - Set **switch.predbat_octopus_intelligent_charging** to True
    - Information about the car's battery size will be automatically extracted from the Octopus Energy integration
    - You should set the cars current soc sensor, **car_charging_soc** in `apps.yaml` to point to a Home Assistant sensor
    that specifies the car's current % charge level to have accurate results. This should normally be a sensor provided by your car charger
    If you don't have this available for your charger then Predbat will assume the car's current charge level is 0%
    - If you set **car_charging_limit** in `apps.yaml` then Predbat can also know if the car's limit is set lower than in Intelligent Octopus.
    If you don't set this Predbat will default to 100%.
    - You can use **car_charging_now** as a workaround to indicate your car is charging but the Intelligent API hasn't reported it
    - The switch **switch.predbat_octopus_intelligent_ignore_unplugged** (_expert mode_)
    can be used to prevent Predbat from assuming the car will be charging when the car is unplugged. This will only work correctly
    if **car_charging_planned** is set correctly in apps.yaml to detect your car being plugged in
    - Let the Octopus app control when your car charges.

- Predbat-led charging - Here Predbat plans and can initiate the car charging based on the upcoming low import rate slots
    - Ensure **car_charging_limit**, **car_charging_soc** and **car_charging_planned** are set correctly in `apps.yaml` to point to the appropriate sensors from your EV (see [Car charging config in apps.yaml](apps-yaml.md#car-charging-integration))
    - Check (and if necessary add) the sensor response value from the sensor configured in **car_charging_planned** that is returned
    when the car is 'plugged in and ready to charge' is in the list of **car_charging_planned_response** values configured in apps.yaml
    - If your car does not have a state of charge (SoC) sensor you can set **switch.predbat_car_charging_manual_soc** to True
    to have Predbat create **input_number.predbat_car_charging_manual_soc_kwh** which will hold the cars SoC in kWh.<BR>
    You will need to manually set this to the cars current charge level before charging, Predbat will increment it during
    charging sessions but will not reset it automatically.<BR>
    NB: **input_number.predbat_car_charging_manual_soc_kwh** must be set to the current kWh value of your car battery NOT a percentage SoC figure
    otherwise Predbat won't know how much energy there currently is in the battery.<BR>
    NB2: If you have **car_charging_soc** set and working for your car SoC sensor in apps.yaml, **switch.predbat_car_charging_manual_soc** must be set to Off
    as otherwise the car SoC sensor will be ignored
    - Ensure **switch.predbat_octopus_intelligent_charging** in Home Assistant is set to Off
    - Set **input_number.predbat_car_charging_rate** to the car's charging rate in kW per hour (e.g. 7.5 for 7.5kWh)
    - Set **select.predbat_car_charging_plan_time** to the time you want the car charging to be completed by
    - Turn on **switch.predbat_car_charging_plan_smart** if you want to use the cheapest slots only. When disabled (turned off) all low rate slots will be used in time order
    - You can set **input_number.predbat_car_charging_plan_max_price** if you want to set a maximum price in pence per kWh to charge your car (e.g. 10p).
    If you set this to zero, this feature is disabled, and all low rate slots will be used. This may mean you need to use expert mode and change your low rate
    threshold to configure which slots should be considered if you have a tariff with more than 2 import rates (e.g. Flux)
    - _WARNING: Do not set **car_charging_now** or you will create a circular dependency._
    - Predbat will set **binary_sensor.predbat_car_charging_slot** when it determines the car can be charged;
    you will need to write a Home Assistant automation based on this sensor to control when your car charges.<BR>
    A sample automation to start/stop car charging using a Zappi car charger and the [MyEnergi Zappi integration](https://github.com/CJNE/ha-myenergi) is as follows,
    this should be adapted for your own charger type and how it controls starting/stopping car charging:

```yaml
alias: Car charging
description: "Start/stop car charging based on Predbat determined slots"
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
actions:
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

NOTE: Multiple cars can be planned with Predbat.

## Additional Car charging configurations

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

- **switch.predbat_car_charging_from_battery** - When set to True the car can drain the home battery, Predbat will manage the correct level of battery accordingly.
When set to False home battery discharge will be prevented when your car charges, all load from the car and home will be from the grid.
This is achieved by setting the battery discharge rate to 0 during car charging and to the maximum otherwise.
The home battery can still charge from the grid/solar in either case. Only use this if Predbat knows your car charging plan,
e.g. you are using Intelligent Octopus or you use the car slots in Predbat to control your car charging.

- **input_number.predbat_car_charging_loss** gives the percentage amount of energy lost when charging the car (load in the home vs energy added to the battery).
A good setting is 0.08 which is 8%.

## Example EV and charger setup

Sample setup and Predbat automation to use the cheapest charging slots with no/limited Home Assistant Integration.

MG4 EV Vehicle with a Hypervolt Car Charger. There is no 3rd party integration with the MG, and the Hypervolt car charger doesn't understand when an EV is plugged in.

Yet it can be stopped and started with a 3rd party integration.

In Home Assistant, create two helper entities (Settings / Devices & Services / Helpers) of type 'Number', and for each set 'Unit of Measurement' to 'kWh':

- Car Max Charge - input_number.car_max_charge
- Car Manual SoC - input_number.car_manual_soc

Create a 'Dropdown' helper entity that has two options 'true' and 'false' (in lower case):

- Car Charger Plugged in - input_select.car_charger_plugged_in

Within the `apps.yaml` configuration file specify the following configuration settings:

Find the line for **car_charger_battery_size** and enter the Car Battery Size in kWh:

**Example**

```yaml
  car_charging_battery_size:
    - 61.7
```

Specify the Car Charging Limit to use the Car Max Charge helper entity created earlier:

```yaml
  car_charging_limit:
    - 'input_number.car_max_charge'
```

Find **car_charging_planned** and replace the template Wallbox and Zappi regular expression with your new dropdown helper entity:

```yaml
  car_charging_planned:
    - 'input_select.car_charger_plugged_in'
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

And add your custom car charging energy sensor in apps.yaml in place of the template Wallbox and Zappi regular expression:

**Example**

```yaml
car_charging_energy: 'sensor.car_energy_used'
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

In Home Assistant (Settings / Automation & Scenes), create an automation to monitor the Predbat car charging slot sensor and turn the charger on and off according to the Predbat plan
(the numeric entity id's below would need replacing with the appropriate sensor name for your car charger):

```yaml
alias: Car Charging Slot
description: ""
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
actions:
  - if:
      - condition: state
        entity_id: binary_sensor.predbat_car_charging_slot
        state: "off"
    then:
      - type: turn_off
        entity_id: f6de2df0758744aba60f6b5f
        domain: switch
  - if:
      - condition: state
        entity_id: binary_sensor.predbat_car_charging_slot
        state: "on"
    then:
      - type: turn_on
        entity_id: f6de2df0758744aba60f6b5f
        domain: switch
mode: single
```

Finally, for simplicity, add the below entities to your HA Dashboard so you can set them when needed:

- Car Max Charge - input_number.car_max_charge
- Car Manual SoC - input_number.car_manual_soc
- Car Charger Plugged in - input_select.car_charger_plugged_in

Annoyingly, you have to calculate the kWh your vehicle has in total by taking the Percentage left in the car / 100 * Total Car Battery capacity.<BR>
For example:

```text
65/100*61.7=40.1
```

Enter '40.1' into 'Car Manual SoC' and '80%' into 'Car Max charge'.

Once the charger is switched to **true** and your Car Max charge (target SoC) % is higher than the kWh currently in the car,
Predbat will plan and charge the car with the kW that are needed to reach the target SoC.
