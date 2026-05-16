# Manual API

**CAUTION** This is an expert feature only, you can break Predbat if you set the wrong things here.

While for most people Predbat will do what you want without any adjustments there are some special cases where users wish to write some more complex automations which override Predbat settings.

For settings inside Home Assistant e.g. switch.predbat_*, select.predbat_* and input_number.predbat_* you can already use an automation to change these values.

For settings in `apps.yaml` it's very difficult or impossible to update them via an automation.

For this reason, there is a selector called **select.predbat_manual_api** which works a bit like the manual override ones but this can have new values added using the select API in Home Assistant.
The only function the selector itself serves is to store override commands, you can clear from the selector but you have to set them using a service call.

Certain settings in `apps.yaml` may be overridden using this method.

Each override is in a string format and works a bit like a web URL, setting the command and the values.

## Data retention

The data for overrides is kept inside the Home Assistant selector itself and so will survive a reboot. There is likely a limit to the size of this data so be sure to remove
old overrides when you are done with them. Keep in mind it's easy to lose all of the overrides with the 'off' option so do not keep important data here only use it for short-term automations.

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

For the rates you can use **rates_export_override** or **rates_import_override** with all the [same rate override options as apps.yaml](energy-rates.md#manually-over-riding-energy-rates), but in a URL type format:

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

The following `apps.yaml` settings can be overridden using predbat_manual_api:

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

Prior to the addition of **select.predbat_manual_load_adjust** a common feedback was that there was no mechanism in Predbat to alter the predicted house load,
for example ignoring the effects of extra washing load in the past, or to take account of planned extra load such as cooking a big Sunday dinner.

The Predbat manual API provides a mechanism to meet this need by setting an export (or import) rates override.

Now that you can use the manual load adjust selector to overwrite predicted load this example solution is retained as a worked example of how you can use the manual API to overwrite `apps.yaml` settings.

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

## Updating additional house load forecasts

Named entries configured with [house_load_additional_forecast](apps-yaml.md#additional-house-load-forecast) can be updated from a Home Assistant automation using **select.predbat_load_forecast_delta_api**. This uses Home Assistant's standard **select.select_option** action, so it is visible in Developer Tools and automations.

For example, to schedule a dishwasher load:

```yaml
action: select.select_option
target:
  entity_id: select.predbat_load_forecast_delta_api
data:
  option: "dishwasher?start_time=20:00&duration=2.0&energy=1.2"
```

The **energy** value is the total kWh across the full duration. Predbat divides it across the generated plan slots.

Forecasts created through **select.predbat_load_forecast_delta_api** are one-shot dynamic loads. Predbat publishes a delete button for each of these forecasts, for example **button.predbat_load_forecast_delta_dishwasher_delete**, and automatically removes the forecast after its finish time. If you want the same forecast again, send the select command again.

While a one-shot forecast is active, Predbat preserves hidden request and selected-window metadata in the stored selector option so the schedule survives Home Assistant or Predbat restarts. Sending the same command again while it is still active does not reset the frozen request time or move a locked/running forecast. To force a fresh schedule, press the forecast delete button first and then send the request again.

If the appliance can run at any time before a deadline, send `mode=flexible`. For flexible loads, `start_time` is the earliest allowed start and `end_time` means done by. Predbat chooses the best block using the full prediction metric, so the selection considers solar, battery state, import/export rates, losses, and the current plan rather than just the import rate:

```yaml
action: select.select_option
target:
  entity_id: select.predbat_load_forecast_delta_api
data:
  option: "dishwasher?enabled=true&mode=flexible&start_time=22:00&end_time=07:00&duration=2.0&energy=1.2"
```

To allow the dishwasher to start any time from now but be done by 07:00, omit `start_time`:

```yaml
action: select.select_option
target:
  entity_id: select.predbat_load_forecast_delta_api
data:
  option: "dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=2.0&energy=1.2"
```

For one-shot forecasts created through **select.predbat_load_forecast_delta_api**, an omitted `start_time` is frozen to the current Predbat plan slot when the command is received. This prevents the published `requested_start` moving forward on later replans. If you send the same command again, Predbat treats it as a fresh request and freezes a new start time. Static YAML forecasts are different: if a YAML flexible load omits `start_time`, it continues to mean the current plan slot each time Predbat refreshes the forecast.

Predbat publishes the result as a binary sensor, for example **binary_sensor.predbat_load_forecast_delta_dishwasher**. For flexible loads, the most useful attributes are **requested_start**, **requested_end**, **suggested_start**, **suggested_end**, **target_times**, and **expires_at**.

A typical dishwasher automation can therefore send a one-shot flexible request when the dishwasher is ready:

```yaml
action: select.select_option
target:
  entity_id: select.predbat_load_forecast_delta_api
data:
  option: "dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=5&energy=0.7"
```

Use **suggested_start** from **binary_sensor.predbat_load_forecast_delta_dishwasher** to trigger the appliance start automation. Use **button.predbat_load_forecast_delta_dishwasher_delete** if you need to cancel the one-shot request before it expires.

### Example dishwasher scheduling automations

The following example uses four Home Assistant automations:

- Request a Predbat flexible load schedule when the dishwasher is ready.
- Start the dishwasher when Predbat reaches the published **suggested_start**.
- Clear the Predbat schedule if the dishwasher is started manually instead.
- Reset the Predbat-start helper after the scheduled run is no longer active.

First create a helper to track whether Predbat started the dishwasher. This prevents the manual-start cleanup automation deleting the schedule when Predbat itself starts the appliance:

```yaml
input_boolean:
  dishwasher_started_by_predbat:
    name: Dishwasher started by Predbat
    icon: mdi:dishwasher
```

Replace the switch and sensor entity IDs with the entities for your dishwasher.

```yaml
alias: Dishwasher - Request Predbat Schedule
description: Request cheapest dishwasher run window from Predbat
triggers:
  - trigger: state
    entity_id: switch.dishwasher_power
    to: "on"
conditions:
  - condition: state
    entity_id: input_boolean.dishwasher_started_by_predbat
    state: "off"
actions:
  - delay:
      seconds: 20
  - condition: state
    entity_id: input_boolean.dishwasher_started_by_predbat
    state: "off"
  - condition: state
    entity_id: sensor.dishwasher_operation_state
    state: ready
  - wait_template: |
      {{ is_state('sensor.dishwasher_door', 'closed') }}
    timeout: "00:01:00"
    continue_on_timeout: false
  - action: select.select_option
    target:
      entity_id: select.predbat_load_forecast_delta_api
    data:
      option: >-
        dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=5&energy=0.7
mode: single
```

The next automation checks every 15 minutes and starts the dishwasher once the current time reaches Predbat's **suggested_start**. Replace **YOUR_DISHWASHER_DEVICE_ID** and the program value with the values for your appliance.

```yaml
alias: Dishwasher - Start On Predbat Schedule
description: Start dishwasher at Predbat suggested start time
triggers:
  - trigger: time_pattern
    minutes: /15
conditions:
  - condition: template
    value_template: |
      {{
        state_attr(
          'binary_sensor.predbat_load_forecast_delta_dishwasher',
          'suggested_start'
        ) is not none
      }}
  - condition: template
    value_template: |
      {% set start = state_attr(
        'binary_sensor.predbat_load_forecast_delta_dishwasher',
        'suggested_start'
      ) %}
      {% if start %}
        {{ now().timestamp() >= as_timestamp(start) }}
      {% else %}
        false
      {% endif %}
  - condition: state
    entity_id: input_boolean.dishwasher_started_by_predbat
    state: "off"
actions:
  - action: input_boolean.turn_on
    target:
      entity_id: input_boolean.dishwasher_started_by_predbat
  - if:
      - condition: state
        entity_id: switch.dishwasher_power
        state: "off"
    then:
      - action: switch.turn_on
        target:
          entity_id: switch.dishwasher_power
      - delay:
          seconds: 20
  - condition: state
    entity_id: sensor.dishwasher_operation_state
    state: ready
  - condition: state
    entity_id: sensor.dishwasher_door
    state: closed
  - action: home_connect.set_program_and_options
    data:
      device_id: YOUR_DISHWASHER_DEVICE_ID
      affects_to: active_program
      program: YOUR_DISHWASHER_PROGRAM
mode: single
```

The next automation removes the Predbat request if the dishwasher is started manually before Predbat reaches **suggested_start**. It only runs when **input_boolean.dishwasher_started_by_predbat** is off, so it will not delete the schedule when the previous automation started the dishwasher for Predbat:

```yaml
alias: Dishwasher - Clear Predbat Schedule When Manually Started
description: Remove Predbat schedule if dishwasher starts manually
triggers:
  - trigger: state
    entity_id: sensor.dishwasher_operation_state
    from: ready
    to:
      - run
      - delayedstart
      - pause
conditions:
  - condition: state
    entity_id: input_boolean.dishwasher_started_by_predbat
    state: "off"
  - condition: template
    value_template: |
      {{
        state_attr(
          'binary_sensor.predbat_load_forecast_delta_dishwasher',
          'suggested_start'
        ) is not none
      }}
actions:
  - action: button.press
    target:
      entity_id: button.predbat_load_forecast_delta_dishwasher_delete
mode: single
```

The final automation resets **input_boolean.dishwasher_started_by_predbat** after the Predbat one-shot forecast has disappeared or after the dishwasher returns to a non-running state. This makes the manual-start cleanup automation ready for the next dishwasher cycle without clearing the current Predbat schedule too early:

```yaml
alias: Dishwasher - Reset Predbat Started Marker
description: Reset Predbat helper after scheduled dishwasher run
triggers:
  - trigger: state
    entity_id: binary_sensor.predbat_load_forecast_delta_dishwasher
    to:
      - unavailable
      - unknown
  - trigger: state
    entity_id: sensor.dishwasher_operation_state
    to:
      - ready
      - inactive
      - finished
      - off
conditions:
  - condition: state
    entity_id: input_boolean.dishwasher_started_by_predbat
    state: "on"
actions:
  - action: input_boolean.turn_off
    target:
      entity_id: input_boolean.dishwasher_started_by_predbat
mode: single
```

You can add appliance-specific options, such as quiet or night mode, inside the Home Connect action if your appliance supports them.

Use `enabled=false` in `apps.yaml` to keep static load injection profiles visible but inactive until an automation sends an API forecast with the same name.

For advanced cases, you can use **slot_energy** instead when you want to set kWh per Predbat plan slot directly. With the default 30-minute plan interval, `slot_energy: 0.5` adds 0.5kWh to each slot for two hours.

You can also include **weighting** to model a higher load at the start of a cycle:

```yaml
action: select.select_option
target:
  entity_id: select.predbat_load_forecast_delta_api
data:
  option: "dishwasher?start_time=20:00&duration=2.0&energy=1.2&weighting=2|2|*"
```

With **energy**, weighting redistributes the total energy without changing the total. With **slot_energy**, weighting multiplies the per-slot energy. Use `|` as the weighting separator when sending commands through **select.predbat_load_forecast_delta_api**, because Home Assistant select options are stored as comma-separated values internally.
