# Car charging

As a bare minimum, a HA-controllable smart plug with a granny charger could be used,
but do consider there could be an electrical spike to the car if the smart plug is turned off when the car is charging. A proper car charger and HA integration are preferable.

You will first need to have installed the appropriate Home Assistant integration for your car charger.

## Configure apps.yaml for your car charging

Start by configuring the car charging settings in `apps.yaml` with the following car-charging related configuration items:

- **num_cars** should be set in `apps.yaml` to the number of cars you want Predbat to plan for.
Set to 0 if you don't have an EV or don't want Predbat to plan for your EV charging (and the remaining car sensors in `apps.yaml` can safely be commented out or deleted as they won't be required).<BR>
NB: num_cars must be set correctly regardless of whether you are using Octopus Intelligent Go to control your EV charging or Predbat to control the charging;
or else Predbat could start discharging your battery when the EV is charging.

- **car_charging_exclusive** should be set to True for each car in `apps.yaml` if you have multiple cars configured in Predbat, but only one car charger.
This indicates that only one car may charge at once (the first car reporting as plugged in will be considered as charging).
If you set this to False for each car then it is assumed that the car can charge independently, and hence two or more cars could charge at once.
One entry per car.

```yaml
  car_charging_exclusive:
    - True
    - True
```

### Filtering Car Charging Energy from house load

Depending upon how the CT clamps and your inverter and electric car charger have been wired, your inverter may 'see' your EV charging as being part of the house load.  This means your house load is artificially raised whenever you charge your car.
In this circumstance you might want to remove your electric car charging data from the historical house load data so as to not bias the calculations, otherwise you will get
high battery charge levels when the car was charged previously (e.g. last week).

*TIP:* Check the house load being reported by your inverter when your car is charging. If it doesn't include the car charging load then there is no need to follow these steps below (and if you do, you'll artificially deflate your house load).

- **switch.predbat_car_charging_hold** - A switch that when turned on (the default) tells Predbat to remove car charging data from your historical house load so that Predbat's battery prediction plan is not distorted by previous car charging.

If you are getting [erroneous house load predictions in your plan](faq.md#why-is-my-house-load-lower-than-expected-or-zero) then check this setting and **car_charging_energy** or **input_number.predbat_car_charging_threshold** are set correctly.

If you don't have an EV then turn **switch.predbat_car_charging_hold** Off as Predbat will by default still consider any house load in excess of **input_number.predbat_car_charging_threshold** to be car charging activity and will exclude it.

- **car_charging_energy** - Set in `apps.yaml` to point to an entity which is the daily incrementing kWh data for the car charger.
This has been pre-defined as a regular expression that should auto-detect the appropriate Wallbox and Zappi car charger sensors,
or edit as necessary in `apps.yaml` for your charger sensor.<BR>
Note that this must be configured to point to an 'energy today' sensor in kWh not an instantaneous power sensor (in kW) from the car charger.<BR><BR>
*IMPORTANT:* Predbat will subtract all car_charging_energy from your historic house load so if car_charging_energy is not configured with the correct sensor,
your car charging energy sensor does not accurately report your car charging data (e.g. it falsely reports charging data when not actually charging), or your house load sensor already excludes car charging,
then this will really mess up your predbat plan as Predbat will exclude all car_charging_energy from your load predictions and you could end up with erroneous or zero house load predictions.  Do check the entity!<BR><BR>
*NOTE:* The car charging energy sensor must be a daily incrementing kWh sensor. Check the history of your sensor in Home Assistant, that it increments through the day when your car is charging, resets to zero at midnight,
and does not dip down in value or reset to zero other than at midnight. Some car charger energy sensors do not behave as Predbat requires them to do; for example, they may show cumulative energy per charge, not cumulative charge energy today.<BR>
You may need to wrap the car charger energy sensor into a daily resetting utility meter to create a sensor that behaves correctly.<BR><BR>
*TIP:* You can also use **car_charging_energy** to remove other house load kWh from the data Predbat uses for the forecast,
e.g. if you want to remove Mixergy hot water tank heating data from the forecast such as if you sometimes heat on gas, and sometimes electric depending upon import rates.<BR>
car_charging_energy can be set to a list of energy sensors, one per line if you have multiple EV car chargers, or want to exclude multiple loads such as heat pump load, e.g.:

```yaml
  car_charging_energy:
    - 're:(sensor.myenergi_zappi_[0-9a-z]+_charge_added_session|sensor.wallbox_portal_added_energy)'
    - sensor.mixergy_ID_energy
    - sensor.ashp_energy_today
```

- **input_number.predbat_car_charging_energy_scale** - Used to define a scaling factor (in the range of 0 to 1.0)
to multiply the **car_charging_energy** sensor data by if required (e.g. set to 0.001 to convert Watts to kW). Default 1.0, i.e. no scaling.

If you do not have a suitable car charging energy kWh sensor in Home Assistant then comment the **car_charging_energy** line out of `apps.yaml` and configure **input_number.predbat_car_charging_threshold**

- **input_number.predbat_car_charging_threshold** (default 6 = 6kW)- Sets the kW power threshold above which home consumption is assumed to be car charging
and **input_number.predbat_car_charging_rate** (in kW) will be subtracted from the historical load data.

Used to 'detect' EV charging if you have an EV charger but it does not have an energy today sensor that you can use.

If you do not have an EV charger then ensure you set **switch.predbat_car_charging_hold** to Off otherwise Predbat will assume any house load in excess of car_charging_threshold is EV charging and remove it from your house load predictions!

### Planned Car Charging

These features allow Predbat to know when you plan to charge your car.

If you have an Intelligent Octopus tariff then planning of charging is done via the Octopus app and Predbat obtains this information through the Octopus Energy integration in Home Assistant.

- **switch.predbat_octopus_intelligent_charging** - When this Home Assistant switch is enabled, Predbat will plan charging around the Intelligent Octopus slots, taking it into account for battery load and generating the slot information

The following `apps.yaml` configuration items are pre-defined with regular expressions to point to appropriate sensors in the Octopus Energy integration. You should not normally need to change these if you have the Octopus Intelligent tariff:

- **octopus_intelligent_slot** - Points to the Octopus Energy integration 'intelligent dispatching' sensor that indicates
whether you are within an Octopus Energy "smart charge" slot, and provides the list of future planned charging activity.

- **octopus_ready_time** - Points to the Octopus Energy integration sensor that details when the car charging will be completed.<BR>
*Note:* the Octopus Integration now provides [Octopus Intelligent target time](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/#target-time-time) in two formats, either a 'select' entity or a 'time' entity.
Predbat uses the time entity (time.octopus_energy_{{DEVICE_ID}}_intelligent_target_time) which is disabled by default, so you will need to enable the time entity and disable the matching select entity.

- **octopus_charge_limit** - Points to the Octopus Energy integration sensor that provides the car charging limit.

- **octopus_slot_low_rate** - Default is True, meaning any Octopus Intelligent Slot reported will be at the lowest rate if at home. If False the existing rates only will be used which is only suitable for tariffs other than IOG.

- **octopus_slot_max** - Default is 48 (disabled). Sets the maximum number of 30-minute cheap rate slots per 24-hour period.
Octopus Intelligent users maybe from March 2026 limited to 6 hours of cheap charging per day. Slots beyond this limit will use standard rates.
Its recommended you set this to 12 (for 6 hours) once Octopus enforce this Octopus Intelligent limit.

If you don't use Intelligent Octopus then the above Octopus Intelligent configuration lines in `apps.yaml` can be commented out or deleted, and there are a number of other `apps.yaml` configuration items that should be set:

- **car_charging_planned** - Optional, can be set to a Home Assistant sensor (e.g. from your car charger integration)
which lets Predbat know the car is plugged in and planned to charge during low-rate slots.
Or manually set it to 'False' to disable this feature, or 'True' to always enable it.<BR>
The `apps.yaml` template supplied with Predbat comes pre-configured with a regular expression that should automatically match Zappi or Wallbox car chargers.
If you have a different type of charger you will need to configure it manually.

- **car_charging_planned_response** - An array of values for the above car_charging_planned sensor which indicate that the car is plugged in and will charge in the next low rate slot.
The template `apps.yaml` comes with a set of pre-defined sensor values that should match most EV chargers.
Customise for your car charger sensor if it sets sensor values that are not in the list.

- **car_charging_now** - For some cases finding details of planned car charging is difficult.<BR>
The car_charging_now configuration item can be set to point to a Home Assistant sensor that tells you that the car is currently charging.
Predbat will then assume this slot is used for charging regardless of the plan.<BR>
If Octopus Intelligent Charging is enabled and car_charging_now indicates the car is charging then Predbat will also assume that this is a
low rate slot for the car/house (and might therefore start charging the battery), otherwise electricity import rates are taken from the normal rate data.<BR>
WARNING: Some cars will briefly start charging as soon as they are plugged in, which Predbat will detect and assume that this is a low rate slot even when it isn't.
It is therefore recommended that you do NOT set car_charging_now unless you have problems with the Octopus Intelligent slots, and car_charging_now should be commented out in `apps.yaml`.

**CAUTION:** It is strongly recommended to not use car_charging_now with Predbat-led charging unless you can't make it work any other way as Predbat will assume all car charging is at a low rate.

- **car_charging_now_response** - Set to the range of positive responses for car_charging_now to indicate that the car is charging. Useful if you have a sensor for your car charger that isn't binary.

To make Predbat planned car charging more accurate, configure the following items in `apps.yaml`:

- **car_charging_battery_size** - Set this value in `apps.yaml` to the car's battery size in kWh which *must* be entered with one decimal place, e.g. 50.0.
If not set, Predbat defaults to 100.0kWh. This will be used to predict when to stop car charging.

- **car_charging_limit** - You should configure this to point to a sensor that specifies the % limit the car is set to charge to.
This could be a sensor on the EV charger integration or a Home Assistant helper entity you can set as you wish.
If you don't specify a sensor Predbat will default to 100% - i.e. fill the car to full.

- **car_charging_soc** - You should configure this to point to a sensor (on the HA integration for your EV charger) that specifies the car's current charge level
expressed as a percentage - it must NOT be set to a sensor that gives the car's current kWh value as this will cause Predbat to charge the car to an incorrect level.
If you don't specify a sensor, Predbat will default to 0%.

If you have [multiple electric cars](#multiple-electric-cars) then car_charging_soc should be set to a list of sensors, e.g.:

```yaml
  car_charging_soc:
    - 'sensor.tsunami_battery'
    - 'sensor.toyota_XXX_battery_level'
```

### Multiple Electric Cars

Multiple cars can be planned with Predbat, in which case you should set **num_cars** in `apps.yaml` to the number of cars you want to plan.

- **car_charging_limit**, **car_charging_planned**, **car_charging_battery_size** and **car_charging_soc** must then be a list of values (i.e. 2 entries for 2 cars)

- If you have Intelligent Octopus then Car 0 will be managed by the Octopus Energy integration, if it's enabled.

- Each car will have its own Home Assistant slot sensor created e.g. **binary_sensor.predbat_car_charging_slot_1**,
SoC planning sensor e.g **predbat.car_soc_1** and **predbat.car_soc_best_1** for car 1

An excellent [worked example of setting up multiple car charging with Predbat](https://github.com/springfall2008/batpred/discussions/3001) is in the 'Show and tell' part of Predbat's GitHub.

## Ohme car charger direct integration

Predbat can talk directly to the Ohme charger by configuring your Ohme account details in `apps.yaml`.

When **ohme_automatic_octopus_intelligent** is set to True then Predbat is automatically configured to take Octopus Intelligent car charging slots from Ohme (rather than from Octopus Intelligent directly).

```yaml
  ohme_login: "user@domain"
  ohme_password: "xxxxxxxxx"
  ohme_automatic_octopus_intelligent: true
```

**NOTE:** It's recommended to store `ohme_password` in `secrets.yaml` and reference it as `ohme_password: !secret ohme_password` - see [Storing secrets](apps-yaml.md#storing-secrets).

## Car Charging Planning

There are two ways that Predbat can plan the slots for charging your car:

### Octopus-led charging

- If you have the Intelligent Octopus import tariff, have completed enrollment of your car/charger to Intelligent Octopus (requires a compatible charger or car),
and you have installed the Octopus Energy integration - in which case Predbat will use the car charging slots allocated by Octopus Energy in battery prediction.
The [Octopus Energy integration supports Octopus Intelligent](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/),
and through that, Predbat gets most of the information it needs.

- **octopus_intelligent_slot** in `apps.yaml` is pre-configured with a regular expression to point to the Intelligent Slot sensor in the Octopus Energy integration.
You should not need to change this, but it is worth checking the [Predbat logfile](output-data.md#predbat-logfile) to confirm that it has found your EV charger details.<BR>
If you are using the [Octopus Energy direct](energy-rates.md#octopus-energy-direct) method of Predbat directly connecting to your Octopus account then this configuration line is not required and should be commented out of `apps.yaml`.

- Set **switch.predbat_octopus_intelligent_charging** to On

- You should set the car's current SoC sensor, **car_charging_soc** in `apps.yaml` to point to a Home Assistant sensor that specifies the car's current % charge level to have accurate results.
This should normally be a sensor provided by your car charger.
If you don't have this available for your charger then Predbat will assume the car's current charge level is 0%.

- If you set **car_charging_limit** in `apps.yaml` then Predbat can also know if the car's limit is set lower than in Intelligent Octopus.
If you don't set this Predbat will default to 100%.

- **octopus_charge_limit** and **octopus_ready_time** in `apps.yaml` are pre-configured with regular expressions to point to the appropriate sensors for your EV charger in the Octopus Energy integration.
These retrieve details of the charge limit and when the car will finish charging from your Octopus app settings.
Again, if you are using the Octopus Energy direct method for Predbat then these configuration lines are not required and should be commented out of `apps.yaml`.

- You can configure **car_charging_now** in `apps.yaml` to point a Home Assistant sensor that indicates that the car is currently charging as a workaround to indicate your car is charging, but the Intelligent API hasn't reported it.

- The switch **switch.predbat_octopus_intelligent_consider_full** (*expert mode*)
(default is Off) when turned on will cause Predbat to predict when your car battery is full and assume no further charging will occur.
This can be useful if Octopus does not know your car battery's state of charge but you have a sensor setup in Predbat (**car_charging_soc**) which does know the current charge level.
Predbat will still assume all Octopus charging slots are low rates even if some are not used by your car.

- The switch **switch.predbat_octopus_intelligent_ignore_unplugged** (*expert mode*) (default value is off) can be used to prevent Predbat from assuming the car will be charging or that future extra low-rate slots apply when the car is unplugged.
This will only work correctly if **car_charging_planned** is set correctly in `apps.yaml` to detect your car being plugged in

- Let the Octopus app control when your car charges.

### Predbat-led charging

Here Predbat plans and can initiate the car charging based on the upcoming low import rate slots

- Ensure **car_charging_limit**, **car_charging_soc** and **car_charging_planned** are set correctly in `apps.yaml` to point to the appropriate sensors from your EV (see [Car charging config in apps.yaml](#configure-appsyaml-for-your-car-charging))

- Check (and if necessary add) the sensor response value from the sensor configured in **car_charging_planned** that is returned when the car is 'plugged in and ready to charge' is in the list of **car_charging_planned_response** values
configured in `apps.yaml`

- If your car does not have a state of charge (SoC) sensor you can set **switch.predbat_car_charging_manual_soc** (for car 0) to On to have Predbat create **input_number.predbat_car_charging_manual_soc_kwh** which will hold the car's SoC in kWh.<BR>
For multiple cars, use **switch.predbat_car_charging_manual_soc_1/2/3** and **input_number.predbat_car_charging_manual_soc_kwh_1/2/3** for cars 1, 2, and 3 respectively.<BR>
You will need to manually set this to the car's current charge level before charging, Predbat will increment it during charging sessions but will not reset it automatically.<BR>
NB: **input_number.predbat_car_charging_manual_soc_kwh** must be set to the current kWh value of your car battery NOT a percentage SoC figure
otherwise, Predbat won't know how much energy there currently is in the battery.<BR>
NB2: If you have **car_charging_soc** set and working for your car SoC sensor in `apps.yaml`, **switch.predbat_car_charging_manual_soc** must be set to Off as otherwise the car SoC sensor will be ignored

- Ensure **switch.predbat_octopus_intelligent_charging** in Home Assistant is set to Off

- Set **input_number.predbat_car_charging_rate** to the car's charging rate in kW per hour (e.g. 7.5 for 7.5kWh)

- If you have more than one car then **input_number.predbat_car_charging_rate_1** will be the second car etc.

- Set **select.predbat_car_charging_plan_time** to the time you want the car charging to be completed by

- Turn on **switch.predbat_car_charging_plan_smart** if you want to use the cheapest slots only. When disabled (turned off) all low-rate slots will be used in time order.
Low-rate slots are time periods where the import rate is below the threshold determined by **input_number.predbat_rate_low_threshold** (*expert mode*).
By default this threshold is calculated automatically based on future import rates - see [Battery margins and metrics options](customisation.md#battery-margins-and-metrics-options) for details on configuring this threshold.

- You can set **input_number.predbat_car_charging_plan_max_price** if you want to set a maximum price in pence per kWh to charge your car (e.g. 10p).
If you set this to zero, this feature is disabled, and all low-rate slots will be used.
This may mean you need to use expert mode and change your low-rate threshold (**input_number.predbat_rate_low_threshold**) to configure which slots should be considered if you have a tariff with more than 2 import rates (e.g. Flux)

- *WARNING:* Do not set **car_charging_now** in `apps.yaml` or you will create a circular dependency.

- Predbat will set **binary_sensor.predbat_car_charging_slot** when it determines the car can be charged; you will need to write a Home Assistant automation based on this sensor to control when your car charges.

A sample automation to start/stop car charging using a Zappi car charger and the [MyEnergi Zappi integration](https://github.com/CJNE/ha-myenergi) is as follows,
this should be adapted for your charger type and how it controls starting/stopping car charging:

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

NOTE: [Multiple cars](car-charging.md#multiple-electric-cars) can be planned with Predbat.

## Additional Car charging configurations

- If you have one charger and multiple cars configured in Predbat then set **car_charging_exclusive** in `apps.yaml` to `True` to indicate that only one
car may charge at once (the first car reporting as plugged in will be considered as charging). If you set this to `False` then it is assumed each car
can charge independently and hence two or more could charge at once

```yaml
  car_charging_exclusive:
    - True
    - True
```

- See [Car charging filtering](#filtering-car-charging-energy-from-house-load) and [Planned car charging](#planned-car-charging)
for further car charging setup details.

- **switch.predbat_car_charging_from_battery** - When set to On the car can drain the home battery, Predbat will manage the correct level of battery accordingly.
When set to Off home battery discharge will be prevented when your car charges, and all load from the car and home will be from the grid.
This is achieved by setting the battery discharge rate to 0 during car charging and to the maximum otherwise.
The home battery can still charge from the grid/solar in either case. Only use this if Predbat knows your car charging plan,
e.g. you are using Intelligent Octopus or you use the car slots in Predbat to control your car charging.

- **input_number.predbat_car_charging_loss** gives the percentage amount of energy lost when charging the car (load in the home vs energy added to the battery).
A good setting is 0.08 which is 8%.

- **switch.predbat_metric_dynamic_load_adjust** (default False) - If turned off then Predbat won't export during times the car is planned to charge even if the car is not charging.

## Example EV and charger setup

Sample setup and Predbat automation to use the cheapest charging slots with no/limited Home Assistant Integration.

MG4 EV Vehicle with a Hypervolt Car Charger. There is no 3rd party integration with the MG (so no idea of the car's current SoC), and the Hypervolt car charger doesn't understand when an EV is plugged in.

Yet it can be stopped and started with a 3rd party integration.

In Home Assistant, create a helper entity (Settings / Devices & Services / Helpers) of type 'Number', check minimum value is set to 0, maximum value to 100, and under Advanced Settings, set 'Unit of Measurement' to '%':

- Car Max Charge - input_number.car_max_charge

Create a 'Dropdown' helper entity that has two options 'true' and 'false' (in lowercase):

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

If your charging device doesn't keep track of kWh you can measure the power sent to the car charger
(e.g. from the EV charger integration or an energy monitor/smart plug for the EV charger) then you can create another helper entity to convert kW power into kWh:

Create a helper entity (Settings / Devices & Services / Helpers) of type 'Integration - Riemann Sum integral':

- Name : car_energy_used
- Input sensor : *sensor that measures power consumed by the car charger*
- Integration method : Right Riemann sum
- Metric prefix : k (kilo)

Please look into [Integration - Riemann sum integral](URL) to convert kW into kWh.

And add your custom car charging energy sensor in `apps.yaml` in place of the template Wallbox and Zappi regular expression:

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
- **switch.predbat_car_charging_manual_soc** (for car 0, or **switch.predbat_car_charging_manual_soc_1/2/3** for additional cars)
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
- Car Manual SoC - input_number.predbat_car_charging_manual_soc_kwh (for car 0)
- For multiple cars, add input_number.predbat_car_charging_manual_soc_kwh_1/2/3 for cars 1/2/3
- Car Charger Plugged in - input_select.car_charger_plugged_in

Annoyingly, you have to calculate the kWh your vehicle has in total by taking the Percentage left in the car / 100 * Total Car Battery capacity.<BR>
For example:

```text
65/100*61.7=40.1
```

Enter '40.1' into 'Car Manual SoC' and '80%' into 'Car Max charge'.

Once the charger is switched to **true** and your Car Max charge (target SoC) % is higher than the kWh currently in the car,
Predbat will plan and charge the car with the kW that are needed to reach the target SoC.

## Example: Separating car charging costs for multiple cars

Predbat provides **predbat.cost_today_car** and **predbat.cost_total_car** which give the cost today and total accumulated cost for all car charging.

If you have multiple cars with a single EV charger then its not possible to segregate the cost per car.

The following solution will accumulate individual charging costs for each car.

- Create two helper entities of type number to collect the cost per car:

    ```yaml
    input_number.car_car1_cost_today
    input_number.car_car2_cost_today
    Min = 0
    Max = 10000
    Step = 0.01
    ```

  Predbat accumulates cost in pence/cents, etc so the Max value should be big enough to hold the maximum car charging cost per day (e.g. £10/$10/€10).

- Create an automation that triggers when **predbat.cost_today_car** changes value. Then, based on which car is connected (sensor.car1_connected or sensor.car2_connected in this case), delta of predbat_cost to the appropriate car cost today sensor:

    ```yaml
    alias: Allocate EV Charging Cost
    description: ""
    triggers:
      - entity_id:
          - predbat.cost_today_car
        trigger: state
    actions:
      - variables:
          new_cost: "{{ trigger.to_state.state | float }}"
          old_cost: "{{ trigger.from_state.state | float }}"
          delta: "{{ new_cost - old_cost }}"
      - condition: template
        value_template: "{{ delta > 0 }}"
      - choose:
          - conditions:
              - condition: template
                value_template: "{{ is_state('sensor.car1_connected', 'on') }}"
            sequence:
              - target:
                  entity_id: input_number.car_car1_cost_today
                data:
                  value: >
                    {{ (states('input_number.car_car1_cost_today') | float) + delta
                    }}
                action: input_number.set_value
          - conditions:
              - condition: template
                value_template: "{{ is_state('sensor.car2_connected', 'on') }}"
            sequence:
              - target:
                  entity_id: input_number.car_car2_cost_today
                data:
                  value: >
                    {{ (states('input_number.car_car2_cost_today') | float) + delta
                    }}
                action: input_number.set_value
    mode: single
    ```

- Finally, create an automation that will reset the cost to 0 at midnight every day:

    ```yaml
    alias: Reset Daily EV Car Costs
    description: ""
    triggers:
      - at: "00:00:00"
        trigger: time
    actions:
      - target:
          entity_id:
            - input_number.car_car1_cost_today
            - input_number.car_car2_cost_today
        data:
          value: 0
        action: input_number.set_value
    mode: single
    ```
