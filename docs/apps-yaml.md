# apps.yaml settings

The basic Predbat configuration is defined in the `apps.yaml` file.

Depending on how you installed Predbat the `apps.yaml` file will be held in one of three different directories in Home Assistant:

- if you have used the [Predbat add-on installation method](install.md#predbat-add-on-install), apps.yaml will be in the directory `/addon_configs/6adb4f0d_predbat`,

- with the [HACS, Appdaemon add-on then Predbat installation method](install.md#predbat-installation-into-appdaemon), it's in `/config/appdaemon/apps/batpred/config/`, or

- if the combined AppDaemon/Predbat add-on installation method was used, it's in `/addon_configs/46f69597_appdaemon-predbat/apps`.

You will need to use a file editor within Home Assistant (e.g. either the File editor or Studio Code Server add-on's)
to edit the `apps.yaml` file - see [editing configuration files within Home Assistant](install.md#editing-configuration-files-in-home-assistant) if you need to install an editor.

This section of the documentation describes what the different configuration items in `apps.yaml` do.

When you edit `apps.yaml`, the change will automatically be detected and Predbat will be reloaded with the updated file.
You don't need to restart the Predbat or AppDaemon add-on for your edits to take effect.

## Warning! apps.yaml file format

When editing the `apps.yaml` file you must ensure that the file remains correctly formatted.  YAML files are especially finicky about how the file contents are indented
and it's very easy to end up with an incorrectly formatted file that will cause problems for Predbat.

The [YAML Basics from This Smart Home](https://www.youtube.com/watch?v=nETF43QJebA) is a good introduction video to how YAML should be correctly structured but as a brief introduction,
YAML files consist of an entity name, colon then the entity value, for example:

```yaml
timezone: Europe/London
```

Or the entity can be set to a list of values with each value being on a new line, two spaces, a dash, and then the value.  For example:

```yaml
car_charging_now_response:
  - 'yes'
  - 'on'
```

The two spaces before the dash are especially critical. It's easy to mis-edit and have one or three spaces which isn't valid YAML.

NB: the sequence of entries in `apps.yaml` doesn't matter, as long as the YAML itself is structured correctly you can move things and edit things anywhere in the file.

## Templates

You can find template configurations in the following location: <https://github.com/springfall2008/batpred/tree/main/templates>

The GivEnergy GivTCP template will be installed by default but if you are using another inverter please copy the correct template into the directory
where your `apps.yaml` is stored, replacing the existing apps.yaml file, and modify it from there.

Please read [Inverter Setup](inverter-setup.md) for inverter control software and details of setting apps.yaml for non-GivEnergy inverters

## Checking your apps.yaml

Syntax errors will be highlighted by the Home Assistant editor or via other YAML-aware editors such as VSCode.

Once you have completed your apps.yaml and started Predbat you may want to open the Predbat Web Interface and click on 'apps.yaml'. Review any items shown
in a red background as those do not match (it's okay for a 2nd inverter not to match if you only have one configured). Regular expressions that do not
match can be ignored if you are not supporting that feature (e.g. Car SOC if you don't have a car).

As an example these do not match and are shown in the web interface in red, I'm ignoring them as I only have one inverter and I'm using
the Predbat internal Solcast rather than the external integration:

![image](https://github.com/user-attachments/assets/0eda352c-c6fc-459c-abda-5c0de0b2372b)

## Basics

Basic configuration items

### prefix

Set to the prefix name to be used for all entities that Predbat creates in Home Assistant. Default 'predbat'. Unlikely that you will need to change this.

```yaml
prefix: predbat
```

### timezone

Set to your local timezone, the default is Europe/London. It must be set to a
[valid Python time zone for your location](https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568)

```yaml
timezone: Europe/London
```

### currency_symbols

Sets your symbol to use for your main currency e.g. £ or $ and for 1/100th unit e.g. p or c

```yaml
currency_symbols:
  - '£'
  - 'p'
```

### template

Initially set to True, this is used to stop Predbat from operating until you have finished configuring your apps.yaml.
Once you have made all other required changes to apps.yaml this line should be deleted or commented out:

```yaml
template: True
```

### Home Assistant connection

Predbat can speak directly to Home Assistant rather than going via AppDaemon.

If you are using a standard Predbat add-on then this will be automatic and you should normally not need to set this.
If you find you get issues where Predbat cannot communicate with Home Assistant after running for a long period of time and you get web socket errors, then creating a HA access key as described below can resolve this.

If you run Predbat in a Docker container then you will need to set the URL or IP address of Home Assistant and an access key.

The access key is a long-lived security access token you can create inside Home Assistant:

- Click on your user initials (bottom left) in HA;
- Click the Security tab
- Scroll to the bottom of the security screen and under 'Long-lived Access tokens', click 'Create Token' then copy the generated access token into ha_key in apps.yaml

![image](https://github.com/springfall2008/batpred/assets/48591903/da5916ce-4630-49b4-a265-81e8e010ff86)

Currently, if this communication is not established Predbat will fall back to AppDaemon, however, some users have experienced failures due to a 10-second timeout set by AppDaemon.

In future versions of Predbat, AppDaemon will be removed.

```yaml
ha_url: 'http://homeassistant.local:8123'
ha_key: 'xxxxxxxxxxx'
```

*TIP:* You can replace *homeassistant.local* with the IP address of your Home Assistant server if you have it set to a fixed IP address.
This will remove the need for a DNS lookup of the IP address every time Predbat talks to Home Assistant and may improve reliability as a result.

### threads

If defined sets the number of threads to use during plan calculation, the default is 'auto' which will use the same number of threads as you have CPUs in your system.

Valid values are:

- 'auto' - Use the same number of threads as your CPU count
- '0' - Don't use threads - disabled
- 'N' - Use N threads, recommended values are between 2 and 8

```yaml
threads: auto
```

## Web interface

Docker users can change the web port by setting **web_port** to a new port number, the default of 5052 must always be used for the Predbat add-on.

### notify_devices

A list of device names to notify when Predbat sends a notification. The default is just 'notify' which contacts all mobile devices

```yaml
notify_devices:
  - mobile_app_treforsiphone12_2
```

### days_previous

Predbat needs to know what your likely future house load will be to set and manage the battery level to support it.
days_previous defines a list (which has to be entered as one entry per line) of the previous days of historical house load that are to be used to predict your future daily load.<BR>
It's recommended that you set days_previous so Predbat calculates an average house load using sufficient days' history so that 'unusual' load activity
(e.g. saving sessions, "big washing day", etc) get averaged out.

For example, if you just want Predbat to assume the house load on a particular day is the same as the same day of last week:

```yaml
days_previous:
  - 7
```

Or if you want Predbat to take the average of the same day for the last two weeks:

```yaml
days_previous:
  - 7
  - 14
```

Further details and worked examples of [how days_previous works](#understanding-how-days_previous-works) are covered at the end of this document.

Do keep in mind that Home Assistant only keeps 10 days of history by default, so if you want to access more than this for Predbat you might need to increase the number of days of history
kept in HA before it is purged by editing and adding the following to the `/homeassistant/configuration.yaml` configuration file and restarting Home Assistant afterwards:

```yaml
recorder:
  purge_keep_days: 14
```

**days_previous_weight** - A list (again with one entry per line) of weightings to be applied to each of the days in days_previous.

For example, to apply a 100% weighting for the first-day entry in days_previous, but only a 50% weighting to the second day in days_previous:

```yaml
days_previous_weight:
  - 1
  - 0.5
```

The default value is 1, and all history days are equally weighted, so if you don't want to weight individual days you can simply use:

```yaml
days_previous_weight:
  - 1
```

### forecast_hours

the number of hours that Predbat will forecast, 48 is the suggested amount, although other values can be used
such as 30 or 36 if you have a small battery and thus don't need to forecast 2 days ahead.

```yaml
forecast_hours: 48
```

## Inverter information

The template `apps.yaml` for each inverter type comes pre-configured with regular expressions that should auto-discover the Home Assistant entity names for that inverter type.

If you have more than one inverter or entity names are non-standard then you will need to edit apps.yaml for your inverter entities.

### Givenergy cloud direct

Predbat now supports direct communication with the GivEnergy cloud services, with this method you can have your inverter auto-configured.
Just log into the GivEnergy web site and create an API key and copy it into the key settings. The number of inverters and their settings
will be configured automatically.

If you set **ge_cloud_automatic** to False then you can manually configure to point to Predbat's GE Cloud configuration.

If you set **ge_cloud_data** to False then Predbat will use the local data for history rather than the cloud data, you will need to wait until
you have a few days work (at least days_previous days) before this will work correctly.

```yaml
ge_cloud_data: True
ge_cloud_serial: '{geserial}'
ge_cloud_key: 'xxxxx'
ge_cloud_direct: True
ge_cloud_automatic: True
```

### num_inverters

The number of inverters you have. If you increase this above 1 you must provide multiple of each of the inverter entities

```yaml
num_inverters: 1
```

### inverter_type

inverter_type defaults to 'GE' (GivEnergy) if not set in apps.yaml, or should be set to one of the inverter types that are already pre-programmed into Predbat:

  GE: GivEnergy
  GEC: GivEnergy Cloud
  GEE: GivEnergy EMC
  GS: Ginlong Solis
  SE: SolarEdge
  SX4: Solax Gen4 (Modbus Power Control)
  SF: Sofar HYD
  SFMB: Sofar HYD with solarman modbus
  HU: Huawei Solar
  SK: Sunsynk
  SA: Solar Assistant

If you have multiple inverters, then set inverter_type to a list of the inverter types.

If you have created a [custom inverter type](inverter-setup.md#i-want-to-add-an-unsupported-inverter-to-predbat) in apps.yaml then inverter_type must be set to the same code as has been used in the custom inverter definition.

### geserial

Only for GE inverters, this is a helper regular expression to find your inverter serial number, if it doesn't work edit it manually or change individual entities to match.
If you  have more than one inverter you will need one per inverter to be used in the later configuration lines

```yaml
geserial: 're:sensor.givtcp_(.+)_soc_kwh'
geserial2: 're:sensor.givtcp2_(.+)_soc_kwh'
```

If you are running GivTCP v3 and have an 'All-In-One' (AIO) or a 3-phase inverter then the helper regular expression will not correctly work
and you will need to manually set geserial in apps.yaml to your inverter serial number, e.g.:

```yaml
geserial: 'chNNNNgZZZ'
```

*TIP:* If you have a single GivEnergy AIO, all control is directly to the AIO and the gateway is not required.<BR>
Check the GivTCP configuration to determine whether inverter 1 (the givtcp sensors) is the AIO or the gateway, or inverter 2 (the givtcp2 sensors) is the AIO or gateway.<BR>
Then in apps.yaml comment out the lines corresponding to the gateway, leaving just the givtcp or givtcp2 lines for the AIO.
Also, delete the [appropriate givtcp_rest inverter control line](#rest-interface-inverter-control) corresponding to the gateway so that Predbat controls the AIO directly.

*TIP2:* If you have multiple GivEnergy AIO's, all the AIO's are controlled by the AIO gateway and not controlled individually.<BR>
geserial should be manually configured to be your AIO gateway serial number 'gwNNNNgZZZ' and all the geserial2 lines should be commented out in apps.yaml.
You should also delete the [second givtcp_rest inverter control line](#rest-interface-inverter-control) so that Predbat controls the AIOs via the gateway.

GivTCP version 3 is required for multiple AIOs or a 3-phase inverter.

## Historical data

Predbat can either get historical data (house load, import, export and PV generation) directly from GivTCP or it can obtain it from the GivEnergy cloud.
Unless you have a specific reason to not use the GivTCP data (e.g. you've lost your GivTCP data), its recommended to use GivTCP.

### Data from Home Assistant

The following configuration entries in `apps.yaml` are pre-configured to automatically use the appropriate sensors.

If you have a 3-phase electricity supply and one inverter (and battery) on each phase then you will need to add one line for the load, import, export and PV sensors
for each of the 3 phases.

If you have a single-phase electricity supply and multiple inverters on the phase then you will need to add one line for each of the load and PV sensors.
You don't need multiple lines for the import or export sensors as each inverter will give the total import or export information.

Edit if necessary if you have non-standard sensor names:

- **load_today** - Entity name for the house load in kWh today (must be incrementing)
- **import_today** - Imported energy today in kWh (incrementing)
- **export_today** - Exported energy today in kWh (incrementing)
- **pv_today** - PV energy today in kWh (incrementing). If you have multiple inverters, enter each inverter PV sensor on a separate line.<BR>
If you have an AC-coupled inverter then enter the Home Assistant sensor for your PV inverter.<BR>
If you don't have any PV panels, comment or delete this line out of apps.yaml.

See the [Workarounds](#workarounds) section below for configuration settings for scaling these if required.

If you have multiple inverters then you may find that the load_today figures are incorrect as the inverters share the house load between them.
In this circumstance, one solution is to create a Home Assistant template helper to calculate house load from {pv generation}+{battery discharge}-{battery charge}+{import}-{export}.
The example below is defined in configuration.yaml (not the HA user interface) so it only updates every 5 minutes rather than on every underlying sensor state change:

e.g.

```yaml
# Home consumption sensor, updated every 5 minutes instead of the default of every sensor state change
template:
  - trigger:
      - platform: time_pattern
        minutes: "/5"
    sensor:
      - name: "House Load Today"
        unique_id: "house_load_today"
        unit_of_measurement: kWh
        state_class: total
        device_class: energy
        state: >
          {% set x=( states('sensor.givtcp_XXX_pv_energy_today_kwh')|float(0) + <inverter 2>...
            + states('sensor.givtcp_XXX_battery_discharge_energy_today_kwh')|float(0) + <inverter 2>...
            - states('sensor.givtcp_XXX_battery_charge_energy_today_kwh')|float(0) - <inverter 2>...
            + states('sensor.givtcp_XXX_import_energy_today_kwh')|float(0)
            - states('sensor.givtcp_XXX_export_energy_today_kwh')|float(0) )
          %}
          {{ max(x,0)|round(1) }}
```

### GivEnergy Cloud Data

If you have an issue with the GivTCP data, Predbat can get the required historical data from the GivEnergy cloud instead. This data is updated every 30 minutes.
Connecting to the cloud is less efficient and means that Predbat will be dependent upon your internet connection and the GivEnergy cloud to operate.

- **ge_cloud_data** - When True Predbat will connect to the GivEnergy cloud rather than GivTCP sensors for historical load_today, import_today and export_today inverter data
- **ge_cloud_serial** - Set the inverter serial number to use for the cloud data
- **ge_cloud_key** - Set to your API Key for the GE Cloud (long string)

If you need to create a ge_cloud_key, in the GivEnergy cloud portal:

- Click 'account settings' in the menu bar (icon of a person overlaid with a cogwheel)
- Click 'Manage Account Security' then 'Manage API Tokens' then 'Create API Token'
- Enter a name for the token e.g. 'Predbat'
- Select 'No expiry' for the token expiry duration, or choose a fixed duration but remember to create a new token before it expires as Predbat's access will stop once the token expires
- Ensure that 'api:inverter' is ticked
- Create token
- Finally, copy/paste the token created into ge_cloud_key within apps.yaml

### GivEnergy Cloud controls

*Experimental*

Predbat now supports GE Cloud controls directly from inside Predbat. When enabled Predbat will connect directly with the GE Cloud and expose
the controls of your inverter inside home assistant.

*Note* You will still have to configure apps.yaml to point to these controls.

- **ge_cloud_direct** - Set to True to enable GE Cloud direct access
- **ge_cloud_key** - Set to your API Key for the GE Cloud (long string)

## Load filtering

By default, if Predbat sees a gap in the historical load data it will fill it with average data. This is to help in the cases of small amounts of lost data.
For entire lost days you should change **days_previous** to point to different days(s) or include 3 or more days and if you set **switch.predbat_load_filter_modal** to true,
the lowest day's historical load will be discarded.

- **load_filter_threshold** - Sets the number of minutes of zero load data to be considered a gap (that's filled with average data), the default is 30.
To disable, set it to 1440.

## iBoost energy

- **iboost_energy_today** - Set to a sensor which tracks the amount of energy sent to your solar diverter, which can also be used to subtract from your historical load
for more accurate predictions.

## Inverter control configurations

- **inverter_limit** - One per inverter. When set, it defines the maximum AC output power in watts for your inverter or micro-inverters (e.g. 3600).
This is used by Predbat in calculating the plan to emulate clipping that occurs in the inverter when your solar produces more than the inverter can handle,
but it won't be that accurate as the source of the data isn't minute-by-minute.
If you have a separate Solar inverter as well then add the solar inverter limit to the battery inverter limit to give one total amount.<BR>
For example, if you have a GivEnergy hybrid inverter you should set export_limit to 3600 or 5000 depending on which size inverter you have.
If you have a GivEnergy All-in-one (6kW AC limit) and a 5kW Solis solar inverter, you should set inverter_limit to 11000 (6000+5000).
For multiple All-in-ones, add each of their limits together, plus any separate solar inverter limits.

NB: inverter_limit is ONLY used by Predbat to improve the quality of the plan, any solar clipping is done by the inverter and is not controlled by Predbat.

- **export_limit** - One per inverter (optional). When set defines the maximum watts of AC power your inverter can export to the grid at (e.g. 2500).
This is used by Predbat in calculating the plan to emulate your inverter's software export limit setting that you will have if your G98/G99
approval was lower than your maximum inverter power (check your install information for details).
If you do not set an export limit then it's the same as the inverter limit.

NB: export_limit is ONLY used by Predbat to improve the quality of the plan, any export limit is done by the inverter and is not controlled by Predbat.

- **inverter_limit_charge** and **inverter_limit_discharge** - One per inverter (optional). When set in Watts, overrides the maximum
charge/discharge rate settings used when controlling the inverter.
This can be used if you need Predbat to cap your inverter battery rate (e.g. charge overnight at a slower rate to reduce inverter/battery heating) as Predbat
will normally configure all timed charges or discharges to be at the inverter's maximum rate.

## Controlling the Inverter

There are a few different ways to control your inverter:

- Home Assistant entity controls (standard)
- GivTCP REST Interface (GivEnergy Inverters only)
- Service API
- MQTT API

### Home Assistant entity inverter control

Predbat can control inverters by updating Home Assistant entities.

The template `apps.yaml` for is pre-configured with regular expressions for many configuration items, but some of them may need updating to match your system.

If you only have a single inverter then the second inverter lines can be commented out if so desired or left in place (as they are ignored).

The **givtcp_rest** line should be commented out/deleted on anything but GivTCP REST mode.

#### Charge/Discharge rate

- **charge_rate** - Battery charge rate entity in watts
- **discharge_rate** - Battery discharge max rate entity in watts

or

- **charge_rate_percent** - Battery charge rate entity in percent of maximum rate (0-100)
- **discharge_rate_percent** - Battery discharge max rate entity in percent of maximum rate (0-100)

#### Battery Information

- **battery_power** - Current battery power in watts
- **battery_voltage** - Current battery voltage (only needed for inverters controlled via amps)
- **battery_rate_max** - Sets the maximum battery charge/discharge rate in watts (e.g. 2500)
- **soc_max** - Entity name for the maximum charge level for the battery in kWh
- **battery_min_soc** - When set limits the target SOC% setting for charge and discharge to a minimum percentage value
- **reserve** - sensor name for the reserve setting in %
- **battery_temperature** - Defined the temperature of the battery in degrees C (default is 20 if not set)

#### Power Data

- **pv_power** - Current PV power in watts
- **load_power** - Current load power in watts

#### Battery SoC

- **soc_kw** - Entity name of the battery SOC in kWh, should be the inverter one not an individual battery

or

- **soc_percent** Entity name of the battery SOC in percent of the maximum battery size, should be the inverter one not an individual battery

#### Inverter Info  

- **inverter_reserve_max** - When set defines the maximum reserve setting in % (default is 100)
- **inverter_mode** - Givenergy inverter mode control
- **inverter_time** - Inverter timestamp, used to track the last update of the inverter data
- **inverter_battery_rate_min** - Defines the minimum discharge/charge rate of the battery in watts (default is 0)

#### Schedule

- **charge_start_time** - Battery charge start time entity - can be a HA select entity in format HH:MM or HH:MM:SS or a HA time entity.
- **charge_end_time** - Battery charge end time entity - can be a HA select entity in format HH:MM or HH:MM:SS or a HA time entity.
- **charge_limit** - Entity name for used to set the SOC target for the battery in percentage (AC charge target)
- **scheduled_charge_enable** - Scheduled charge enable config
- **scheduled_discharge_enable** - Scheduled discharge enable config
- **discharge_start_time** - scheduled discharge slot_1 start time
- **discharge_end_time** - scheduled discharge slot_1 end time
- **discharge_target_soc** - Set the battery target percent for timed exports, will be written to minimum by Predbat.
- **pause_mode** - Givenergy pause mode register (if present)
- **pause_start_time** - scheduled pause start time (only if supported by your inverter)
- **pause_end_time** - scheduled pause start time (only if supported by your inverter)

If you are using REST control the configuration items should still be kept as not all controls work with REST.

*TIP:* Some older GivEnergy inverters such as the Gen 1 hybrid and AC3 inverter that have had firmware upgrades to introduce battery pause functionality do not have sufficient memory on the inverter to provide control of battery pause start and end times.
GivTCP does not recognise this and so still provides the select.givtcp_xxxx_battery_pause_start_time_slot and end_time_slot controls, but they do not work.<BR>
Predbat can report an error trying to set them, or they revert back to 00:00:00 after being changed by Predbat and there will also be errors setting these controls reported in the GivTCP log.<BR>
For these inverters the pause_start_time and pause_end_time entries should be commented out in apps.yaml to stop Predbat trying to use them.

See section below on [creating the battery charge power curve](#workarounds).

### REST Interface inverter control

For GivEnergy inverters Predbat can control the inverter directly via REST instead of via the Home Assistant GivTCP inverter controls detailed above.

When configured in apps.yaml, control communication from Predbat to GivTCP is via REST which bypasses some issues with MQTT.

- **givtcp_rest** - One entry per Inverter, sets the GivTCP REST API URL ([http://homeassistant.local:6345](http://homeassistant.local:6345)
is the normal address and port for the first inverter, and the same address but ending :6346 if you have a second inverter - if you don't have a second inverter
(or if you have multiple AIO's that are controlled through the gateway), delete the second line.<BR>
If you are using Docker then change 'homeassistant.local' to the Docker IP address.

*TIP:* You can replace *homeassistant.local* with the IP address of your Home Assistant server if you have it set to a fixed IP address.
This may improve reliability of the REST connection as it doesn't need to lookup the HA server IP address each time.

To check your REST is working open up the readData API point in a Web browser e.g: [http://homeassistant.local:6345/readData](http://homeassistant.local:6345/readData)

If you get a bunch of inverter information back then it's working!

Note that Predbat will still retrieve inverter information via REST, this configuration only applies to how Predbat controls the inverter.

### Service API

Some inverters have the Service API enabled, this allows the configuration to call an arbitrary Home Assistant service to start/stop charging and discharging

- **charge_start_service** - Should be set to a service that is called when charging starts
- **charge_freeze_service** - If your inverter supports charge freeze set to a service that starts this mode
- **charge_stop_service** - Should be set to a service that is called when charging/charge freeze stops

- **discharge_start_service**  - Should be set to a service that is called when force export (discharge) starts
- **discharge_freeze_service** - If your inverter supports export freeze set to a service that starts this mode
- **discharge_stop_service** - Should be set to a service that is called when export/export freeze stops

Services that are not configuration will not be called.

Example service is below:

```yaml
  charge_start_service:
    service: switch.turn_off
    entity_id: "switch.sunsynk_inverter_use_timer"
```

See [Service API](https://springfall2008.github.io/batpred/inverter-setup/#service-api) for details.

Note that **device_id** will be passed to the service automatically, it can be set in apps.yaml.

### MQTT API

Some Inverters are enabled with an MQTT API, in this case certain MQTT messages are send via the HA MQTT service.

The **mqtt_topic** in apps.yaml set in the root of the MQTT topic (shown as **topic** below).

#### Set reserve

Called when the reserve is changed

topic: **topic**/set/reserve
payload: reserve

#### Set target soc

Called when the target (charge %) SOC is set.

topic: **topic**/set/target_soc
payload: soc

#### Set charge rate

Called to change the charge rate in Watts

topic: **topic**/set/charge_rate
payload: charge_rate

#### Set discharge rate

Called to change the discharge rate in Watts

topic: **topic**/set/discharge_rate
payload: discharge_rate

#### Set charge

Called when a charge is started

topic: **topic**/set/charge
payload: charge_rate

#### Set discharge

Called when a forced export (discharge) is started

topic: **topic**/set/discharge
payload: discharge_rate

#### Set auto

Called when a charge/discharge is cancelled and the inverter goes back to home demand mode.

topic: **topic**/set/auto
payload: true

## Solcast Solar Forecast

As described in the [Predbat installation instructions](install.md#solcast-install), Predbat needs a solar forecast
in order to predict solar generation and battery charging which can be provided by the Solcast integration.

By default, the template `apps.yaml` is pre-configured to use the [Solcast forecast integration](install.md#solcast-home-assistant-integration-method) for Home Assistant.
The `apps.yaml` contains regular expressions for the following configuration items that should auto-discover the Solcast forecast entity names.
They are unlikely to need changing although a few people have reported their entity names don't contain 'solcast' so worth checking, or editing if you have non-standard names:

- **pv_forecast_today** - Entity name for today's Solcast forecast
- **pv_forecast_tomorrow** - Entity name for tomorrow's Solcast's forecast
- **pv_forecast_d3** - Entity name for Solcast's forecast for day 3
- **pv_forecast_d4** - Entity name for Solcast's forecast for day 4 (also d5, d6 & d7 are supported, but not that useful)

If you do not have a PV array then comment out or delete these Solcast lines from `apps.yaml`.

Alternatively, Predbat can obtain the [solar forecast directly from Solcast](install.md#predbat-direct-solcast-method) and the Solcast integration is thus not required.
Uncomment the following Solcast cloud interface settings in `apps.yaml` and set the API key correctly:

```yaml
solcast_host: 'https://api.solcast.com.au/'
solcast_api_key: 'xxxx'
solcast_poll_hours: 8
```

Note that by default the Solcast API will be used to download all sites (up to 2 for hobby accounts), if you want to override this set your sites manually using
**solcast_sites** as an array of site IDs:

```yaml
solcast_sites:
   - 'xxxx'
```

If you have more than 2 array orientations and thus more than one Solcast API key, enter each key in a list:

```yaml
api_key:
  - xxxx_API_key_1
  - yyyy_API_key_2
```

Keep in mind hobbyist accounts only have 10 polls per day so you need to ensure that the solcast_poll_hours refresh period is set so that you do not exceed the 10 poll limit.
If you have two arrays then each Solcast refresh will consume 2 polls so its suggested that you set solcast_poll_hours to 4.8 to maximise your polls over a 24 hour period (5 polls a day, 24/5=poll every 4.8 hours).
If you use the same Solcast account for other automations the total polls need to be kept under the limit or you will experience failures.

If you use the same Solcast account for other automations the poll frequency will need to be reduced to ensure the total polls is kept under your account daily poll limit or you will experience failures.

If you have multiple PV arrays connected to hybrid inverters or you have AC-coupled inverters, then ensure your PV configuration in Solcast covers all arrays.

If however, you have a mixed PV array setup with some PV that does not feed into the inverters that Predbat is managing
(e.g. hybrid GE inverters with older firmware but a separate older FIT array that directly feeds AC into the house),
then it's recommended that Solcast is only configured for the PV connected to the inverters that Predbat is managing.<BR>
NB: Gen2, Gen3 and Gen1 hybrid inverters with the 'fast performance' firmware can charge their batteries from excess AC that would be exported,
so for these inverters, you should configure Solcast with your total solar generation capability.

Solcast produces 3 forecasted PV estimates, the 'central' (50% or most likely to occur) PV forecast, the '10%' (1 in 10 more cloud coverage 'worst case') PV forecast,
and the '90%' (1 in 10 less cloud coverage 'best case') PV forecast.<BR>
By default, Predbat will use the central (PV50) estimate and apply to it the **input_number.predbat_pv_metric10_weight** weighting of the 10% (worst case) estimate.
You can thus adjust the metric10_weight to be more pessimistic about the solar forecast.

Predbat models cloud coverage by using the difference between the PV and PV10 forecasts to work out a cloud factor,
this modulates the PV output predictions up and down over the 30-minute slot as if there were passing clouds.
This can have an impact on planning, especially for things like freeze charging which could assume the PV will cover the house load but it might not due to clouds.

- **pv_estimate** in `apps.yaml` can be used to configure Predbat to always use the 10% forecast by setting the configuration item to '10',
or '90' to always use the 90% PV estimate (not recommended!).<BR>
Set to blank or delete / comment out the line to use the default central estimate.

If **pv_estimate** is set to 10 then **input_number.predbat_pv_metric10_weight** in Home Assistant should be set to 1.0.

See also [PV configuration options in Home Assistant](customisation.md#solar-pv-adjustment-options).

## Energy Rates

There are a number of configuration items in `apps.yaml` for telling Predbat what your import and export rates are.

These are described in detail in [Energy Rates](energy-rates.md) and are listed here just for completeness:

- **metric_octopus_import** - Import rates from the Octopus Energy integration
- **metric_octopus_export** - Export rates from the Octopus Energy integration
- **metric_octopus_gas** - Gas rates from the Octopus Energy integration
- **octopus_intelligent_slot** - Octopus Intelligent GO slot sensor from the Octopus Energy integration
- **octopus_saving_session** - Energy saving sessions sensor from the Octopus Energy integration
- **octopus_saving_session_octopoints_per_penny** - Sets the Octopoints per pence
- **rates_import_octopus_url** - Octopus pricing URL (over-rides metric_octopus_import)
- **rates_export_octopus_url** - Octopus export pricing URL (over-rides metric_octopus_export)
- **metric_standing_charge** - Standing charge in pounds
- **rates_import** - Import rates over a 24-hour period with start and end times
- **rates_export** - Export rates over a 24-hour period with start and end times
- **rates_gas** - Gas rates over a 24-hour period with start and end times
- **rates_import_override** - Over-ride import rate for specific date and time range, e.g. Octopus Power-up events
- **rates_export_override** - Over-ride export rate for specific date and time range
- **futurerate_url** - URL of future energy market prices for Agile users
- **futurerate_adjust_import** and **futurerate_adjust_export** - Whether tomorrow's predicted import or export prices should be adjusted based on market prices or not
- **futurerate_peak_start** and **futurerate_peak_end** - start/end times for peak-rate adjustment
- **carbon_intensity** - Carbon intensity of the grid in half-hour slots from an integration.

## Car Charging Integration

Predbat can include electric vehicle charging in its plan and manage the battery activity so that the battery isn't discharged into your car when the car is charging
(although you can override this if you wish by setting the **switch.predbat_car_charging_from_battery** to True in Home Assistant).

There are two different ways of planning car charging into cheap slots with Predbat, either by the Octopus Energy integration or by Predbat identifying the cheapest slots.
These approaches and the set of settings that need to be configured together are described in [Car Charging](car-charging.md).

The full list of car charging configuration items in `apps.yaml` that are used to plan car charging activity within Predbat are described below.
The Home Assistant controls (switches, input numbers, selectors, etc) related to car charging are described in [Car Charging configuration within Home Assistant](car-charging.md),
with a brief mention of pertinent controls included here alongside the apps.yaml configuration items where relevant for context.

- **num_cars** should be set in apps.yaml to the number of cars you want Predbat to plan for.
Set to 0 if you don't have an EV (and the remaining car sensors in apps.yaml can safely be commented out or deleted as they won't be required).<BR>
NB: num_cars must be set correctly regardless of whether you are using Octopus Intelligent Go to control your EV charging or Predbat to control the charging;
or else Predbat could start discharging your battery when the EV is charging.

- **car_charging_exclusive** should be set to True for each car in apps.yaml if you have multiple cars configured in Predbat, but only one car charger.
This indicates that only one car may charge at once (the first car reporting as plugged in will be considered as charging).
If you set this to False for each car then it is assumed that the car can charge independently, and hence two or more cars could charge at once.
One entry per car.

```yaml
car_charging_exclusive:
  - True
  - True
```

### Car Charging Filtering

Depending upon how the CT clamps and your inverter and electric car charger have been wired, your inverter may 'see' your EV charging as being part of the house load.  This means your house load is artificially raised whenever you charge your car.
In this circumstance you might want to remove your electric car charging data from the historical house load data so as to not bias the calculations, otherwise you will get
high battery charge levels when the car was charged previously (e.g. last week).

*TIP:* Check the house load being reported by your inverter when your car is charging. If it doesn't include the car charging load then there is no need to follow these steps below (and if you do, you'll artificially deflate your house load).

- **switch.predbat_car_charging_hold** - A Home Assistant switch that when turned on (True) tells Predbat to remove car charging data from your historical house load
so that Predbat's battery prediction plan is not distorted by previous car charging.

- **car_charging_energy** - Set in `apps.yaml` to point to a Home Assistant entity which is the daily incrementing kWh data for the car charger.
This has been pre-defined as a regular expression that should auto-detect the appropriate Wallbox and Zappi car charger sensors,
or edit as necessary in `apps.yaml` for your charger sensor.<BR>
Note that this must be configured to point to an 'energy today' sensor in kWh not an instantaneous power sensor (in kW) from the car charger.<BR>
*TIP:* You can also use **car_charging_energy** to remove other house load kWh from the data Predbat uses for the forecast,
e.g. if you want to remove Mixergy hot water tank heating data from the forecast such as if you sometimes heat on gas, and sometimes electric depending upon import rates.<BR>
car_charging_energy can be set to a list of energy sensors, one per line if you have multiple EV car chargers, or want to exclude multiple loads, e.g.:

```yaml
  car_charging_energy:
    - 're:(sensor.myenergi_zappi_[0-9a-z]+_charge_added_session|sensor.wallbox_portal_added_energy)'
    - sensor.mixergy_ID_energy
```

- **input_number.predbat_car_charging_energy_scale** - A Home Assistant entity used to define a scaling factor (in the range of 0.1 to 1.0)
to multiply the car_charging_energy sensor data by if required (e.g. set to 0.001 to convert Watts to kW).

If you do not have a suitable car charging energy kWh sensor in Home Assistant then comment the car_charging_energy line out of `apps.yaml` and configure the following Home Assistant entity:

- **input_number.predbat_car_charging_threshold** (default 6 = 6kW)- Sets the kW power threshold above which home consumption is assumed to be car charging
and **input_number.predbat_car_charging_rate** will be subtracted from the historical load data.

### Planned Car Charging

These features allow Predbat to know when you plan to charge your car.

If you have an Intelligent Octopus tariff then planning of charging is done via the Octopus app and Predbat obtains this information through the Octopus Energy integration in Home Assistant.

- **switch.predbat_octopus_intelligent_charging** - When this Home Assistant switch is enabled, Predbat will plan charging around the Intelligent Octopus slots, taking
it into account for battery load and generating the slot information

The following `apps.yaml` configuration items are pre-defined with regular expressions to point to appropriate sensors in the Octopus Energy integration.
You should not normally need to change these if you have the Octopus Intelligent tariff:

- **octopus_intelligent_slot** - Points to the Octopus Energy integration 'intelligent dispatching' sensor that indicates
whether you are within an Octopus Energy "smart charge" slot, and provides the list of future planned charging activity.

- **octopus_ready_time** - Points to the Octopus Energy integration sensor that details when the car charging will be completed.<BR>
*Note:* the Octopus Integration now provides [Octopus Intelligent target time](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/#target-time-time) in two formats, either a 'select' entity or a 'time' entity.
Predbat uses the time entity (time.octopus_energy_{{ACCOUNT_ID}}_intelligent_target_time) which is disabled by default, so you will need to enable the time entity and disable the matching select entity.

- **octopus_charge_limit** - Points to the Octopus Energy integration sensor that provides the car charging limit.

- **octopus_slot_low_rate** - Default is True, meaning any Octopus Intelligent Slot reported will be at the lowest rate if at home. If False the existing rates only will be used
which is only suitable for tariffs other than IOG.

If you don't use Intelligent Octopus then the above 3 Octopus Intelligent configuration lines in `apps.yaml` can be commented out or deleted,
and there are a number of other apps.yaml configuration items that should be set:

- **car_charging_planned** - Optional, can be set to a Home Assistant sensor (e.g. from your car charger integration)
which lets Predbat know the car is plugged in and planned to charge during low-rate slots.
Or manually set it to 'False' to disable this feature, or 'True' to always enable it.<BR>
The `apps.yaml` template supplied with Predbat comes pre-configured with a regular expression that should automatically match Zappi or Wallbox car chargers.
If you have a different type of charger you will need to configure it manually.

- **car_charging_planned_response** - An array of values for the above car_charging_planned sensor which indicate that the car is plugged in and will charge in the next low rate slot.
The template `apps.yaml` comes with a set of pre-defined sensor values that should match most EV chargers.
Customise for your car charger sensor if it sets sensor values that are not in the list.

- **car_charging_now** - For some cases finding details of planned car charging is difficult.<BR>
The car_charging_now configuration item can be set to point to a Home Assistant sensor that tells you that the car is currently charging.
Predbat will then assume this 30-minute slot is used for charging regardless of the plan.<BR>
If Octopus Intelligent Charging is enabled and car_charging_now indicates the car is charging then Predbat will also assume that this is a
low rate slot for the car/house (and might therefore start charging the battery), otherwise electricity import rates are taken from the normal rate data.<BR>
WARNING: Some cars will briefly start charging as soon as they are plugged in, which Predbat will detect and assume that this is a low rate slot even when it isn't.
It is therefore recommended that you do NOT set car_charging_now unless you have problems with the Octopus Intelligent slots, and car_charging_now should be commented out in `apps.yaml`.

**CAUTION:** Do not use car_charging_now with Predbat led charging or you will create an infinite loop. Do you use car_charging_now with Octopus intelligent
unless you can't make it work any other way as it will assume all car charging is at a low rate.

- **car_charging_now_response** - Set to the range of positive responses for car_charging_now to indicate that the car is charging.
Useful if you have a sensor for your car charger that isn't binary.

To make planned car charging more accurate, configure the following items in `apps.yaml`:

- **car_charging_battery_size** - Set this value in `apps.yaml` to the car's battery size in kWh which *must* be entered with one decimal place, e.g. 50.0.
If not set, Predbat defaults to 100.0kWh. This will be used to predict when to stop car charging.

- **car_charging_limit** - You should configure this to point to a sensor that specifies the % limit the car is set to charge to.
This could be a sensor on the EV charger integration or a Home Assistant helper entity you can set as you wish.
If you don't specify a sensor Predbat will default to 100% - i.e. fill the car to full.

- **car_charging_soc** - You should configure this to point to a sensor (on the HA integration for your EV charger) that specifies the car's current charge level
expressed as a percentage - it must NOT be set to a sensor that gives the car's current kWh value as this will cause Predbat to charge the car to an incorrect level.
If you don't specify a sensor, Predbat will default to 0%.

### Multiple Electric Cars

Multiple cars can be planned with Predbat, in which case you should set **num_cars** in `apps.yaml` to the number of cars you want to plan

- **car_charging_limit**, **car_charging_planned**, **car_charging_battery_size** and **car_charging_soc** must then be a list of values (i.e. 2 entries for 2 cars)

- If you have Intelligent Octopus then Car 0 will be managed by the Octopus Energy integration, if it's enabled

- Each car will have its own Home Assistant slot sensor created e.g. **binary_sensor.predbat_car_charging_slot_1**,
SoC planning sensor e.g **predbat.car_soc_1** and **predbat.car_soc_best_1** for car 1

## Load Forecast

In addition to the historical house load data that Predbat uses by default, you can optionally provide a forecast of future load
such as is produced by [Predheat for Hot water and Heat Pump heating systems](https://github.com/springfall2008/predheat) or via [Predai](https://github.com/springfall2008/predai)

- **load_forecast** - this should be configured to point to a sensor and attribute. The attribute must be in either
    - The format of 'last_updated' timestamp and 'energy' in incrementing kWh.
    - The format of a dictionary of timestamps and energy data in incremental KWh.

For example:<BR>
![IMAGE](images/load_forecast.png)

Or

![image](https://github.com/springfall2008/batpred/assets/48591903/5ac60be6-7a96-4caf-b53c-f097674e347f)

`apps.yaml` should be configured to point to the forecast sensor and attribute (in the above formats) like this:

```yaml
load_forecast:
  - sensor_name$attribute_name
```

So if using Predheat it would be configured as:

```yaml
load_forecast:
  - predheat.heat_energy$external
```

Set **load_forecast_only** to True if you do not wish to use the Predbat forecast but instead want to use this as your only forecast data e.g using PredAi:

```yaml
load_forecast_only: True
load_forecast:
   - sensor.givtcp_{geserial}_load_energy_today_kwh_prediction$results
```

## Balance Inverters

When you have two or more inverters it's possible they get out of sync so they are at different charge levels or they start to cross-charge (one discharges into another).
When enabled, balance inverters try to recover this situation by disabling either charging or discharging from one of the batteries until they re-align.

Most of the Predbat configuration for balancing inverters is through a number of [Home Assistant controls for Balancing Inverters](customisation.md#balance-inverters),
but there is one configuration item in `apps.yaml`:

```yaml
balance_inverters_seconds: seconds
```

Defines how often to run the inverter balancing, 30 seconds is recommended if your machine is fast enough, but the default is 60 seconds.

## Workarounds

There are a number of different configuration items in `apps.yaml` that can be used to tweak the way Predbat operates and workaround
weirdness you may have from your inverter and battery setup.

### Clock skew

```yaml
clock_skew: minutes
```

Skews the local (computer) time that Predbat uses (from the computer that Predbat is running on).<BR>
Set to 1 means add a minute to the Predbat computer time, set to -1 means take a minute off the Predbat computer time.
This clock adjustment will be used by Predbat when real-time actions happen e.g. triggering a charge or discharge.

If your inverter's time is different to the time on the computer running Home Assistant, you may need to skew the time settings made on the inverter when you trigger charging or discharging.
Again 1 means the inverter is 1 minute fast and -1 means the inverter is 1 minute slow.

Separate start and end options are applied to the start and end time windows, mostly as you want to start battery activity late (not early) and finish early (not late).

You can adjust the charge and discharge times written to the inverter by setting the following in `apps.yaml`:

```yaml
inverter_clock_skew_start: minutes
inverter_clock_skew_end: minutes
```

Skews the setting of the charge slot registers vs the predicted start time

```yaml
inverter_clock_skew_discharge_start: minutes
inverter_clock_skew_discharge_end: minutes
```

Skews the setting of the discharge slot registers vs the predicted start time

### Battery size scaling

```yaml
battery_scaling:
  - scale
```

Default value 1.0. Multiple battery size scales can be entered, one per inverter on separate lines.

This setting is used to scale the battery-reported SoC kWh to make it appear bigger or larger than it is.
As the GivEnergy inverters treat all batteries attached to an inverter as in effect one giant battery,
if you have multiple batteries on an inverter that need scaling you should enter a composite scaling value for all batteries attached to the inverter.

*TIP:* If you have a GivEnergy 2.6 or 5.2kWh battery then it will have an 80% depth of discharge but it will falsely report its capacity as being the 100% size,
so set battery_scaling to 0.8 to report the correct usable capacity figure to Predbat.

*TIP:* Likewise, if you have one or multiple GivEnergy All-in-Ones (AIOs),
it will incorrectly report the 13.5kWh usable capacity of each AIO as 15.9kWh, so set battery_scaling to 0.85 to correct this.

If you are going to chart your battery SoC in Home Assistant then you may want to use **predbat.soc_kw_h0** as your current SoC (as this will be scaled)
rather than the usual *givtcp_SERIAL_NUMBER_soc* GivTCP entity so everything lines up.

### Import export scaling

```yaml
import_export_scaling: scale
```

Default value 1.0. Used to scale the import & export kWh data from GivTCP if the inverter information is incorrect.

### Inverter rate minimum

```yaml
inverter_battery_rate_min: watts
```

One per inverter (optional), set in Watts, when set models a "bug" in the inverter firmware
in some models where if charge or discharge rates are set to 0 you actually get a small amount of charge or discharge.
The recommended setting is 200 for Gen 1 hybrids with this issue.

### Inverter reserve maximum

```yaml
inverter_reserve_max: percent
```

Global, sets the maximum reserve % that may be set to the inverter, the default is 98, as some Gen 2 & Gen 3 inverters and
AIO firmware versions refuse to be set to 100.  Comment the line out or set it to 100 if your inverter allows setting it to 100%.

## Automatic restarts

If the add-on that is providing the inverter control stops functioning it can prevent Predbat from functioning correctly.
In this case, you can tell Predbat how to restart the add-on using a service.

Right now only communication loss with GE inverters is detectable but in the future other systems will be supported.

When enabled if communication is lost then the service configured will be triggered and can cause a restart which may restart the connection.
This may be useful with GivTCP if you have time sync errors or lose the REST service every now and again.

The auto_restart itself is a list of commands to run to trigger a restart.

- The **shell** command will call a 'sh' shell and can be used to delete files and suchlike.
- The **service** command is used to call a service and can contain arguments of **addon** and/or **entity_id**. The configuration below is for GivTCP v3.

```yaml
auto_restart:
  - shell: 'rm -rf /homeassistant/GivTCP/*.pkl'
  - service: hassio/addon_restart
    addon: 533ea71a_givtcp
```

NB: If you are running GivTCP v2 then the line '533ea71a_givtcp' must be replaced with 'a6a2857d_givtcp'
as the slug-id (Home Assistant add-on identifier) is different between GivTCP v2 and v3.

## Battery charge/discharge curves

Some batteries tail off their charge rate at high SoC% or their discharge rate at low SoC%, and these optional configuration items enable you to model this tail-off in Predbat.
Note that the charge/discharge curves *only* affect the accuracy of the charging/discharging model Predbat applies in the forward battery plan,
Predbat will still instruct the inverter to charge/discharge at full rate regardless of the charging curve.

If you know the battery charge or discharge curves (e.g. manufacturer info or your own testing) then you can manually configure this in apps.yaml,
or Predbat can calculate the curves based on historical inverter charging/discharging data in Home Assistant.

If the battery has not recently been fully charged or fully discharged then Predbat will not be able to calculate the curves and you'll get a warning in the logfile.

- **battery_charge_power_curve** - This optional configuration item enables you to model in Predbat a tail-off in charging at high SoC%.

Enter the charging curve as a series of steps of % of max charge rate for each SoC percentage.

The default is 1.0 (full power) charge to 100%.

Modelling the charge curve becomes important if you have limited charging slots (e.g. only a few hours a night) or you wish to make accurate use of the
[low power charging mode](customisation.md#inverter-control-options) (**switch.predbat_set_charge_low_power**).

If the battery_charge_power_curve option is *not* set in apps.yaml and Predbat performs an initial run (e.g. due to restarting the Predbat/AppDaemon add-on,
or an edit being made to apps.yaml), then Predbat will automatically calculate the charging curve for you from historical battery charging information.

You should look at the [Predbat logfile](output-data.md#predbat-logfile) to find the predicted battery charging curve and copy/paste it into your `apps.yaml` file.
The logfile will also include a recommendation for how to set your **battery_rate_max_scaling** setting in HA.

The YouTube video [charging curve and low power charging](https://youtu.be/L2vY_Vj6pQg)
explains how the curve works and shows how Predbat automatically creates it.

Setting this option to **auto** will cause the computed curve to be stored and used automatically. This is not recommended if you use low power charging mode as your
history will eventually not contain any full power charging data to compute the curve, so in this case it's best to manually configure the charge curve in apps.yaml.

NB: For Predbat to calculate your charging curve it needs to have access to historical Home Assistant data for battery_charge_rate, battery_power and soc_kw.
These must be configured in apps.yaml to point to Home Assistant entities that have appropriate history data for your inverter/battery.

If you have a GivEnergy inverter and are using the recommended default [REST mode to control your inverter](#inverter-control-configurations)
then you will need to uncomment out the following entries in `apps.yaml`:

```yaml
charge_rate:
  - number.givtcp_{geserial}_battery_charge_rate
battery_power:
  - sensor.givtcp_{geserial}_battery_power
soc_kw:
  - sensor.givtcp_{geserial}_soc_kwh
```

Example charging curve from a GivEnergy 9.5kWh battery with the latest firmware and Gen 1 inverter:

```yaml
battery_charge_power_curve:
  91 : 0.91
  92 : 0.81
  93 : 0.71
  94 : 0.62
  95 : 0.52
  96 : 0.43
  97 : 0.33
  98 : 0.24
  99 : 0.24
  100 : 0.24
```

- **battery_discharge_power_curve** - This optional configuration item enables you to model in Predbat a tail-off in discharging at low SoC%.

Enter the discharging curve as a series of steps of % of max discharge rate for each SoC percentage.

The default is 1.0 (full power) discharge to 0%.

If the battery_discharge_power_curve option is *not* set in apps.yaml and Predbat performs an initial run (e.g. due to restarting the Predbat/AppDaemon add-on,
or an edit being made to apps.yaml), then Predbat will automatically calculate the discharging curve for you from historical battery discharging information.

You should look at the [Predbat logfile](output-data.md#predbat-logfile) to find the predicted battery discharging curve and copy/paste it into your `apps.yaml` file.

Setting This option to **auto** will cause the computed curve to be stored and used automatically. This may not work very well if you don't do regular discharges to empty the battery.

In the same way, as for the battery charge curve above, Predbat needs to have access to historical Home Assistant data for battery_discharge_rate, battery_power and soc_kw.
These must be configured in apps.yaml to point to Home Assistant entities that have appropriate history data for your inverter/battery.

If you are using REST mode to control your GivEnergy inverter then the following entries in `apps.yaml` will need to be uncommented :

```yaml
discharge_rate:
  - number.givtcp_{geserial}_battery_discharge_rate
battery_power:
  - sensor.givtcp_{geserial}_battery_power
soc_kw:
  - sensor.givtcp_{geserial}_soc_kwh
```

## Battery temperature curves

Your battery's maximum charge and discharge rate can be impacted by cold weather, Predbat can predict this if you provide a temperature sensor and define a curve.

- You must make sure battery_temperature is defined (one per inverter).
- Set **battery_temperature_history** to a sensor with history, this will be used to predict future temperatures based on past changes
- Set **battery_temperature_charge_curve** to define the maximum charge rate in C which is a percentage of your battery capacity.
- Set **battery_temperature_discharge_curve** to define the maximum discharge rate in C which is a percentage of your battery capacity.

An example for GivEnergy Gen2 battery is below.

*Note* You must adjust the curve for your system.
gaps in the curve above 20 will use 20 degrees, and gaps below 0 will use 0 degrees. Do not leave gaps in the curve between 20 and 0.

```yaml
  # Battery temperature charge adjustment curve
  # Specific in C which is a multiple of the battery capacity
  # e.g. 0.33 C is 33% of the battery capacity
  # values unspecified will be assumed to be 1.0 hence rate is capped by the max charge rate
  battery_temperature_history: sensor.givtcp_battery_stack_1_bms_temperature
  battery_temperature_charge_curve:
    20: 0.50
    19: 0.33
    18: 0.33
    17: 0.33
    16: 0.33
    15: 0.33
    14: 0.33
    13: 0.33
    12: 0.33
    11: 0.33
    10: 0.25
    9: 0.25
    8: 0.25
    7: 0.25
    6: 0.25
    5: 0.25
    4: 0.25
    3: 0.25
    2: 0.25
    1: 0.15
    0: 0.00
```

## Alert System

Predbat can take data directly from the Meteo-Alarm feed and use it to trigger keeping your battery charged so you have power in the event of a power cut.

Please look at their web site for more details. The apps.yaml must be configured to select the URL for your country.

The event severity and certainty are all regular expressions and can be set to one or multiple values using regular expression syntax.
Any unset values are ignored.

Your location (from Home Assistant) is used to filter alerts that apply only to your area.

Events that match the given criteria will try to keep your battery at the percentage level specified by keep (default 100%) during the entire event period.
This works by using a much stronger version of best_soc_keep but only for that time period.

Your Predbat status will also have [Alert] in it during the alert time period and the triangle alert symbol will show on your HTML plan for the time period
of the alert.

```yaml
  # Alert feeds - customise to your country, the alert types, severity and keep value
  # Customise to your needs, delete the ones you don't want to trigger on - e.g. remove Amber, Moderate and Possible.
  alerts:
    url: "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom"
    event: "(Amber|Yellow|Orange|Red).*(Wind|Snow|Fog|Rain|Thunderstorm|Avalanche|Frost|Heat|Coastal event|Flood|Forestfire|Ice|Low temperature|Storm|Tornado|Tsunami|Volcano|Wildfire)"
    severity: "Moderate|Severe|Extreme"
    certainty: "Possible|Likely|Expected"
    keep: 40
```

![image](https://github.com/user-attachments/assets/4d1e0a59-c6f8-4fb1-9c89-51aedfa77755)

## Triggers

- **export_triggers** - The export trigger feature is useful to help trigger your own automation based on Predbat predicting in the plan
that you will have spare solar energy that would be exported - this could happen if the battery is full or there is more predicted solar generation than can be charged into the battery.
You can use the trigger in an automation, for example, you could turn on an immersion heater or the washing machine to consume the excess solar power.

The triggers count export energy until the next active charge slot only.

For each trigger give a name, the minutes of export needed, and the energy required in that time.

Multiple triggers can be enabled by Predbat at once so in total you could use too much energy if multiple triggered automations all run.

Each trigger specified in `apps.yaml` will create a Home Assistant entity called 'binary_sensor.predbat_export_trigger_*name*'
which will be turned on when the predicted trigger conditions are valid.
Connect this binary sensor to your automation to start whatever you want to trigger.

Set the name for each trigger, the number of minutes of solar export you need, and the amount of energy in kWh you will need available during that period in apps.yaml:

For example:

```yaml
export_triggers:
  - name: "large"
    minutes: 60
    energy: 1.0
  - name: "small"
    minutes: 15
    energy: 0.25
```

**Note:** Predbat will set an export trigger to True if in the plan it predicts
that there will be more than the specified amount of excess solar energy over the specified time.<BR>
In the example above, the 'large' trigger will be set to True for the 1-hour period where Predbat predicts
that there will be a *total* of 1kWh of excess solar generation *over that period*.
For clarity the trigger is not set based on actual excess solar generation or export.<BR>
It should also be recognised that this prediction could be wrong; there could be less solar generation or more house load than was predicted in the plan.

If you wish to trigger activities based on Predbat charging or discharging the battery rather than spare solar energy you can instead use the following binary sensors in Home Assistant:

- **binary_sensor.predbat_charging** - Will be True when the home battery is inside a charge slot (either being charged or being held at a level).
Note that this does include charge freeze slots where the discharge rate is set to zero without charging the battery.

- **binary_sensor.predbat_exporting** - Will be True when the home battery is inside a force discharge slot. This does not include
discharge freeze slots where the charge rate is set to zero to export excess solar only.

## Understanding how days_previous works

As described earlier, **days_previous** is a list of the previous days of historical house load that are averaged together to predict your future daily load.

e.g., if you want the average of the same day for the last 2 weeks:

```yaml
days_previous:
  - 7
  - 14
```

This section describes in more detail how days_previous is used by Predbat in creating the future battery plan, and gives some worked examples and a 'gotcha' to be aware of.

When Predbat forecasts future home demand it counts backwards the days_previous number of days to find the appropriate historical home consumption.
This is best explained through a worked example:

In this example, days_previous is set to use history from 2 days ago:

```yaml
days_previous:
  - 2
```

If right now today it's Monday 3:15pm and Predbat is predicting the forward plan for the next 48 hours:

- For tomorrow's (Tuesday) 9am slot, Predbat will look backwards 2 days from Tuesday so will use the historical home consumption from Sunday 9am
as being the predicted load for Tuesday 9am.
- For the day after (Wednesday) 9am slot, Predbat again looks backwards 2 days from that day, so will use historical home consumption from Monday 9am as being the Wednesday 9am prediction.

This pattern of counting backwards days_previous days to find the appropriate time slot to load historical home consumption from
requires Predbat to operate some additional special processing if days_previous is set to a low value or forecast_hours to a high value.

Extending the previous example but this time days_previous is set to use history from just the previous day:

```yaml
days_previous:
  - 1
```

Today it's still Monday 3:15pm and Predbat is predicting the forward plan for the next 48 hours:

- For tomorrow's (Tuesday) 9am slot, Predbat will look backwards 1 day from Tuesday so will use the historical home consumption from today's (Monday) 9am
as being the predicted load for Tuesday 9am.
- For the day after (Wednesday) 9am slot, Predbat again looks backwards 1 day from that day,
so looks for historical home consumption from Tuesday 9am as being the Wednesday 9am prediction,
but of course, it's still Monday, and Tuesday hasn't happened yet so we can't know what that historical consumption was!<BR>
What Predbat does in this circumstance is to subtract a further day from days_previous and for Wednesday 9am's prediction, it will therefore use the historical load from Monday 9am.

This issue of finding future historical load only occurs when days_previous is set to 1 and Predbat is forecasting more than 24 hours from 'now'.
So to highlight this with some edge cases, today is still Monday 3:15pm, days_previous is still set to '1' and in the forward plan:

- For tomorrow's (Tuesday) 2:30pm slot, Predbat looks backwards 1 day from Tuesday and takes the historical home consumption from today's (Monday) 2:30pm slot.
- For tomorrow's (Tuesday) 3:00pm slot, Predbat looks backwards 1 day and takes the historical load from today's (Monday) 3:00pm slot - which we are only part way through
so only 15 minutes of load will be predicted for tomorrow 3pm.
- For tomorrow's (Tuesday) 3:30pm slot, Predbat looks backwards 1 day but the 3:30pm slot today hasn't yet occurred so Predbat will take the historical load from the prior day
and has to use Sunday's 3:30pm load for tomorrow's prediction.
- Ditto the predicted load for tomorrow's (Tuesday) 4:00pm slot comes from Sunday 4pm.

As today rolls forward and Predbat keeps on updating the forward plan every 5 minutes the prediction will be updated with the correct previous_day history as and when it exists.

It's recommended therefore that days_previous isn't set to 1, or if it is, that you understand the way this has to work and the consequences.
If you want to set days_previous to take an average of the house load over all the days of the last week it's suggested that it be set as:

```yaml
days_previous:
  - 2
  - 3
  - 4
  - 5
  - 6
  - 7
  - 8
```
