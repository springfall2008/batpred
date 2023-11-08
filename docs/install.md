# Install

## GivTCP install

- You must have GivTCP installed and running first ([https://github.com/britkat1980/giv_tcp](https://github.com/britkat1980/giv_tcp))
    - You will need at least 24 hours history in HA for this to work correctly, the default is 7 days (but you configure this back 1 day if you need to)

## AppDaemon install

- Install AppDaemon add-on [https://github.com/hassio-addons/addon-appdaemon](https://github.com/hassio-addons/addon-appdaemon)
    - Set the **time_zone** correctly in appdaemon.yml (e.g. Europe/London)
    - Add **thread_duration_warning_threshold: 30** to the appdaemon.yml file in the appdaemon section

## HACS install

- Install HACS if you haven't already ([https://hacs.xyz/docs/setup/download](https://hacs.xyz/docs/setup/download))
- Enable AppDaemon in HACS: [https://hacs.xyz/docs/categories/appdaemon_apps/](https://hacs.xyz/docs/categories/appdaemon_apps/)

## Predbat install

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

- Once installed you will get automatic updates from each release!

- Add https://github.com/springfall2008/batpred as a custom repository of type 'AppDaemon'
- Click on the Repo and Download the app

*After an update with HACS you may need to reboot AppDaemon as it sometimes reads the config wrongly during the update (If this happens you will get a template configuration error).*

- Edit in HomeAssistant config/appdaemon/apps/predbat/config/apps.yml to configure
    - You must delete the 'template: True' line in the configuration to enable Predbat once you are happy with your configuration
    - Note that future updates will not overwrite apps.yml, but you may need to copy settings for new features across manually

## Predbat manual install

**Not recommended if you have HACS**

- Copy apps/predbat/predbat.py to 'config/appdaemon/apps/' directory in home assistant
- Copy apps/predbat/apps.yml to 'config/appdaemon/apps' directory in home assistant
- Edit in HomeAssistant config/appdaemon/apps/apps.yml to configure

- If you later install with HACS then you must move the apps.yml into config/appdaemon/apps/predbat/config

## Solcast install

Predbat needs a solar forecast in order to predict battery levels.

If you don't have solar then comment out the Solar forecast part of the apps.yml: **pv_forecast_* **

- Make sure Solcast is installed and working (https://github.com/oziee/ha-solcast-solar)

- Note that Predbat does not update Solcast for you, it's recommended that you disable polling (due to the API polling limit) in the Solcast plugin and instead have your own automation that updates the forecast a few times a day (e.g. dawn, dusk and just before your nightly charge slot).

- Example Solcast update script:

```yaml
alias: Solcast update
description: ""
trigger:
  - platform: time
    at: "23:00:00"
  - platform: time
    at: "12:00:00"
  - platform: time
    at: "04:00:00"
condition: []
action:
  - service: solcast_solar.update_forecasts
    data: {}
mode: single
```

## Octopus Energy

Follow the instructions at [https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy/).

Also see the notes in the [Energy Rates](energy-rates.md#octopus-energy-plugin) section.
