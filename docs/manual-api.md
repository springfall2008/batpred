# Predbat automation API

**CAUTION** This is an expert feature only, you can break Predbat if you set the wrong things here.

While for most people Predbat will do what you want without any adjustments there are some special cases where users wish to write some more complex
automations which override Predbat settings

For settings inside Home Assistant e.g. switch.predbat_*, select.predbat_* and input_number.predbat_* you can already use an automation to change these values

For settings in apps.yaml it's very difficult or impossible to update them via an automation.

For this reason, there is a selector called **select.predbat_manual_api** which works a bit like the manual override ones but this can have new values added using the select API in Home Assistant.
The only function the selector itself serves is to store override commands, you can clear from the selector but you have to set them using a service call.

Certain settings from apps.yaml may be overridden using this method.

Each override is in a string format and works a bit like a web URL, setting the command and the values.

## Data retention

The data for overrides is kept inside the Home Assistant selector itself and so will survive a reboot. There is likely a limit to the size of this data so be sure to remove
old overrides when you are done with them. Keep in mind it's easy to lose all of the overrides with the 'off' option so do not keep important data here only use it for short-term
automations.

## Supported command formats

The supported formats are:

```text
off
<command>=<value>
<command>(index)=<value>
<command>?<name>=<value>
<command>(index)?<name>=<value>
<command>?<name>=<value>&<name2>=<value2>
<command>(index)?<name>=<value>&<name2>=<value2>
```

Commands are disabled again by putting them in square brackets e.g:

```text
[<command>?<name>=<value>&<name2>=<value2>]
````

Below is an example of setting a rate override, you can clear all overrides by calling 'off' or this specific one only by calling the same thing again but in square brackets []

For the rates you can use **rates_export_override** or **rates_import_override** with all the same options as [apps.yaml](energy-rates.md#manually-over-riding-energy-rates) but in a URL type format:

```text
rates_export_override?start=17:00:00&end=19:00:00&rate=0
```

See below for an [example of using the API to over-ride predicted house load]

If you override a single value item in a list with something like:

```text
inverter_limit(0)=4000
```

To disable this override again:

```text
[inverter_limit(0)=4000]
```

If you omit the index then all entries in the list will be overridden.

To disable all overrides

```text
off
```

### Supported overrides

The following settings can be overridden with this method:

- rates_export_override
- rates_import_override
- inverter_limit
- export_limit
- days_previous
- days_previous_weight
- inverter_battery_rate_min
- inverter_reserve_max
- battery_rate_max
- car_charging_soc
- car_charging_limit
- car_charging_battery_size
- battery_scaling
- forecast_hours
- import_export_scaling
- inverter_limit_charge
- inverter_limit_discharge

## Example solution to over-ride predicted house load

One common feedback is there is no mechanism in Predbat to alter the predicted house load, for example ignoring the effects of extra washing load in the past, or to take account of planned extra load such as cooking a big Sunday dinner.

The Predbat manual API provides a mechanism to meet this need by setting an export (or import) rates override.

1. Control variables

Create a date/time helper called predbat_override_date of type date, another called predbat_override_start_time of type time, and a third called predbat_override_end_time also of type time.

Create an input number helper called predbat_override_load_percent. I made it an input field and with a maximum value of 5.

These will hold the date, start time, end time and load adjustment percentage.

2. Create an automation script to send the event details to Predbat:

```yaml
alias: Send Load Adjustment details to Predbat manual API
sequence:
  - action: select.select_option
    data:
      option: >-
        rates_export_override?date={{
        states('input_datetime.predbat_override_date')| as_timestamp |
        timestamp_custom('%Y-%m-%d') }}&start={{
        states('input_datetime.predbat_override_start_time')
        }}&end={{
        states('input_datetime.predbat_override_end_time')
        }}&load_scaling={{
        states('input_number.predbat_override_load_percent')|float }}
    target:
      entity_id: select.predbat_manual_api
mode: single
description: ""
```

The script collects the above input variables, and sends these to Predbat as an export rate override.

3. Dashboard:

On an existing Home Assistant dashboard, or on a new one, create a control of type 'entities' and paste the following in:

```yaml
type: entities
entities:
  - entity: input_datetime.predbat_override_session_date
  - entity: input_datetime.predbat_override_start_time
  - entity: input_datetime.predbat_override_end_time
  - entity: input_number.predbat_override_load_percent
  - type: button
    name: Send Load Adjustment details to Predbat
    icon: mdi:script-text-play-outline
    action_name: Execute
    tap_action:
      action: perform-action
      perform_action: script.send_load_adjustment_details_to_predbat_manual_api
```

You simply enter the date, start time, end time and load percentage adjustment (e.g. 0.5=50%), then click the 'Execute' button.
The load adjustment details will be sent to the Predbat manual API and you will see the load change and a small +/- symbol against the export rate in the Predbat plan.
