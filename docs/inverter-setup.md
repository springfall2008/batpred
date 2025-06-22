# Inverter setup

PredBat was originally written for GivEnergy inverters using the GivTCP integration but this is now being extended to other inverter models:

   | Name                          | Integration     | Template |
   | :---------------------------- | :------------- | :------------ |
   | [GivEnergy with GivTCP](#givenergy-with-givtcp) | [GivTCP](https://github.com/britkat1980/ha-addons) | [givenergy_givtcp.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_givtcp.yaml) |
   | [Solis Hybrid inverters](#solis-inverters) | [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus) | [ginlong_solis.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/ginlong_solis.yaml) |
   | [Solax Gen4 inverters](#solax-gen4-inverters) | [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus)<BR>in Modbus Power Control Mode |  [solax_sx4.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solax_sx4.yaml) |
   | [Sofar inverters](#sofar-inverters) | [Sofar MQTT integration](https://github.com/cmcgerty/Sofar2mqtt) |  [sofar.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar.yaml) |
   | [Huawei inverters](#huawei-inverters) | [Huawei Solar](https://github.com/wlcrs/huawei_solar) | [huawei.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/huawei.yaml) |
   | [SolarEdge inverters](#solaredge-inverters) | [Solaredge Modbus Multi](https://github.com/WillCodeForCats/solaredge-modbus-multi) | [solaredge.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solaredge.yaml) |
   | [Givenergy with GE Cloud](#givenergy-with-ge_cloud) | [ge_cloud](https://github.com/springfall2008/ge_cloud) | [givenergy_cloud.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_cloud.yaml) |
   | [Givenergy with GE Cloud EMS](#givenergy-with-ems) | [ge_cloud EMS](https://github.com/springfall2008/ge_cloud) | [givenergy_ems.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_ems.yaml) |
   | [Givenergy/Octopus No Home Assistant](#givenergyoctopus-cloud-direct---no-home-assistant) | n/a | [ge_cloud_octopus_standalone.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/ge_cloud_octopus_standalone.yaml) |
   | [SunSynk](#sunsynk) | [Sunsynk](https://github.com/kellerza/sunsynk) | [sunsynk.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sunsynk.yaml) |
   | [Fox](#fox) | [Foxess](https://github.com/nathanmarlor/foxess_modbus) | [fox.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/fox.yaml) |
   | [LuxPower](#lux-power) | [LuxPython](https://github.com/guybw/LuxPython_DEV) | [luxpower.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/luxpower.yaml) |
   | [Growatt with Solar Assistant](#growatt-with-solar-assistant) | [Solar Assistant](https://solar-assistant.io/help/home-assistant/setup) | [solar_assistant_growatt.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solar_assistant_growatt.yaml) |
   | [SigEnergy](#sigenergy-sigenstor) | [SigEnergy](https://github.com/TypQxQ/Sigenergy-Home-Assistant-Integration) | [sigenergy_sigenstor.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sigenergy_sigenstor.yaml)|

Note that support for all these inverters is in various stages of development. Please expect things to fail and report them as Issues on GitHub.

NB: By default the apps.yaml template for GivTCP is installed with Predbat.
If you are using a different inverter then you will need to copy the appropriate apps.yaml template from the above list and use it to **replace the GivTCP apps.yaml** - if
you copy but don't replace the standard template then Predbat will not function correctly.

## GivEnergy with GivTCP

It's recommended that you first watch the [Installing GivTCP and Mosquitto Add-on's video from Speak to the Geek](https://www.youtube.com/watch?v=ygD9KyciX54).
Although the video covers GivTCP v2 and v3 has been recently released, the installation and setup process are very similar.

The below instructions assume you are installing GivTCP v3, with changes highlighted against GivTCP v2 as covered in the video.

1. Install Mosquitto Broker add-on:

- Go to Settings / Add-ons / Add-on Store (bottom right)
- Scroll down the add-on store list, to find 'Mosquitto broker', click on the add-on, then click 'INSTALL'
- Once the Mosquitto broker has been installed, ensure that the 'Start on boot' and 'Watchdog' options are turned on, and click 'START' to start the add-on
- Next, configure Mosquitto broker by going to Settings / Devices and Services / Integrations.
Mosquitto broker should appear as a Discovered integration so click the blue 'CONFIGURE' button, then SUBMIT to complete configuring Mosquitto broker
- With GivTCP v3 you no longer need to create a dedicated Home Assistant user for MQTT so this part of the video can be skipped over

2. Install the GivTCP add-on:

- Go to Settings / Add-ons / Add-on Store
- Click the three dots in the top right corner, then Repositories
- You'll need to add the GivTCP repository as an additional custom repository so paste/type
'[https://github.com/britkat1980/ha-addons](https://github.com/britkat1980/ha-addons')' into the text box and click 'Add' the 'Close'<BR>
NB: this URL is for GivTCP v3, not v2 as covered in the video.
- Click the back button and then re-navigate to Settings / Add-ons / Add-on Store so Home Assistant picks up the GivTCP add-on from the custom repository
- Scroll down the add-on store list, to find 'GivTCP-V3', you should see the three addons; the production version, the latest beta and the latest dev versions.
Click on the 'GivTCP' add-on, then click 'INSTALL'
- Once GivTCP has been installed, ensure that the 'Start on boot' and 'Watchdog' options are turned on

3. Configure GivTCP:

- The configuration process for GivTCP in v3 has changed from that shown in the video,
the Configuration tab is no longer used and all configuration is now done via the add-on's Web interface
- On the GivTCP add-on, click 'START' to start the add-on
- Once the add-on has started, click 'Open Web UI' or go to [http://homeassistant.local:8099/](http://homeassistant.local:8099/), then click 'Go to Config Page' to configure GivTCP
- GivTCP will auto-discover your inverters and batteries so you shouldn't need to manually enter these, but check the IP address(s) it finds are correct
- If you have a single AIO then for Predbat to be able to communicate via REST to the AIO, it MUST be the first device configured in GivTCP.  Conversely if you have a gateway and multiple AIO's then the gateway MUST be the first device in GivTCP
- If you have multiple inverters you may wish to change the default device prefixes that GivTCP assigns ('givtcp', 'givtcp2', 'givtcp3', etc)
to make it easier to identify your devices within Home Assistant.<BR>
For example, if you have a gateway and two AIOs you could use the prefixes 'GW', 'AIO-1' and 'AIO-2'.
The prefixes should be set before you start using GivTCP in anger
as changing the prefixes later on will result in both the old and new sensor names appearing in Home Assistant with the 'old' sensors being "unavailable".<BR>
Note that if you do change the givtcp prefixes then you will also have to edit the apps.yaml configuration file to match,
and change the sensor names that Predbat is looking for (by default prefixed 'givtcp_xxx') to your new sensor naming structure
- Click Next and Next to get to the Selfrun page, and turn on Self Run. The Self Run Loop Timer is how often GivTCP will retrieve data from your inverters - it's
recommended that set this to a value between 20 and 60, but not less than 15 seconds as otherwise the inverter will then spend all its time talking to GivTCP
and won't communicate with the GivEnergy portal and app
- GivTCP now auto-populates the MQTT page so as long as you're using Mosquitto broker within Home Assistant;
you won't need to create a dedicated MQTT user or enter the details on the MQTT page
- You don't need to configure the Influx page. Tariff and Palm pages can also be skipped as these functions are done by Predbat
- (Optional) On the Web page, you can turn the Dashboard on to see a simple power flow diagram for your inverters (similar to the GivEnergy mobile app)
- On the 'Misc' page check that 'Print Raw' is set to on for added monitoring
- Finally, click 'Save and Restart' and GivTCP should start communicating with your inverters
and will automatically create a set of 'givtcp_xxx' entities in Home Assistant for your inverter data, inverter controls and battery data
- Check the GivTCP Log tab that there aren't any errors; it should end with 'Publishing Home Assistant Discovery messages'

4. Before you start using GivTCP to control your inverter

Verify in the GivEnergy portal settings the following inverter settings are set correctly as these are settings that Predbat doesn't control, and if not set correctly could affect your battery activity:

- "Inverter Charge Power Percentage" is set to 100 (Predbat has its own low-rate charge control you can use if you wish)
- "Inverter Discharge Power Percentage" is set to 100. If you do wish to set a lower discharge rate then its recommended that instead you set [inverter_limit_discharge in apps.yaml](apps-yaml.md#inverter-control-configurations) to the rate
- "Battery Cutoff % Limit" is set to 4
- "Enable AC Charge Upper Limit' is enabled (if you have this option)
- That charge slot 2 (or more) are disabled (as Predbat only uses slot1)
- That discharge slot 2 (or more) are disabled  (as Predbat only uses slot1)

5. Specific Predbat configuration requirements for certain GivEnergy equipment

The rest of the [Predbat installation instructions](install.md) should now be followed,
but its worth highlighting that there are a few specific settings that should be set for certain GivEnergy equipment.
These settings are documented in the appropriate place in the documentation, but for ease of identification, are repeated here:

- If you are using GivTCP v3 and have an AIO or 3-phase inverter then you will need to manually set [geserial in apps.yaml](apps-yaml.md#geserial) to your inverter serial number as the auto-detect doesn't work for this setup
- If you have a single AIO then control is directly to the AIO. Ensure [geserial in apps.yaml](apps-yaml.md#geserial) is correctly picking the AIO and comment out geserial2 lines
- If you have multiple AIOs then all control of the AIOs is done through the Gateway so [geserial in apps.yaml](apps-yaml.md#geserial) should be set to the Gateway serial number
- If you have multiple AIOs you might want to consider setting [inverter charge and discharge limits](apps-yaml.md#inverter-control-configurations)
unless you want to charge and discharge at the full 12kWh!
- If you have a 2.6kWh, 5.2kWh or AIO battery then you will need to set [battery_scaling in apps.yaml](apps-yaml.md#battery-size-scaling)
as the battery size is incorrectly reported to GivTCP
- If you have an older inverter (AC3 or Gen 1 hybrid) with firmware that has battery pause support you may need to [comment out pause start and end time controls in apps.yaml](apps-yaml.md#schedule)
- If you have a Gen 2, Gen 3 or AIO then you may need to set [inverter_reserve_max in apps.yaml](apps-yaml.md#inverter-reserve-maximum) to 98.
If you have a Gen 1 or a firmware version that allows the reserve being set to 100 then you can change the default from 98 to 100
- If your inverter has been wired as an EPS (Emergency Power Supply) or AIO 'whole home backup', consider setting
[input_number.predbat_set_reserve_min](customisation.md#inverter-control-options) to reserve some battery power for use in emergencies.

**NB: GivTCP and Predbat do not currently yet work together for 3-phase inverters**.
This is being worked on by the author of GivTCP, e.g. see [GivTCP issue: unable to charge or discharge 3 phase inverters with predbat](https://github.com/britkat1980/giv_tcp/issues/218)

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
   You will need to un-comment the `template` line to enable it. Save it to the appropriate [Predbat software directory](apps-yaml.md#appsyaml-settings).
   Set **solax_modbus_new** in apps.yaml to True if you have integration version 2024.03.2 or greater
6. Ensure that the inverter is set to Control Mode 35 - on the Solax integration this is `Timed Charge/Discharge`.
   If you want to use the `Reserve` functionality within PredBat you will need to select `Backup/Reserve` (code 51) instead but be aware that
   this is not fully tested. In due course, these mode settings will be incorporated into the code.
7. Your inverter will require a "button press" triggered by Predbat to update the schedules. Some Solis inverter integrations feature a combined charge/discharge update button, in which case a single entry of:

```yaml
charge_discharge_update_button:
  - button.solis_charge_discharge
```

is sufficient. For other configurations (for example using the "solis_fb00" plugin) where separate buttons are used for charging and discharging, provide both:

```yaml
charge_update_button:
  - button.solis_charge
discharge_update_button:
  - button.solis_discharge
```

Ensure the correct entity IDs are used for your specific inverter setup. These entries should correspond to the buttons exposed by your Home Assistant Solis integration.

## Solax Gen4 Inverters

Use the template configuration from: [solax.sx4.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solax_sx4.yaml)

- Set **solax_modbus_new** in apps.yaml to True if you have integration version 2024.03.2 or greater

Please see this ticket in Github for ongoing discussion: <https://github.com/springfall2008/batpred/issues/259>

## Sofar Inverters

For this integration, the key elements are:

- Hardware - [sofar2mqtt EPS board](https://www.instructables.com/Sofar2mqtt-Remote-Control-for-Sofar-Solar-Inverter/) - Relatively easy to solder and flash, or can be bought pre-made.
- Software - [Sofar MQTT integration](https://github.com/cmcgerty/Sofar2mqtt) - MQTT integration
- Home Assistant configuration - [sofar_inverter.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar_inverter.yaml) (in templates directory),
defines the custom HA entities and should be added to HA's `configuration.yaml`. This is the default Sofar HA configuration with a couple of additional inputs to support battery capacity.
- Predbat configuration - [sofar.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar.yaml) template for Predbat (in templates directory). This file should be copied to apps.yaml

- Please note that the inverter needs to be put into "Passive Mode" for the sofar2mqtt to control the inverter.
- This integration has various limitations, it can charge and discharge the battery but does not have finer control over reserve and target SOC%
- Note: You will need to change the min reserve in Home Assistant to match your minimum battery level (**input_number.predbat_set_reserve_min**).

Please see this ticket in Github for ongoing discussions: <https://github.com/springfall2008/batpred/issues/395>

## Huawei Inverters

The discussion ticket is here: <https://github.com/springfall2008/batpred/issues/684>

- Please copy the template apps.yaml from <https://github.com/springfall2008/batpred/blob/main/templates/huawei.yaml> and modify them for your system
- Ensure you set **input_number.predbat_set_reserve_min** to the minimum value for your system which may be 12%

## SolarEdge Inverters

The discussion ticket is here: <https://github.com/springfall2008/batpred/issues/181>

- Please copy the template apps.yaml from <https://github.com/springfall2008/batpred/blob/main/templates/solaredge.yaml> and modify them for your system
- The default entity name prefix for the integration is 'solaredge' but if you have changed this on installation then you will need to amend the apps.yaml template and the template sensors to match your new prefix
- Ensure that **number.solaredge_i1_storage_command_timeout** is set to a reasonably high value e.g. 3600 seconds to avoid the commands issued being cancelled
- Power Control Options, as well as Enable Battery Control, must be enabled in the Solaredge Modbus Multi integration configuration,
and **switch.solaredge_i1_advanced_power_control** must be on.

- For **pv_today**, **pv_power** and **load_power** sensors to work you need to create these as a template within your Home Assistant `configuration.yaml`.
Please see: <https://gist.github.com/Ashpork/f80fb0d3cb22356a12ed24734065061c>. These sensors are not critical so you can just comment it out in apps.yaml if you can't get it to work

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

If you have multiple batteries connected to your SolarEdge inverter and are using the SolarEdge Modbus Multi integration, this enumerates the multiple batteries as b1, b2, b3, etc with separate entities per battery.

You will need to make a number of changes to the solaredge apps.yaml, replacing the following entries:

```yaml
  battery_rate_max:
    - sensor.calc_power_batteries_max_charge_power # maximum charge power of all the batteries
  battery_power:
    - sensor.calc_power_batteries_dc_power
  soc_percent:
    - sensor.calc_battery_all_state # average SoC of the batteries
  soc_max:
    - sensor.calc_battery_total_capacity # combined kWh maximum value of all the batteries
  soc_kw:
    - sensor.calc_battery_current_capacity
```

- set charge_rate and discharge_rate to the SolarEdge inverter values, e.g. 5000

And add the following additional template sensors to configuration.yaml:

```yaml
    - sensor:
      # Template sensor for Max Battery Charge rate
      # This is the sum of all three batteries charge rate as the max charge rate can be higher than inverter capacity (e.g. 8k) when charging from AC+Solar
      # Returns 5000W as the minimum max value, the single battery charge/discharge limit to ensure at least one battery can always be charged if one or more batteries have 'gone offline' to modbus
      - name: "Calc Power - Batteries Max Charge Power"
        unique_id: calc_power_batteries_max_charge_power
        unit_of_measurement: "W"
        device_class: "power"
        state_class: "measurement"
        state: >
          {% set myB1 = float(states('sensor.solaredge_b1_max_charge_power'),0) %}
          {% set myB2 = float(states('sensor.solaredge_b2_max_charge_power'),0) %}
          {% set myB3 = float(states('sensor.solaredge_b3_max_charge_power'),0) %}
          {% set myValue = ((myB1 + myB2 + myB3)) | int %}
          {{ (myValue if (myValue) > 5000 else 5000) }}

      # Calculate Total Battery Power Value
      - name: "Calc Power - Batteries DC Power"
        unique_id: calc_power_batteries_dc_power
        unit_of_measurement: "W"
        device_class: "power"
        state_class: "measurement"
        state: >
          {% set myB1 = float(states('sensor.solaredge_b1_dc_power'),0) %}
          {% set myB2 = float(states('sensor.solaredge_b2_dc_power'),0) %}
          {% set myB3 = float(states('sensor.solaredge_b3_dc_power'),0) %}
          {% set myValue = ((myB1 + myB2 + myB3)) %}
          {{ myValue }}

      # Average state of charge across the batteries
      - name: "Calc Battery All State"
        unique_id: calc_battery_all_state
        unit_of_measurement: "%"
        state: >
          {% set myB1 = float(states('sensor.solaredge_b1_state_of_energy'),0) %}
          {% set myB2 = float(states('sensor.solaredge_b2_state_of_energy'),0) %}
          {% set myB3 = float(states('sensor.solaredge_b3_state_of_energy'),0) %}
          {% set myValue = ((myB1 + myB2 + myB3) / 3) | round(0) %}
          {{ myValue }}

      # Total Energy Stored in the Batteries
      - name: "Calc Battery Total Capacity"
        unique_id: calc_battery_total_capacity
        unit_of_measurement: kWh
        state: >
          {% set myB1 = float(states('sensor.solaredge_b1_maximum_energy'),0) %}
          {% set myB2 = float(states('sensor.solaredge_b2_maximum_energy'),0) %}
          {% set myB3 = float(states('sensor.solaredge_b3_maximum_energy'),0) %}
          {% set myValue = ((myB1 + myB2 + myB3)) %}
          {{ myValue }}

      # Current Energy Stored in the Batteries
      - name: "Calc Battery Current Capacity"
        unique_id: calc_battery_current_capacity
        unit_of_measurement: kWh
        state: >
          {% set myValue = (float(states('sensor.calc_battery_all_state'),0) / 100) * float(states('sensor.calc_battery_total_capacity'),0) %}
          {{ myValue }}
```

## GivEnergy with ge_cloud

This is an experimental system, please discuss it on the ticket: <https://github.com/springfall2008/batpred/issues/905>

- First set up ge_cloud integration using your API key <https://github.com/springfall2008/ge_cloud>
- Now copy the template givenergy_cloud.yaml from templates into your apps.yaml and edit
    - Set geserial to your inverter serial number
- Make sure that the 'discharge down to' registers are set to 4% and slots 2, 3 and 4 for charge and discharge are disabled in the portal (if you have them)

## GivEnergy with EMS

- First set up ge_cloud integration using your API key <https://github.com/springfall2008/ge_cloud>
- Now copy the template givenergy_ems.yaml from templates into your apps.yaml and edit
    - Set geserial to your first inverter serial and geserial2 to the second (look in HA for entity names)
    - Set geseriale to the EMS inverter serial number (look in HA for the entity names)
- Turn off charge, export and discharge slots 2, 3 and 4 as Predbat will only use slot 1 - set the start and end times for these to 00:00

## GivEnergy/Octopus Cloud Direct - No Home Assistant

- Take the template and enter your GivEnergy API key directly into apps.yaml
- Set your Octopus API key in apps.yaml
- Set your Solcast API key in apps.yaml
- Review any other configuration settings

Launch Predbat with hass.py (from the Predbat-addon repository) either via a Docker or just on a Linux/MAC/WSL command line shell.

## Fox

**Experimental**

- I've managed to get Batpred working on my Fox ESS inverter, connected via an Elfin EW11 modbus and using Nathan's Fox ESS Modbus tool.
See: <https://github.com/springfall2008/batpred/issues/1401>

The template is in the templates area, give it a try

## Lux Power

This requires the LuxPython component which integrates with your Lux Power inverter

- Copy the template luxpower.yaml from templates into your apps.yaml and edit inverter and battery settings as required
- LuxPower does not have a SoC max entity in kWh and the SoC percentage entity never reports the battery reaching 100%, so create the following template helper sensors:

```text
name: Lux SoC Max kWh
template:
  {{ (states("sensor.lux_battery_capacity_ah") |float) *
     (states("sensor.lux_battery_voltage_live") | float) / 1000}}
unit of measurement: kWh
device class: Energy
state class: Total
```

```text
name: Lux Battery SoC Corrected
template:
  {% set soc = states('sensor.lux_battery')|int %}
  {% set charging_stopped = states('sensor.lux_bms_limit_charge_live')|float == 0 %}
  {% if charging_stopped and soc > 97 %}
    100
  {% else %}
    {{ soc }}
  {% endif %}
unit of measurement: %
device class: Battery
state class: Measurement
```

## Growatt with Solar Assistant

You need to have a Solar Assistant installation <https://solar-assistant.io>

Growatt has two popular series of inverters, SPA and SPH. Copy the template that matches your model from templates into your apps.yaml and edit inverter and battery settings as required. Yours may have different entity IDs on Home Assistant.

## Sunsynk

- Copy the Sunsynk apps.yaml template and edit for your system.
- Create the following Home Assistant automations:

```yaml
alias: Predbat Charge / Discharge Control
description: "Turn SunSynk charge/discharge on/off to mirror Predbat"
trigger:
  - platform: state
    entity_id:
      - binary_sensor.predbat_charging
    to: "on"
    id: predbat_charge_on
  - platform: state
    entity_id:
      - binary_sensor.predbat_charging
    to: "off"
    id: predbat_charge_off
  - platform: state
    entity_id:
      - binary_sensor.predbat_exporting
    to: "on"
    id: predbat_discharge_on
  - platform: state
    entity_id:
      - binary_sensor.predbat_exporting
    to: "off"
    id: predbat_discharge_off
condition: []
action:
  - choose:
      - conditions:
          - condition: trigger
            id:
              - predbat_charge_on
        sequence:
          - service: switch.turn_on
            data: {}
            target:
              entity_id: switch.sunsynk_grid_charge_timezone1
      - conditions:
          - condition: trigger
            id:
              - predbat_charge_off
        sequence:
          - service: switch.turn_off
            target:
              entity_id:
                - switch.sunsynk_grid_charge_timezone1
            data: {}
      - conditions:
          - condition: trigger
            id:
              - predbat_discharge_on
        sequence:
          - service: switch.turn_on
            data: {}
            target:
              entity_id: switch.sunsynk_solar_sell
      - conditions:
          - condition: trigger
            id:
              - predbat_discharge_off
        sequence:
          - service: switch.turn_off
            target:
              entity_id:
                - switch.sunsynk_solar_sell
            data: {}
mode: single
```

```yaml
alias: PredBat - Copy Charge Limit
description: Copy Battery SOC to all timezone (time) slots
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

- Create the following templates sensors in your configuration.yaml:

```yaml
template:
  sensor:
    - name: "sunsynk_max_battery_charge_rate"
      unit_of_measurement: "w"
      state_class: measurement
      state: >
        {{ [8000,[states('input_number.sunsynk_battery_max_charge_current_limit')|int,states('sensor.sunsynk_battery_charge_limit_current')|int]|min
        * states('sensor.sunsynk_battery_voltage')|float]|min }}

    - name: "sunsynk_max_battery_discharge_rate"
      unit_of_measurement: "w"
      state_class: measurement
      state: >
        {{ [8000,[states('input_number.sunsynk_battery_max_discharge_current_limit')|int,states('sensor.sunsynk_battery_discharge_limit_current')|int]|min
        * states('sensor.sunsynk_battery_voltage')|float]|min }}

    - name: "sunsynk_charge_rate_calc"
      unit_of_measurement: "w"
      state_class: measurement
      state: >
        {{ [8000,[states('input_number.test_sunsynk_battery_max_charge_current')|int,states('sensor.sunsynk_battery_charge_limit_current')|int]|min
        * states('sensor.sunsynk_battery_voltage')|float]|min }}

    - name: "sunsynk_discharge_rate_calc"
      unit_of_measurement: "w"
      state_class: measurement
      state: >
        {{ [8000,[states('input_number.test_sunsynk_battery_max_discharge_current')|int,states('sensor.sunsynk_battery_discharge_limit_current')|int]|min
         * states('sensor.sunsynk_battery_voltage')|float]|min }}
```

## Sigenergy Sigenstor

To integrate your Sigenergy Sigenstor inverter with Predbat, you will need to follow the steps below:

- make sure the inverter is already integrated via Modbus. Here is a ([repo](https://github.com/TypQxQ/Sigenergy-Home-Assistant-Integration)) with full integration.
- make sure your charge rate is in W and not in kW. The above integration converts the values to kW by default.
- Copy the template sigenergy_sigenstor.yaml template over your apps.yaml and edit for your system.

  The following additions are needed to facilitate integration with Predbat and need to put put in Home Assistant `configuration.yaml`:

```yaml
    input_select:
      set_ems_mode:
        name: Set EMS mode
        options:
          - Maximum self-consumption
          - Command charging Grid
          - Command charging PV
          - Command discharging PV
          - Command discharging Bat
          - Command freeze charge
          - Command freeze discharge
        icon: mdi:battery-unknown

    template:
      - sensor:
          - name: "Sigen Battery Power W"
            unique_id: sigen_battery_power_w
            state: >
              {% set power = states('sensor.sigen_battery_power') | float(0) %}
              {{ (power * 1000) | round(0) }}
            unit_of_measurement: "W"  # Assuming the original is in kW and we're converting to W
            device_class: power
            state_class: measurement

    automation:
      - id: ems_mode_selector_action
        alias: "EMS mode input selector action"
        description: "Sets the remote EMS control mode predbat"
        trigger:
          - platform: state
            entity_id:
              - input_select.set_ems_mode
        condition: []
        action:
          - service: modbus.write_register
            data_template:
              hub: Sigen
              slave: 247
              address: 40031
              value: >
                {% if is_state('input_select.set_ems_mode', "Maximum self-consumption") %} 2
                {% elif is_state('input_select.set_ems_mode', "Command charging Grid") %} 3
                {% elif is_state('input_select.set_ems_mode', "Command charging PV") %} 4
                {% elif is_state('input_select.set_ems_mode', "Command discharging PV") %} 5
                {% elif is_state('input_select.set_ems_mode', "Command discharging Bat") %} 6
                {% elif is_state('input_select.set_ems_mode', "Command freeze charge") %} 4
                {% elif is_state('input_select.set_ems_mode', "Command freeze discharge") %} 6
                {% endif %}
          - choose:
              - conditions:
                  - condition: state
                    entity_id: input_select.set_ems_mode
                    state: "Command freeze charge"
                sequence:
                  - service: modbus.write_register
                    data_template:
                      hub: Sigen
                      slave: 247
                      address: 40032
                      value:
                        - 0
                        - 0
              - conditions:
                  - condition: state
                    entity_id: input_select.set_ems_mode
                    state: "Command freeze discharge"
                sequence:
                  - service: modbus.write_register
                    data_template:
                      hub: Sigen
                      slave: 247
                      address: 40034
                      value:
                        - 0
                        - 0
        mode: single

      - id: "automation_sigen_ess_max_charging_limit_input_number_action"
        alias: "sigen ESS max charging limit input number action"
        description: "Sigen ESS max charging limit action for batpred"
        triggers:
          - trigger: state
            entity_id: input_number.charge_rate
        action:
          - action: modbus.write_register
            data_template:
              hub: Sigen
              slave: 247
              address: 40032
              value:
                - >-
                  0
                - >-
                  {{ (states('input_number.charge_rate')| float) |
                  round(0) | int }}
        mode: single

      - id: "automation_sigen_ess_max_discharging_limit_input_number_action"
        alias: "sigen ESS max discharging limit input number action"
        description: "Sigen ESS max discharging limit action for batpred"
        triggers:
          - trigger: state
            entity_id: input_number.discharge_rate
        action:
          - action: modbus.write_register
            data_template:
              hub: Sigen
              slave: 247
              address: 40034
              value:
                - >-
                  0
                - >-
                  {{ states('input_number.discharge_rate') |
                  round(0) | int }}
        mode: single

    input_number:
      charge_rate:
        name: Battery charge rate
        initial: 8400
        min: 0
        max: 20000
        step: 1
        mode: box

      discharge_rate:
        name: Battery discharge rate
        initial: 8400
        min: 0
        max: 20000
        step: 1
        mode: box
```

## I want to add an unsupported inverter to Predbat

- First copy one of the template configurations that is close to your system and try to configure it to match the sensors you have
- Create a GitHub ticket for support and add what you know to the ticket
- Then find out how to control your inverter inside Home Assistant, ideally share any automation you have to control the inverter
- You can create a new inverter type in apps.yaml and change the options as to which controls it has
- Set [inverter_type in apps.yaml](apps-yaml.md#inverter_type) to match the custom inverter definition ('MINE' in the example below)
- The easy way to integrate using Home Assistant services to start charges and discharges, edit the template below:

```yaml
  inverter_type: MINE
  inverter:
    name: "My Shiny new Inverter"
    has_rest_api: False
    has_mqtt_api: False
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

The following options are supported per inverter:

### has_rest_api

When True the REST API will be used to fetch data/control the inverter. This is currently only for GivEnergy inverters with GivTCP and **givtcp_rest** must be set in apps.yaml

### has_mqtt_api

When True the MQTT API to Home Assistant will be used to issue control messages for the inverter

The MQTT/publish service is used with the topic as defined by **mqtt_topic** in apps.yaml

Messages will be sent through these controls:

Values that are updated:

- **topic**/set/reserve  - payload=reserve
- **topic**/set/charge_rate - payload=new_rate
- **topic**/set/discharge_rate - payload=new_rate
- **topic**/set/target_soc - payload=target_soc

These three change between battery charge/discharge and auto (idle) mode:

- **topic**/set/charge - payload=charge_rate
- **topic**/set/discharge - payload=discharge_rate
- **topic**/set/auto - payload=true

### Service API

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
      device_id: "{device_id}"
      power: "{power}"
      soc: "{target_soc}"
      charge_start_time: "{charge_start_time}"
      charge_end_time: "{charge_end_time}"
```

Here you can define all the values passed to the service and use the default values from the template or define your own.

You can also call more than one service e.g:

```yaml
  charge_start_service:
    - service: my_charge_start_service
      device_id: "{device_id}"
      power: "{power}"
      soc: "{target_soc}"
    - service: switch.turn_off
      entity_id: switch.tsunami_charger
```

Note: By default the service will only be called once until things change, e.g. **charge_start_service** will be called once and then won't be called again until **charge_stop_service** stops the charge.
If however, you want the service to be called on each Predbat run then you should set **repeat** to True for the given service e.g:

```yaml
  charge_start_service:
    - service: my_charge_start_service
      device_id: "{device_id}"
      power: "{power}"
      soc: "{target_soc}"
      repeat: True
```

#### charge_start_service

Called to start a charge

The default options passed in are:

- device_id - as defined in apps.yaml by **device_id**
- target_soc - The SOC to charge to
- power - The charge power to use
- charge_start_time - Start time for the charge
- charge_end_time - End time for the charge

#### charge_freeze_service

If defined will be called for freeze charge, otherwise, charge_start_service is used for freeze charge also.

#### discharge_start_service

Called to start a discharge

The default options passed in are:

- device_id - as defined in apps.yaml by **device_id**
- target_soc - The SOC to discharge to
- power - The discharge power to use

#### discharge_freeze_service

If defined will be called for Discharge freeze, otherwise, discharge_start_service is used for freeze discharge also.

#### charge_stop_service

Called to stop a charge

device_id - as defined in apps.yaml by **device_id**

#### discharge_stop_service

Called to stop a discharge, if not set then **charge_stop_service** will be used instead

- device_id - as defined in apps.yaml by **device_id**

### output_charge_control

Set to power, current or none

When power the inverter has a **charge_rate** and **discharge_rate** setting in watts defined in apps.yaml

When current the inverter has  **timed_charge_current** and **timed_discharge_current** setting in amps defined in apps.yaml

### charge_control_immediate

When True the inverter **timed_charge_current** or **timed_discharge_current** is used to control charging or discharging as/when it starts and stops rather than using a timed method.

### current_dp

Sets the number of decimal places when setting the current in Amps, which should be 0 or 1

### has_charge_enable_time

When True the inverter has a setting defined in apps.yaml called **scheduled_charge_enable** when can be used to enable/disable timed charging.

### has_discharge_enable_time

When True the inverter has a setting defined in apps.yaml called **scheduled_discharge_enable** when can be used to enable/disable timed discharging.

### has_target_soc

When True the inverter has a target charge SoC setting in apps.yaml called **charge_limit**.
When False charging must be turned on and off by Predbat rather than the inverter doing it based on the target SoC %.

### has_reserve_soc

When True the inverter has a discharge reserve SoC setting in apps.yaml called **reserve** which is the target % to discharge the battery down to.
When False discharging must be turned on and off by Predbat rather than the inverter doing it based on discharge Soc %.

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

When True the inverter has an idle time register which must be set to the start and end times for ECO mode (GivEnergy EMS)

### can_span_midnight

When True start and end times for charge and discharge can span midnight e.g. 23:00:00 - 01:00:00 is a 2-hour slot.

### charge_discharge_with_rate

When True when charging discharge rate must be 0 and visa-versa. When false the rate does not have to change.
