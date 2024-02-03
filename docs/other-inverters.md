# Other Inverters

PredBat was originally written for GivEnergy inverters using the GivTCP integration but this is now being extended to other models:

- Solis Hybrid inverters [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus)
- Solax Gen4 inverters [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus) in Modbus Power Control Mode
- Sofar inverters [Sofar MQTT integration](https://github.com/cmcgerty/Sofar2mqtt)
- SolarEdge inverters - **Work in progress, please contribute**

Note that support for all these inverters is in various stages of development. Please expect things to fail and report them as Issues on Github.
Please also ensure you have set up enhanced logging in AppDaemon as described here.

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
6. Ensure that the inverter is set Control Mode 35 - on the Solax integration this is `Timed Charge/Discharge`.
   If you want to use the `Reserve` functionality within PredBat you will need to select `Backup/Reserve` (code 51) instead but be aware that
   this is not fully tested. In due course these mode settings will be incorporated into the code.

## Solax Inverters

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
