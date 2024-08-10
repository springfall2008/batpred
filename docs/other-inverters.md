# Other Inverters

PredBat was originally written for GivEnergy inverters using the GivTCP integration but this is now being extended to other models:

   | Name                          | Integration     | Template |
   | :---------------------------- | :------------- | :------------ |
   | GivEnergy with GivTCP | [GivTCP](https://github.com/britkat1980/giv_tcp) | [givenergy_givtcp.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_givtcp.yaml) |
   | Solis Hybrid inverters | [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus) | [ginlong_solis.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/ginlong_solis.yaml) |
   | Solax Gen4 inverters | [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus) in Modbus Power Control Mode |  [solax_sx4.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solax_sx4.yaml) |
   | Sofar inverters | [Sofar MQTT integration](https://github.com/cmcgerty/Sofar2mqtt) |  [sofar.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar.yaml) |
   | Huawei inverters | [Huawei Solar](https://github.com/wlcrs/huawei_solar) | [huawei.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/huawei.yaml) |
   | SolarEdge inverters | [Solaredge Modbus Multi](https://github.com/WillCodeForCats/solaredge-modbus-multi) | [solaredge.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solaredge.yaml) |
   | Givenergy with GE Cloud | [ge_cloud](https://github.com/springfall2008/ge_cloud) | [givenergy_cloud.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_cloud.yaml) |
   | Givenergy with GE Cloud EMC | [ge_cloud](https://github.com/springfall2008/ge_cloud) | [givenergy_ems.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_ems.yaml) |
   | SunSynk | [Sunsynk](https://github.com/kellerza/sunsynk) | [sunsynk.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sunsynk.yaml) |

Note that support for all these inverters is in various stages of development. Please expect things to fail and report them as Issues on Github.
Please also ensure you have set up enhanced logging in AppDaemon as described here.

## GivEnergy with GivTCP

Please see the main installation instructions, you will need to install GivTCP first and then use the supplied template

## Solis Inverters

To run PredBat with Solis hybrid inverters, follow the following steps:

1. Install PredBat as per the [Installation Summary](installation-summary.md)
2. Ensure that you have the Solax Modbus integration running. There are a number of entities which this integration disables by default that you
   will need to enable via the Home Assistant GUI:

   | Name                          | Description     |
   | :---------------------------- | :-------------- |
   | `sensor.solisx_rtc`           | Real Time Clock |
   | `sensor.solisx_battery_power` | Battery Power   |

4. Instead of `apps.yaml` use `ginlong_solis.yaml` from this Repo as your starting template.
   The majority of settings should be correct but please check.
   You will need to un-comment the `template` line to enable it. Save it to the `config/appdaemon/apps/predbat/config` folder.
   Set **solax_modbus_new** in apps.yaml to True if you have integration version 2024.03.2 or greater
6. Ensure that the inverter is set Control Mode 35 - on the Solax integration this is `Timed Charge/Discharge`.
   If you want to use the `Reserve` functionality within PredBat you will need to select `Backup/Reserve` (code 51) instead but be aware that
   this is not fully tested. In due course these mode settings will be incorporated into the code.

## Solax Gen4 Inverters

Use the template configuration from: [solax.sx4.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solax_sx4.yaml)

- Set **solax_modbus_new** in apps.yaml to True if you have integration version 2024.03.2 or greater

Please see this ticket in Github for ongoing discussion: <https://github.com/springfall2008/batpred/issues/259>

## Sofar Inverters

For this integration the key elements are:

- Hardware - [sofar2mqtt EPS board](https://www.instructables.com/Sofar2mqtt-Remote-Control-for-Sofar-Solar-Inverter/) - Relatively easy to solder and flash, or can be bought pre-made.
- Software - [Sofar MQTT integration](https://github.com/cmcgerty/Sofar2mqtt) - MQTT integration
- Home Assistant configuration - [sofar_inverter.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar_inverter.yml) package (in templates directory)
with the MQTT sensors. This is the default with a couple of additional inputs to support battery capacity. This should be installed in Home Assistant.
- Predbat configuration - [sofar.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar.yaml) template for Predbat (in templates directory).
This file should be copied to apps.yaml

- Please note that the inverter needs to be put into "Passive Mode" for the sofar2mqtt to control the inverter.
- This integration has various limitations, it can charge and discharge the battery but does not have finer control over reserve and target SOC%
- Note: You will need to change the min reserve in Home Assistant to match your minimum battery level (**input_number.predbat_set_reserve_min**).

Please see this ticket in Github for ongoing discussions: <https://github.com/springfall2008/batpred/issues/395>

## Huawei Inverters

Discussion ticket is here: <https://github.com/springfall2008/batpred/issues/684>

- Please copy the template apps.yaml from <https://github.com/springfall2008/batpred/blob/main/templates/huawei.yaml> and modify for your system
- Ensure you set **input_number.predbat_set_reserve_min** to the minimum value for your system which maybe 12%

## SolarEdge Inverters

Discussion ticket is here: <https://github.com/springfall2008/batpred/issues/181>

- Please copy the template apps.yaml from <https://github.com/springfall2008/batpred/blob/main/templates/solaredge.yaml> and modify for your system
- Ensure that **number.solaredge_i1_storage_command_timeout** is set to reasonably high value e.g. 3600 seconds to avoid the commands issued being cancelled
- Power Control Options, as well as Enable Battery Control, must be enabled in the Solaredge Modbus Multi integration configuration,
and switch.solaredge_i1_advanced_power_control must be on.

- For **pv_today**, **pv_power** and **load_power** sensors to work you need to create this as a template within your Home Assistant configuration.yml
Please see: <https://gist.github.com/Ashpork/f80fb0d3cb22356a12ed24734065061c>. These sensors are not critical so you can just comment it out in apps.yaml
if you can't get it to work

```yaml
template:
  - sensor:
      - name: "Solar Panel Production W"
        unique_id: solar_panel_production_w
        unit_of_measurement: "W"
        icon: mdi:solar-power
        state: >
          {% set i1_dc_power = states('sensor.solaredge_i1_dc_power') | float(0) %}
          {% set b1_dc_power = states('sensor.solaredge_b1_dc_power') | float(0) %}
          {% if (i1_dc_power + b1_dc_power <= 0) %}
            0
          {% else %}
            {{ (i1_dc_power + b1_dc_power) }}
          {% endif %}
        availability: >
          {{ states('sensor.solaredge_i1_dc_power') | is_number and states('sensor.solaredge_b1_dc_power') | is_number }}

      - name: "Solar House Consumption W"
        unique_id: solar_house_consumption_w
        unit_of_measurement: "W"
        icon: mdi:home
        state: >
          {% set i1_ac_power = states('sensor.solaredge_i1_ac_power') | float(0) %}
          {% set m1_ac_power = states('sensor.solaredge_m1_ac_power') | float(0) %}
          {% if (i1_ac_power - m1_ac_power <= 0) %}
            0
          {% else %}
            {{ (i1_ac_power - m1_ac_power) }}
          {% endif %}
        availability: >
          {{ states('sensor.solaredge_i1_ac_power') | is_number and states('sensor.solaredge_m1_ac_power') | is_number }}

sensor:
  - platform: integration
    source: sensor.solar_panel_production_w
    method: left
    unit_prefix: k
    name: solar_panel_production_kwh
```

## Givenergy with ge_cloud

This is experimental system, please discuss on the ticket: <https://github.com/springfall2008/batpred/issues/905>

- First set up ge_cloud integration using your API key <https://github.com/springfall2008/ge_cloud>
- Now copy the template givenergy_cloud.yaml from templates into your apps.yaml and edit
    - Set geserial to your inverter serial
- Make sure discharge down to registers are set to 4% and slots 2, 3 and 4 for charge and discharge are disabled (if you have them)

## Givenergy with EMC

- First set up ge_cloud integration using your API key <https://github.com/springfall2008/ge_cloud>
- Now copy the template givenergy_emc.yaml from templates into your apps.yaml and edit
    - Set geserial to your first inverter serial and geserial2 to the second (look in HA for entity names)
    - Set geseriale to the EMS inverter serial number (look in HA for entity names)
- Turn off slots 2, 3 and 4 for charge, export and discharge as Predbat will only use 1 slot (set the start and end times to 00:00)

## Sunsynk

This is experimental system, please discuss on the ticket: <https://github.com/springfall2008/batpred/issues/1060>

- A few custom template sensors are required, the code for those are listed inside the apps.yaml template for Sunsynk, copy them
into your HA configuration.

- An automation is required to update the charge limits across all timezone's:

```yaml
alias: PredBat - Copy Charge Limit
description: ""
trigger:
  - platform: state
    entity_id:
      - number.sunsynk_set_soc_timezone1
    to: null
condition: []
action:
  - service: number.set_value
    data_template:
      entity_id:
        - number.sunsynk_set_soc_timezone2
        - number.sunsynk_set_soc_timezone3
        - number.sunsynk_set_soc_timezone4
        - number.sunsynk_set_soc_timezone5
        - number.sunsynk_set_soc_timezone6
      value: "{{ states('number.sunsynk_set_soc_timezone1')|int(20) }}"
mode: single
```

## I want to add an unsupported inverter to Predbat

- First copy one of the template configurations that is close to your system and try to configure it to match the sensors you have
- Create a github ticket for support and add in what you know to the ticket
- Then find out how to control your inverter inside Home Assistant, ideally share any automation you have to control the inverter
- You can create a new inverter type in apps.yaml and change the options as to which controls it has
- The easy way to integrate is to use a HA service to start charges and discharges, edit the template below

```yaml
 inverter_type: MINE
 inverter:
    name : "My Shiny new Inverter"
    has_rest_api: False
    has_mqtt_api: False
    has_service_api: True
    output_charge_control: "power"
    has_charge_enable_time: False
    has_discharge_enable_time: False
    has_target_soc: False
    has_reserve_soc: False
    charge_time_format: "S"
    charge_time_entity_is_option: False
    soc_units: "%"
    num_load_entities: 1
    has_ge_inverter_mode": False
    time_button_press: False
    clock_time_format: "%Y-%m-%d %H:%M:%S"
    write_and_poll_sleep: 2
    has_time_window: False
    support_charge_freeze: False
    support_discharge_freeze": False

  # Services to control charging/discharging
  charge_start_service:
    service: select.select_option
    entity_id: "select.solaredge_i1_storage_command_mode"
    option: "Charge from Solar Power and Grid"
  charge_stop_service:
    service: select.select_option
    entity_id: "select.solaredge_i1_storage_command_mode"
    option: "Charge from Solar Power"
  discharge_start_service:
    service: select.select_option
    entity_id: "select.solaredge_i1_storage_command_mode"
    option: "Maximize Self Consumption"

```

## Inverter control option

The follow options are supported per inverter:

### has_rest_api

When True the REST API will be used to fetch data/control the inverter. This is currently only for GivEnergy inverters with GivTCP and **givtcp_rest** must be set in apps.yaml

### has_mqtt_api

When True the MQTT API to Home Assistant will be used to issue control messages for the inverter

The mqtt/publish service is used with the topic as defined by **mqtt_topic** in apps.yaml

Messages will be sent these controls:

Values that are updated:

- **topic**/set/reserve  - payload=reserve
- **topic**/set/charge_rate - payload=new_rate
- **topic**/set/discharge_rate - payload=new_rate
- **topic**/set/target_soc - payload=target_soc

These three change between battery charge/discharge and auto (idle) mode:

- **topic**/set/charge - payload=charge_rate
- **topic**/set/discharge - payload=discharge_rate
- **topic**/set/auto - payload=true

### inv_has_service_api

When True a Home Assistant service will be used to issue control messages for the inverter

For each service you wish to use it must be defined in apps.yaml.

There are two ways to define a service, the basic mode:

```yaml
charge_start_service: my_service_name_charge
```

Will call my_service_name_charge for the charge start service.

Or the custom method:

```yaml
charge_start_service:
   - service: my_charge_start_service
     device_id: {device_id}
     power: {power}
     soc: {target_soc}
```

Here you can define all the values passed to the service and use the default values from the template or define your own.

You can also call more than one service e.g:

```yaml
charge_start_service:
   - service: my_charge_start_service
     device_id: {device_id}
     power: {power}
     soc: {target_soc}
   - service: switch.turn_off
     entity_id: switch.tsunami_charger
```

#### charge_start_service

Called to start a charge

The default options passed in are:

- device_id - as defined in apps.yaml by **device_id**
- target_soc - The SOC to charge to
- power - The charge power to use

#### discharge_start_service

Called to start a discharge

The default options passed in are:

- device_id - as defined in apps.yaml by **device_id**
- target_soc - The SOC to discharge to
- power - The discharge power to use

#### charge_stop_service

Called to stop a charge or stop a discharge

device_id - as defined in apps.yaml by **device_id**

#### discharge_stop_service

Called to stop a discharge

- device_id - as defined in apps.yaml by **device_id**

### output_charge_control

Set to power, current or none

When power the inverter has a **charge_rate** and **discharge_rate** setting in watts defined in apps.yaml

When current the inverter has  **timed_charge_current** and **timed_discharge_current** setting in amps defined in apps.yaml

### charge_control_immediate

When True the inverter **timed_charge_current** or **timed_discharge_current** is used to control charging or discharging as/when it starts and stops rather than using a timed method.

### current_dp

Sets the number of decimal places when setting the current in Amps, should be 0 or 1

### has_charge_enable_time

When True the inverter has a setting defined in apps.yaml called **scheduled_charge_enable** when can be used to enable/disable timed charging.

### has_discharge_enable_time

When True the inverter has a setting defined in apps.yaml called **scheduled_discharge_enable** when can be used to enable/disable timed discharging.

### has_target_soc

When True the inverter has a target soc setting in apps.yaml called **charge_limit**, when False charging must be turned on and off by Predbat rather
than the inverter doing it based on the target %

### has_reserve_soc

When True the inverter has a reserve soc setting in apps.yaml called **reserve**

### has_timed_pause

When True the inverter has a setting in apps.yaml called **pause_mode** and settings **pause_start_time** and **pause_end_time** which can be used to pause the inverter from
charging and discharging the battery - this is for GivEnergy systems only right now.

### charge_time_format

When set to "HH:MM:SS" the inverter has:

**charge_start_time** **charge_end_time**
**discharge_start_time** **discharge_end_time**

Which are option selectors in the format HH:MM:SS (e.g. 12:23:00) where seconds are always 00.

When set to "H M" the inverter has:

**charge_start_hour** **charge_end_hour** **charge_start_minute** **charge_end_minute**
**discharge_start_hour** **discharge_end_hour** **discharge_start_minute** **discharge_end_minute**

Settings in apps.yaml which can be used to set the start and end times of charges and discharges

### charge_time_entity_is_option

When True **charge_start_time** **charge_end_time** **discharge_start_time** and **discharge_end_time** are all Options, when false they are number values.

### clock_time_format

Defines the time format of the inverter clock setting **inverter_time** in apps.yaml

### soc_units

Defines the units of the SOC setting (currently not used)

### time_button_press

When true the inverter has a button press which is needed to update the inverter registers from the Home Assistant values.

The apps.yaml setting **charge_discharge_update_button** is the entity name of the button that must be pressed and polled until it updates after each inverter register change.

### support_charge_freeze

When True the inverter supports charge freeze modes

### support_discharge_freeze

When True the inverter supports discharge freeze modes

### has_ge_inverter_mode

When True the inverter as the GivEnergy inverter modes (ECO, Timed Export etc).

### num_load_entities

Sets the number of **load_power_n** settings in apps.yaml are present in addition to **load_power** (the default)

### write_and_poll_sleep

Sets the number of seconds between polls of inverter settings

### has_idle_time

When True the inverter has an idle time register which must be set to the start and end times for ECO mode (GivEnergy EMC)

### can_span_midnight

When True start and end times for charge and discharge can span midnight e.g. 23:00:00 - 01:00:00 is a 2 hour slot.

### charge_discharge_with_rate

When True when charging discharge rate must be 0 and visa-versa. When false the rate does not have to change.
