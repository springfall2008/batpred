# Inverter setup

PredBat was originally written for GivEnergy inverters using the GivTCP integration but has been extended to many other inverter models.

The table below lists the inverters and required Home Assistant integrations that have had Predbat configurations developed.

Follow the [Predbat installation guide](install.md) for full instructions to setup and configure Predbat. This document covers only the steps that are specific to different inverter types.

Additionally, if your inverter type is not listed, you can create a [custom inverter definition for Predbat](#i-want-to-add-an-unsupported-inverter-to-predbat).
Once you get everything working please share the configuration as a github issue so it can be incorporated into the Predbat documentation.

To setup the inverter with Predbat you will need to:

1. Install the appropriate Home Assistant integration for your inverter
2. Configure the integration according to its documentation
3. Confirm that the integration is working.  Are you receiving data from the various sensors (grid energy, charge limit, solar PV generated, etc)?<BR>
Can you control the inverter using its Home Assistant controls?
4. For each inverter there is a custom `apps.yaml` template configuration file that must be used in place of the GivTCP template file installed by default with Predbat:

    - Open the inverter-specific template file with a browser
    - Using a [file editor in Home Assistant](install.md#editing-configuration-files-in-home-assistant), edit the default `apps.yaml` configuration file
    - Select-all in `apps.yaml` and delete the entire template contents
    - Select-all in the inverter-specific template file opened earlier, and copy and paste the contents into the Home Assistant file editor - if
    you copy but don't replace the standard `apps.yaml` template then Predbat will not function correctly.

5. Follow the inverter-specific setup steps detailed below for each inverter (click on the inverter name in the table). Steps vary for each inverter, for some there are no additional steps,
but for other inverters there are additional controls, scripts and automations that have to be created for Predbat to work.
6. Follow the rest of the [Predbat install instructions](install.md), in particular review that `apps.yaml` is configured correctly for your inverter.

   | Name                          | Integration     | Template |
   | :---------------------------- | :------------- | :------------ |
   | [GivEnergy with GivTCP](#givenergy-with-givtcp) | [GivTCP](https://github.com/britkat1980/ha-addons) | [givenergy_givtcp.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_givtcp.yaml) |
   | [Solis Hybrid inverters (Firmware before FB00)](#solis-inverters-before-fb00) | [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus) | [ginlong_solis.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/ginlong_solis.yaml) |
   | [Solis Hybrid inverters (Firmware FB00 and later)](#solis-inverters-fb00-or-later) | [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus) | [ginlong_solis.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/ginlong_solis.yaml) |
   | [Solax Gen4 inverters](#solax-gen4-inverters) | [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus)<BR>in Modbus Power Control Mode |  [solax_sx4.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solax_sx4.yaml) |
   | [Sofar inverters](#sofar-inverters) | [Sofar MQTT integration](https://github.com/cmcgerty/Sofar2mqtt) |  [sofar.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar.yaml) |
   | [Huawei inverters](#huawei-inverters) | [Huawei Solar](https://github.com/wlcrs/huawei_solar) | [huawei.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/huawei.yaml) |
   | [SolarEdge inverters](#solaredge-inverters) | [Solaredge Modbus Multi](https://github.com/WillCodeForCats/solaredge-modbus-multi) | [solaredge.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solaredge.yaml) |
   | [Givenergy with GE Cloud](#givenergy-with-ge_cloud) | [ge_cloud](https://github.com/springfall2008/ge_cloud) | [givenergy_cloud.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_cloud.yaml) |
   | [Givenergy with GE Cloud EMS](#givenergy-with-ems) | [ge_cloud EMS](https://github.com/springfall2008/ge_cloud) | [givenergy_ems.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/givenergy_ems.yaml) |
   | [Givenergy/Octopus No Home Assistant](#givenergyoctopus-cloud-direct---no-home-assistant) | n/a | [ge_cloud_octopus_standalone.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/ge_cloud_octopus_standalone.yaml) |
   | [SunSynk](#sunsynk) | [Sunsynk](https://github.com/kellerza/sunsynk) | [sunsynk.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sunsynk.yaml) |
   | [Fox](#fox) | [Foxess](https://github.com/nathanmarlor/foxess_modbus) | [fox.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/fox.yaml) |
   | [Fox Cloud](#fox-cloud) | Predbat | [fox_cloud.yaml](https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/templates/fox_cloud.yaml) |
   | [LuxPower](#lux-power) | [LuxPython](https://github.com/guybw/LuxPython_DEV) | [luxpower.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/luxpower.yaml) |
   | [Growatt with Solar Assistant](#growatt-with-solar-assistant) | [Solar Assistant](https://solar-assistant.io/help/home-assistant/setup) | [spa.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solar_assistant_growatt_spa.yaml) or [sph.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/solar_assistant_growatt_sph.yaml)|
   | [SigEnergy](#sigenergy-sigenstor) | [SigEnergy](https://github.com/TypQxQ/Sigenergy-Home-Assistant-Integration) | [sigenergy_sigenstor.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sigenergy_sigenstor.yaml)|
   | [Tesla Powerwall](#tesla-powerwall) | [Tesla Fleet](https://www.home-assistant.io/integrations/tesla_fleet) or [Teslemetry](https://www.home-assistant.io/integrations/teslemetry) | [tesla_powerwall.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/tesla_powerwall.yaml) |
   | [Victron](#victron) | [Victron MQTT](https://github.com/tomer-w/victron_mqtt) | [victron.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/victron.yaml) |

Note that support for all these inverters is in various stages of development. Please expect things to fail and report them as Issues on GitHub.

## GivEnergy with GivTCP

It's recommended that you first watch the [Installing GivTCP and Mosquitto Add-on's video from Speak to the Geek](https://www.youtube.com/watch?v=d06Mqeplvns).

1. Install Mosquitto Broker add-on:

- Go to Settings / Add-ons / Add-on Store (bottom right)
- Scroll down the add-on store list, to find 'Mosquitto broker', click on the add-on, then click 'INSTALL'
- Once the Mosquitto broker has been installed, ensure that the 'Start on boot' and 'Watchdog' options are turned on, and click 'START' to start the add-on
- Next, configure Mosquitto broker by going to Settings / Devices and Services / Integrations.
Mosquitto broker should appear as a Discovered integration so click the blue 'CONFIGURE' button, then SUBMIT to complete configuring Mosquitto broker

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

- All configuration for GivTCP is done via the add-on's Web interface
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
- Click Next and Next to get to the Selfrun page, and turn on Self Run so that GivTCP automatically retrieves data from your inverter. The Self Run Loop Timer is how often GivTCP will retrieve data - it's
recommended that set this to a value between 20 and 60, but not less than 15 seconds as otherwise the inverter will then spend all its time talking to GivTCP
and won't communicate with the GivEnergy portal and app
- GivTCP auto-populates the MQTT page so as long as you're using Mosquitto broker within Home Assistant;
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

- If you are using GivTCP v3 and have an AIO or 3-phase inverter then you will need to manually set [geserial in apps.yaml](apps-yaml.md#geserial) to your inverter serial number in lower case as the auto-detect doesn't work for this setup
- If you have a single AIO then control is directly to the AIO. Ensure [geserial in apps.yaml](apps-yaml.md#geserial) is correctly picking the AIO and comment out geserial2 lines
- If you have multiple AIOs then all control of the AIOs is done through the Gateway so [geserial in apps.yaml](apps-yaml.md#geserial) should be set to the Gateway serial number in lower case
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
This is being worked on by the author of GivTCP, e.g. see [GivTCP issue: unable to charge or discharge 3 phase inverters with Predbat](https://github.com/britkat1980/giv_tcp/issues/218)

## Solis Inverters before FB00

To run PredBat with Solis hybrid inverters with firmware level prior to FB00 (you can recognise these by having fewer than 6 slots for charging times), follow the following steps:

1. Install PredBat as per the [Installation Summary](installation-summary.md)
2. Ensure that you have the Solax Modbus integration running and select the inverter type solis.
There are a number of entities which this integration disables by default that you will need to enable via the Home Assistant GUI:

   | Name                          | Description     |
   | :---------------------------- | :-------------- |
   | `sensor.solis_rtc`           | Real Time Clock |
   | `sensor.solis_battery_power` | Battery Power   |

3. Copy the template <https://github.com/springfall2008/batpred/blob/main/templates/gilong_solis.yaml> over the top of your `apps.yaml`, and modify it for your system
4. Set **solax_modbus_new** in `apps.yaml` to True if you have integration version 2024.03.2 or greater
5. Ensure that the inverter is set to Control Mode 35 - on the Solax integration this is `Timed Charge/Discharge`.
If you want to use the `Reserve` functionality within PredBat you will need to select `Backup/Reserve` (code 51) instead but be aware that this is not fully tested.
In due course, these mode settings will be incorporated into the code.
6. Your inverter will require a "button press" triggered by Predbat to update the schedules. Some Solis inverter integrations feature a combined charge/discharge update button, in which case a single `apps.yaml` entry of:

```yaml
  charge_discharge_update_button:
    - button.solis_update_charge_discharge_times
```

7. Ensure the correct entity IDs are used for your specific inverter setup. These entries should correspond to the buttons exposed by your Home Assistant Solis integration.

## Solis Inverters FB00 or later

To run PredBat with Solis hybrid inverters with firmware level FB00 or later (you can recognise these by having 6 slots for charging times), follow the following steps:

1. Install PredBat as per the [Installation Summary](installation-summary.md)
2. Ensure that you have the Solax Modbus integration running and select the inverter type solis_fb00.
There are a number of entities which this integration disables by default that you will need to enable via the Home Assistant GUI:

   | Name                          | Description     |
   | :---------------------------- | :-------------- |
   | `sensor.solisx_rtc`           | Real Time Clock |
   | `sensor.solisx_battery_power` | Battery Power   |

3. Copy the template <https://github.com/springfall2008/batpred/blob/main/templates/gilong_solis.yaml> over the top of your `apps.yaml`, and modify it for your system.
You will need to update these lines:

- Replace **inverter_type: "GS"** with **inverter_type: "GS_fb00"** to enable the inverter template for the newer firmware version of Solis inverters
- Un-comment **charge_update_button** and **discharge_update_button** and comment out **charge_discharge_update_button** to enable the two "button presses" needed for writing charge/discharge times to the inverter
- Un-comment **scheduled_charge_enable** and **scheduled_discharge_enable** to enable Predbat to enable/disable the charge/discharge slots
- Un-comment **charge_limit** to enable the charge limit through setting an upper SoC value
- Set **solax_modbus_new** to True if you have integration version 2024.03.2 or greater
- Lastly you will need to comment out or delete the **template** line to enable the configuration

4. Save the file as `apps.yaml` to the appropriate [Predbat software directory](apps-yaml.md#appsyaml-settings).

5. Ensure that the inverter is set to Control Mode 35 - on the Solax integration this is `Timed Charge/Discharge`.
If you want to use the `Reserve` functionality within PredBat you will need to select `Backup/Reserve` (code 51) instead but be aware that this is not fully tested.
In due course, these mode settings will be incorporated into the code.

6. Note: Predbat will read the minimum SoC level set on the inverter via **sensor.solis_battery_minimum_soc** configured in `apps.yaml`.
You must set the minimum SoC level that Predbat will set in **input_number.predbat_set_reserve_min** to at least 1% more than the inverter minimum SoC.<BR>
So for example, if the inverter minimum SoC is set to 20%, predbat_set_reserve_min must be set to at least 21%. If this is not done then when Predbat sets the reserve SoC, the instruction will be rejected by the inverter and Predbat will error.

7. Ensure the correct entity IDs are used for your specific inverter setup. These entries should correspond to the buttons exposed by your Home Assistant Solis integration.

## Solax Gen4+ Inverters

The Predbat Solax configuration can either either use the Mode1 remote control or the newer Mode8 option. Both should work with the SolaX Gen 4, 5 or 6 inverters.  Thanks @TCWORLD for this configuration.

- Please copy the template <https://github.com/springfall2008/batpred/blob/main/templates/solax_sx4.yaml> over the top of your `apps.yaml`, and modify it for your system and the work mode that your inverter is set to
- Install and configure the Solax Modbus integration in Home Assistant and confirm that it is connected to your inverter
- The regular expressions in the custom SX4+ `apps.yaml` should auto-match to the entity names provided by your Solax Modbus integration, but do double-check that they do
- To use Mode 1 remote control, create and save the following automation script (Settings/Automations/Scripts) which will act as the interface between Predbat and the Solax Modbus integration.<BR>
You can change the limits for the power field if you have a larger inverter, it doesn't matter if this limit is larger than the inverter can handle as the value gets clipped to the inverter limits by the Solax Modbus integration.<BR>
You may need to amend the 'solax_' prefixes on the entity names that this script sets if your Modbus integration has slightly different entity names (e.g. 'solaxmodbus_' or 'solax_inverter_'):

```yaml
alias: SolaX Remote Control
description: ""
fields:
  power:
    selector:
      number:
        min: 0
        max: 6600
    default: 0
  operation:
    selector:
      select:
        multiple: false
        options:
          - Disabled
          - Force Charge
          - Force Discharge
          - Freeze Charge
          - Freeze Discharge
    default: Disabled
    required: false
  duration:
    selector:
      number:
        min: 300
        max: 86400
    default: 28800
    required: false
sequence:
  - variables:
      defaultPower: "{{ 200 }}"
      mode: |-
        {% set map = {
           'Disabled': 'Disabled',
           'Force Charge': 'Enabled Battery Control',
           'Force Discharge': 'Enabled Battery Control',
           'Freeze Charge': 'Enabled No Discharge',
           'Freeze Discharge': 'Enabled Feedin Priority'} %}
        {{ map.get( operation, 'Disabled' ) }}
      activeP: >-
        {% set chargePower = (power | int(defaultPower)) if power is defined else
        defaultPower %}

        {% set dischargePower = (0 - chargePower) %}

        {% set map = {
           'Disabled': 0,
           'Force Charge': chargePower,
           'Force Discharge': dischargePower,
           'Freeze Charge': 0,
           'Freeze Discharge': 0} %}
        {{ map.get( operation, 0 ) }}
  - action: number.set_value
    data:
      value: "{{ activeP }}"
    target:
      entity_id: number.solax_remotecontrol_active_power
  - action: number.set_value
    data:
      value: "60"
    target:
      entity_id: number.solax_remotecontrol_duration
  - action: number.set_value
    data:
      value: "{{ duration if duration is defined else 28800 }}"
    target:
      entity_id: number.solax_remotecontrol_autorepeat_duration
  - action: select.select_option
    data:
      option: "{{ mode if mode is defined else Disabled }}"
    target:
      entity_id: select.solax_remotecontrol_power_control
  - action: button.press
    data: {}
    target:
      entity_id: button.solax_remotecontrol_trigger
mode: queued
max: 10
```

- To use Mode 1 remote control, ensure the following entities are enabled:

    - number.solax_remotecontrol_active_power
    - number.solax_remotecontrol_duration
    - number.solax_remotecontrol_autorepeat_duration
    - select.solax_remotecontrol_power_control
    - button.solax_remotecontrol_trigger

- To use Mode 8 power control API (Gen 4 or newer inverter) which has direct control over the battery charge/discharge rate, and can directly set the battery (dis)charge rate without limiting any PV generation,
create and save the following automation script (Settings/Automations/Scripts) which will act as the interface between Predbat and the Solax Modbus integration.<BR>
In the script, change 'maxPvPower: "{{ 12000 }}"' to a value larger than your PV array size so the script doesn't limit PV generation.<BR>
Change 'max: 6600' - to a value larger than the maximum charge/discharge power for your battery (doesn't matter if higher).<BR>
Note: Mode8 requires version 2025.10.7 or newer of the SolaX Modbus integration as there are some necessary Mode 8 improvements added:

```yaml
alias: SolaX Remote Control (Mode 8)
description: ""
fields:
  power:
    selector:
      number:
        min: 0
        max: 6600
    default: 0
  operation:
    selector:
      select:
        multiple: false
        options:
          - Disabled
          - Force Charge
          - Force Discharge
          - Freeze Charge
          - Freeze Discharge
    default: Disabled
    required: false
  duration:
    selector:
      number:
        min: 60
        max: 86400
    default: 28800
sequence:
  - variables:
      maxPvPower: "{{ 12000 }}"
      defaultPower: "{{ 200 }}"
      mode: |-
        {% set map = {
           'Disabled': 'Disabled',
           'Force Charge': 'Mode 8 - PV and BAT control - Duration',
           'Force Discharge': 'Mode 8 - PV and BAT control - Duration',
           'Freeze Charge': 'Enabled No Discharge',
           'Freeze Discharge': 'Export-First Battery Limit'} %}
        {{ map.get( operation, 'Disabled' ) }}
      activeP: >-
        {% set dischargePower = (power | int(defaultPower)) if power is defined
        else defaultPower %} {% set chargePower = (0 - dischargePower) %} {% set map
        = {
           'Disabled': 0,
           'Force Charge': chargePower,
           'Force Discharge': dischargePower,
           'Freeze Charge': 0,
           'Freeze Discharge': 0} %}
        {{ map.get( operation, 0 ) }}
  - action: number.set_value
    data:
      value: "{{ activeP }}"
    target:
      entity_id: number.solax_remotecontrol_push_mode_power_8_9
  - action: number.set_value
    data:
      value: "{{ maxPvPower }}"
    target:
      entity_id: number.solax_remotecontrol_pv_power_limit
  - action: number.set_value
    data:
      value: "30"
    target:
      entity_id: number.solax_remotecontrol_duration
  - action: number.set_value
    data:
      value: "300"
    target:
      entity_id: number.solax_remotecontrol_timeout
  - action: number.set_value
    data:
      value: "{{ duration if duration is defined else 28800 }}"
    target:
      entity_id: number.solax_remotecontrol_autorepeat_duration
  - action: select.select_option
    data:
      option: VPP Off
    target:
      entity_id: select.solax_inverter_remotecontrol_timeout_next_motion_mode_1_9
  - action: select.select_option
    data:
      option: "{{ mode if mode is defined else Disabled }}"
    target:
      entity_id: select.solax_remotecontrol_power_control_mode
  - action: button.press
    data: {}
    enabled: true
    target:
      entity_id: button.solax_powercontrolmode8_trigger
mode: queued
max: 10
```

- To use Mode 8 power control, ensure the following entities are enabled:

    - number.solax_remotecontrol_push_mode_power_8_9
    - number.solax_remotecontrol_pv_power_limit
    - number.solax_remotecontrol_duration
    - number.solax_remotecontrol_timeout
    - number.solax_remotecontrol_autorepeat_duration
    - select.solax_inverter_remotecontrol_timeout_next_motion_mode_1_9
    - select.solax_remotecontrol_power_control_mode
    - button.solax_powercontrolmode8_trigger

- Predbat needs a 'Todays House Load' sensor, this can be created from inverter-supplied information by creating two custom helper entities:
    - Create a helper entity of type 'Integral', set the Name to 'Todays House Load Integral', Metric Prefix to 'k (kilo)', Time unit to 'Hours', Input sensor to 'House Load', Integration method to 'Trapezoidal', Precision to '2'
    and Max sub-interval to '0:05:00'
    - Create a helper entity of type 'Utility Meter', set the Name to 'Todays House Load', Input sensor to 'Todays House Load Integral' (that you just created) and Meter Reset Cycle to 'Daily'

- When you first start Predbat, check the [Predbat log](output-data.md#predbat-logfile) to confirm that the correct sensor names are identified by the regular expressions in `apps.yaml`. Any non-matching expressions should be investigated and resolved

Please see this ticket in Github for ongoing discussion: <https://github.com/springfall2008/batpred/issues/259>

## Sofar Inverters

For this integration, the key elements are:

- Hardware - [sofar2mqtt EPS board](https://www.instructables.com/Sofar2mqtt-Remote-Control-for-Sofar-Solar-Inverter/) - Relatively easy to solder and flash, or can be bought pre-made.
- Software - [Sofar MQTT integration](https://github.com/cmcgerty/Sofar2mqtt) - MQTT integration
- Home Assistant configuration - [sofar_inverter.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar_inverter.yaml) (in templates directory),
defines the custom HA entities and should be added to HA's `configuration.yaml`. This is the default Sofar HA configuration with a couple of additional inputs to support battery capacity.
- Predbat configuration - [sofar.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sofar.yaml) template for Predbat (in templates directory). This file should be copied over the top of your `apps.yaml` and edited for your installation

- Please note that the inverter needs to be put into "Passive Mode" for the sofar2mqtt to control the inverter.
- This integration has various limitations, it can charge and discharge the battery but does not have finer control over reserve and target SoC%
- Note: You will need to change the min reserve in Home Assistant to match your minimum battery level (**input_number.predbat_set_reserve_min**).

Please see this ticket in Github for ongoing discussions: <https://github.com/springfall2008/batpred/issues/395>

## Huawei Inverters

The discussion ticket is here: <https://github.com/springfall2008/batpred/issues/684>

- Please copy the template <https://github.com/springfall2008/batpred/blob/main/templates/huawei.yaml> over the top of your `apps.yaml`, and modify it for your system
- Ensure you set **input_number.predbat_set_reserve_min** to the minimum value for your system which may be 12%

- Huawei inverters can charge the battery from DC solar and discharge at one power level (e.g. 5kWh), but have a lower limit (e.g. 3kWh) for AC charging.
At present Predbat doesn't have the ability to model separate DC and AC charging limits,
so battery_rate_max is set to the lower limit in watts (e.g. 3000) in the template `apps.yaml` to ensure that Predbat correctly plans AC charging of the battery at the right rate.

- However this means Predbat will also limit DC solar charging to this lower limit and to avoid that an automation is used to overwrite the **inverter_limit_charge** during the hours of sunrise and sunset:

```yaml
alias: Predbat change inverter charge rate at sunrise and sunset
description: Using predbat_manual_api
triggers:
  - trigger: time
    at:
      entity_id: sensor.sun_next_rising
    id: sunrise
  - trigger: time
    at:
      entity_id: sensor.sun_next_setting
    id: sunset
conditions: []
actions:
  - choose:
      - conditions:
          - condition: trigger
            alias: Sunrise
            id:
              - sunrise
        sequence:
          - action: select.select_option
            alias: set inverter charge rate to 5000W at sunrise for maximum DC solar charging
            target:
              entity_id:
                - select.predbat_manual_api
            data:
              option: inverter_limit_charge(0)=5000
      - conditions:
          - condition: trigger
            alias: Sunset
            id:
              - sunset
        sequence:
          - action: select.select_option
            alias: set inverter charge rate to 1500W at sunset for reduced AC charging rate
            target:
              entity_id:
                - select.predbat_manual_api
            data:
              option: inverter_limit_charge(0)=3000
mode: single
```

- Set the Huawei inverter work mode to 'TOU' (Time Of Use).

## SolarEdge Inverters

- Please copy the template <https://github.com/springfall2008/batpred/blob/main/templates/solaredge.yaml> over the top of your `apps.yaml` and modify it for your system
- The default entity name prefix for the integration is 'solaredge' but if you have changed this on installation then you will need to amend the `apps.yaml` template and the template sensors to match your new prefix
- Ensure that **number.solaredge_i1_storage_command_timeout** is set to a reasonably high value e.g. 3600 seconds to avoid the commands issued being cancelled
- Power Control Options, as well as Enable Battery Control, must be enabled in the Solaredge Modbus Multi integration configuration,
and **switch.solaredge_i1_advanced_power_control** must be on.

- For **pv_today**, **pv_power** and **load_power** sensors to work you need to create these as a template entities within your Home Assistant `configuration.yaml`.
These sensors are not critical so you can just comment them out in `apps.yaml` if you can't get them to work:

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

- And add the following additional template sensors to `configuration.yaml` after the existing 'template:' line (from the earlier template sensor definitions):

```yaml
  - sensor:
    # Template sensor for Max Battery Charge rate
    # This is the sum of all three batteries charge rate as the max charge rate can be higher than inverter capacity (e.g. 8k) when charging from AC+Solar
    # Returns 5000W as the minimum max value, the single battery charge/discharge limit to ensure at least one battery can always be charged if one or more batteries have 'gone offline' to modbus
    # Remove all 'B3' entries if you only have two batteries, or follow the same pattern for adding 'B4', etc if you have more than 3 batteries
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
- Now copy the template `givenergy_cloud.yaml` from templates over the top of your `apps.yaml` and edit
    - Set geserial to your inverter serial number
- Make sure that the 'discharge down to' registers are set to 4% and slots 2, 3 and 4 for charge and discharge are disabled in the portal (if you have them)

## GivEnergy with EMS

- First set up ge_cloud integration using your API key <https://github.com/springfall2008/ge_cloud>
- Now copy the template `givenergy_ems.yaml` from templates over the top of your `apps.yaml` and edit
    - Set geserial to your first inverter serial and geserial2 to the second (look in HA for entity names)
    - Set geseriale to the EMS inverter serial number (look in HA for the entity names)
- Turn off charge, export and discharge slots 2, 3 and 4 as Predbat will only use slot 1 - set the start and end times for these to 00:00

## GivEnergy/Octopus Cloud Direct - No Home Assistant

- Take the template and enter your GivEnergy API key directly into `apps.yaml`
- Set your Octopus API key in `apps.yaml`
- Set your Solcast API key in `apps.yaml`
- Review any other configuration settings

Launch Predbat with hass.py (from the Predbat-addon repository) either via a Docker or just on a Linux/MAC/WSL command line shell.

## Fox

**Experimental**

- I've managed to get Batpred working on my Fox ESS inverter, connected via an Elfin EW11 modbus and using Nathan's Fox ESS Modbus tool.
See: <https://github.com/springfall2008/batpred/issues/1401>

The template is in the templates area, give it a try

## Fox Cloud

**Experimental**

- Predbat now has a built-in Fox cloud integration. Today it requires a battery that supports the scheduler mode to function.

Try the template for auto-integration.

## Lux Power

This requires the LuxPython component which integrates with your Lux Power inverter

- Copy the template `luxpower.yaml` from templates over the top of your `apps.yaml`, and edit inverter and battery settings as required
- LuxPower does not have a SoC max entity in kWh and the SoC percentage entity never reports the battery reaching 100%, so create the following template helper sensors:

```yaml
name: Lux SoC Max kWh
template:
  {{ (states("sensor.lux_battery_capacity_ah") |float) *
     (states("sensor.lux_battery_voltage_live") | float) / 1000}}
unit of measurement: kWh
device class: Energy
state class: Total
```

```yaml
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

- Thanks to the work of @brickatius, the following set of automations and configurations have been devised to enable LuxPower inverters to deliver Freeze Charge functionality in Predbat:

    - create the following helper input boolean and template binary sensor:

      ```yaml
      input_boolean.freeze_charge_guard

      name: binary_sensor.solar_compare_home
      template:
        {{ 'on' if states('sensor.lux_solar_output_live') | float(0)
              <= states('sensor.lux_home_consumption_live') | float(0)
          else 'off' }}
      ```

    - Create the following freeze charge automation that is enabled when Predbat starts Freeze Charge mode, and is disabled when Freeze Charge is stopped.  Under other Predbat modes the automation is disabled - this is normal behaviour!<BR>
    The automation writes progress status entries to the Home Assistant log so you can see what it is doing. These system_log.write actions can be removed if wanted, they are only there to aid debugging.

      ```yaml
      alias: LuxPower freeze charge
      description: Full AC charge control with guard, SoC update, and debug logging.
      triggers:
        - entity_id: binary_sensor.solar_compare_home
          to:
            - "on"
            - "off"
          trigger: state
        - entity_id: switch.lux_ac_charge_enable
          from: "off"
          to: "on"
          trigger: state
        - entity_id: automation.luxpower_freeze_charge
          from: "off"
          to: "on"
          trigger: state
      conditions: []
      actions:
        - data:
            level: debug
            message: >
              LuxPowerFreezeCharge TRIGGERED: trigger={{ trigger.entity_id if trigger
              is defined else 'unknown' }}, from={{ trigger.from_state.state if
              trigger.from_state is defined else 'unknown' }}, to={{
              trigger.to_state.state if trigger.to_state is defined else 'unknown' }},
              sensor={{ states('binary_sensor.solar_compare_home') }}, ac={{
              states('switch.lux_ac_charge_enable') }}, guard={{
              states('input_boolean.freeze_charge_guard') }}
          action: system_log.write
        - choose:
            - conditions:
                - condition: template
                  value_template: |
                    {{ trigger.to_state is defined
                      and trigger.from_state.state == 'off'
                      and trigger.to_state.state == 'on' }}
              sequence:
                - target:
                    entity_id: number.lux_ac_battery_charge_level
                  data:
                    value: "{{ states('sensor.lux_battery_soc_corrected') | float }}"
                  action: number.set_value
                - target:
                    entity_id: input_boolean.freeze_charge_guard
                  action: input_boolean.turn_on
                - data:
                    level: debug
                    message: >
                      LuxPowerFreezeCharge: Automation ON → SoC set to {{
                      states('sensor.lux_battery_soc_corrected') }} and guard ON
                  action: system_log.write
        - choose:
            - conditions:
                - condition: template
                  value_template: |
                    {{ not (trigger.to_state is defined
                            and trigger.from_state.state == 'off'
                            and trigger.to_state.state == 'on')
                      and (states('sensor.lux_battery_soc_corrected') | float !=
                            states('number.lux_ac_battery_charge_level') | float) }}
              sequence:
                - target:
                    entity_id: number.lux_ac_battery_charge_level
                  data:
                    value: "{{ states('sensor.lux_battery_soc_corrected') | float }}"
                  action: number.set_value
                - data:
                    level: debug
                    message: >
                      LuxPowerFreezeCharge: SoC updated to {{
                      states('sensor.lux_battery_soc_corrected') }} due to value
                      difference
                  action: system_log.write
        - condition: state
          entity_id: input_boolean.freeze_charge_guard
          state: "on"
        - choose:
            - conditions:
                - condition: state
                  entity_id: binary_sensor.solar_compare_home
                  state: "on"
                - condition: state
                  entity_id: switch.lux_ac_charge_enable
                  state: "off"
              sequence:
                - target:
                    entity_id: switch.lux_ac_charge_enable
                  action: switch.turn_on
                - data:
                    level: debug
                    message: "LuxPowerFreezeCharge: AC turned ON (sensor ON)"
                  action: system_log.write
            - conditions:
                - condition: state
                  entity_id: binary_sensor.solar_compare_home
                  state: "off"
                - condition: state
                  entity_id: switch.lux_ac_charge_enable
                  state: "on"
              sequence:
                - target:
                    entity_id: switch.lux_ac_charge_enable
                  action: switch.turn_off
                - data:
                    level: debug
                    message: "LuxPowerFreezeCharge: AC turned OFF (sensor OFF)"
                  action: system_log.write
        - choose:
            - conditions:
                - condition: template
                  value_template: |
                    {{ states('predbat.status') in ['Charging','Hold charging'] }}
              sequence:
                - target:
                    entity_id: automation.luxpower_freeze_charge
                  action: automation.turn_off
                - data:
                    level: debug
                    message: >
                      LuxPowerFreezeCharge: Predbat status {{ states('predbat.status')
                      }} → automation OFF, guard cleared
                  action: system_log.write
      mode: single
      ```

    - Create the following automation to reset the input_boolean when the freeze charge automation is turned off by Predbat stopping Freeze Charge mode:

      ```yaml
      alias: Freeze Charge – Guard Reset
      description: "Turn off the input boolean guard whenever the LuxPower freeze charge automation is turned off."
      triggers:
        - entity_id: automation.luxpower_freeze_charge
          to: "off"
          trigger: state
      actions:
        - target:
            entity_id: input_boolean.freeze_charge_guard
          action: input_boolean.turn_off
        - data:
            level: debug
            message: >-
              FreezeCharge: Guard cleared because automation was turned OFF
              externally.
          action: system_log.write
      mode: single
      ```

    - Check that the Predbat configuration switch **switch.predbat_set_charge_freeze** is turned On.

- If you have a LuxPower inverter with the 'Charge Last' feature you should enable the Predbat discharge freeze service. Enabling this will ensure you get the most out of Predbat. In your `apps.yaml` file:

    - change the 'support_discharge_freeze' line in the Inverter section from 'False' to 'True'
    - uncomment the following two lines in the 'discharge_stop_service' section so that Predbat turns switch.lux_charge_last off when it stops discharge from your inverter
    - uncomment the next three lines, so adding a new 'discharge_freeze_service'
    - make sure the indentation and alignment of these new lines is consistent with the other service entries

  Check that the Predbat configuration switch **switch.predbat_set_export_freeze** is turned On.

  After the Predbat Plan has recalculated you may notice some 'FrzExp' in the state column next to some slots.

## Growatt with Solar Assistant

You need to have a Solar Assistant installation <https://solar-assistant.io>

Growatt has two popular series of inverters, SPA and SPH. Copy the template that matches your model from templates over the top of your `apps.yaml`, and edit inverter and battery settings as required. Yours may have different entity IDs on Home Assistant.

## Sunsynk

- Copy the Sunsynk template over the top of your `apps.yaml`, and edit for your system.
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
description: Copy Battery SoC to all timezone (time) slots
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

- Create the following templates sensors in your `configuration.yaml`:

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

- make sure the inverter is already integrated into Home Assistant. Here is a ([repo](https://github.com/TypQxQ/Sigenergy-Local-Modbus)) with full integration (this is the Python version of the Sigenergy Home Assistant integration).
- Copy the template [sigenergy_sigenstor.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/sigenergy_sigenstor.yaml) template over your `apps.yaml`, and edit for your system.

- All the Sigenergy entities referenced in `apps.yaml` need to be enabled for Predbat to use them. The following are disabled by default and will need enabling:

    - sensor.sigen_plant_available_max_discharging_capacity
    - sensor.sigen_plant_daily_consumed_energy
    - number.sigen_plant_ess_backup_state_of_charge
    - number.sigen_plant_ess_charge_cut_off_state_of_charge
    - number.sigen_plant_ess_discharge_cut_off_state_of_charge
    - sensor.sigen_plant_max_active_power

- The following additions are needed to facilitate integration with Predbat and need to be put into Home Assistant's `configuration.yaml` or configured via the HA user interface:

```yaml
input_select:
  predbat_requested_mode:
    name: "Predbat Requested Mode"
    options:
      - "Demand"
      - "Charging"
      - "Freeze Charging"
      - "Discharging"
      - "Freeze Discharging"
    initial: "Demand"
    icon: mdi:battery-unknown

input_number:
  charge_rate:
    name: Battery charge rate
    initial: 6950
    min: 0
    max: 20000
    step: 1
    mode: box
    unit_of_measurement: W

  discharge_rate:
    name: Battery discharge rate
    initial: 8000
    min: 0
    max: 20000
    step: 1
    mode: box
    unit_of_measurement: W
```

Add the following automations to `automations.yaml` (or configure via the UI):

```yaml
- id: predbat_requested_mode_action
  alias: "Predbat Requested Mode Action"
  description: "Acts as a mapper for the input_select.predbat_requested_mode to the select.sigen_plant_remote_ems_control_mode"
  mode: restart
  triggers:
    - trigger: state
      entity_id:
        - input_select.predbat_requested_mode
  conditions: []
  actions:
    - action: select.select_option
      metadata: {}
      target:
        entity_id: select.sigen_plant_remote_ems_control_mode
      data:
        option: >
          {% if is_state('input_select.predbat_requested_mode', "Demand") %}Maximum Self Consumption
          {% elif is_state('input_select.predbat_requested_mode', "Charging") %}Command Charging (PV First)
          {% elif is_state('input_select.predbat_requested_mode', "Freeze Charging") %}Maximum Self Consumption
          {% elif is_state('input_select.predbat_requested_mode', "Discharging") %}Command Discharging (PV First)
          {% elif is_state('input_select.predbat_requested_mode', "Freeze Discharging") %}Maximum Self Consumption
          {% endif %}

    - choose:
        # Freeze Charging
        # Docs:
        #  Freeze charging - The battery is charging but the current battery level (SoC) is frozen (held). Think of it
        #  as a charge to the current battery level. The grid or solar covers any house load. If there is a shortfall of
        #  Solar power to meet house load, the excess house load is met from grid import, but if there is excess Solar
        #  power above the house load, the excess solar will be used to charge the battery
        # In Sigenergy, this is effectively "self consumption" mode with discharging prohibited
        - conditions:
            - condition: state
              entity_id: input_select.predbat_requested_mode
              state: "Freeze Charging"
          sequence:
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_ess_charge_cut_off_state_of_charge
                value: 100
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_ess_discharge_cut_off_state_of_charge
                value: 100
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_grid_import_limitation
                value: 0

        # Freeze Discharging
        # Docs:
        #  Freeze exporting (mapped to Freeze Discharging in sigenergy_sigenstor.yaml) - The battery is in demand mode,
        #  but with charging disabled. The battery or solar covers the house load. As charging is disabled, if there is
        #  excess solar generated, the current SoC level will be held and the excess solar will be exported. If there is
        #  a shortfall of generated solar power to meet the house load, the battery will discharge to meet the extra load.
        # In Sigenergy, this is effectively "self consumption" mode with charging prohibited
        - conditions:
            - condition: state
              entity_id: input_select.predbat_requested_mode
              state: "Freeze Discharging"
          sequence:
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_ess_charge_cut_off_state_of_charge
                value: 0
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_ess_discharge_cut_off_state_of_charge
                value: 0
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_grid_import_limitation
                value: 0

        # If neither of the above conditions are met, set the limits to the input numbers
        - conditions:
          - condition: not
            conditions:
              - condition: state
                entity_id: input_select.predbat_requested_mode
                state: "Freeze Charging"
              - condition: state
                entity_id: input_select.predbat_requested_mode
                state: "Freeze Discharging"
          sequence:
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_ess_charge_cut_off_state_of_charge
                value: 100
            - action: number.set_value
              data_template:  
                entity_id: number.sigen_plant_ess_discharge_cut_off_state_of_charge  
                value: "{{ states('input_number.predbat_set_reserve_min') | float(10) }}"  
            - action: number.set_value
              data_template:
                entity_id: number.sigen_plant_grid_import_limitation
                value: 100

- id: "automation_sigen_ess_max_charging_limit_input_number_action"
  alias: "Predbat max charging limit action"
  description: "Mapper from input_number.charge_rate to number sigen_plant_ess_max_charging_limit"
  triggers:
    - trigger: state
      entity_id: input_number.charge_rate
  action:
    - action: number.set_value
      target:
        entity_id: number.sigen_plant_ess_max_charging_limit
      data:
        value: >-
          "{{ [(states('input_number.charge_rate') | float / 1000) | round(2),
          states('sensor.sigen_inverter_ess_rated_charging_power') | float] | min}}"
        value: "{{ (states('input_number.charge_rate')| float / 1000) | round(2) }}"
  mode: single

- id: "automation_sigen_ess_max_discharging_limit_input_number_action"
  alias: "Predbat max discharging limit action"
  description: "Mapper from input_number.discharge_rate to number.sigen_plant_ess_max_discharging_limit"
  triggers:
    - trigger: state
      entity_id: input_number.discharge_rate
  action:
    - action: number.set_value
      target:
        entity_id: number.sigen_plant_ess_max_discharging_limit
      data:
        value: >-
          "{{ [(states('input_number.discharge_rate') | float / 1000) | round(2),
          states('sensor.sigen_inverter_ess_rated_discharging_power') | float] | min}}"
  mode: single
```

## Tesla Powerwall

Integration of the Tesla Powerwall follows the approach outlined in [Ed Hull's blog](https://edhull.co.uk/blog/2025-08-24/predbat-docker-tesla).
Ed's setup only covered Predbat controlling charging the Powerwall, the below configuration (thanks @Slee2112) covers both charging and discharging (exporting).

*Note:* This Predbat Tesla configuration has been developed with a Powerwall 3. It may require changes for older Powerwall models. Please raise a Github issue with details of any changes you find are required so the documentation can be updated.

- The Predbat Tesla `apps.yaml` configuration was developed using the Tesla Fleet integration, and you can use this, or you can use the Teslemetry integration which provides easier access to Tesla API's, but requires a [Teslemetry subscription](https://teslemetry.com/)
- Install and configure either the Tesla Fleet integration or Teslemetry integration in Home Assistant
- Copy the template [tesla_powerwall.yaml](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/tesla_powerwall.yaml) template over the top of your `apps.yaml`, and edit for your system

Exporting with Powerwall is tricky, as there is no built in button as such to do it, you have to trick the Powerwall to export by changing the tariff options using the Tesla API.

In order to do this you firstly need to create an API refresh token.

- Create 8 input_text helpers to hold the Tesla access and refresh security tokens and your Tesla site id. These can be created via the HA UI, or added to `configuration.yaml`:

```yaml
input_text:
  tesla_refresh_token_part1:
    name: "Tesla Refresh Token - Part 1"
    max: 255
    mode: password

  tesla_refresh_token_part2:
    name: "Tesla Refresh Token - Part 2"
    max: 255
    mode: password

  tesla_refresh_token_part3:
    name: "Tesla Refresh Token - Part 3"
    max: 255
    mode: password

  tesla_refresh_token_part4:
    name: "Tesla Refresh Token - Part 4"
    max: 255
    mode: password

  tesla_access_token_part1:
    name: "Tesla Access Token - Part 1"
    max: 255
    mode: password

  tesla_access_token_part2:
    name: "Tesla Access Token - Part 2"
    max: 255
    mode: password

  tesla_access_token_part3:
    name: "Tesla Access Token - Part 3"
    max: 255
    mode: password

  tesla_access_token_part4:
    name: "Tesla Access Token - Part 4"
    max: 255
    mode: password

  tesla_energy_site_id:
    name: "Tesla Energy Site ID"
    unit_of_measurement: ""
    icon: mdi:lightning-bolt-outline
```

- Use the [Access Token Generator for Tesla](https://chromewebstore.google.com/detail/access-token-generator-fo/djpjpanpjaimfjalnpkppkjiedmgpjpe?hl=en) to create a token

- This token needs to be copied, and then split into 4 parts (up to 255 characters long), so each part can be copied into the "refresh" input helpers

- An automation then uses the refresh token to generate an access token valid for 8 hours, and a new refresh token than is valid for ~30 days.<BR>
Create the following automation using the HA UI or by adding to `configuration.yaml`, the automation triggers an automatic refresh of the access token every 8 hours:

```yaml
automation:
  alias: "Refresh Tesla Access Token"
  description: "Refresh Tesla access token every 8 hours"
  trigger:
    platform: time_pattern
    hours: "/8"
  action:
    - service: rest_command.tesla_refresh_token
      response_variable: tesla_response
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_access_token_part1
      data:
        value: "{{ tesla_response.content.access_token[0:250] }}"
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_access_token_part2
      data:
       value: "{{ tesla_response.content.access_token[250:500] }}"
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_access_token_part3
      data:
        value: "{{ tesla_response.content.access_token[500:750] }}"
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_access_token_part4
      data:
        value: "{{ tesla_response.content.access_token[750:] }}"
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_refresh_token_part1
      data:
        value: "{{ tesla_response.content.refresh_token[0:250] }}"
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_refresh_token_part2
      data:
        value: "{{ tesla_response.content.refresh_token[250:500] }}"
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_refresh_token_part3
      data:
        value: "{{ tesla_response.content.refresh_token[500:750] }}"
    - service: input_text.set_value
      target:
        entity_id: input_text.tesla_refresh_token_part4
      data:
        value: "{{ tesla_response.content.refresh_token[750:] }}"
    - service: persistent_notification.create
      data:
        title: "Tesla Tokens Updated"
        message: "Access and refresh tokens have been successfully updated"
      notification_id: "tesla_token_update"
```

- An automation executes every time HA starts and every midnight to populate the Tesla site id input_helper.
Create the following automation using the HA UI or by adding to `configuration.yaml`:

```yaml
automation:
  - alias: "Update Tesla Energy Site ID"
    trigger:
      - platform: homeassistant
        event: start
      - platform: time
        at: "00:00:00"
    action:
      - service: rest_command.tesla_api_get_products
        response_variable: products_response
      - service: input_text.set_value
        target:
          entity_id: input_text.tesla_energy_site_id
        data:
          value: "{{ products_response.content.response[0].energy_site_id }}"
```

- A number of REST commands are required to communicate to the Tesla API's:

    - tesla_refresh_token - automatically regenerates access and refresh tokens,
    - tesla_api_get_products - used to retrieve your Tesla site id,
    - tesla_api_get_current_tariff - retrieves your current Tariff information from the Powerwall,
    - tesla_api_set_export_now_tariff - sets a custom export rate tariff to force the Powerwall to export,
    - tesla_api_set_iog_custom_tariff - returns the Powerwall to the Octopus IOG tariff.  If you are on a different tariff you will need to customise the REST payload to your tariff details

  In `configuration.yaml` add the following lines:

```yaml
rest_command:
  tesla_refresh_token:
    url: "https://auth.tesla.com/oauth2/v3/token"
    method: POST
    content_type: "application/x-www-form-urlencoded"
    payload: >-
      grant_type=refresh_token&client_id=ownerapi&refresh_token={{
        (states('input_text.tesla_refresh_token_part1') or '') +
        (states('input_text.tesla_refresh_token_part2') or '') +
        (states('input_text.tesla_refresh_token_part3') or '') +
        (states('input_text.tesla_refresh_token_part4') or '') }}&scope=openid%20email%20offline_access"

  tesla_api_get_products:
    url: "https://owner-api.teslamotors.com/api/1/products"
    method: GET
    headers:
      Authorization: >-
        Bearer {{ (states('input_text.tesla_access_token_part1') or '') +
          (states('input_text.tesla_access_token_part2') or '') +
          (states('input_text.tesla_access_token_part3') or '') +
          (states('input_text.tesla_access_token_part4') or '') }}

  tesla_api_get_current_tariff:
    url: "https://owner-api.teslamotors.com/api/1/energy_sites/{{ states('input_text.tesla_energy_site_id') }}/tariff_rate"
    method: GET
    headers:
      Authorization: >-
        Bearer {{ (states('input_text.tesla_access_token_part1') or '') +
          (states('input_text.tesla_access_token_part2') or '') +
          (states('input_text.tesla_access_token_part3') or '') +
          (states('input_text.tesla_access_token_part4') or '') }}

  tesla_api_set_export_now_tariff:
    url: "https://owner-api.teslamotors.com/api/1/energy_sites/{{ states('input_text.tesla_energy_site_id') }}/time_of_use_settings"
    method: POST
    headers:
      Authorization: >-
        Bearer {{ (states('input_text.tesla_access_token_part1') or '') +
          (states('input_text.tesla_access_token_part2') or '') +
          (states('input_text.tesla_access_token_part3') or '') +
          (states('input_text.tesla_access_token_part4') or '') }}
      Content-Type: application/json
    payload: >
      {% set now = now() %}
      {% set minute = now.minute %}
      {% set start = now.replace(minute=0) if minute < 30 else now.replace(minute=30) %}
      {% set end = start + timedelta(minutes=60) %}
      {% set ns = namespace(super_off_peak_periods=[]) %}
      {% if start.hour > 0 %}
        {% set ns.super_off_peak_periods = ns.super_off_peak_periods + [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0, "toHour": start.hour, "toMinute": start.minute}] %}
      {% endif %}
      {% if end.hour > 0 %}
        {% set ns.super_off_peak_periods = ns.super_off_peak_periods + [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": end.hour, "fromMinute": end.minute, "toHour": 0, "toMinute": 0}] %}
      {% endif %}
      {
        "tou_settings": {
          "tariff_content_v2": {
            "version": 1,
            "utility": "Octopus Energy",
            "code": "OCTO-IOG-CUSTOM",
            "name": "Octopus IOG (Force Export Now)",
            "currency": "GBP",
            "monthly_minimum_bill": 0,
            "min_applicable_demand": 0,
            "max_applicable_demand": 0,
            "monthly_charges": 0,
            "daily_charges": [
              { "name": "Charge", "amount": 0 }
            ],
            "daily_demand_charges": {},
            "demand_charges": {
              "ALL": { "rates": { "ALL": 0 } },
              "AllYear": { "rates": {} }
            },
            "energy_charges": {
              "ALL": { "rates": { "ALL": 0 } },
              "AllYear": {
                "rates": {
                  "SUPER_OFF_PEAK": 0.07,
                  "ON_PEAK": 0.31
                }
              }
            },
            "seasons": {
              "AllYear": {
                "fromMonth": 1,
                "fromDay": 1,
                "toMonth": 12,
                "toDay": 31,
                "tou_periods": {
                  "SUPER_OFF_PEAK": {
                    "periods": {{ ns.super_off_peak_periods | tojson }}
                  },
                  "ON_PEAK": {
                    "periods": [
                      { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": {{ start.hour }}, "fromMinute": {{ start.minute }}, "toHour": {{ end.hour }}, "toMinute": {{ end.minute }} }
                    ]
                  }
                }
              }
            },
            "sell_tariff": {
              "min_applicable_demand": 0,
              "max_applicable_demand": 0,
              "monthly_minimum_bill": 0,
              "monthly_charges": 0,
              "utility": "Octopus Energy",
              "daily_charges": [
                { "name": "Charge", "amount": 0 }
              ],
              "demand_charges": {
                "ALL": { "rates": { "ALL": 0 } },
                "AllYear": { "rates": {} }
              },
              "energy_charges": {
                "ALL": { "rates": { "ALL": 0 } },
                "AllYear": {
                  "rates": {
                    "SUPER_OFF_PEAK": 0.07,
                    "ON_PEAK": 0.30
                  }
                }
              },
              "seasons": {
                "AllYear": {
                  "fromMonth": 1,
                  "fromDay": 1,
                  "toMonth": 12,
                  "toDay": 31,
                  "tou_periods": {
                    "SUPER_OFF_PEAK": {
                      "periods": {{ ns.super_off_peak_periods | tojson }}
                    },
                    "ON_PEAK": {
                      "periods": [
                        { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": {{ start.hour }}, "fromMinute": {{ start.minute }}, "toHour": {{ end.hour }}, "toMinute": {{ end.minute }} }
                      ]
                    }
                  }
                }
              }
            }
          }
        }
      }

  tesla_api_set_iog_custom_tariff:
    url: "https://owner-api.teslamotors.com/api/1/energy_sites/{{ states('input_text.tesla_energy_site_id') }}/time_of_use_settings"
    method: POST
    headers:
      Authorization: >-
        Bearer {{ (states('input_text.tesla_access_token_part1') or '') +
          (states('input_text.tesla_access_token_part2') or '') +
          (states('input_text.tesla_access_token_part3') or '') +
          (states('input_text.tesla_access_token_part4') or '') }}
      Content-Type: application/json
    payload: >
      {
        "tou_settings": {
          "tariff_content_v2": {
            "version": 1,
            "monthly_minimum_bill": 0,
            "min_applicable_demand": 0,
            "max_applicable_demand": 0,
            "monthly_charges": 0,
            "utility": "Octopus Energy",
            "code": "OCTO-IOG-CUSTOM",
            "name": "Octopus IOG (Custom-restored)",
            "currency": "GBP",
            "daily_charges": [
              { "name": "Charge", "amount": 0 }
            ],
            "daily_demand_charges": {},
            "demand_charges": {
              "ALL": { "rates": { "ALL": 0 } },
              "AllYear": { "rates": {} }
            },
            "energy_charges": {
              "ALL": { "rates": { "ALL": 0 } },
              "AllYear": {
                "rates": {
                  "SUPER_OFF_PEAK": 0.07,
                  "PARTIAL_PEAK": 0.31,
                  "ON_PEAK": 0.31
                }
              }
            },
            "seasons": {
              "AllYear": {
                "fromMonth": 1,
                "fromDay": 1,
                "toMonth": 12,
                "toDay": 31,
                "tou_periods": {
                  "SUPER_OFF_PEAK": {
                    "periods": [
                      { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0, "toHour": 5, "toMinute": 30 },
                      { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 23, "fromMinute": 30, "toHour": 0, "toMinute": 0 }
                    ]
                  },
                  "ON_PEAK": {
                    "periods": [
                      { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 2, "fromMinute": 0, "toHour": 3, "toMinute": 0 },
                      { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 5, "fromMinute": 30, "toHour": 16, "toMinute": 0 },
                      { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 16, "fromMinute": 0, "toHour": 19, "toMinute": 0 },
                      { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 19, "fromMinute": 0, "toHour": 23, "toMinute": 30 }
                    ]
                  }
                }
              }
            },
            "sell_tariff": {
              "min_applicable_demand": 0,
              "max_applicable_demand": 0,
              "monthly_minimum_bill": 0,
              "monthly_charges": 0,
              "utility": "Octopus Energy",
              "daily_charges": [
                { "name": "Charge", "amount": 0 }
              ],
              "demand_charges": {
                "ALL": { "rates": { "ALL": 0 } },
                "AllYear": { "rates": {} }
              },
              "energy_charges": {
                "ALL": { "rates": { "ALL": 0 } },
                "AllYear": {
                  "rates": {
                    "SUPER_OFF_PEAK": 0.07,
                    "PARTIAL_PEAK": 0.30,
                    "ON_PEAK": 0.22
                  }
                }
              },
              "seasons": {
                "AllYear": {
                  "fromMonth": 1,
                  "fromDay": 1,
                  "toMonth": 12,
                  "toDay": 31,
                  "tou_periods": {
                    "SUPER_OFF_PEAK": {
                      "periods": [
                        { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0, "toHour": 5, "toMinute": 30 },
                        { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 23, "fromMinute": 30, "toHour": 0, "toMinute": 0 }
                      ]
                    },
                    "ON_PEAK": {
                      "periods": [
                        { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 2, "fromMinute": 0, "toHour": 3, "toMinute": 0 },
                        { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 5, "fromMinute": 30, "toHour": 16, "toMinute": 0 },
                        { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 16, "fromMinute": 0, "toHour": 19, "toMinute": 0 },
                        { "fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 19, "fromMinute": 0, "toHour": 23, "toMinute": 30 }
                      ]
                    }
                  }
                }
              }
            }
          }
        }
      }
```

- Manually run the two automations to ensure the helper input_texts are all pre-populated before use.

## Victron

This is at an early stage of development, see Github discussion [#789](https://github.com/springfall2008/batpred/discussions/798) and [#2846](https://github.com/springfall2008/batpred/issues/2846)


## I want to add an unsupported inverter to Predbat

- First copy one of the template configurations that is close to your system and try to configure it to match the sensors you have
- Create a GitHub ticket for support and add what you know to the ticket
- Then find out how to control your inverter inside Home Assistant, ideally share any automation you have to control the inverter
- You can create a new inverter type in `apps.yaml` and change the options as to which controls it has
- You **must** set [inverter_type in apps.yaml](apps-yaml.md#inverter_type) with a custom name ('MINE' in the example below) - if you do not do this then Predbat will assume you have a GivEnergy inverter
and will apply inverter limits for that inverter (e.g. max charge/discharge of 2600W)
- Configure Predbat with the appropriate Home Assistant services to start charges and discharges, etc.

The following template can be used as a starting point:

```yaml
  inverter_type: MINE
  inverter:
    name: "MINE"
    has_rest_api: False
    has_mqtt_api: False
    has_service_api: True
    output_charge_control: "power"
    charge_control_immediate: False
    current_dp: 1
    charge_discharge_with_rate: False
    has_charge_enable_time: False
    has_discharge_enable_time: False
    has_target_soc: False
    target_soc_used_for_discharge: True
    has_reserve_soc: False
    has_timed_pause: False
    time_button_press: False
    support_charge_freeze: False
    support_discharge_freeze: False
    has_ge_inverter_mode: False
    has_fox_inverter_mode: False
    has_idle_time: False
    has_time_window: False
    charge_time_format: "S"
    charge_time_entity_is_option: False
    can_span_midnight: False
    clock_time_format: "%Y-%m-%d %H:%M:%S"
    num_load_entities: 1
    soc_units: "%"
    write_and_poll_sleep: 2

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

## Inverter control options

The following options are supported per inverter:

### has_rest_api

When True the REST API will be used to fetch data/control the inverter. This is currently only for GivEnergy inverters with GivTCP and **givtcp_rest** must be set in `apps.yaml`

### has_mqtt_api

When True the Home Assistant MQTT API will be used to issue control messages for the inverter

The MQTT/publish service is used with the topic as defined by **mqtt_topic** in `apps.yaml`

Messages will be sent through these controls:

Values that are updated:

- **topic**/set/reserve - payload=reserve
- **topic**/set/charge_rate - payload=new_rate
- **topic**/set/discharge_rate - payload=new_rate
- **topic**/set/target_soc - payload=target_soc

These three messages change between battery charge/discharge and auto (demand) mode:

- **topic**/set/charge - payload=charge_rate
- **topic**/set/discharge - payload=discharge_rate
- **topic**/set/auto - payload=true

### has_service_api

When True a Home Assistant service will be used to issue control messages for the inverter.

For each service you wish to use it must be defined in `apps.yaml`.

There are two ways to define a service, the basic mode:

```yaml
  charge_start_service: my_service_name_charge
```

Will call `my_service_name_charge` for the charge start service.

Or the custom method where you can define all the parameter values passed to the service and use the default values from the template, or define your own:

```yaml
  charge_start_service:
    - service: my_charge_start_service
      device_id: "{device_id}"
      power: "{power}"
      soc: "{target_soc}"
      charge_start_time: "{charge_start_time}"
      charge_end_time: "{charge_end_time}"
```

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

- device_id - as defined in `apps.yaml` by **device_id**
- target_soc - The SoC to charge to
- power - The charge power to use
- charge_start_time - Start time for the charge
- charge_end_time - End time for the charge

#### charge_freeze_service

If defined will be called for freeze charge, otherwise, charge_start_service is used for freeze charge also.

#### charge_stop_service

Called to stop a charge

- device_id - as defined in `apps.yaml` by **device_id**

#### discharge_start_service

Called to start a discharge

The default options passed in are:

- device_id - as defined in `apps.yaml` by **device_id**
- target_soc - The SoC to discharge to
- power - The discharge power to use

#### discharge_freeze_service

If defined will be called for Discharge freeze, otherwise, discharge_start_service is used for freeze discharge also.

#### discharge_stop_service

Called to stop a discharge, if not set then **charge_stop_service** will be used instead

- device_id - as defined in `apps.yaml` by **device_id**

### output_charge_control

Controls what charge control units are to be used when starting charging. Set to "power", "current" or "none".

When set to "power", Predbat will use the inverter sensors configured as **charge_rate** and **discharge_rate** in `apps.yaml` to set the inverter charge/discharge power levels. These inverter sensors must be in watts.

When set to "current", Predbat will use the inverter sensors configured as  **timed_charge_current** and **timed_discharge_current** in `apps.yaml` to set the inverter charge/discharge current levels. These inverter sensors must be in amps.<BR>
Additionally if you are using "current" control for your inverter you must set **battery_voltage** in `apps.yaml` to your nominal maximum battery voltage (NB: not the current battery voltage)
as Predbat will use this to convert its output commands from watts to amps for the inverter.

### charge_control_immediate

When True, the inverter uses **timed_charge_current** and **timed_discharge_current** in `apps.yaml` to control charging and discharging by setting current levels directly, instead of following a time-based plan.

### current_dp

Sets the number of decimal places to be used when setting the current in Amps, which should be 0 or 1.

### charge_discharge_with_rate

When True, the inverter requires that when charging the discharge rate must set be 0; and vice-versa, when discharging the charge rate must be set to 0.

When False, the charge/discharge rate does not have to change.

### has_charge_enable_time

When True, Predbat uses the **scheduled_charge_enable** switch configured in `apps.yaml` to enable/disable timed charging on the inverter.

### has_discharge_enable_time

When True, Predbat uses the **scheduled_discharge_enable** switch configured in `apps.yaml` to enable/disable timed discharging on the inverter.

### has_target_soc

When True, Predbat uses the **charge_limit** sensor configured in `apps.yaml` to set the target charge SoC % setting for the inverter. The charge limit is the limit that the inverter will charge the battery up to.
When False, charging will be turned on and off by Predbat rather than the inverter doing it based on the target SoC %.

### target_soc_used_for_discharge

When True, Predbat will use the **charge_limit** sensor configured in `apps.yaml` to control the target discharge SoC% for the inverter.

When False, Predbat will not adjust the **charge_limit** sensor when discharging.

### has_reserve_soc

When True, Predbat uses the **reserve** sensor configured in `apps.yaml` to set the discharge reserve SoC % for the inverter. The reserve SoC is the target % to discharge the battery down to.
When False, discharging will be turned on and off by Predbat rather than the inverter doing it based on discharge SoC %.

### has_timed_pause

When True, Predbat uses the **pause_mode** and optional **pause_start_time** and **pause_end_time**  settings in `apps.yaml` to pause the inverter from charging and discharging the battery. This setting is for GivEnergy systems only right now.

### time_button_press

When True, the inverter requires a button press to update the inverter registers from the Home Assistant values.

The `apps.yaml` setting **charge_discharge_update_button** is the entity name of the button that Predbat will "push" to update the inverter registers.

### support_charge_freeze

When True, the inverter supports charge freeze modes.

### support_discharge_freeze

When True, the inverter supports discharge freeze modes.

### has_ge_inverter_mode

When True, the inverter supports the GivEnergy inverter modes (ECO, Timed Export etc).

### has_fox_inverter_mode

When True, the inverter supports Fox inverter modes, i.e. Eco (Paused) is treated the same as Eco mode and the inverter mode is always set to "SelfUse" as all charging and discharging is controlled by schedule, not inverter modes.

### has_idle_time

When True, the inverter has an idle time register which must be set to the start and end times for Eco mode (GivEnergy EMS).  **idle_start_time** and **idle_end_time** must be configured in `apps.yaml` to the appropriate inverter controls.

### has_time_window

Not currently used by Predbat.

### charge_time_format

This setting is used to control what format of charge and discharge times the inverter requires.

When set to "HH:MM:SS", Predbat will control the inverter charge/discharge start and end times by setting the entities defined by **charge_start_time**, **charge_end_time**, **discharge_start_time** and **discharge_end_time** in `apps.yaml`.

The format of these entities depends on **charge_time_entity_is_option** as defined below.

When set to "H M", Predbat will control the inverter charge/discharge start and end times by setting the entities defined by **charge_start_hour**, **charge_start_minute**, **charge_end_hour**, **charge_end_minute**,
**discharge_start_hour**,  **discharge_start_minute**, **discharge_end_hour** and **discharge_end_minute** in `apps.yaml`.

These entities are used to set the start and end hours and minutes of charges and discharges.

When set to "H:M-H:M", Predbat will control the inverter charge/discharge start and end times by setting the entities defined by **charge_time** and **discharge_time** in `apps.yaml`.
The entities take a single time range value in the format "*start hour*:*start minute*-*end hour*:*end minute*"

### charge_time_entity_is_option

When True, **charge_start_time** **charge_end_time** **discharge_start_time** and **discharge_end_time** are all option selectors for time in the format HH:MM:SS (e.g. 12:23:00) where seconds are always 00.

When False, these entities are all number values.

### can_span_midnight

When True, start and end times for charge and discharge can span midnight e.g. 23:00:00 - 01:00:00 is a 2-hour slot.

When False, start and end times can't span midnight and Predbat will control the inverter with separate charges/discharges up to and then after midnight if required by the plan.

### clock_time_format

Defines the time format of the inverter clock setting **inverter_time** in `apps.yaml`

### num_load_entities

Enables you to define additional house load power sensors in `apps.yaml` in addition to the default **load_power** sensor.  e.g. if set to 2 then Predbat will additionally use **load_power_1** and **load_power_2** settings in `apps.yaml`.
This setting might be required for 3-phase inverters.

### soc_units

Defines the units of the SoC setting (currently not used), it defaults to "%".

### write_and_poll_sleep

Sets the number of seconds between polls of inverter settings.
