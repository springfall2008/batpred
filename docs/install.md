# Install

## Inverter Control Integration install (GivTCP/SolaX-ModBus)

The Integration that communicates with your inverter will be depend on the brand of inverter you have:

| Brand     | Integration  | Github Link                                                                      |
| :-------- | :----------- | :------------------------------------------------------------------------------- |
| GivEnergy | GivTCP       | [https://github.com/britkat1980/giv_tcp](https://github.com/britkat1980/giv_tcp) |
| Solis     | SolaX ModBus | <https://github.com/wills106/homeassistant-solax-modbus>                           |

- You will need at least 24 hours history in Home Assistant for Predbat to work correctly, the default is 7 days (but you configure this back to 1 day if you need to).

## AppDaemon install

- Install the AppDaemon add-on [https://github.com/hassio-addons/addon-appdaemon](https://github.com/hassio-addons/addon-appdaemon)
- You will need to edit the *appdaemon.yaml* configuration file for AppDaemon
- Find the appdaemon.yaml file in the directory addon_configs/a0d7b954_appdaemon: ![image](https://github.com/springfall2008/batpred/assets/48591903/bf8bf9cf-75b1-4a8d-a1c5-fbb7b3b17521)
    - If you are using the File Editor to edit appdaemon.yaml, you will need to turn off **enforce base path** to enable you to access files in the appdaemon directory:<br>
    ![image](https://github.com/springfall2008/batpred/assets/48591903/298c7a19-3be9-43d6-9f1b-b46467701ca7)
- Add to the appdaemon.yaml configuration file:
  - A section **app_dir** which should point to /homeassistant/appdaemon/apps
  - Ensure that the **time_zone** is correctly (e.g. Europe/London)
  - Add **thread_duration_warning_threshold: 120** in the appdaemon section
- It's recommended you also add a **logs** section and specify a new logfile location so that you can see the complete logs, I set mine
to /homeassistant/appdaemon/appdaemon.log and increase the logfile maximum size and number of logfile generations to capture a few days worth.

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

# write log records to a file, retaining 9 versions, rather than the standard appdaemon log
logs:
  main_log:
    filename: /homeassistant/appdaemon/appdaemon.log
    log_generations: 9
    log_size: 10000000
```

CAUTION: If you are upgrading AppDaemon from an older version to version 0.15.2 or above:

- Make sure you have access to the HA filesystem, e.g. I use the Samba add on and connect to the drives on my Mac, but you can use ssh also.
- Update AppDaemon to 0.15.2
- Go into addon_configs/a0d7b954_appdaemon and edit appdaemon.yaml. You need to add app_dir (see above) to point to the
old location and update your logfile location (if you have set it). You should remove the line that points to secrets.yaml
(most people don't use this file) or adjust it's path to the new location (/homeassistant/secrets.yaml).
- Move the entire 'apps' directory from addon_configs/a0d7b954_appdaemon (new location) to config/appdaemon (the old location)
- Restart AppDaemon
- Check it has started and confirm Predbat is running correctly again.

## HACS install

- Install HACS if you haven't already ([https://hacs.xyz/docs/setup/download](https://hacs.xyz/docs/setup/download))
- Enable AppDaemon in HACS: [https://hacs.xyz/docs/categories/appdaemon_apps/](https://hacs.xyz/docs/categories/appdaemon_apps/)

## Install Predbat through HACS

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

Once installed you will get automatic updates for each new release of Predbat!

- In HACS, click on Automation
- Click on the three dots in the top right corner, choose *Custom Repositories*
- Add <https://github.com/springfall2008/batpred> as a custom repository of Category 'AppDaemon' and click 'Add'
- Click *Explore and download repositories* (bottom right), type 'Predbat' in the search box, select the Predbat Repository, then click 'Download' to install the Predbat app.

## Predbat manual install

Note: **Not recommended if you are using HACS**

- Copy apps/predbat/predbat.py to 'config/appdaemon/apps/' directory in home assistant (or wherever you set appdaemon apps_dir to)
- Copy apps/predbat/apps.yml to 'config/appdaemon/apps' directory in home assistant (or wherever you set appdaemon apps_dir to)
- Edit in HomeAssistant config/appdaemon/apps/apps.yaml to configure Predbat

- If you later install with HACS then you must move the apps.yaml into config/appdaemon/apps/predbat/config

## Solcast Install

Predbat needs a solar forecast in order to predict battery levels.

If you don't have solar then use a file editor to comment out the following lines from the Solar forecast part of the apps.yaml configuration:

```yaml
  pv_forecast_today: re:(sensor.(solcast_|)(pv_forecast_|)forecast_today)
  pv_forecast_tomorrow: re:(sensor.(solcast_|)(pv_forecast_|)forecast_tomorrow)
  pv_forecast_d3: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_3|d3))
  pv_forecast_d4: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_4|d4))
```

If you do have solar panels its recommended to use the Solcast integration to automatically retrieve your forecast solar generation.
Predbat is configured to automatically discover the Solcast forecast entities in Home Assistant.

Install the Solcast integration (<https://github.com/oziee/ha-solcast-solar>).
Make sure the Solcast integration is working by going to Developer Tools / States, filtering on 'solcast'
and checking that you can see the half-hourly solar forecasts in the Solcast entities.

Note that Predbat does not update Solcast for you, it's recommended that you disable polling (due to the API polling limit)
in the Solcast integration and instead have your own automation that updates the solar forecast a few times a day
(e.g. dawn, dusk and just before your nightly charge slot).

Example Solcast update script:

```yaml
alias: Solcast update
description: "Update Solcast solar forecast"
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

## Energy Rates

Predbat needs to know what your electricity import and export rates are in order to optimise battery charging and discharging to minimise your expenditure.

Follow the instructions in the [Energy Rates](energy-rates.md#octopus-energy-plugin) section.

## Configuring Predbat

Edit in HomeAssistant the file *config/appdaemon/apps/batpred/config/apps.yaml* to configure Predbat - see [Configuring apps.yaml](config-yml-settings.md#Basics).

When Predbat starts up initially it will perform a sanity check of the AppDaemon configuration itself and confirm the right files are present.
You will see this check in the log, should it fail a warning will be issued and **predbat.status** will also reflect the warning.
While the above warning might not prevent Predbat from starting up, you should fix the issue ASAP as it may cause future problems.

## Predbat Output and Configuration Controls

As described above, the basic configuration of Predbat is held in the *apps.yaml* configuration file.
When Predbat first runs it will create a number of output and configuration control entities in Home Assistant which
are used to fine-tune how Predbat operates.
The entities are all prefixed *predbat* and can be seen (and changed) from the Integrations / Entities list in Home Assistant.

It is recommended that you create a dashboard page with all the required entities to control Predbat
and another page to display Predbat's charging and discharging plan for your battery.

The [Output Data](output-data.md) section describes this in more detail.

## Ready to light the touch-paper

By now you should have successfully installed and configured Predbat in AppDaemon and the other components it is dependent upon (e.g. GivTCP, Solcast, Octopus Integration).
You have checked the logfile doesn't have any errors (there is a lot of output in the logfile, this is normal).
You have configured predbat's control entities and are ready to light the touch-paper.

In order to enable Predbat you must delete the 'template: True' line in apps.yaml once you are happy with your configuration.

You may initially want to set **select.predbat_mode** to *Monitor* to see how Predbat operates, e.g. studying the Predbat Plan.

Once you are happy with the plan Predbat is producing, and are ready to let Predbat start controlling your inverter, set **select.predbat_mode**
to the correct mode of operation for your system.

## Updating Predbat

Note that future updates to Predbat will not overwrite the apps.yaml configuration file that you have tailored to your setup.
You may therefore need to manually copy across any new apps.yaml settings from the [Template apps.yaml](config-yml-settings.md#Templates) for new features.

## HACS Update

Note that HACS only checks for updates once a day by default, you can however force it to check again or download a specific version
by using the re-download option from the three dots top-right menu for the Predbat repository in HACS.

**NOTE:** If you update Predbat through HACS you may need to restart AppDaemon as it sometimes reads the config wrongly during the update.
(If this happens you will get a template configuration error in the entity predbat.status).<BR>
Go to Add-on's, AppDaemon, and click 'Restart'.

## Predbat built-in update

Predbat can now update itself, just select the version you want from the **select.predbat_update** drop down menu, the latest will be
at the top of the list. Predbat will update itself and automatically restart.

## Manual update of Predbat

You can go to Github and download predbat.py from the releases tab and then manually copy this file
over the existing version in *config/appdaemon/apps/batpred/* manually.
