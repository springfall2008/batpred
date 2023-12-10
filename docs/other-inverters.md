## Other Inverters

PredBat was originally written for GivEnergy inverters using the GivTCP integration but this is now being extended to other models:

- Solis Hybrid inverters [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus)
- Solax Gen4 inverters [Solax Modbus integration](https://github.com/wills106/homeassistant-solax-modbus) in Modbus Power Control Mode\*
- SolarEdge inverters\*

\*Work in progress

Note that support for all these inverters is in various stages of development. Please expect things to fail and report them as Issues on Github. Please also ensure you have set up enhanced logging in AppDaemon as described here

### Solis Inverters

To run PredBat with Solis hybrid inverters, follow the following steps:

1. Install PredBat as per the [Installation Summary](installation-summary.md)
2. Ensure that you have the Solax Modbus integration running. There are a number of entities which this integration disables by default that you will need to enable via the Home Assistant GUI:

   | Name                          | Description     |
   | :---------------------------- | :-------------- |
   | `sensor.solisx_rtc`           | Real Time Clock |
   | `sensor.solisx_battery_power` | Battery Power   |

3. Instead of `apps.yaml` use `ginlong_solis.yaml` from this Repo as your starting template. The majority of settings should be correct but please check. You will need to un-comment the `template` line to enable it. Save it to the `config/appdaemon/apps/predbat/config` folder.
4. Ensure that the inverter is set Control Mode 35 - on the Solax integration this is `Timed Charge/Discharge`. If you want to use the `Reserve` functionality within PredBat you will need to select `Backup/Reserve` (code 51) instead but be aware that this is not fully tested. In due course these mode settings will be incorporated into the code.
