# Install

## Inverter Control Integration install (GivTCP/SolaX-ModBus)

The Integration that communicates with your inverter will be depend on the brand:

| Brand     | Integration  | Github Link                                                                      |
| :-------- | :----------- | :------------------------------------------------------------------------------- |
| GivEnergy | GivTCP       | [https://github.com/britkat1980/giv_tcp](https://github.com/britkat1980/giv_tcp) |
| Solis     | SolaX ModBus | <https://github.com/wills106/homeassistant-solax-modbus>                           |

- You will need at least 24 hours history in HA for this to work correctly, the default is 7 days (but you configure this back 1 day if you need to)

## AppDaemon-Predbat combined install

The simplest way to install Predbat now is with a combined AppDaemon/Predbat add-in. This is a fork of AppDaemon which installs
Predbat automatically.

- Go to settings, Add-ons, Add on store (bottom right), Top right three dots, Repositories and add
'<https://github.com/springfall2008/appdaemon-predbat>' to the list and close.
- Next select AppDaemon with Predbat and Install and then hit 'start'
- If you haven't already change your Editor settings to ensure 'enforce base path' is disabled (settings, Add-ons, File Editor, Configuration)
- Now use your Editor to find '/addon_configs/46f69597_appdaemon-predbat', here there is
    - predbat.log - contains the active logfile with any errors
    - apps/apps.yaml - you need to edit apps.yaml to remove the template settings and customise
- Once you have edited apps.yaml click 'restart' on the appdaemon-predbat add-on

Once installed you should perform updates directly within Predbat by using the 'select.predbat_update' selector or by enabling the **switch.predbat_auto_update**

If you use this method you do not need to install AppDaemon or HACS so you can skip directly to Solcast install (below)

## Predbat installation into AppDaemon

### AppDaemon install

- Install the AppDaemon add-on [https://github.com/hassio-addons/addon-appdaemon](https://github.com/hassio-addons/addon-appdaemon)
- You will find the appdaemon.yaml file in addon_configs/a0d7b954_appdaemon ![image](https://github.com/springfall2008/batpred/assets/48591903/bf8bf9cf-75b1-4a8d-a1c5-fbb7b3b17521)
    - If using the File Editor remember to turn off **enforce base path** to allow access: ![image](https://github.com/springfall2008/batpred/assets/48591903/298c7a19-3be9-43d6-9f1b-b46467701ca7)
- Add to the appdaemon: section **apps_dir** which should point to /homeassistant/appdaemon/apps
- Set the **time_zone** correctly in appdaemon.yaml (e.g. Europe/London)
- Add **thread_duration_warning_threshold: 120** to the appdaemon.yaml file in the appdaemon section
- It's recommended you set a new logfile location so that you can see the complete logs, I set mine
to /homeassistant/appdaemon/appdaemon.log and increase the maximum size and number of generations to capture a few days worth

Example config:

```yaml
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

CAUTION: Migrating from an older Appdaemon to 0.15.2 or above:

- Make sure you have access to the HA filesystem, e.g. I use the Samba add on and connect to the drives on my Mac, but you can use ssh also.
Update AppDaemon to 0.15.2
- Go into addon_configs/a0d7b954_appdaemon and edit appdaemon.yaml. You need to add app_dir (see above) to point to the
old location and update your logfile location (if you have set it). You should remove the line that points to secrets.yaml
(most people don't use this file) or adjust it's path to the new location (/homeassistant/secrets.yaml).
- Move the entire 'apps' directory from addon_configs/a0d7b954_appdaemon (new location) to config/appdaemon (the old location)
- Restart appdaemon
- Check it has started and confirm Predbat is running correctly again.

### HACS install

- Install HACS if you haven't already ([https://hacs.xyz/docs/setup/download](https://hacs.xyz/docs/setup/download))
- Enable AppDaemon in HACS: [https://hacs.xyz/docs/categories/appdaemon_apps/](https://hacs.xyz/docs/categories/appdaemon_apps/)

### Predbat install with HACS

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

Once installed you will get automatic updates from each release!

- Add <https://github.com/springfall2008/batpred> as a custom repository of type 'AppDaemon'
- Click on the Repo and Download the app

Edit in HomeAssistant config/appdaemon/apps/batpred/config/apps.yaml to configure Predbat - see [Configuring apps.yaml](config-yml-settings.md#Basics).

You must delete the 'template: True' line in the configuration to enable Predbat once you are happy with your configuration.

You may initially want to set **select.predbat_mode** to Monitor to see how Predbat operates, to enable Predbat controls you should then set it to
the correct mode of operation for your system.

Note that future updates to Predbat will not overwrite apps.yaml, but you may need to copy settings for new features across manually

When Predbat starts up initially it will perform a sanity check of the appDaemon configuration itself and confirm the right files are present.
You will see this check in the log, should it fail a warning will be issued and predbat.status will also reflect the warning.
While the above warning might not prevent Predbat from startup you should fix the issue ASAP as it may cause future problems.

## Predbat manual install

A manual install is suitable for those running Docker type systems where HACS does not function correctly and you had to manually install AppDaemon

Note: **Not recommended if you are using HACS**

- Copy apps/predbat/predbat.py to 'config/appdaemon/apps/' directory in home assistant (or wherever you set appdaemon apps_dir to)
- Copy apps/predbat/apps.yml to 'config/appdaemon/apps' directory in home assistant (or wherever you set appdaemon apps_dir to)
- Edit in HomeAssistant config/appdaemon/apps/apps.yml to configure

- If you later install with HACS then you must move the apps.yml into config/appdaemon/apps/predbat/config

## Predbat updates

## HACS Update

Note that HACS only checks for updates once a day by default, you can however force it to check again or download a specific version
by using the re-download option on the custom repository.

*After an update with HACS you may need to reboot AppDaemon as it sometimes reads the config wrongly during the update
(If this happens you will get a template configuration error in the entity predbat.status).*

## Predbat built-in update

Predbat can now update itself, just select the version you want from the **select.predbat_update** drop down menu, the latest will be
at the top of the list. Predbat will update itself and restart.

## Manual update

You can go to Github and download predbat.py from the releases tab and then copy this file over the existing version manually.

## Solcast install

Predbat needs a solar forecast in order to predict battery levels.

If you don't have solar then comment out the Solar forecast part of the apps.yml:

```yaml
  pv_forecast_today: re:(sensor.(solcast_|)(pv_forecast_|)forecast_today)
  pv_forecast_tomorrow: re:(sensor.(solcast_|)(pv_forecast_|)forecast_tomorrow)
  pv_forecast_d3: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_3|d3))
  pv_forecast_d4: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_4|d4))
```

Or make sure Solcast is installed and working (<https://github.com/oziee/ha-solcast-solar>).

Note that Predbat does not update Solcast for you, it's recommended that you disable polling (due to the API polling limit)
in the Solcast plugin and instead have your own automation that updates the forecast a few times a day (e.g. dawn, dusk and
just before your nightly charge slot).

Example Solcast update script:

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
