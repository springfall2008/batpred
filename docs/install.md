# Install

## GivTCP install

- You must have GivTCP installed and running first ([https://github.com/britkat1980/giv_tcp](https://github.com/britkat1980/giv_tcp))
    - You will need at least 24 hours history in HA for this to work correctly, the default is 7 days (but you configure this back 1 day if you need to)

## AppDaemon install

- Install AppDaemon add-on [https://github.com/hassio-addons/addon-appdaemon](https://github.com/hassio-addons/addon-appdaemon)
    - You will find the appdaemon.yaml file in addon_configs/a0d7b954_appadaemon
        - Add to the appdaemon: setion **apps_dir** which should point to /homeassistant/appdaemon/apps
        - Set the **time_zone** correctly in appdaemon.yaml (e.g. Europe/London)
        - Add **thread_duration_warning_threshold: 120** to the appdaemon.yaml file in the appdaemon section
        - It's recommended you set a new logfile location so that you can see the complete logs, I set mine to /homeassistant/appdaemon/appdaemon.log and increase the maximum size and number of generations to capture a few days worth
     
Example config:
```
---
appdaemon:
  latitude: 52.379189
  longitude: 4.899431
  elevation: 2
  time_zone: Europe/London
  thread_duration_warning_threshold: 120
  plugins:
    HASS:
      type: hass
  app_dir: /homeassistant/appdaemon/apps
http:
  url: http://homeassistant.local:5050
admin:
api:
hadashboard:
logs:
  main_log: 
    filename: /homeassistant/appdaemon/appdaemon.log
    log_generations: 9
    log_size: 10000000
```
 
CAUTION: Migration from an older Appdaemon to 0.15.2 or above:

- Make sure you have access to the HA filesystem, e.g. I use the Samba add on and connect to the drives on my Mac, but you can use ssh also.
Update AppDaemon to 0.15.2
- Go into addon_configs/a0d7b954_appadaemon and edit appdaemon.yaml. You need to add app_dir (see above) to point to the old location and update your logfile location (if you have set it). You should remove the line that points to secrets.yaml (most people don't use this file) or adjust it's path to the new location (/homeassistant/secrets.yaml).
- Move the entire 'apps' directory from addon_configs/a0d7b954_appadaemon (new location) to config/appdaemon (the old location)
- Restart appdaemon
- Check it has started and confirm Predbat is running correctly again.

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
