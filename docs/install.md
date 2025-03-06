# Install

These instructions will take you through the process of installing and configuring Predbat for first-time use.

If you have a working Predbat installation using AppDaemon and are changing to use the Predbat add-on,
the [AppDaemon to Predbat add-on upgrade process](#upgrading-from-appdaemon-to-predbat-add-on) is described below.

It's recommended that you watch the [Predbat Video Guides](video-guides.md) before you start.

We have tried to make the documentation as comprehensive as possible but a level of familiarity with the basics of
Home Assistant, Add-ons, Integrations, Entities, File Editing and YAML is assumed.
There are plenty of "Home Assistant basics" tutorials on YouTube, but here are a few useful videos to introduce you to Home Assistant and displaying inverter data:

- [What is Home Assistant from Smart Home Junkie](https://www.youtube.com/watch?v=Frd-C7ZeZAo)
- [YAML Basics from This Smart Home](https://www.youtube.com/watch?v=nETF43QJebA)
- [Installing HACS from Speak to the Geek](https://www.youtube.com/watch?v=jzpm89956Pw)
- [Setting up the Energy dashboard for GivEnergy inverters from Speak to the Geek](https://www.youtube.com/watch?v=YPPpwTKIz7M)
- [Power Flow Card Plus from Speak to the Geek](https://www.youtube.com/watch?v=C4Zh35E9wJE)

If you get stuck, please read the [FAQs](faq.md) and if necessary raise a [Github ticket](https://github.com/springfall2008/batpred/issues) for support.

## Inverter Control install

You will need to install an integration to communicate with and control your inverter. The specific integration you need will depend on the brand of inverter you have:

| Brand     | Integration     | GitHub Link                                                                      |
| :-------- | :-----------     | :------------------------------------------------------------------------------- |
| GivEnergy | GivTCP           | <https://github.com/britkat1980/ha-addons>    |
| GivEnergy | GivEnergy Cloud  | <https://github.com/springfall2008/ge_cloud> |
| GivEnergy EMS | GivEnergy Cloud  | <https://github.com/springfall2008/ge_cloud> |
| Solis     | SolaX ModBus     | <https://github.com/wills106/homeassistant-solax-modbus> |
| Solax Gen4| Solax Modbus     | <https://github.com/wills106/homeassistant-solax-modbus> |
| Sofar     | Sofar MQTT       | <https://github.com/cmcgerty/Sofar2mqtt> |
| Huawei    | Huawei Modbus    | <https://github.com/wlcrs/huawei_solar> |
| SolarEdge | SolarEdge Modbus | <https://github.com/WillCodeForCats/solaredge-modbus-multi> |
| SunSynk   | SunSynk Modbus   | <https://github.com/kellerza/sunsynk> |
| Fox       | Fox Modbus       | <https://github.com/nathanmarlor/foxess_modbus> |
| LuxPower  | LuxPython        | <https://github.com/guybw/LuxPython_DEV> |

Predbat was originally written for GivEnergy inverters controlled by the GivTCP add-on but has been extended for other inverter types.

Please see [Inverter Setup](inverter-setup.md) for details on installing and configuring the appropriate inverter control software
so that Home Assistant is able to 'see' and manage your inverter.

You will need at least 24 hours of history in Home Assistant for Predbat to work correctly, the default is 7 days (but you configure this back to 1 day if you need to).

NB: If you have multiple GivEnergy AIOs or a 3-phase inverter, GivTCP version 3 is required.

## Editing Configuration Files in Home Assistant

The basic configuration for Predbat is stored in a configuration file called `apps.yaml`.
A standard template apps.yaml file will be installed as part of the Predbat installation and you will need to edit and customise this configuration file for your own system setup.

You will therefore need a method of editing configuration files within your Home Assistant environment.

There are several ways to achieve this in Home Assistant, but two of the simplest are to use either the File Editor or Studio Code Server add-ons.
Whichever you use is a personal preference. File Editor is a bit simpler, Studio Code Server is more powerful
but does require HACS (the Home Assistant Community Store) to be installed first.

If you do not have one of these file editors already installed in Home Assistant:

- For Studio Code Server you will need to [install HACS](#hacs-install) first if you don't currently have it installed
- Go to Settings / Add-ons / Add-on Store (bottom right)
- Scroll down the add-on store list, to find either 'File editor' or 'Studio Code Server' as appropriate, click on the add-on, click 'INSTALL'
- Once the editor has been installed, ensure that the 'Start on boot' option is turned on, and click 'START' to start the add-on

Thereafter whenever you need to edit a configuration file in Home Assistant you can navigate to Settings / Add-ons / *editor_you_chose_to_use* / 'OPEN WEB UI'.
You can also turn the 'Show in sidebar' option on to give a quicker way to directly access the editor.

If you are using the File Editor to edit Predbat's configuration files, you will need to turn **OFF** the **Enforce Basepath** option
to access files in different directories (i.e. within the appdaemon directory):

- From the File editor add-on page, click on the 'Configuration' tab to change this setting). It is set to 'On' by default:<BR>
![image](https://github.com/springfall2008/batpred/assets/48591903/298c7a19-3be9-43d6-9f1b-b46467701ca7)

If you are using Studio Code Server it will default to showing just files and folders in the /config directory.
To access the entire HA directory structure, click the three horizontal bars to the left of 'Explorer', File, Open Folder, type '/' (root) and click OK.

## Predbat add-on install

**Recommended**

The simplest way to install Predbat now is with the Predbat add-on.

Go to settings, add-ons, select Add-on Store, three dots on the top right, Repositories, then add the following repo
'<https://github.com/springfall2008/predbat_addon>' to the list and click close. Now refresh the list and find Predbat, click on it and click 'install'.
Ensure 'start on boot' is enabled and click 'start'.

**NOTE:** Throughout the rest of the Predbat documentation you will find reference to the Predbat configuration file `apps.yaml` and the Predbat logfile.

These are located under the Home Assistant directory `/addon_configs/6adb4f0d_predbat` which contains:

- **predbat.log** - Predbat's active logfile that reports details of what Predbat is doing, and details of any errors
- **apps.yaml** - Predbat's configuration file which will need to be customised to your system and requirements. This configuration process is described below.

You can use your file editor (i.e. 'File editor' or 'Studio Code Server' add-on) to open the directory `/addon_configs/6adb4f0d_predbat` and view these files.

If you have used the Predbat add-on installation method you do not need to install HACS or AppDaemon so you can skip directly to [Solcast install](#solcast-install) below.

The Predbat web interface will work through the Predbat add-on, you can click on the 'Web UI' button to open it once Predbat is running.

If you wish to use Docker with Predbat it is recommended you read the Docker installation instructions inside the Predbat add-on rather than going down the AppDaemon route
listed below.

## Predbat installation into AppDaemon

**Not Recommended**

**NOTE:** The Predbat web interface will not work with the AppDaemon installation method.

This is the old way of installing Predbat, firstly install HACS (the Home Assistant Community Store), then install the AppDaemon add-on,
and finally, install Predbat from HACS to run within AppDaemon.

**NOTE:** If you are using AppDaemon you now *must* set **ha_key** and **ha_url** in apps.yaml to point to your Home Assistant. The key can be obtained from HA by creating an access token.

### HACS install

Predbat and AppDaemon are available through the Home Assistant Community Store (HACS). You can install Predbat manually (see below) but its usually easier to install it through HACS.

- Install HACS if you haven't already ([https://hacs.xyz/docs/setup/download](https://hacs.xyz/docs/setup/download))
- Enable AppDaemon in HACS: [https://hacs.xyz/docs/categories/appdaemon_apps/](https://hacs.xyz/docs/categories/appdaemon_apps/)

### AppDaemon install

Predbat is written in Python and runs on a continual loop (default every 5 minutes) within the AppDaemon add-on to Home Assistant.
The next task therefore is to install and configure AppDaemon.

- Install the AppDaemon add-on [https://github.com/hassio-addons/addon-appdaemon](https://github.com/hassio-addons/addon-appdaemon)
- Once AppDaemon has finished installing, ensure that the 'Start on boot' option is turned on, then click 'START'
- You will need to edit the `appdaemon.yaml` configuration file for AppDaemon and so will need to have either
[the File Editor or Studio Code Server add-ons installed](#editing-configuration-files-in-home-assistant) first
- Find the `appdaemon.yaml` file in the directory `/addon_configs/a0d7b954_appdaemon`: ![image](https://github.com/springfall2008/batpred/assets/48591903/bf8bf9cf-75b1-4a8d-a1c5-fbb7b3b17521)
- Add to the `appdaemon.yaml` configuration file:
    - A section **app_dir** which should refer to the directory `/homeassistant/appdaemon/apps` where Predbat will be installed
    - Ensure that the **time_zone** is set correctly (e.g. Europe/London)
    - Add **thread_duration_warning_threshold: 120** in the appdaemon section
- It's recommended you also add a **logs** section and specify a new logfile location so that you can see the complete logs, I set mine
to `/homeassistant/appdaemon/appdaemon.log` and increase the logfile maximum size and number of logfile generations to capture a few days worth of logs.

Example AppDaemon config in `appdaemon.yaml`:

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

CAUTION: If you are upgrading AppDaemon from an older version to version 0.15.2 or above you need to follow these steps to ensure Predbat continues working.
These are only required if you are upgrading AppDaemon from an old version, they're not required for new installations of AppDaemon:

- Make sure you have access to the HA filesystem, e.g. I use the Samba add-on and connect to the drives on my Mac, but you can use ssh also.
- Update AppDaemon to the latest version
- Go into the directory `/addon_configs/a0d7b954_appdaemon` and edit `appdaemon.yaml`. You need to add app_dir (see above) to point to the
old location and update your logfile location (if you have set it). You should remove the line that points to secrets.yaml
(most people don't use this file) or adjust it's path to the new location (`/homeassistant/secrets.yaml`)
- Move the entire 'apps' directory from `/addon_configs/a0d7b954_appdaemon` (new location) to `/config/appdaemon` (the old location)
- Restart AppDaemon
- Check it has started and confirm Predbat is running correctly again.

### Install Predbat through HACS

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

If you install Predbat through HACS, once installed you will get automatic updates for each new release of Predbat!

- In HACS, click on Automation
- Click on the three dots in the top right corner, and choose *Custom Repositories*
- Add <https://github.com/springfall2008/batpred> as a custom repository of Category 'AppDaemon' and click 'Add'
- Click *Explore and download repositories* (bottom right), type 'Predbat' in the search box, select the Predbat Repository, then click 'Download' to install the Predbat app.

**NOTE:** Throughout the rest of the Predbat documentation you will find reference to the Predbat configuration file `apps.yaml` and the Predbat logfile.

As you are following the 'install Predbat through HACS' installation method these are located under the Home Assistant directory `/config/appdaemon/` which contains:

- **appdaemon.log** - AppDaemon and Predbat's active logfile that reports details of what Predbat is doing, and details of any errors
- **apps/batpred/config/apps.yaml** - Predbat's configuration file which will need to be customised to your system and requirements. This configuration process is described below.

### Predbat manual install

A manual install is suitable for those running Docker type systems where HACS does not function correctly and you had to manually install AppDaemon.

Note: **Not recommended if you are using HACS**

- Copy all the .py files to the `/config/appdaemon/apps/` directory in Home Assistant (or wherever you set appdaemon app_dir to)
- Copy apps/predbat/apps.yaml to the `/config/appdaemon/apps/` directory in Home Assistant (or wherever you set appdaemon app_dir to)
- Edit in Home Assistant the `/config/appdaemon/apps/apps.yaml` file to configure Predbat

- If you later install with HACS then you must move the `apps.yaml` into `/config/appdaemon/apps/predbat/config`

## Solcast Install

Predbat needs a solar forecast to predict solar generation and battery charging.
If you do have solar panels it's recommended to use the Solcast integration to retrieve your forecast solar generation.

If you don't have one already, register for a free [Solcast hobbyist account](https://solcast.com/) and enter the details of your system.
You can create 2 sites maximum under one (free hobbyist) account, if you have more aspects then it suggests you average the angle based on the number of panels
e.g. $7/10 * 240^\circ + 3/10 * 120^\circ$.

**Hybrid inverters only**: If your hybrid inverter capacity is smaller than your array peak capacity, tell Solcast that your AC capacity is equal to your DC capacity
(both equal to your array peak kW). Otherwise, Solcast will provide forecast data clipped at your inverter capacity. Let Predbat handle any necessary clipping instead.
When supplied with the unclipped Solcast forecast data, Predbat can allow in its model for PV over the inverter capacity going to battery charging
(bypassing the hybrid inverter).

You will need your API key for the next steps:

![image](https://github.com/springfall2008/batpred/assets/48591903/e6cce04f-2677-4722-b269-eb051be5c769)

### Predbat direct Solcast method

Predbat can obtain the solar forecast directly from Solcast and the Solcast integration described below is not required.

First, get your API key from the Solcast website, then as described in the [Solcast apps.yaml documentation](apps-yaml.md#solcast-solar-forecast),
uncomment the Solcast cloud interface settings in `apps.yaml` and set the API key correctly:

```yaml
solcast_host: 'https://api.solcast.com.au/'
solcast_api_key: 'xxxx'
solcast_poll_hours: 8
```

NB: If you use Predbat to obtain your Solcast solar forecast then you can't
[include the Solar Forecast within the Home Assistant Energy dashboard](https://www.home-assistant.io/dashboards/energy/#solar-production-graph)
as you can with the Solcast integration described below.<BR>
The Solcast integration also contains a 'solar dampening' feature that may be useful to reduce the solar forecast that Predbat receives at certain times of day,
e.g. if your panels are shaded by trees or buildings.

### Solcast Home Assistant integration method

Install the Solcast integration (<https://github.com/BJReplay/ha-solcast-solar>), create a free [Solcast account](https://solcast.com/),
configure details of your solar arrays, and request an API key that you enter into the Solcast integration in Home Assistant.

Predbat is configured in `apps.yaml` to automatically discover the Solcast forecast entities created by the Solcast integration in Home Assistant.

If you don't have any solar generation then use a file editor to comment out the following lines from the Solar forecast part of the `apps.yaml` configuration:

```yaml
  pv_forecast_today: re:(sensor.(solcast_|)(pv_forecast_|)forecast_today)
  pv_forecast_tomorrow: re:(sensor.(solcast_|)(pv_forecast_|)forecast_tomorrow)
  pv_forecast_d3: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_3|d3))
  pv_forecast_d4: re:(sensor.(solcast_|)(pv_forecast_|)forecast_(day_4|d4))
```

Note that Predbat does not update Solcast integration for you so you will need to create your own Home Assistant automation that updates the solar
forecast a few times a day (e.g. dawn, dusk, and just before your nightly charge slot). Keep in mind hobbyist accounts only have 10 polls per day
so the refresh period needs to be less than this. If you use the same Solcast account for other automations the total polls need to be kept under the limit or you will experience failures.

Due to the popularity of the Solcast Hobbyist service, Solcast has introduced rate limiting for Hobbyist (free) accounts. If your update gets a 429 error then this is due to rate limiting.
Solcast recommends that you poll for updated solar forecasts at random times, i.e. don't poll at precisely X o'clock and zero seconds.
The Solcast integration will auto-retry if it gets a 429 error,
but to minimise the potential rate limiting the sample Solcast automation below contains non-precise poll times for just this reason.

Example Solcast update automation script:

```yaml
alias: Solcast update
description: "Update Solcast solar forecast"
triggers:
  - trigger: time
    at:
      - "06:02:34"
      - "12:07:47"
      - "18:09:56"
      - "23:11:18"
conditions: []
actions:
  - action: solcast_solar.update_forecasts
    data: {}
mode: single
```

Manually run the automation and then make sure the Solcast integration is working in Home Assistant by going to Developer Tools / States, filtering on 'solcast',
and check that you can see the half-hourly solar forecasts in the Solcast entities.

## Energy Rates

Predbat needs to know what your electricity import and export rates are to optimise battery charging and discharging to minimise your expenditure.

These rates are configured in Predbat's `apps.yaml` configuration file. Follow the instructions in the [Energy Rates](energy-rates.md) document.

**Note:** that if you are using the Octopus integration the 'sensor.octopus_xxx' and 'event.octopus_xxx' entities must have a similar pattern of
names for Predbat to work correctly - see the [FAQ's](faq.md) if they are not.

## Configuring Predbat

You will need to use a file editor (either the File editor or Studio Code Server add-on) to edit the `apps.yaml` file in Home Assistant
to configure Predbat - see [Configuring apps.yaml](apps-yaml.md#Basics).

When Predbat starts up initially it will perform a sanity check of itself and the configuration and confirm the right files are present.
You will see this check in the log, should it fail a warning will be issued and **predbat.status** will also reflect the warning.
While the above warning might not prevent Predbat from starting up, you should fix the issue ASAP as it may cause future problems.

**Note:** If you are running the Predbat through the Predbat add-on or via Docker you will get a logfile warning message
"Warn: unable to find /addon_configs/6adb4f0d_predbat/appdaemon.yaml skipping checks as Predbat maybe running outside of AppDaemon" - this is normal and can be ignored.

## Predbat Output and Configuration Controls

As described above, the basic configuration of Predbat is held in the `apps.yaml` configuration file.

When Predbat first runs it will create a number of output and configuration control entities in Home Assistant which are used to fine-tune how Predbat operates.
The entities are all prefixed *predbat* and can be seen (and changed) from the Settings / Devices & Services / Entities list in Home Assistant.

It is recommended that you create a dashboard page with all the required entities to control Predbat
and another page to display Predbat's charging and discharging plan for your battery.

The [Output Data](output-data.md) section describes these points in more detail including using the auto-generated `predbat_dashboard.yaml` dashboard file.

The Home Assistant entity **predbat.status** contains details of what status Predbat is currently in (e.g. Idle, Charging, Error).
Detailed progress messages and error logging are written to the [Predbat logfile](output-data.md#predbat-logfile) which you can view within Home Assistant using a [file editor](#editing-configuration-files-in-home-assistant).

The [Predbat Configuration Guide](configuration-guide.md) gives an overview of the main Predbat configuration items and
detail of 'standard Predbat configuration' settings for different electricity tariff types - e.g. a cheap overnight rate,
multiple import rates during the day, and variable tariffs such as Agile, etc.

The detailed [Predbat Customisation Guide](customisation.md) details all the Predbat configuration items (switches, input numbers, etc) in Home Assistant, and what each of them does.

## Ready to light the touch-paper

By now you should have successfully installed and configured Predbat and the other components it is dependent upon
(e.g. an inverter controller such as GivTCP, Solcast solar forecast, Octopus Energy integration, etc).

![image](https://github.com/springfall2008/batpred/assets/48591903/48cffa4a-5f05-4cbc-9356-68eb3d8fb730)

You have checked the [Predbat log file](output-data.md#predbat-logfile) doesn't have any errors (there is a lot of output in the logfile, this is normal).

You have [configured Predbat's control entities](customisation.md), created some [dashboard pages to control and monitor Predbat](output-data.md#displaying-output-data),
and are ready to start Predbat generating your plan.

You may initially want to set **select.predbat_mode** to *Monitor* to see how Predbat operates, e.g. by studying the [Predbat Plan](predbat-plan-card.md).
In *Monitor* mode Predbat will monitor (but not change) the current inverter settings and predict the battery SoC based on predicted Solar Generation and House Load.<BR>
NB: In *Monitor* mode Predbat will *NOT* plan any battery charge or discharge activity of its own,
it will report on the predicted battery charge level based on the current inverter charge & discharge settings, predicted house load and predicted solar generation.

In order to enable Predbat to start generating your plan you must delete the 'template: True' line in `apps.yaml` once you are happy with your configuration.

Predbat will automatically run, analyse your house load, battery status, solar prediction, etc and produce a plan based on the current battery settings.

Check the [Predbat logfile](output-data.md#predbat-logfile) again for errors. Voluminous output is quite normal but any errors or warnings should be investigated.
Read the [Predbat FAQ's](faq.md) for answers to common questions you may have.
Also, check the [Predbat status **predbat.status**](what-does-predbat-do.md#predbat-status) - major errors will also be flagged here.

Once Predbat is running successfully the recommended next step is to start Predbat planning your inverter charging and discharging activity, but not (yet) make any changes to the inverter.
This enables you to get a feel for the Predbat plan and further [customise Predbat's settings](customisation.md) to meet your needs.

Set **select.predbat_mode** to the correct [mode of operation](customisation.md#predbat-mode) for your system - usually 'Control charge' or 'Control charge & discharge'.
Also, you should set **switch.predbat_set_read_only** to True to stop Predbat from making any changes to your inverter.

You can see the planned solar and grid charging and discharging activity in the [Predbat Plan](predbat-plan-card.md).
Another set of views can be seen in the detailed [Apex Charts showing Predbat's predictions](creating-charts.md).

Once you are happy with the plan Predbat is producing, and are ready to let Predbat start controlling your inverter charging and discharging,
set the switch **switch.predbat_set_read_only** to False and Predbat will start controlling your inverter.

## Updating Predbat

Note that any future updates to Predbat will not overwrite the `apps.yaml` configuration file that you have tailored to your setup.
If new Predbat releases introduce new features to apps.yaml you may therefore need to manually copy across the new apps.yaml settings from the [Template apps.yaml](apps-yaml.md#templates).

## Update via Home Assistant

**Recommended**

Predbat can now be updated using the Home Assistant update feature. When a new release is available you should see it in the Home Assistant settings:

![image](https://github.com/springfall2008/batpred/assets/48591903/516c77b8-7258-45e7-868f-eea40ee380ac)

Click on the update and select Install:

![image](https://github.com/springfall2008/batpred/assets/48591903/e708899d-a4aa-4bd4-b7d1-1c6687dd7e23)

## Predbat built-in update

**Recommended for manual selection of versions or automatic updates**

Predbat can now update itself, just select the version of Predbat you want to install from the **select.predbat_update** drop-down menu,
the latest version will be at the top of the list. Predbat will update itself and automatically restart.

Alternatively, if you turn on **switch.predbat_auto_update**, Predbat will automatically update itself as new releases are published on GitHub.

![image](https://github.com/springfall2008/batpred/assets/48591903/56bca491-1069-4abb-be29-a50b0a67a6b9)

Once Predbat has been installed and configured you should update Predbat to the latest version by selecting the latest version in the **select.predbat_update** selector,
or by enabling the **switch.predbat_auto_update** to auto-update Predbat.

Please note that using the internal update mechanism of Predbat will not inform HACS that Predbat has been updated. If you used HACS to install Predbat you do not need
to use it again unless your system is in need of repair.

## HACS Update

**Not Recommended**

HACS checks for updates and new releases only once a day by default, you can however force it to check again or download a specific version
by using the 'Redownload' option from the top-right three dots menu for Predbat in the HACS Automation section.

**NOTE:** If you update Predbat through HACS you may need to restart AppDaemon as it sometimes reads the config wrongly during the update.
(If this happens you will get a template configuration error in the entity **predbat.status**).<BR>
Go to Settings, Add-ons, AppDaemon, and click 'Restart'.

If you update Predbat via Home Assistant or Predbat's build-in update then HACS will not know about this and you'll continue to get messages in HACS about updating Predbat.

## Manual update of Predbat

**Expert only**

You can go to GitHub and download all the .py files from the releases tab and then manually copy these files
over the existing version in `/config/appdaemon/apps/batpred/` manually.

## Upgrading from AppDaemon to Predbat add-on

These steps assume you already have a working Predbat system and want to upgrade to using the Predbat add-on instead of using either the AppDaemon or the AppDaemon-predbat add-on.

Using the Predbat add-on is the strategic direction for Predbat and resolves some performance and data load issues that can occur with AppDaemon.
The Predbat code that runs is the same and the configuration is exactly the same, it is just changing the 'container' that Predbat runs within.

1. Before starting, watch the [installing Predbat add-on video](https://www.youtube.com/watch?v=PvGyENVJup8)

2. Although the upgrade steps are low risk, take a full backup of Home Assistant before starting

3. [Install the Predbat add-on](#predbat-add-on-install):
    - Add the Predbat add-on to the list of Repositories in the add-on store
    - Install the Predbat add-on
    - But **do not** start it - *yet*

4. [Install a file editor](#editing-configuration-files-in-home-assistant) if you don't have one already installed - either File Editor or Studio Code Server, it doesn't matter

5. Shutdown your existing AppDaemon or AppDaemon-predbat add-on:
    - Go to Settings/Add-ons
    - Click on the existing AppDaemon/AppDaemon-predbat add-on
    - Click STOP, and untick 'Start on boot'

6. Briefly start the new Predbat add-on so that it creates the addon_config folder and the template `apps.yaml` file:
    - Go to Settings/Add-ons
    - Click on the Predbat add-on
    - Click START, wait a minute for the add-on to initialise itself, then click STOP. A predbat status warning that you have a template apps.yaml file is normal and can be ignored

7. Open your file editor and open your existing `apps.yaml` file:
    - If you are using the old 'combined AppDaemon/Predbat add-on installation method' it's in the directory `/addon_configs/46f69597_appdaemon-predbat/apps`,
    or

    - with the [HACS, Appdaemon add-on then Predbat installation method](#predbat-installation-into-appdaemon), it's in `/config/appdaemon/apps/batpred/config/`

8. Select all the contents of the apps.yaml file and 'copy' (control-C, command-C, etc as appropriate)

9. Now open the template `apps.yaml` file that's supplied with the Predbat add-on and has been created in the directory `/addon_configs/6adb4f0d_predbat`,
select all the contents of the template apps.yaml file, and paste in the contents of your existing apps.yaml, overwriting the template with your specific configuration

10. Now you are ready to swap from running the AppDaemon or AppDaemon-predbat add-on to the Predbat add-on:
    - Go to Settings/Add-ons
    - Click on the existing AppDaemon/AppDaemon-predbat add-on
    - Make sure it is not running and 'Start on boot' is not ticked
    - Click the back arrow
    - Click on the Predbat add-on
    - Click START, and tick 'Start on boot'

11. If you are using the [Predbat automatic monitor](output-data.md#predbat-error-monitor) then you will need to enable the predbat_running binary sensor and change the automation,
replacing the AppDaemon add-on id (a0d7b954_appdaemon) with 'a06adb4f0d_predbat', and 'binary_sensor.appdaemon_running' with 'binary_sensor.predbat_running'.

And that's it.

You should check the Log tab to ensure it all starts properly, but it should do as you've copied over your existing configuration.

Note that if you are using the [Predbat direct connection to Solcast](#predbat-direct-solcast-method) then the Predbat add-on will need to download your solar forecast
so will use up one or two of your daily API calls (hobbyist accounts have a 10 API calls a day limit).
If you are using the [Solcast integration](#solcast-home-assistant-integration-method) then this won't be required.

You may find that the Predbat add-on installed with an older version of Predbat than you were previously using,
which might require you to [update Predbat to the correct version](#predbat-built-in-update).

11. When you are happily running the Predbat add-on you can delete the AppDaemon or AppDaemon-predbat add-on.

## Backing up Home Assistant and Predbat

It's strongly recommended that you implement an automatic mechanism to back up your Home Assistant and Predbat system.

There are several ways of backing up Home Assistant but one of the simplest is the [Home Assistant Google Drive Backup](https://github.com/sabeechen/hassio-google-drive-backup)
which is an add-on that runs every night, automatically makes a backup of Home Assistant (including Predbat), and copies that backup to a Google Drive for safekeeping.

If you create a new Google account specifically for your Home Assistant backups you will automatically get 15Gb of free Google Drive storage, enough for a couple of weeks of backups.

As well as the full Home Assistant backup you manually copy the contents of Predbat's `apps.yaml` configuration file to somewhere safe so that if you accidentally mis-edit it,
you can get Predbat working quickly again by copying it back again.

## Uninstalling Predbat

Incredible though it may be to imagine, its possible you may want to uninstall Predbat.

Removing the Predbat (or AppDaemon) add-on is easy, System / Add-ons / Predbat then select 'Uninstall'.

Its recommended that you do a full restart of Home Assistant and all add-on's after removing Predbat.

You will find that entities created by Predbat unfortunately don't get removed when you remove the Predbat add-on, and as they do not have unique Home Assistant id's, they can't be removed from the Devices & Services / Entities list.

To remove the Predbat entities you will need to use a different mechanism and purge them from Home Assistant:

- Developer Tools / Actions
- Search for 'Recorder: Purge Entities'
- Tick 'Domains to remove' and enter 'predbat' as the domain
- Tick 'Entity globs to remove' and enter '*.predbat_*'
- Tick 'Days to keep' and set to zero days

Then click 'Perform Action'

This will remove the Predbat entities.  Then do another full reboot of Home Assistant all the add-on's.
