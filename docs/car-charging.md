# Car charging

As a bare minimum, a HA-controllable smart plug with a granny charger could be used,
but do consider there could be an electrical spike to the car if the smart plug is turned off when the car is charging. A proper car charger and HA integration are preferable.

You will first need to have installed the appropriate Home Assistant integration for your car charger.  Details of existing Car charging configurations can be found in the [Devices](devices.md) section.

If you have the Intelligent Octopus tariff, have completed enrollment of your car/charger to Intelligent Octopus (requires a compatible charger or car), and want to take advantage of Octopus planning your charging via the Octopus app, then Predbat will obtain the Octopus charging information through the [Octopus Energy integration](energy-rates.md#octopus-energy-home-assistant-integration) in Home Assistant or the [Octopus direct connection](energy-rates.md#octopus-energy-direct). Predbat will plan your home battery charging around the Intelligent Octopus slots.

This is referred to as 'Octopus led' charging in this documentation.

Alternatively, Predbat can plan your car charging based upon your energy rates, this is known as 'Predbat led' charging'.

There are different configuration options that have to be set in Predbat depending upon whether you are using Octopus-led or Predbat-led car charging.  Note that if you are on the Intelligent Octopus tariff then you can still have Predbat plan your car charging, not Octopus, in which case you need to follow the Predbat-led configuration steps.

## Configure apps.yaml for your car charging

Start by configuring the car charging settings in `apps.yaml` with the following car-charging related configuration items:

- **num_cars** should be set in `apps.yaml` to the number of cars you want Predbat to plan for.
Set to 0 if you don't have an EV or don't want Predbat to plan for your EV charging (and the remaining car sensors in `apps.yaml` can safely be commented out or deleted as they won't be required).<BR>
NB: num_cars must be set correctly regardless of whether you are using Octopus Intelligent Go to control your EV charging or Predbat to control the charging;
or else Predbat could start discharging your battery when the EV is charging.

- **car_charging_exclusive** should be set to `true` for each car in `apps.yaml` if you have multiple cars configured in Predbat, but only one car charger.
This indicates that only one car may charge at once (the first car reporting as plugged in will be considered as charging).
If you set this to `false` for each car then it is assumed that the cars can charge independently, and hence two or more cars could charge at once. One entry per car.

```yaml
  car_charging_exclusive:
    - true
    - true
```

### Filtering Car Charging Energy from house load

Depending upon how the CT clamps and your inverter and electric car charger have been wired, your inverter may 'see' your EV charging as being part of the house load.  This means your house load is artificially raised whenever you charge your car.
In this circumstance you might want to remove your electric car charging data from the historical house load data so as to not bias the calculations, otherwise you will get
high battery charge levels when the car was charged previously (e.g. last week).

*TIP:* Check the house load being reported by your inverter when your car is charging. If it doesn't include the car charging load then there is no need to follow these steps below (and if you do, you'll artificially deflate your house load).

- **switch.predbat_car_energy_reported_load** - A switch (default On) that tells Predbat whether your EV charger is on the **house-load side of the CT clamp** (i.e. the inverter 'sees' the car charging as part of house load).

    - **On (default)** - The car charger is inside the CT clamp and its energy is reported as part of the house load. Predbat will attempt to strip car charging energy from historical load data (via **switch.predbat_car_charging_hold**) and will prevent the battery from discharging into the car when **switch.predbat_car_charging_from_battery** is Off.
    - **Off** - The car charger is wired outside the CT clamp (e.g. directly to the grid connection). The inverter cannot see the car charging load directly. In this case Predbat will:
        - Automatically disable **switch.predbat_car_charging_hold** (no need to strip car data from house load, as it was never included).
        - Automatically set **switch.predbat_car_charging_from_battery** to On in the model (the battery cannot discharge into the car anyway as it is on a separate circuit).
        - Model that any export from the battery/PV may flow into the car rather than the grid, and so conservatively not credit that energy with export income.

- **switch.predbat_car_charging_hold** - A switch that when turned On (the default) tells Predbat to remove car charging data from your historical house load so that Predbat's battery prediction plan is not distorted by previous car charging. This switch is automatically overridden to Off when **switch.predbat_car_energy_reported_load** is Off, since the car load is not in the house load data.

If you are getting [erroneous house load predictions in your plan](faq.md#why-is-my-house-load-lower-than-expected-or-zero) then check this setting and **car_charging_energy** or **input_number.predbat_car_charging_threshold** are set correctly.

If you don't have an EV then turn **switch.predbat_car_charging_hold** Off as Predbat will by default still consider any house load in excess of **input_number.predbat_car_charging_threshold** to be car charging activity and will exclude it.

- **car_charging_energy** - Set in `apps.yaml` to point to an entity which is the daily incrementing kWh data for the car charger.
This has been pre-defined as a regular expression that should auto-detect the appropriate Wallbox and Zappi car charger sensors,
or edit as necessary in `apps.yaml` for your charger sensor.<BR>
Note that this must be configured to point to an 'energy today' sensor in kWh not an instantaneous power sensor (in kW) from the car charger.<BR><BR>
*IMPORTANT:* Predbat will subtract all car_charging_energy from your historic house load so if car_charging_energy is not configured with the correct sensor,
your car charging energy sensor does not accurately report your car charging data (e.g. it falsely reports charging data when not actually charging), or your house load sensor already excludes car charging,
then this will really mess up your Predbat plan as Predbat will exclude all car_charging_energy from your load predictions and you could end up with erroneous or zero house load predictions.  Do check the entity!<BR><BR>
*NOTE:* The car charging energy sensor must be a daily incrementing kWh sensor. Check the history of your sensor in Home Assistant, that it increments through the day when your car is charging, resets to zero at midnight,
and does not dip down in value or reset to zero other than at midnight. Some car charger energy sensors do not behave as Predbat requires them to do; for example, they may show cumulative energy per charge, not cumulative charge energy today.<BR>
You may need to wrap the car charger energy sensor into a daily resetting utility meter to create a sensor that increments through the day and only changes to zero at midnight.<BR><BR>

*NOTE:* A charger that reports 'unavailable' or 'unknown' when the car isn't plugged in needs no special handling. Predbat skips those readings when it loads the sensor history,
and does not treat them as a configuration error, so the sensor can be used exactly as it is.<BR>
Do **not** wrap such a sensor in a template that substitutes zero (for example `| float(0)`). For a daily incrementing sensor, a zero part way through the day looks exactly like the midnight reset,
so Predbat starts counting the day's energy again from that point and the charging before it is counted twice.

*TIP:* You can also use **car_charging_energy** to remove other house load kWh from the data Predbat uses for the forecast,
e.g. if you want to remove Mixergy hot water tank heating data from the forecast such as if you sometimes heat on gas, and sometimes electric depending upon import rates.<BR>
car_charging_energy can be set to a list of energy sensors, one per line if you have multiple EV car chargers, or want to exclude multiple loads such as heat pump load, e.g.:

```yaml
  car_charging_energy:
    - 're:(sensor.myenergi_zappi_[0-9a-z]+_charge_added_session|sensor.wallbox_portal_added_energy)'
    - sensor.mixergy_ID_energy
    - sensor.ashp_energy_today
```

- **input_number.predbat_car_charging_energy_scale** - Used to define a scaling factor (in the range of 0 to 1.0)
to multiply the **car_charging_energy** sensor data by if required (e.g. set to 0.001 to convert Watts to kW). Default 1.0, i.e. no scaling.

- **car_charging_power** - Set in `apps.yaml` to point to an entity giving the **live charging power** of your car charger (in Watts, or any unit Predbat can convert such as kW).
This has been pre-defined as a regular expression that should auto-detect the appropriate Wallbox and Zappi car charger sensors, or edit as necessary in `apps.yaml` for your charger sensor.<BR>
Unlike **car_charging_energy** this is display only - it has no effect at all on the Predbat plan. When it is set:

- the [web interface](web-interface.md) power flow diagram gains a Car showing what the charger is drawing.
What the Car is drawn as being fed from follows **switch.predbat_car_energy_reported_load**: with it on (the default) your charger sits inside the house CT clamp,
so the Car hangs off the House and its power is subtracted from the House figure to stop the car being counted twice;
with it off the charger is outside the clamp and was never part of your house load, so the Car hangs off the Grid instead and the House figure is left alone
- Predbat publishes a **predbat.car_charging_power** sensor (in kW) which you can graph or use in your own automations

Like car_charging_energy it can be a list of sensors, one per line per car charger, if you have more than one charger - they are added together in Predbat:

```yaml
  car_charging_power:
    - sensor.zappi_charge_power
    - sensor.wallbox_charging_power
```

If you have multiple cars sharing one charger, then only include a single entry for the charger.

If your car charger has no live power sensor, leave **car_charging_power** commented out in `apps.yaml`; the power flow diagram then shows the same four items it always has, and no **predbat.car_charging_power** sensor is published.<BR>
If you use one of the supported charger integrations (Ohme, myenergi Zappi, Wallbox, Hypervolt, GivEnergy EV charger, AlphaESS EV charger or the Predbat gateway with `gateway_evc_automatic`) then this is configured automatically and you do not need an `apps.yaml` entry of your own.

If you do not have a suitable car charging energy kWh sensor in Home Assistant then comment the **car_charging_energy** line out of `apps.yaml` and configure **input_number.predbat_car_charging_threshold**

- **input_number.predbat_car_charging_threshold** (default 6 = 6kW)- Sets the kW power threshold above which home consumption is assumed to be car charging
and **input_number.predbat_car_charging_rate** (in kW) will be subtracted from the historical load data.

Used to 'detect' EV charging if you have an EV charger but it does not have an energy today sensor that you can use.  If **car_charging_energy** is set in `apps.yaml` then **input_number.predbat_car_charging_threshold** is ignored.

If you do not have an EV charger then ensure you set **switch.predbat_car_charging_hold** to Off otherwise Predbat will assume any house load in excess of car_charging_threshold is EV charging and remove it from your house load predictions!

### Planned Car Charging

These features allow Predbat to know when you plan to charge your car.

If you are on the Octopus Intelligent Tariff set the following entries in `apps.yaml`:

- **octopus_slot_low_rate** - Default is `true`, meaning any Octopus Intelligent Slot reported will be at the lowest rate if at home. If `false` the existing rates only will be used which is only suitable for tariffs other than IOG.

- **octopus_slot_max** - Sets the maximum cheap rate dispatch time per 24-hour period, in 30-minute slots (12 is 6 hours). It is counted in the minutes of each dispatch as Octopus reports them, so a 10-minute dispatch uses 10 minutes of it. With more than one car on Intelligent Octopus the limit is shared between them (see [Multiple cars with Octopus Intelligent Go](#multiple-cars-with-octopus-intelligent-go-iog)). Dispatches beyond this limit will use standard rates.
If unset, Predbat defaults this to 12 (6 hours) automatically for tariffs that Octopus enforces the 6-hour Intelligent cap on (tariff codes containing `IOG-SMB`), and to 48 (disabled) for other tariffs such as the older `INTELLI-VAR`. Set this explicitly to override the automatic default in either direction.

- **intelligent_dispatch_billing** - How your supplier bills the half hours of a planned smart-charging dispatch. `octopus` bills a half hour at the cheap rate only when the car charges in it, as Octopus Intelligent does: Predbat keeps an elapsed half hour cheap until Octopus lists it as a completed dispatch, and stops treating a dispatch as cheap once it sees the car is not charging in it. `window` bills every half hour a planned dispatch was in force at the cheap rate, whether or not the car drew any power, as E.ON Next Drive Smart has been seen to do: Predbat keeps each of those half hours cheap for the rest of the day and never removes the cheap rate because the car is not charging. The default, `auto`, uses `window` when the [Kraken component](components.md#kraken-energy-kraken) is running with `kraken_provider: eon` and `octopus` otherwise. Set it explicitly if your supplier bills the other way.

If you are using Octopus-led charging with the [Octopus Energy integration](energy-rates.md#octopus-energy-home-assistant-integration):

The following `apps.yaml` configuration items are pre-defined with regular expressions to point to appropriate sensors in the Octopus Energy integration. You should not normally need to change these if you have the Octopus Intelligent tariff:

```yaml
  octopus_intelligent_slot: 're:(binary_sensor.octopus_energy([0-9a-z_]+|)_intelligent_dispatching)'
  octopus_intelligent_smart_control: 're:(switch.octopus_energy([0-9a-z_]+|)_intelligent_smart_charge)'
  octopus_ready_time: 're:((select|time).octopus_energy_([0-9a-z_]+|)_intelligent_target_time)'
  octopus_charge_limit: 're:(number.octopus_energy([0-9a-z_]+|)_intelligent_charge_target)'
```

- **octopus_intelligent_slot** - Points to the Octopus Energy integration 'intelligent dispatching' sensor in the Octopus Energy integration that indicates
whether you are within an Octopus Energy "smart charge" slot, and provides the list of future planned charging activity.
For **multiple IOG-enrolled vehicles**, set this to a list with one sensor per car (see [Multiple Electric Cars](#multiple-electric-cars)).

- **octopus_intelligent_smart_control** - Points to the Octopus Energy integration 'intelligent smart charge' switch (`switch.octopus_energy_{{DEVICE_ID}}_intelligent_smart_charge`),
which is on while Octopus Smart Control is on for the car. Octopus keeps reporting the charging plan it had already made after you switch Smart Control off, so while this switch is `off`
Predbat ignores the *planned* Octopus slots (including any bonus slots) as they will not happen; slots already delivered, and bump/boost charges you asked for yourself, still count.
It holds the entity ID of the switch, not a value, so Predbat follows the switch live, however it is turned off (the Octopus app, Home Assistant or an automation).
If you set up `apps.yaml` before this item existed, add it to pick up the change. If the switch cannot be found, planned slots are used as normal.
If `octopus_intelligent_slot` points at your charger's own integration (for example Ohme) rather than the Octopus Energy integration, comment out `octopus_intelligent_smart_control`,
otherwise the charger's own planned slots are ignored whenever Octopus Smart Control is off.
Planned slots taken from **octopus_intelligent_planned_slot** are the charger's own plan rather than Octopus's, so they are still used while Smart Control is off, but as car charging at your normal rate rather than as cheap Octopus slots.
For **multiple IOG-enrolled vehicles**, set this to a list with one switch per car.

- **octopus_intelligent_planned_slot** - Optional, see [Taking planned slots from another sensor](#taking-planned-slots-from-another-sensor).

- **octopus_ready_time** - Points to the Octopus Energy integration sensor that details when the car charging will be completed.<BR>
*Note:* the Octopus Integration now provides [Octopus Intelligent target time](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/#target-time-time) in two formats, either a 'select' entity or a 'time' entity.
Predbat uses the time entity (time.octopus_energy_{{DEVICE_ID}}_intelligent_target_time) which is disabled by default, so you will need to enable the time entity and disable the matching select entity.
For **multiple IOG-enrolled vehicles**, set this to a list with one sensor per car.

- **octopus_charge_limit** - Points to the Octopus Energy integration sensor that provides the car charging limit you want the car to charge to.
For **multiple IOG-enrolled vehicles**, set this to a list with one sensor per car.
When Octopus is linked to your charger rather than your car, Octopus's target is how much charge to add, not the level to charge to.
Predbat detects this from the Octopus dispatch sensor (from Predbat's own Octopus component, or from which of the charge point power and the battery size the Octopus Energy integration fills in), and adds the target to the car's SoC when it was plugged in, still capped by **car_charging_limit**. It also reads a `device_type` attribute on the charge target entity you set as **octopus_charge_limit**, should the integration add one.
If neither says which is linked (for example a charger model Octopus does not list), the target is read as the level to charge to.

If you are using Octopus-led charging with the [Octopus direct connection](energy-rates.md#octopus-energy-direct) method:

- Predbat gets its Octopus charging slot information direct from the Octopus API, so comment out or delete octopus_intelligent_slot, octopus_intelligent_smart_control, octopus_ready_time and octopus_charge_limit from `apps.yaml`.
- Predbat publishes a `switch.predbat_octopus_{account}_intelligent_smart_charge_{n}` switch for each Intelligent device, which is on while Octopus Smart Control is on. Turning it off or on in Home Assistant sends SUSPEND / UNSUSPEND to Octopus on Predbat's next Octopus update, the same as the Smart Control toggle in the Octopus app. The switch shows the change once Octopus has accepted it, and stays as it was if Octopus rejects it. It is also shown for a device that is suspended, so that you can turn Smart Control back on. A suspended device is no longer wired into Predbat, so while Smart Control is off Predbat plans that car's charging as it would without Intelligent Octopus, until Smart Control is turned back on.

- Predbat also looks up your enrolled charger's power, or your enrolled car's battery size, in Octopus's catalogue of chargers and cars, by make and model.
A battery size found there is used in place of **car_charging_battery_size**, so that setting can't change it.
A charger power found there is used if it is higher than **input_number.predbat_car_charging_rate** (or **input_number.predbat_car_charging_rate_1** etc. for further cars), but never to lower it. It can never raise it above the car's own maximum, **car_charging_max_rate**, if you set that (see below).
Some makes and models are listed more than once with different values (myenergi's "zappi (all models)" is listed at both 7.4 and 22 kW), and the name alone can't tell them apart.
For those, Predbat takes the lowest of the charger powers listed and the largest of the battery sizes, and logs a warning naming the values the catalogue gave.
As the car is planned at whichever is higher of that charger power and **input_number.predbat_car_charging_rate**, set **input_number.predbat_car_charging_rate** to your charger's rate.

If you are using Predbat-led charging:

The following entries are pre-configured in the `apps.yaml` template:

```yaml
  car_charging_planned:
    - 're:(sensor.wallbox_portal_status_description|sensor.myenergi_zappi_[0-9a-z]+_plug_status)'

  car_charging_planned_response:
    - 'yes'
    - 'on'
    - 'true'
    - 'connected'
    - 'ev connected'
    - 'charging'
    - 'paused'
    - 'waiting for car demand'
    - 'waiting for ev'
    - 'scheduled'
    - 'enabled'
    - 'latched'
    - 'locked'
    - 'plugged in'
    - 'waiting'

  #car_charging_now:
  #  - off

  # Positive responses for car_charging_now
  car_charging_now_response:
    - 'yes'
    - 'on'
    - 'true'
    - 'charging'
```

- **car_charging_planned** - Optional, can be set to a Home Assistant sensor (e.g. from your car charger integration) which lets Predbat know the car is plugged in and planned to charge during low-rate slots.
Or manually set it to 'false' to disable this feature, or 'true' to always enable it.<BR>
The `apps.yaml` template supplied with Predbat comes pre-configured with a regular expression that should automatically match Zappi or Wallbox car chargers.
If you have a [different type of EV charger](devices.md) you will need to configure it manually.

- **car_charging_planned_response** - An array of values for the above car_charging_planned sensor which indicate that the car is plugged in and will charge in the next low rate slot.
The template `apps.yaml` comes with a set of pre-defined sensor values that should match most EV chargers.
Customise for your car charger sensor if it sets sensor values that are not in the list.

- **car_charging_plugged** - Optional, a Home Assistant sensor that says only whether the car is plugged in, whether or not it still wants charge. It is matched against **car_charging_planned_response** in the same way.
It is used to reset the [manual car SoC](#predbat-led-charging) when the car is unplugged, and a car with no SoC sensor needs it (with **car_charging_energy**) for its SoC to be tracked automatically.
**car_charging_planned** is not used in its place, as many chargers turn it off when a charge finishes with the car still plugged in. As with **car_charging_planned**, use a list with one entry per car if you have more than one.
Predbat's own Ohme, myenergi, Wallbox, Hypervolt, GivEnergy Cloud EV charger and GivEnergy Gateway components set it for you.

- **car_charging_now** - Optional, can be set to a Home Assistant sensor that tells Predbat the car is charging right now (e.g. from your car charger integration).
This can be an on/off sensor, matched against **car_charging_now_response**, or - for chargers that have no "charging" sensor - a charging power sensor in W or kW, where 200W or more counts as charging (e.g. `sensor.wallbox_portal_charging_power` for a Wallbox).<BR>
While it reports the car charging, Predbat holds the house battery for the car ("Hold for car") so the battery does not discharge into it, unless **switch.predbat_car_charging_from_battery** is On.
The hold starts and stops within about 15 seconds of the sensor changing, rather than at the next 5-minute plan update.
It also counts as the car being plugged in, so Predbat-led charging will plan for the car.<BR>
car_charging_now never adds a charging slot of its own: slots come only from the Predbat car planner or from Octopus Intelligent dispatches.
So it does not turn on **binary_sensor.predbat_car_charging_slot**, and an automation that starts the charger from that sensor cannot keep a charge going through it.
With Octopus Intelligent charging a charge outside a dispatch does not get the cheap rate either.<BR>
A car charging outside any charging slot is also modelled in the plan at **input_number.predbat_car_charging_rate** until the end of the current plan slot (**plan_interval_minutes**, 30 minutes by default), so no export is planned over it. With **switch.predbat_metric_dynamic_load_adjust** On its load is also taken out of the recent-load estimate, so it is not counted twice - see [Dynamic Load Adjust](customisation.md#scaling-and-weight-options).<BR>
Leave it commented out if you have no sensor that reports the car actually drawing power.
The Ohme (`ohme_automatic`), myenergi Zappi (`myenergi_automatic`), Wallbox (`wallbox_automatic`) and Hypervolt (`hypervolt_automatic`) integrations set it for you, unless you have set it yourself in `apps.yaml`. The [GivEnergy Gateway](#givenergy-gateway-ocpp-ev-charger) integration (`gateway_evc_automatic`) sets it too, replacing any value of your own.

- **car_charging_now_response** - Set to the range of positive responses for car_charging_now to indicate that the car is charging. Useful if you have a sensor for your car charger that isn't binary.
The sensor's state must match one of these exactly (ignoring case). If unset it defaults to `yes`, `on`, `enable`, `true` and `charging`, which covers on/off sensors and chargers whose status reads `Charging`, such as a myenergi Zappi's plug status.
If your charger reports something else while charging, add that value here. Otherwise Predbat never sees the car charging, and with Octopus Intelligent it [cancels the car's dispatches](#checking-intelligent-dispatches-against-the-car).

To make Predbat-led car charging more accurate, additionally you can configure the following items in `apps.yaml`:

```yaml
  #car_charging_battery_size:
  #  - 75
  #car_charging_limit:
  #  - 're:number.tsunami_charge_limit'
  #car_charging_soc:
  #  - 're:sensor.tsunami_battery'
```

- **car_charging_battery_size** - Set this value in `apps.yaml` to the car's battery size in kWh, as a list with one entry per car:

```yaml
  car_charging_battery_size:
    - 75
```

Writing the number on the same line as the key (`car_charging_battery_size: 75`) fails Predbat's `apps.yaml` validation
with *"is not of type 'sensor'"*, so use the list form above. A whole number is fine - a decimal place is not required.
If not set, Predbat defaults to 100.0kWh. This will be used to predict when Predbat will stop car charging.

- **car_charging_limit** - You should configure this to point to a sensor that specifies the % limit the car is set to charge to.
This could be a sensor on the EV charger integration or a Home Assistant helper entity you can set as you wish.
If you don't specify a sensor Predbat will default to 100% - i.e. fill the car to full.

- **car_charging_soc** - You should configure this to point to a sensor (on the HA integration for your EV charger) that specifies the car's current charge level
expressed as a percentage - it must NOT be set to a sensor that gives the car's current kWh value as this will cause Predbat to charge the car to an incorrect level.
If you don't specify a sensor, Predbat will default to 0%.

- **car_charging_max_rate** - Optional, the car's own maximum charge rate in kW, as a list with one entry per car.
The car charging rate (**input_number.predbat_car_charging_rate**, a charger integration's reported power, or the Octopus Intelligent charger power) is only what the charger can deliver.
Many cars, particularly plug-in hybrids, have an on-board charger that takes less (for example 3.6 kW on a 7.4 kW charger), and Predbat would then plan too short a charge.
With this set, each car is planned at whichever is lower, its charging rate or its own maximum. It applies to every charger integration, and Octopus's charger power can not raise the rate back above it:

```yaml
  car_charging_max_rate:
    - 3.6
    - sensor.second_car_max_charge_kw
```

Each entry is either a number or a sensor holding the car's rate (in kW, or in W with a `W` unit), and is read again every plan cycle, so a change of car takes effect without a restart.
A blank entry, 0, or leaving a car off the end of the list means no cap for that car.
If the sensor is briefly `unknown`, `unavailable` or `none`, for example while Home Assistant restarts, Predbat keeps the last cap it read for that car rather than dropping it. The last cap is saved, so this holds across a restart of Predbat too.
Any other reading that is not a number also keeps the last cap, with a warning logged once.
A sensor that does not exist is reported by `apps.yaml` validation and logged once as a warning, and that car is then not capped.
As with **car_charging_battery_size**, write it as a list: a number on the same line as the key fails `apps.yaml` validation.
A single sensor on the same line as the key (`car_charging_max_rate: sensor.my_car_max_charge_kw`) is accepted, but applies that one sensor to every car; with more than one car, use the list form so each car has its own entry.

If you have [multiple electric cars](#multiple-electric-cars) then car_charging_soc should be set to a list of sensors, e.g.:

```yaml
  car_charging_soc:
    - 'sensor.tsunami_battery'
    - 'sensor.toyota_XXX_battery_level'
```

#### Taking planned slots from another sensor

**octopus_intelligent_planned_slot** is optional. It names a second sensor, per car, that Predbat takes the planned charging slots from, while **octopus_intelligent_slot**
goes on supplying the completed ones.

It is for a charger that knows the charge still to come better than Octopus reports it, but keeps no lasting record of the slots already used.
The [Ohme direct integration](#ohme-car-charger-direct-integration) sets it for you; you would only set it by hand for another charger whose sensor has a `planned_dispatches` attribute:

```yaml
  octopus_intelligent_slot: 're:(binary_sensor.octopus_energy([0-9a-z_]+|)_intelligent_dispatching)'
  octopus_intelligent_planned_slot: 'binary_sensor.my_charger_slots'
```

With it set:

- Completed slots, which decide the off-peak rate shown for charging already done, come from **octopus_intelligent_slot**.
- Planned slots come from **octopus_intelligent_planned_slot**. Any planned slots on **octopus_intelligent_slot** are ignored for that car.
- Slots the planned sensor reports as already finished are also used, until **octopus_intelligent_slot** lists them as completed.

It is only read for a car that also has **octopus_intelligent_slot** set, and not when `octopus_intelligent_slot_action_config` is in use.
For multiple cars, set it to a list with one sensor per car.

### Multiple Electric Cars

Multiple cars can be planned with Predbat, in which case you should set **num_cars** in `apps.yaml` to the number of cars you want to plan.

- **car_charging_limit**, **car_charging_planned**, **car_charging_battery_size** and **car_charging_soc** must then be a list of values (i.e. 2 entries for 2 cars), and so should **car_charging_now** if you set it

- Each car will have its own Home Assistant slot sensor created e.g. **binary_sensor.predbat_car_charging_slot_1**,
SoC planning sensor e.g **predbat.car_soc_1** and **predbat.car_soc_best_1** for car 1

#### One charger shared by more than one car

A single charger sensor listed for both cars shows both cars as plugged in (in **car_charging_planned**) or charging (in **car_charging_now**) whenever either one is.
With **car_charging_exclusive** set to `true` Predbat then plans only one of them, but it goes through the cars in order, so it plans car 0 whenever car 0 has charging to plan, whichever car is actually on the charger.

This applies to a charger whose car settings you set yourself in `apps.yaml` - not one set up by Predbat's own charger integrations (Ohme, myenergi, Wallbox, Hypervolt, GivEnergy Gateway or GivEnergy Cloud EV charger), whose automatic setup and charger control assume one car per charger.
To use it with a Zappi on Predbat's own myenergi integration, set **myenergi_automatic_zappi** to `false` so the integration leaves your Zappi's car settings to you. That includes **car_charging_energy** (and **car_charging_power**), which you then set yourself to the Zappi's `sensor.predbat_myenergi_zappi_<serial>_session_energy` (still published) so its energy is still taken out of your house load. Zappi charger control then stays off, as it needs that setting on.

To fix it, tell Predbat which car is on the charger, and give each car its own sensors that follow the charger only while that car is the one on it (keep **car_charging_exclusive** set to `true` as well).
For example, with an `input_select` helper of your own, with options `Car 0`, `Car 1` and `None`, which you or an automation set to the car on the charger:

```yaml
template:
  - sensor:
      # Use the same unit as your charger's power sensor: "kW" if it is in kW, which Predbat converts
      - name: "Car 0 charging power"
        unit_of_measurement: "W"
        device_class: power
        availability: "{{ has_value('input_select.car_on_charger') and (not is_state('input_select.car_on_charger', 'Car 0') or has_value('sensor.your_charger_power')) }}"
        state: "{{ states('sensor.your_charger_power') if is_state('input_select.car_on_charger', 'Car 0') else 0 }}"
      - name: "Car 1 charging power"
        unit_of_measurement: "W"
        device_class: power
        availability: "{{ has_value('input_select.car_on_charger') and (not is_state('input_select.car_on_charger', 'Car 1') or has_value('sensor.your_charger_power')) }}"
        state: "{{ states('sensor.your_charger_power') if is_state('input_select.car_on_charger', 'Car 1') else 0 }}"
  - binary_sensor:
      # Use your charger's plugged-in sensor; with a status sensor instead, test for its exact "no car" state,
      # e.g. for a Zappi's plug status: has_value('sensor.your_charger_status') and not is_state('sensor.your_charger_status', 'EV Disconnected')
      - name: "Car 0 plugged in"
        state: "{{ is_state('input_select.car_on_charger', 'Car 0') and is_state('binary_sensor.your_charger_plugged_in', 'on') }}"
      - name: "Car 1 plugged in"
        state: "{{ is_state('input_select.car_on_charger', 'Car 1') and is_state('binary_sensor.your_charger_plugged_in', 'on') }}"
```

```yaml
  car_charging_now:
    - sensor.car_0_charging_power
    - sensor.car_1_charging_power
  car_charging_planned:
    - binary_sensor.car_0_plugged_in
    - binary_sensor.car_1_plugged_in
  car_charging_exclusive:
    - true
    - true
```

The `availability` lines matter with Octopus Intelligent: the [dispatch check](#checking-intelligent-dispatches-against-the-car) treats an unavailable sensor as no evidence, but a definite 0W as the car not charging.

With Octopus Intelligent, this works when each car is enrolled separately with its own **octopus_intelligent_slot** entry, and **octopus_intelligent_planned_slot** entry if you use one (see [below](#multiple-cars-with-octopus-intelligent-go-iog)).
With **octopus_automatic**, suspending or removing a car can move the other car to a different index (see [how Predbat picks which car goes in which slot](#how-predbat-picks-which-car-goes-in-which-slot-octopus_automatic)), so check that your `input_select` options still name the right car afterwards.
When only the charger is enrolled, its dispatches belong to the car whose **octopus_intelligent_slot** entry it is, normally car 0. While the other car is selected, the enrolled car reads 0W, so Predbat can cancel the dispatch - and with **switch.predbat_octopus_intelligent_ignore_unplugged** On it also reads as unplugged, so the dispatch is ignored. This recipe does not suit that setup.

### Multiple cars with Octopus Intelligent Go (IOG)

If you have **two or more EVs enrolled in Octopus Intelligent Go**, Predbat can track the scheduled dispatch slots for each car independently.

If you use the Octopus Direct function inside Predbat then you can set **octopus_automatic** to True to automatically configure IOG cars.

Otherwise if using Bottle Cap Dave's Octopus integration then set **octopus_intelligent_slot**, **octopus_intelligent_smart_control**, **octopus_ready_time** and **octopus_charge_limit**
to lists with one entry per car in `apps.yaml`:

```yaml
  num_cars: 2

  car_charging_exclusive:
    - True
    - True

  octopus_intelligent_slot:
    - 'binary_sensor.octopus_energy_{{DEVICE_ID_CAR1}}_intelligent_dispatching'
    - 'binary_sensor.octopus_energy_{{DEVICE_ID_CAR2}}_intelligent_dispatching'

  octopus_intelligent_smart_control:
    - 'switch.octopus_energy_{{DEVICE_ID_CAR1}}_intelligent_smart_charge'
    - 'switch.octopus_energy_{{DEVICE_ID_CAR2}}_intelligent_smart_charge'

  octopus_ready_time:
    - 'time.octopus_energy_{{DEVICE_ID_CAR1}}_intelligent_target_time'
    - 'time.octopus_energy_{{DEVICE_ID_CAR2}}_intelligent_target_time'

  octopus_charge_limit:
    - 'number.octopus_energy_{{DEVICE_ID_CAR1}}_intelligent_charge_target'
    - 'number.octopus_energy_{{DEVICE_ID_CAR2}}_intelligent_charge_target'
```

Replace `{{DEVICE_ID_CAR1}}` and `{{DEVICE_ID_CAR2}}` with the actual device IDs shown in your Octopus Energy integration.
Each entry in the list corresponds to the matching car index (car 0, car 1, …).

**Only one car on IOG?** If you have `num_cars: 2` but only car 0 is enrolled in Octopus Intelligent Go, you do not need to provide a list.
Just keep the single-sensor config (or provide a one-entry list) and car 1 will be managed by Predbat-led charging instead:

```yaml
  num_cars: 2

  # Only car 0 uses IOG - a single entry is sufficient
  octopus_intelligent_slot: 'binary_sensor.octopus_energy_{{DEVICE_ID_CAR1}}_intelligent_dispatching'
  octopus_ready_time: 'time.octopus_energy_{{DEVICE_ID_CAR1}}_intelligent_target_time'
  octopus_charge_limit: 'number.octopus_energy_{{DEVICE_ID_CAR1}}_intelligent_charge_target'
```

*Note:* The `octopus_slot_max` limit applies per Octopus account, not per car, so with two cars on IOG they share one daily allowance between them. Predbat shares it between all the cars with an `octopus_intelligent_slot` entry, so they are taken to be on the one account. It is used up by the cars' dispatches in time order, earliest first, whichever car each one is for, and counted in clock time: two cars given dispatches together from 13:00 to 16:00 use 3 hours of it, not 6.

#### How Predbat picks which car goes in which slot (octopus_automatic)

With **octopus_automatic** set to True, Predbat wires the car slots itself from the devices your Octopus account
reports as live. Devices that Octopus reports as suspended are skipped, and the remaining devices are assigned to
car slots in a fixed order, so the same set of cars always produces the same car indexes.

Slots are re-packed when a car goes away, so removing or suspending a car can move the cars after it up an index -
for example if car 0 is removed, the car that was car 1 becomes car 0. `num_cars` is never reduced automatically, so
the now-unused slot at the end simply falls back to Predbat-led charging. If you set per-car options in `apps.yaml`
(**car_charging_battery_size**, **car_charging_limit**, **car_charging_exclusive**) or use manual SoC entry, check
they still line up after adding or removing a car.

Predbat re-checks this on every device poll (roughly every 2 minutes) and re-wires the slots if the set of live,
non-suspended devices changes - for example when you enrol a second EV, remove one, or suspend one car in favour of
another. You do not need to restart Predbat for a change made in the Octopus app to be picked up. Each re-wire is
logged as:

```text
OctopusAPI: Live intelligent devices changed from [...] to [...], reconfiguring car slots
OctopusAPI: Car slots wired to intelligent devices [...]
```

To confirm which car slot is actually following IOG, look for the per-car lines from the plan cycle:

```text
Car 0 using Octopus Intelligent, charging planned - charging limit ..., ready time ...
Car 0 using Octopus Intelligent, no charging is planned
```

The earlier `Cars {n} charging from battery ... smart ... max_price ...` line is **not** the IOG state. It is printed
during configuration read, before any Octopus dispatch data is merged, and reports the Predbat-led car charging
settings (`car_charging_plan_smart`, `car_charging_plan_max_price`). It shows those settings as configured (for example `max_price: 0.0p`)
even when IOG is working correctly, so do not use it to diagnose IOG linkage.

An excellent [worked example of setting up multiple car charging with Predbat](https://github.com/springfall2008/batpred/discussions/3001) is in the 'Show and tell' part of Predbat's GitHub.

## Ohme car charger direct integration

Predbat can talk directly to the Ohme charger by configuring your Ohme account details in `apps.yaml`.

```yaml
  ohme_login: !secret ohme_login
  ohme_password: !secret ohme_password
  ohme_automatic: true
```

Keep your Ohme login and password in `secrets.yaml` rather than in `apps.yaml` - see [Storing secrets](apps-yaml.md#storing-secrets).

There are two separate automatic settings, so you can have Predbat plan for the car without involving Octopus Intelligent at all:

**ohme_automatic** registers the Ohme charger with Predbat as a car. Predbat wires `num_cars`, `car_charging_planned` (from `binary_sensor.predbat_ohme_connected`, on whenever a car is
plugged in and still wants charge), `car_charging_plugged` (from `binary_sensor.predbat_ohme_plugged`, on whenever a car is plugged in, including after its charge has finished), `car_charging_now` (from `sensor.predbat_ohme_power_watts`, so the car counts as charging from 200W), `car_charging_soc` (from
`sensor.predbat_ohme_battery_percent`) and `car_charging_energy` (see [Ohme charge energy](#ohme-charge-energy) below).
If you have already set `car_charging_now` in `apps.yaml` - to your car's own charging sensor, say - Predbat keeps yours.
The car's battery size and target charge level are left to your existing `car_charging_battery_size` and `car_charging_limit` settings, as Ohme cannot report them.

**ohme_automatic_octopus_intelligent** takes the planned Octopus Intelligent car charging slots, the ready time and the charge limit from Ohme rather than from Octopus Intelligent
directly, by pointing `octopus_intelligent_planned_slot`, `octopus_ready_time` and `octopus_charge_limit` at the Ohme entities. Left unset it is auto-detected: if `ohme_automatic` is on, the Octopus component reports an Intelligent tariff and
there is no Intelligent device on the account for Octopus to report dispatches for, Predbat uses the Ohme slots. Set it explicitly to override that either way - `true` forces it on (needed if you have no
Octopus component for Predbat to detect from), `false` forces it off so the slots come from Octopus directly.

Which device Octopus Intelligent controls matters, because that is the device Octopus schedules the charge through:

- **Your Ohme charger, your car** (a BMW, Mini or Volkswagen linked to Octopus directly, say) **or another make of charger** - Octopus schedules the charge through that device, and the Octopus component
  reports its dispatches. Predbat leaves the car slots, ready time and charge limit with the Octopus component, and does not use Ohme's schedule at all. Ohme's slots are only a copy
  of the same dispatches, passed on a few minutes later, and do not say which kind of dispatch each one is - a boost started from the Ohme app would look like a cheap slot.
  The Ohme is still registered as the charger, so its power and energy readings are used.
  `ohme_control` is ignored here too, as Octopus is already scheduling the charge - unless the car is the Octopus device and you turn on
  **switch.predbat_octopus_intelligent_charger_follows_car**, which lets `ohme_control` drive the Ohme to the car's dispatches (see [Octopus Intelligent and charger control](#octopus-intelligent-and-charger-control)).
  Where the Ohme itself is the Octopus device, Predbat never drives it.
- **Smart charging suspended** on every device in the Octopus app - Octopus is not scheduling anything, so there are no dispatches. Predbat uses Ohme's own schedule at your normal
  tariff rates, as described in [Which car charging plan Predbat shows](#which-car-charging-plan-predbat-shows) below.
- **No Intelligent device on the account at all** - Predbat takes the slots from Ohme, as Octopus has nothing of its own to offer.
  See [Completed slots on Octopus Intelligent](#completed-slots-on-octopus-intelligent) below for where the slots already used come from.

Predbat checks this every two minutes, so linking a different device to Octopus Intelligent is picked up without a restart.

```yaml
  ohme_login: !secret ohme_login
  ohme_password: !secret ohme_password
  ohme_automatic: true
  ohme_automatic_octopus_intelligent: true
```

If you run the Octopus component as well, only one of them can own the ready time and charge limit wiring. Whichever source is in use, Predbat records the owner so the other
component stops re-wiring those settings - previously both could write them and the wiring would alternate as Octopus re-detected your tariff or devices.

### Completed slots on Octopus Intelligent

Ohme only reports the slots of the charging session in progress. Once the car is unplugged that session is over, and Ohme no longer says which slots the car charged in.
If Predbat relied on Ohme alone, the off-peak rate for the slots already used would disappear from the plan history and from today's cost as soon as you unplugged.

So where Octopus has its own record of the dispatches, Predbat keeps `octopus_intelligent_slot` on that sensor for the completed slots and takes only the planned ones from Ohme:

| `octopus_intelligent_slot` is | Completed slots come from | Planned slots come from |
| ----------------------------- | ------------------------- | ----------------------- |
| Wired by the Octopus component to its dispatch sensor, or set in `apps.yaml` to the Octopus Energy integration's 'intelligent dispatching' sensor | That Octopus sensor | Ohme |
| Not set, a pattern that matches no sensor, or an Ohme sensor | Ohme, until the car is unplugged | Ohme |

In the first case a slot that has just finished is still taken from Ohme until Octopus lists it as completed, so there is no gap in between.
In the second case there is no Octopus record to use, so the slots still lose their off-peak rate in the history once the car is unplugged.
To avoid that, run the Octopus component, or leave `octopus_intelligent_slot` on the Octopus Energy integration's sensor rather than replacing it with an Ohme one.

Predbat re-checks this every two minutes and logs which source it is using.

Setting only **ohme_automatic_octopus_intelligent** (with no `ohme_automatic`) still behaves as it did before: the Intelligent slots are wired, and nothing else is.

### Which car charging plan Predbat shows

With `ohme_automatic` on, the car charging plan in Predbat comes from whoever is starting and stopping the charger:

| Setup | Who schedules the car | The car plan in Predbat |
| ----- | --------------------- | ----------------------- |
| Octopus Intelligent tariff with the Ohme as the Intelligent device | Octopus, through Ohme | Octopus's own dispatches, from the Octopus component |
| Octopus Intelligent tariff with no Intelligent device on the account - or any setup with `ohme_automatic_octopus_intelligent: true` | Octopus, through Ohme | Ohme's planned slots and Octopus's completed ones where there is an Octopus sensor, priced at the Intelligent off-peak rate |
| Octopus Intelligent tariff with your car (or another charger) as the Intelligent device | Octopus, through that device | Octopus's own dispatches, from the Octopus component |
| `ohme_control: true` | Predbat | Predbat's own plan, which it carries out on the charger |
| Neither of the above | Ohme | Ohme's own schedule, priced at your normal tariff rates |

In the last case Predbat reads the slots of Ohme's current charge session and uses them as the car plan, so the plan matches what the charger is actually going to do. The slots add the
car's load to the plan and nothing else: your import rates are not changed, and the car is costed at whatever your tariff charges at that time. If Ohme has no slots scheduled, Predbat
plans no car charging. Predbat does not work out a plan of its own here, as nothing would carry it out - turn on `ohme_control` if you want Predbat to decide when the car charges.

Predbat re-checks which row applies every two minutes, so if you move on or off an Octopus Intelligent tariff it switches over by itself, without a restart. The tariff is read from
the Octopus component, which refreshes your account details every 30 minutes. If you set `ohme_automatic_octopus_intelligent` to `true` or `false` yourself, that is kept whatever the tariff.

A few things to know about this mode:

- `car_charging_battery_size`, `car_charging_limit` and the Ohme battery percentage are not used to size the charge - Ohme's schedule is trusted as it stands.
- The energy in each slot is the charge rate Ohme reports for the slot multiplied by its length. Ohme can charge below that rate, so the planned energy can be higher than the car takes.
- If the car is inside an Ohme slot but is not drawing power, Predbat drops the slots from the plan until it sees the car charging again
  (**switch.predbat_octopus_intelligent_dynamic**, see [Checking Intelligent dispatches against the car](#checking-intelligent-dispatches-against-the-car)).
- It relies on **switch.predbat_octopus_intelligent_charging** being on (the default), as the slots reach the plan the same way Octopus Intelligent slots do.
- If `octopus_intelligent_slot` is already set to another sensor, in `apps.yaml` or by the Octopus component, Predbat leaves it alone and the Ohme schedule is not used.
  That is what happens on an Intelligent tariff with `ohme_automatic_octopus_intelligent: false`: the Octopus component supplies the slots and their off-peak rate, not Ohme.

### Predbat-led Ohme charging

**ohme_control** lets Predbat start and stop the charger itself, according to its own car charging plan. Unlike the GivEnergy and myenergi chargers, it is only on when you set it to `true` (see [Predbat starting and stopping your charger](#predbat-starting-and-stopping-your-charger)); otherwise the Ohme keeps to its own schedule, which becomes Predbat's car plan. Set `car_charging_battery_size` and `car_charging_limit` too, as Predbat sets the charger to max charge, which overrides the target in the Ohme app:

```yaml
  ohme_login: !secret ohme_login
  ohme_password: !secret ohme_password
  ohme_automatic: true
  ohme_control: true
```

It requires `ohme_automatic` (there is no plan to enforce until the car is registered) and stands aside when the Intelligent slots come from Ohme, as Octopus drives the charger in that case.

You must still set `car_charging_battery_size` and `car_charging_limit` yourself - Ohme cannot report either, and Predbat needs them to work out how much charge to add. Getting these
right matters more than usual here: setting the charger to max charge overrides its own target percentage, so **the length of Predbat's planned window is the only thing limiting the
charge** - the target you have set in the Ohme app will not stop it. Predbat restores that target when it releases the charger, so your normal Ohme charging is unaffected once Predbat
is no longer in control.

Predbat sets the charger to max charge while a planned window is running, and pauses it the rest of the time. It re-reads the plan every minute rather than following the
`binary_sensor.predbat_car_charging_slot` state directly, so window boundaries are acted on promptly instead of waiting for Predbat's next full update. If you change the charger in the
Ohme app while Predbat is in control, Predbat notices at its next poll and puts it back.

**While Ohme control is on, Predbat owns the charger.** Outside a planned window it holds the charger paused, including when nothing is planned at all. Switching Predbat to
[read only mode](customisation.md#predbat-mode) is what releases it - Predbat then hands the charger back to Ohme's own smart schedule, and picks it up again when you turn read only
off. A component restart deliberately does *not* release the charger, so restarting Predbat will not interrupt a charge in progress. If Predbat stops unexpectedly while the charger is
paused, it stays paused until you turn read only on, set `ohme_control: false`, or resume the charge in the Ohme app.

### Ohme charge energy

Predbat publishes `sensor.predbat_ohme_energy_today` - the energy the charger has delivered to the car so far today, in kWh, resetting at midnight.

Ohme's API does not report delivered energy directly; the figure it does report is the car's own battery level, which reads zero for cars that don't report their state of charge
and jumps by the whole battery content for those that do. Predbat therefore builds the sensor itself by summing the charger's power reading over time, the same approach the
Home Assistant Ohme integration recommends now that its own `energy` sensor has been [removed](devices.md#ohme-ha-in-built-integration). The charge session is polled every two
minutes, so expect an error of up to a couple of hundred Wh per charging session - fine for filtering car charging out of your house load, but not a revenue-grade meter reading.

When **ohme_automatic** is set to `true`, Predbat points [car_charging_energy](apps-yaml.md#car-charging-integration) at this sensor automatically so that
[car charging hold](#filtering-car-charging-energy-from-house-load) can subtract your car charging precisely rather than falling back to the `car_charging_threshold` heuristic. If you already have
another charger's energy sensor configured - a Zappi or Wallbox, say - Predbat leaves your setting alone and logs that it has done so.

## Wallbox car charger direct integration

Predbat can talk directly to your Wallbox charger by configuring your Wallbox account details in `apps.yaml`:

```yaml
  wallbox_username: !secret wallbox_username
  wallbox_password: !secret wallbox_password
  # Let Predbat pause and resume the charger from its own car charging plan
  #wallbox_control: True
```

Predbat then registers each charger on the account as a car and sets **car_charging_energy**, **car_charging_planned**, **car_charging_power** and **car_charging_now** for you. A Wallbox charger cannot report the car's state of charge, so set **car_charging_soc** from your car's own integration if you want Predbat to plan to a target.

See [Wallbox Charger](components.md#wallbox-charger-wallbox) for the entities it publishes and how Predbat-led charging behaves.

If you would rather use the Home Assistant Wallbox integration, see [Wallbox Pulsar](devices.md#wallbox-pulsar).

## Hypervolt car charger direct integration

Predbat can talk directly to your Hypervolt charger by configuring your Hypervolt account details in `apps.yaml`:

```yaml
  hypervolt_username: !secret hypervolt_username
  hypervolt_password: !secret hypervolt_password
  # Let Predbat stop and start the charger from its own car charging plan
  #hypervolt_control: True
```

Predbat then registers each charger on the account as a car and sets **car_charging_energy**, **car_charging_planned**, **car_charging_plugged**, **car_charging_power** and **car_charging_now** for you. A Hypervolt cannot report the car's state of charge, so set **car_charging_soc** from your car's own integration if you want Predbat to plan to a target.

For Predbat-led charging (`hypervolt_control: True`), set the charger to `boost` mode and turn off any schedule in the Hypervolt app: a schedule holds the charger to its own times, and the Eco modes only charge from spare solar.

A Hypervolt 2.0 on older firmware cannot report a car that is plugged in but not charging, so Predbat only sees the car as plugged in while it charges, and logs a note naming any such charger. With Predbat-led charging, leave the charger in plug and charge: the car starts charging when it is plugged in, Predbat sees it, and from then on treats a charger it has stopped as still connected so the car keeps its plan. Without Predbat-led charging, if you want Predbat to plan for the car before it starts charging, set `hypervolt_automatic: False` and configure the car charging settings yourself, with **car_charging_planned** pointing at a helper you switch, as in [Example EV and charger setup](#example-ev-and-charger-setup) below. The same limit means such a charger's plugged-in sensor never reads on or off, so **car_charging_plugged** cannot reset the manually tracked car SoC when the car is unplugged; if you rely on that, set `hypervolt_automatic: False` and set **car_charging_plugged** yourself, as automatic configuration would otherwise replace it. Newer firmware and Hypervolt 3 chargers report the cable and need none of this.

See [Hypervolt Charger](components.md#hypervolt-charger-hypervolt) for the entities it publishes and how Predbat-led charging behaves.

If you would rather use the Home Assistant Hypervolt integration, see [Hypervolt](devices.md#hypervolt).

## GivEnergy Gateway OCPP EV charger

When Predbat is connected to a GivEnergy Gateway that has an OCPP EV charger attached, the charger's live state is reported to Predbat over the gateway's MQTT telemetry and exposed as Home Assistant entities.

Entities are named after the charger, using the last 6 characters of its OCPP charge point id (lower-cased) — for example a charge point id ending `3XB749` gives `sensor.predbat_gateway_ev_3xb749_power`.
The id is used whether one charger or several are attached, so a charger keeps the same entity ids for its whole life. Below, `<id>` stands for that suffix; a charger that has not yet reported an id falls back to a bare `ev`.

- `binary_sensor.predbat_gateway_ev_<id>_connected` - a car is connected to the charge point (off while the charger is offline)
- `binary_sensor.predbat_gateway_ev_<id>_plugged` - a car is plugged in; unavailable while the charger is offline, as the charger cannot then say
- `sensor.predbat_gateway_ev_<id>_status` - OCPP status (e.g. `Available`, `Charging`)
- `sensor.predbat_gateway_ev_<id>_power` - live charge power (W)
- `sensor.predbat_gateway_ev_<id>_session_energy` - energy delivered this session (kWh)
- `sensor.predbat_gateway_ev_<id>_soc` - EV battery SoC % (reported directly by the car, or estimated from session energy / configured battery size when the car does not report it)
- `sensor.predbat_gateway_ev_<id>_current_limit` / `sensor.predbat_gateway_ev_<id>_max_current` - present and configured charge current (A)
- `sensor.predbat_gateway_ev_<id>_voltage` - supply voltage (V)
- `sensor.predbat_gateway_ev_<id>_eco_mode` - the charger's current EcoMode setting
- `sensor.predbat_gateway_ev_<id>_charge_rate` - charge-rate capability (kW), derived from the reported current/voltage (falls back to 7.4 kW when the charger does not report its capability)

To have Predbat plan for the gateway charger as a car, enable it in `apps.yaml`:

```yaml
  gateway_evc_automatic: true
```

When enabled, Predbat registers the charger as a car and maps the EV entities above onto the standard `car_charging_*` settings, so the normal [Predbat-led car charging](#predbat-led-charging) planning applies.
The charge rate tracks the `sensor.predbat_gateway_ev_<id>_charge_rate` capability sensor. The car's battery size and target charge level are taken from your existing `car_charging_battery_size` and `car_charging_limit` settings (the charger cannot report them).
`car_charging_soc` is not mapped: a charger rarely knows the car's SoC, and when it does only during a session. Instead the car's SoC is tracked in **input_number.predbat_car_charging_manual_soc_kwh** from the session energy and the plugged-in sensor (`car_charging_plugged`)
(see [manual car SoC](#predbat-led-charging)), so you can set it to the car's real level when you plug in. If you map `car_charging_soc` to `sensor.predbat_gateway_ev_<id>_soc` yourself, it holds the last SoC the charger reported between sessions;
until the charger has reported one it is an estimate from the session energy. `sensor.predbat_gateway_ev_<id>_session_energy` reads unavailable while the charger is offline.

To also have Predbat send the plan to the charger (so the EVC charges according to Predbat's schedule), add a second flag:

```yaml
  gateway_evc_automatic: true
  gateway_evc_control: true
```

When `gateway_evc_control` is enabled, Predbat compares the charger's reported state with the planned car-charging windows (from `binary_sensor.predbat_car_charging_slot`) once per minute and sends OCPP commands through the hub to bring the two into line:

Read-only mode blocks all of these automatic EV commands, including a live override from another component. Charging already in progress is left alone until read-only mode ends.

For a car left plugged in across several windows, use Hub firmware containing [predbat-gateway #501](https://github.com/Predictive-Cloud-Ltd/predbat-gateway/pull/501). That fix preserves the car's plugged-in status after a stop and ignores connector-0 status for the plug. Older firmware can falsely report `Available`, which clears the car's future charging plan on the next fetch. Predbat does not assume a car is present merely because the charger is online.

- **Inside a window**, with no charging session, it sends `SetChargingProfile` (at the charger's max current) followed by `RemoteStartTransaction`, unless the charger reports `Unavailable`, `Faulted` or `Offline`. It does not additionally require a plugged-in status at command time. The car must still have a plan from the last fetch cycle.
- **Outside every window**, with a charging session active, it sends `RemoteStopTransaction`. The charger does not report who started a session, so this stops **any** session outside the plan — including one started from the charger's app, a button or its own schedule — for as long as `gateway_evc_control` is on. Turn it off to charge outside Predbat's plan.

Because Predbat works from what the charger reports rather than from what it last sent, a restart, a lost connection or a command the charger ignored is corrected on a later minute. Each start or stop is tried at most three times, at least two minutes apart. After the third attempt Predbat logs a warning and gives up until a planned window next starts or ends, even if the charger's state changes in the meantime. A start that does not take is normal when no car is plugged in. A session that will not stop is flagged as an error in `predbat.status` and needs stopping by hand. If the charger is offline or the hub has sent no charger data for two minutes, Predbat cannot see its state, so it sends nothing and logs a warning. Only one charger is controlled: the one `gateway_evc_automatic` registered as the car, whose plan is the one being enforced.

`car_charging_now` is wired to the charger's charging sensor, which is on while the car is drawing power (OCPP status `Charging`), so Predbat holds the house battery for the car while it charges. A car that stays connected once it is full or paused (`SuspendedEV`) does not hold the battery. It never adds a charging slot, so it cannot hold a session open against the window boundaries `gateway_evc_control` enforces.

## Predbat starting and stopping your charger

Predbat can start and stop a GivEnergy EV charger, an Ohme charger or a myenergi Zappi itself, following its own car charging plan. Each charger follows its own car: charger N follows car N.

Each has a control setting, off unless you set it to `true` in `apps.yaml`. Control also needs the automatic setup that maps the charger to its car:

| Charger | Setting | Also needs |
|---------|---------|------------|
| GivEnergy EV charger | `ge_cloud_evc_control` | `ge_cloud_automatic_evc` set to `true` |
| Ohme | `ohme_control` | `ohme_automatic` set to `true` |
| myenergi Zappi | `myenergi_zappi_control` | `myenergi_automatic`, `myenergi_automatic_zappi` and `myenergi_enable_controls` (all on by default) |

If something it needs is missing, Predbat logs a warning saying which and leaves the charger alone.

Without `ohme_control`, Predbat already follows the Ohme: the charger's own schedule is taken as the car charging plan. Predbat control sets it to max charge, which overrides the target in the Ohme app, so with `ohme_control: true` also set the car's battery size and charge limit - left at their defaults (100 kWh, 100%) the car would be charged to full.

A Zappi Predbat controls is Stopped outside a planned window, so it no longer diverts surplus solar to the car - leave `myenergi_zappi_control` off if you would rather keep solar diversion.

The GivEnergy Gateway's `gateway_evc_control` (above) works the same way: off unless you set it.

### Octopus Intelligent and charger control

On Octopus Intelligent (or E.ON/EDF SmartFlex through the Kraken component), what Predbat does depends on which device Octopus drives for that car. Predbat reads this from the car's own dispatch sensor in `octopus_intelligent_slot`:

- **Octopus drives the charger** - your charger is the Intelligent device, or that car's Intelligent slots come from the Ohme. Predbat leaves the charger alone, and hands it back if it was holding it, so the two do not fight. Handing back never starts a charge: a charger Predbat had stopped is left for Octopus to start, an Ohme has max charge turned off, and a Zappi goes back to its own mode (Eco+ in place of Fast). The exception is a Hypervolt, which is let charge again: it is not known whether Octopus can undo a Hypervolt stop, and a stop it could not undo would leave the car unable to charge.
- **Octopus drives the car** - the car itself is the Intelligent device, so the car starts and stops its own charge. By default Predbat leaves the charger alone, and hands back a charger it was holding as usual, so one it had stopped is started again rather than left stopped. Turn on **switch.predbat_octopus_intelligent_charger_follows_car** (*expert mode*) to have Predbat drive the charger instead: on while a dispatch is running, off otherwise, so the car cannot charge outside the dispatches on its own timers. It follows both its plan and the dispatch times on the car's dispatch sensor, so a new dispatch starts the charger within a few minutes of Octopus announcing it rather than waiting for the next plan, and the charger stops when the dispatch ends.
- **Predbat cannot tell** - for example the dispatch sensor comes from the Octopus Energy integration, which does not say, or Predbat's Octopus component has not discovered your devices yet. Setting the charger's control setting to `true` tells Predbat your charger is not the Octopus device, so Predbat drives the charger to its plan. If your charger is the Octopus device, leave its control setting off.

Wherever Predbat drives the charger with **switch.predbat_octopus_intelligent_charging** on, it also follows the dispatch times on the car's dispatch sensor, as above. While **octopus_intelligent_smart_control** is `off` it does not follow the planned ones, which Octopus will not carry out, apart from bump/boost charges you asked for yourself.

**switch.predbat_octopus_intelligent_charging** only changes Predbat's own planning. With it off, a charger Octopus drives is still left to Octopus - Octopus goes on switching it either way - and a charger for a car Octopus drives follows Predbat's own plan, whether or not **switch.predbat_octopus_intelligent_charger_follows_car** is on.

### Restarts and charger control

For the GivEnergy, myenergi, Ohme and Wallbox chargers, Predbat saves which chargers it has stopped, alongside the control switch. After a restart it still knows, so turning on read only mode, turning the control switch off, turning on guest charging or turning the charger's control setting off in `apps.yaml` lets them charge again. If Octopus Intelligent now drives that charger, Predbat hands it to Octopus instead of starting it. A restart on its own releases nothing, so it does not interrupt a charge. A stop is saved before it is sent, so one whose reply was lost is released like any other. With `myenergi_enable_controls` off Predbat never writes to a Zappi, so it is not released either.

### Guest charging

While Predbat drives a charger it holds it off outside the planned windows, so a visitor's car would not charge. Turn on the charger's guest charging switch - `switch.predbat_gecloud_guest_charging`, `switch.predbat_myenergi_guest_charging`, `switch.predbat_ohme_guest_charging` or `switch.predbat_hypervolt_guest_charging` or `switch.predbat_wallbox_guest_charging` - and Predbat hands the charger back, as it does for read only mode. Choose how the guest charges (boost, solar only and so on) on the charger itself.

Guest charging turns itself off when a car plugged in after you switched it on is unplugged, or after 12 hours. Unplugging a car that was already on the charger - your own, making way for the guest - does not end it, so if the guest plugged in before you switched it on, it runs until the 12 hours are up or you switch it off. It is not remembered across a restart, which puts Predbat back in charge.

While it is on, the guest's charging is counted as your car's charging - Predbat has no way to tell the two apart.

## Car Charging Planning

There are two ways that Predbat can plan the slots for charging your car:

### Octopus-led charging

- **switch.predbat_octopus_intelligent_charging** - Turn this Home Assistant switch to On and Predbat will plan charging around the Intelligent Octopus slots, taking it into account for battery load and generating the slot information

- You should set the car's current SoC sensor, **car_charging_soc** in `apps.yaml` to point to a Home Assistant sensor that specifies the car's current % charge level to have accurate detection of when the car charging will be complete.
This should normally be a sensor provided by your car charger.
If you don't have this available for your charger then Predbat will assume the car's current charge level is 0%.

- If you set **car_charging_limit** in `apps.yaml` then Predbat can also know if the car's limit is set lower than in Intelligent Octopus.
If you don't set this Predbat will default to 100%.

- **octopus_charge_limit** and **octopus_ready_time** in `apps.yaml` are pre-configured with regular expressions to point to the appropriate sensors for your EV charger in the Octopus Energy integration.
These retrieve details of the charge limit and when the car will finish charging from your Octopus app settings.
Again, if you are using the Octopus Energy direct method for Predbat then these configuration lines are not required and should be commented out of `apps.yaml`.

- You can configure **car_charging_now** in `apps.yaml` to point to a Home Assistant sensor that indicates that the car is currently charging - an on/off sensor, or a charging power sensor.
Predbat then holds the house battery for the car while it charges, and checks the car really is charging during each dispatch - see [Checking Intelligent dispatches against the car](#checking-intelligent-dispatches-against-the-car).
It does not add an Intelligent slot or its cheap rate for a charge the Intelligent API hasn't reported - those come only from Octopus's dispatches.

- The switch **switch.predbat_octopus_intelligent_consider_full** (*expert mode*)
(default is Off) when turned On will cause Predbat to predict when your car battery is full and assume no further charging will occur.
This can be useful if Octopus does not know your car battery's state of charge but you have a sensor setup in Predbat (**car_charging_soc**) which does know the current charge level.
Slots your car won't need are also not trusted as low rate for the house battery: Octopus only bills a slot at the low rate when the car charges in it, so once Predbat expects the car to be full, the rest of that dispatch and any later daytime slots are planned at the normal rate.
The half hour in which the car is expected to finish stays low rate, as does any slot another car still needs and the overnight 23:30-05:30 rate, which is part of the tariff.

- The switch **switch.predbat_octopus_intelligent_ignore_unplugged** (*expert mode*) (default value is Off) can be used to prevent Predbat from assuming the car will be charging or that future extra low-rate slots apply when the car is unplugged.
This will only work correctly if **car_charging_planned** is set correctly in `apps.yaml` to detect your car being plugged in

- The switch **switch.predbat_octopus_intelligent_dynamic** (*expert mode*) (default value is On) checks each dispatch against whether your car is actually charging, and cancels the car's slots and the dispatch's cheap rate when it is not - see [Checking Intelligent dispatches against the car](#checking-intelligent-dispatches-against-the-car).
It is independent of **switch.predbat_metric_dynamic_load_adjust**.

- The switch **switch.predbat_octopus_intelligent_trust_slots** (*expert mode*) (default value is On) controls whether Predbat believes an Intelligent charging slot will happen before your car has actually been seen charging in it.
When On, every slot is trusted until the car is seen not charging in one (see [Checking Intelligent dispatches against the car](#checking-intelligent-dispatches-against-the-car)).
When Off, Intelligent slots are assumed not to happen: the car's charging is not predicted and a daytime slot's low rate is not used for the house battery, until the car is seen charging inside a slot.
From then on that slot and the later ones are trusted, until the car stops charging or the slot ends, after which the next slot has to be confirmed again.
The overnight 23:30-05:30 rate stays low either way, as that is part of the tariff.
Whether the car is charging is judged the same way as for **switch.predbat_octopus_intelligent_dynamic**. With neither **car_charging_now** nor the car inside the CT clamp, the car's slots can never be confirmed, and Predbat logs a warning.
It needs **switch.predbat_octopus_intelligent_dynamic** On, which does the checking; with that Off this switch has no effect and Predbat logs a warning.
It only applies when **switch.predbat_octopus_intelligent_charging** is On, as it acts on the car plan that Octopus Intelligent charging builds. With that Off, the Intelligent dispatch rates are used as before and Predbat logs a warning.

- Octopus often moves or withdraws a dispatch before it starts, so Predbat plans for that in its pessimistic PV10% scenario (weighted by **input_number.predbat_pv_metric10_weight**).
In that scenario, any part of a dispatch more than 30 minutes ahead that is cheap only because of the dispatch - not the fixed 23:30-05:30 off-peak - is assumed to go away.
The rest of the house then pays the peak rate for those minutes, and the battery is not held for the car, although the car's own charging is still costed at the dispatch rate so the two scenarios stay comparable.
The effect is that with a low battery Predbat may top it up in the first half hour of a dispatch, which is treated as certain, so that it can carry the house through to the fixed off-peak window if the rest of the dispatch disappears.
This applies whether the dispatches come from the Octopus Energy integration or from Predbat's own Octopus connection.

- Let the Octopus app control when your car charges.

#### Checking Intelligent dispatches against the car

Octopus Intelligent tells Predbat when it plans to charge your car (its *dispatches*), and Predbat treats those times as cheap for the house battery too - it may charge the battery then, or hold it back for the car.
But a dispatch does not always happen: the car may be full, asleep or unplugged, or Octopus may change its plan. Octopus may also bill a dispatch the car does not use at the full rate.
**switch.predbat_octopus_intelligent_dynamic** (*expert mode*, On by default) makes Predbat check that the car really is charging during a dispatch, and stop relying on the dispatch when it is not.
It only applies to Octopus Intelligent charging (**switch.predbat_octopus_intelligent_charging** On); slots Predbat plans itself (Predbat-led charging) are never checked.

Octopus only confirms a dispatch as completed some time after it has run, typically 15 to 60 minutes later.
Until then Predbat keeps the dispatch rate on each half hour the dispatch ran in, unless the check above cancelled it, so today's cost does not jump up after each half hour of charging and fall back later.
A half hour Octopus has still not confirmed after 90 minutes goes back to your normal rate.

With the Octopus component, Predbat also reads what Octopus says the device is doing:

- **Smart charge in progress**: if the car is charging (from **car_charging_now**) in a half hour Octopus has no dispatch listed for, that half hour is kept at the dispatch rate as well.
  Octopus starts charging as soon as the car is plugged in, but only lists that first half hour once it has completed, an hour or so later.
  Octopus also answers with no planned dispatches for a few minutes while it re-plans a charge in progress, so Predbat keeps the last plan it had for up to 10 minutes.
- **Unplugged, or smart control off**: Octopus is not dispatching anything, so Predbat ignores any planned dispatches Octopus still lists.
- **Boosting**: a boost is billed at the full rate and Octopus lists it as a boost, so it is never treated as cheap.

**Setting it up**

Point **car_charging_now** in `apps.yaml` at a sensor that shows the car charging.
If you use `ohme_automatic`, `myenergi_automatic`, `wallbox_automatic` or the Predbat gateway with `gateway_evc_automatic`, this is already done for you.
It can be an on/off sensor (matched against **car_charging_now_response**), or a charging power sensor in W or kW, where 200W or more counts as charging - useful for chargers such as Wallbox that have no "charging" sensor:

```yaml
  car_charging_now:
    - sensor.wallbox_portal_charging_power
```

A status sensor works too, as long as its state while charging is one of the **car_charging_now_response** values. For example a myenergi Zappi's plug status, from the Home Assistant myenergi integration, reads `Charging`, which the default list accepts:

```yaml
  car_charging_now:
    - sensor.myenergi_zappi_XXXXXXXX_plug_status
```

If you set **car_charging_now_response** yourself, keep `charging` in it, and the values your other cars' sensors use: the one list covers every car.
With `myenergi_automatic`, Predbat already points **car_charging_now** at the Zappi's charging power, so leave it unset.

If the sensor's state never matches, Predbat never sees the car charging, so it cancels every dispatch the car is in, even while the car charges.

Without **car_charging_now**, if your car is inside the CT clamp (**switch.predbat_car_energy_reported_load** On), Predbat uses the house load instead - slower and less certain, as other appliances also move it.
With neither, nothing is checked.

**How Predbat decides**

Octopus schedules dispatches to the minute, and Predbat checks them against their real start and end times. Back-to-back dispatch slots count as one dispatch.

| Evidence | A "not charging" reading counts... | Slots cancelled, at the earliest |
|---|---|---|
| **car_charging_now** | from 3 minutes into the dispatch, while the car and charger wake up, and it must last 2 minutes | 5 minutes after the dispatch starts |
| House load | only for a 5-minute load reading taken entirely inside the dispatch, and two in a row must be too low for a car to be charging | 10 minutes after the dispatch starts |

The house load is too low for a car to be charging when it is under 85% of **car_charging_threshold** (5.1kW with the default of 6kW).
Where **load_power** is set in `apps.yaml`, the live load has to be under that figure as well as the 5-minute reading: the 5-minute reading lags a car that is still ramping up,
and an inverter whose data arrives late, so on its own it can read low while the car is charging at full power.
A low 5-minute reading with a live load that is not low, or is unavailable, is ignored - it neither cancels the slots nor brings them back.

A reading that shows the car charging counts straight away, and an "unknown" or "unavailable" sensor is ignored rather than taken as not charging.
With more than one car, any car's **car_charging_now** showing charging counts for all of them. Octopus links a dispatch to one car, but Predbat cannot tell which charger that car is plugged into: with two Zappis, `myenergi_automatic` numbers them by serial, not by which one Octopus controls.
With **car_charging_now**, Predbat checks the sensor every 15 seconds between plan updates, so it reacts within seconds of these times.

**Very short dispatches**

Octopus sometimes issues dispatches only a minute or two long, which the car is unlikely to charge in.
A dispatch shorter than 5 minutes is never treated as cheap for the house, and does not use up any of the day's low-rate allowance (**octopus_slot_max**), unless Predbat sees the car charging during the half hour it falls in. Then that whole half hour is cheap.
That check needs **switch.predbat_octopus_intelligent_dynamic** On. In practice it also needs a **car_charging_now** sensor, because only the 15-second check is quick enough to catch a dispatch that short.
Without one, very short dispatches always stay at the normal rate.
The car's own plan still includes them, priced at the normal rate.
This does not apply when **intelligent_dispatch_billing** is `window`, where the supplier bills every dispatch half hour cheap whether or not the car charges.
This only covers the dispatches Predbat prices itself. If your import rates come from the Octopus Energy integration and the integration has already lowered the rate for a short dispatch, Predbat leaves that rate as it is.

**What happens when the car is not charging**

- That dispatch slot and every later one for the car are cancelled: Predbat no longer holds the battery for the car ("Hold for car"), no longer predicts the car's load, and no longer uses the dispatch's cheap rate for the house battery. The overnight 23:30-05:30 rate stays cheap, as that is part of the tariff.
- If the car was seen charging earlier in the same half hour (for example it finished its planned kWh early), the house keeps the cheap rate until the end of that half hour: once a dispatch has started, Octopus bills the whole half hour off-peak. Later half hours of the dispatch lose it as above. A **car_charging_now** reading only counts for this from 2 minutes into the half hour, as the sensor can still show charging for a car that stopped just before it. Without **car_charging_now**, this needs the house load to have reached **car_charging_threshold**, so a cooker or hot tub alone does not count.
- This is remembered across a Predbat restart, as is which cars are currently cancelled, so restarting part-way through a dispatch does not lose the kept half hour or briefly trust a slot the car has stopped charging in.
- The slots come back as soon as the car starts charging again, or the dispatch ends.
- Cancelled slots are still shown in the car column of the plan with a question mark after the kWh (e.g. **3.5?**), so you can see the car's schedule even though the plan is not counting on it.
- The log shows `Octopus Intelligent: car 0 is in a dispatch but not charging, cancelling its slots`, and later `Octopus Intelligent: car 0 slots resumed`.

For example, Octopus dispatches the car from 15:55 to 16:01 but the charger reports 0W throughout:

| Time | What Predbat does |
|---|---|
| 15:55 | The dispatch starts. Too early to judge - the car may still be waking up. |
| 15:58 | Still 0W, 3 minutes in: Predbat starts timing. |
| 16:00 | Still 0W after 2 more minutes: the slots are cancelled, the battery hold is released, and the dispatch's cheap rate is dropped. |
| 16:01 | The dispatch ends, and the car's next dispatch is trusted again as normal. |

**Only trusting slots once the car charges**

**switch.predbat_octopus_intelligent_trust_slots** (*expert mode*, On by default) reverses this check when turned Off: Intelligent slots are assumed *not* to happen until the car is seen charging inside one, see below.

**If nothing is ever cancelled**

- Check **switch.predbat_octopus_intelligent_dynamic** is On (you need expert mode to see it).
- Check **car_charging_now** names a real sensor, e.g. `sensor.wallbox_portal_charging_power`, not a fixed value like `off` - or that your car is inside the CT clamp.
- Check **switch.predbat_octopus_intelligent_charging** is On, so Predbat builds the car plan from the Octopus dispatches.
- Look in the log for `Octopus Intelligent: car` lines. If there are none while the car sits idle in a dispatch, the check is not running. Note that the `Dynamic load last period ...` line is written every cycle whatever these settings are, so it does not show the check is running.

**If a dispatch is cancelled while the car is charging**

The log shows `car 0 is in a dispatch but not charging, cancelling its slots` although the car is charging. Predbat is not reading **car_charging_now** as charging:

- Look at the sensor's history in Home Assistant while the car charges. If it shows a status such as `Charging`, check that value is in **car_charging_now_response**. A `car_charging_now_response` list you set in `apps.yaml` replaces the default, so it must include `charging` itself.
- The log's `Cars ... charging_now [False]` line shows what Predbat made of the sensor each cycle.
- A charging power sensor (200W or more counts as charging) avoids matching status text altogether.

**Charging the battery while the car is charging**

A dispatch is only cheap while the car really charges in it, and the car could be unplugged at any time, so the half hour it is charging in now is the one certain part of the dispatch.

While your car is in a dispatch that has not been cancelled (the same check that marks cancelled slots with a **?**), and topping up the battery in the dispatch would cost the same as waiting for your fixed off-peak window, Predbat now prefers the dispatch, earliest half hours first. With **switch.predbat_octopus_intelligent_trust_slots** Off this only starts once the car has been seen charging. For example, with a dispatch from 19:00 and off-peak from 23:30, the battery charges at 19:00 rather than waiting, so the house is covered if the car is unplugged at 20:00.

- This only breaks ties: it never changes the predicted cost, and a cheaper slot later on still wins.
- It does not apply to a dispatch inside your fixed off-peak window, which carries no such risk.
- The log shows `Octopus Intelligent: car charging in its dispatch, preferring battery charge in the run to ...`, and Predbat replans straight away when this starts.

#### Reading the dispatch timeline in the logs

When Octopus Intelligent charging is active Predbat writes a diagnostic line to the log each cycle
showing how your dispatch slots have changed over time. It is purely informational - nothing in the
plan depends on it - but it is the quickest way to see whether Octopus has moved or withdrawn a slot
Predbat was relying on:

```text
Octopus: Dispatch timeline car 0 @ 09-13 18:00:00 [-4h..+24h]: -----P--|....p.......................... soc 12.4/40.0kWh plugged
```

Each character covers 30 minutes, running from 4 hours in the past to 24 hours ahead, and the `|`
marks now - so everything left of it has already happened. The characters are:

| Symbol | Meaning |
|--------|---------|
| `.` | Nothing scheduled, at a normal (expensive) import rate |
| `-` | Nothing scheduled, but this is a cheap slot - normally the overnight off-peak session |
| `?` | The import rate for that block is not known yet (rates are still being fetched) |
| `p` | A planned (provisional) dispatch slot |
| `s` | A slot Octopus has started |
| `c` | A completed slot |
| `P` `S` `C` | UPPERCASE means Predbat's own plan is charging in that slot too |
| `I` | Predbat plans to import here on a cheap rate, with no dispatch slot |
| `X` | Predbat plans to import here at a normal rate, with no dispatch slot |

The case distinction is the useful one. A lowercase `p` that vanishes costs nothing because Predbat
was not relying on it, whereas an uppercase `P` that disappears before reaching the `|` column is a
charge Predbat had committed to and will now not get - so the slots worth worrying about are the
ones that shout.

`X` is the one to watch for. Every block where Predbat plans to import shows as exactly one of
`P`/`S`/`C` (inside a dispatch slot), `I` (cheap rate, no slot) or `X` (normal rate, no slot). When
Octopus withdraws a slot Predbat had committed to, the dispatch letter disappears but the import
does not - so the stripe turns from `P` into `I` or `X` instead of vanishing, and an `X` trail
means Predbat is planning to import at full price where it expected a dispatch.

The end of the line shows the car's current SoC and target, and whether the car is plugged in
(`plugged` / `unplugged`, from the **car_charging_planned** sensor in `apps.yaml`). An unplugged car
with planned slots is normal - Octopus still publishes the schedule - but an unplugged car is also
the usual explanation for a plan that never charges.

Lines are written every 30 minutes, plus immediately whenever the timeline changes - a change-driven
line is marked with a trailing `*`. Because every line is the same width and aligned to the same
30-minute grid, stacking them in a monospace viewer shows each dispatch drifting one column left per
line, so a withdrawn slot appears as a stripe that stops before it reaches `|`.

### Predbat-led charging

Here Predbat plans and can initiate the car charging based upon the upcoming low import rate slots

- Ensure **car_charging_limit**, **car_charging_soc** and **car_charging_planned** are set correctly in `apps.yaml` to point to the appropriate sensors from your EV (see [Car charging config in apps.yaml](#configure-appsyaml-for-your-car-charging))

- Check (and if necessary add) the sensor response value from the sensor configured in **car_charging_planned** that is returned when the car is 'plugged in and ready to charge' is in the list of **car_charging_planned_response** values
configured in `apps.yaml`

- If your car does not have a state of charge (SoC) sensor (**car_charging_soc** is not set in `apps.yaml` for that car) but its charger has an energy sensor (**car_charging_energy**)
and a plugged-in sensor (**car_charging_plugged** - **car_charging_planned** is not enough), Predbat tracks the car's SoC in kWh in **input_number.predbat_car_charging_manual_soc_kwh** (for car 0) itself.
Without both sensors the car is planned from empty each time, as Predbat would have no way to tell when the car stops charging or is unplugged.<BR>
To track the SoC in any other case - a car with no sensors at all, or one whose SoC sensor you would rather not use - turn on **switch.predbat_car_charging_manual_soc** (for car 0).
If you do have a car SoC sensor and leave the switch off, Predbat shows its reading (in kWh) in **input_number.predbat_car_charging_manual_soc_kwh** and changing it there has no effect;
turning the switch on then starts the manual SoC from the sensor's last reading.<BR>
For multiple cars, use **switch.predbat_car_charging_manual_soc_1/2/3** and **input_number.predbat_car_charging_manual_soc_kwh_1/2/3** for cars 1, 2, and 3 respectively.<BR>
Set the value to the car's current charge level before charging. Predbat then keeps it up to date:
    - If **car_charging_energy** is set for the car in `apps.yaml` Predbat adds the energy the charger's history shows (less **input_number.predbat_car_charging_loss**),
    but no more than the car could have taken at **car_charging_rate** (plus 10%), so a glitch in the charger's energy counter cannot fill the car.
    With more than one car, **car_charging_energy** must be a list with the cars' chargers first, in car order. A single sensor is used for car 0 only.
    - Otherwise Predbat adds the charge it planned for the car during each charging slot - unless **car_charging_now** is set and reports that the car is not charging.
    - The plan never uses more than the car's battery size or less than zero, whatever value is entered. The value itself is kept as entered or counted, as the battery size Octopus Intelligent reports can be larger than the configured one.
    - If **car_charging_plugged** is set to a sensor for the car in `apps.yaml` the value is reset to 0 when the sensor goes from plugged in to unplugged
    and stays unplugged for 10 minutes, ready to be set again when the car is next plugged in. The wait means a charger that briefly reads unplugged while it reconnects does not reset it. A sensor that becomes unavailable does not reset it, and a value you enter while the car is unplugged is kept.
    **car_charging_planned** never resets it, as many chargers turn it off when a charge finishes with the car still plugged in.
    - Without **car_charging_plugged** the value is reset to 0 once the car has been idle for 12 hours - no value entered, no charge counted or planned, and **car_charging_now** (if set) not reporting charging.
    - A value you enter is the new starting point: energy is counted from when you entered it.
    - Predbat publishes where it has got to as **sensor.predbat_car_manual_soc_tracked** (`_1`, `_2`... for further cars): the tracked kWh, with the time energy is counted from (`since`), the kWh already added since then (`counted`), the last plugged-in reading (`plugged`), when a pending unplug was first seen (`unplugged_since`) and when the car was last active (`active`) as attributes.
    It reads this back when it restarts, so charging or unplugging while Predbat is restarting is still counted.<BR>
NB: **input_number.predbat_car_charging_manual_soc_kwh** must be set to the current kWh value of your car battery NOT a percentage SoC figure
otherwise, Predbat won't know how much energy there currently is in the battery.<BR>
NB2: If you have **car_charging_soc** set and working for your car SoC sensor in `apps.yaml`, leave **switch.predbat_car_charging_manual_soc** Off as otherwise the car SoC sensor will be ignored

- Ensure **switch.predbat_octopus_intelligent_charging** in Home Assistant is set to Off

- Set **input_number.predbat_car_charging_rate** to the car's charging rate in kW per hour (e.g. 7.5 for 7.5kWh)

- If you have more than one car then **input_number.predbat_car_charging_rate_1** will be the second car etc.
To set the starting values in `apps.yaml` instead, use either a key per car (`car_charging_rate: 11.0` and `car_charging_rate_1: 7.4`) or one list with an entry per car (`car_charging_rate: [11.0, 7.4]`).
As with any Predbat setting these are only the initial values, once the input_number exists in Home Assistant its value is what Predbat uses.

- Set **select.predbat_car_charging_plan_time** to the time you want the car charging to be completed by

- **switch.predbat_car_charging_plan_smart** (On by default) makes Predbat use the cheapest slots before the charge-by time.
When disabled (turned Off) all low-rate slots will be used in time order, so the car starts charging in the first low-rate slot after it is plugged in, even when cheaper slots come later.
Low-rate slots are time periods where the import rate is below the threshold determined by **input_number.predbat_rate_low_threshold** (*expert mode*).
By default this threshold is calculated automatically based upon future import rates - see [Battery margins and metrics options](customisation.md#battery-margins-and-metrics-options) for details of configuring this threshold.

- You can set **input_number.predbat_car_charging_plan_max_price** if you want to set a maximum price in pence per kWh to charge your car (e.g. 10p).
If you set this to zero, this feature is disabled, and all low-rate slots will be used.
This may mean you need to use expert mode and change your low-rate threshold (**input_number.predbat_rate_low_threshold**) to configure which slots should be considered if you have a tariff with more than 2 import rates (e.g. Flux)

- You can set **car_charging_now** in `apps.yaml` to a sensor that reports the car charging, so Predbat holds the house battery while it charges - including a charge you start by hand.
It never adds a charging slot, so it cannot keep **binary_sensor.predbat_car_charging_slot** (and your automation) on.

- Predbat will set **binary_sensor.predbat_car_charging_slot** when it determines the car can be charged; you will need to write a Home Assistant automation based upon this sensor to control when your car charges.

A sample automation to start/stop car charging using a Zappi car charger and the [MyEnergi Zappi integration](https://github.com/CJNE/ha-myenergi) is as follows,
this should be adapted for your charger type and how it controls starting/stopping car charging:

```yaml
alias: Car charging
description: "Start/stop car charging based upon Predbat determined slots"
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
actions:
  - choose:
      - conditions:
          - condition: state
            entity_id: binary_sensor.predbat_car_charging_slot
            state: "on"
        sequence:
          <commands to turn on your car charger, e.g.>
          - service: select.select_option
            data:
              option: Eco+
            target:
              entity_id: select.myenergi_zappi_charge_mode
      - conditions:
          - condition: state
            entity_id: binary_sensor.predbat_car_charging_slot
            state: "off"
        sequence:
          <commands to turn off your car charger, e.g.>
          - service: select.select_option
            data:
              option: Stopped
            target:
              entity_id: select.myenergi_zappi_charge_mode
mode: single
```

Note: [Multiple cars](car-charging.md#multiple-electric-cars) can be planned with Predbat.

## Additional Car charging configurations

- **switch.predbat_car_charging_from_battery** - When set to On the car can drain the home battery, Predbat will manage the correct level of battery accordingly.
When set to Off home battery discharge will be prevented when your car charges, and all load from the car and home will be from the grid.
This is achieved by setting the battery discharge rate to 0 during car charging and to the maximum otherwise.
On inverters without a timed pause, the car hold does not set the discharge rate to 0 while the home battery is itself charging in a charge window, as a charging battery cannot feed the car.
The home battery can still charge from the grid/solar in either case. Only use this if Predbat knows your car charging plan,
e.g. you are using Intelligent Octopus or you use the car slots in Predbat to control your car charging.

  *Note:* If **switch.predbat_car_energy_reported_load** is set to Off (car outside the CT clamp), this setting has no effect as the battery cannot supply the car directly. Predbat will automatically treat the car as being supplied from the grid/export.

- **input_number.predbat_car_charging_loss** gives the percentage amount of energy lost when charging the car (load in the home vs energy added to the battery).
A good setting is 0.08 which is 8%.

- **switch.predbat_octopus_intelligent_dynamic** (default `true`) - cancels an Octopus Intelligent car's slots, and the dispatch's cheap rate, while the car is inside a dispatch but not actually charging, which releases the battery hold set by **switch.predbat_car_charging_from_battery** - see [Checking Intelligent dispatches against the car](#checking-intelligent-dispatches-against-the-car) above.
**switch.predbat_metric_dynamic_load_adjust** only adjusts the near-term load prediction from your current load; it does not affect car slots.

- See [Car charging filtering](#filtering-car-charging-energy-from-house-load) and [Planned car charging](#planned-car-charging)
for further car charging setup details.

## Example EV and charger setup

Sample setup and Predbat automation to use the cheapest charging slots with no/limited Home Assistant Integration.

MG4 EV Vehicle with a Hypervolt Car Charger. There is no 3rd party integration with the MG (so no idea of the car's current SoC), and the Hypervolt car charger doesn't understand when an EV is plugged in.

Yet it can be stopped and started with a 3rd party integration.

In Home Assistant, create a helper entity (Settings / Devices & Services / Helpers) of type 'Number', check minimum value is set to 0, maximum value to 100, and under Advanced Settings, set 'Unit of Measurement' to '%':

- Car Max Charge - input_number.car_max_charge

Create a 'Dropdown' helper entity that has two options 'true' and 'false' (in lowercase):

- Car Charger Plugged in - input_select.car_charger_plugged_in

Within the `apps.yaml` configuration file specify the following configuration settings:

Find the line for **car_charger_battery_size** and enter the Car Battery Size in kWh:

**Example**

```yaml
  car_charging_battery_size:
    - 61.7
```

Specify the Car Charging Limit to use the Car Max Charge helper entity created earlier:

```yaml
  car_charging_limit:
    - 'input_number.car_max_charge'
```

Find **car_charging_planned** and replace the template Wallbox and Zappi regular expression with your new dropdown helper entity:

```yaml
  car_charging_planned:
    - 'input_select.car_charger_plugged_in'
```

Find **car_charging_planned_response** and add **'true'** to the list:

```yaml
  car_charging_planned_response:
    - 'yes'
    - 'on'
    - 'true'
```

If possible, add an entity keeping track of the kWh used for car charging to **car_charging_energy**.

If your charging device doesn't keep track of kWh you can measure the power sent to the car charger
(e.g. from the EV charger integration or an energy monitor/smart plug for the EV charger) then you can create another helper entity to convert kW power into kWh:

Create a helper entity (Settings / Devices & Services / Helpers) of type 'Integration - Riemann Sum integral':

- Name : car_energy_used
- Input sensor : *sensor that measures power consumed by the car charger*
- Integration method : Right Riemann sum
- Metric prefix : k (kilo)

Please look into [Integration - Riemann sum integral](URL) to convert kW into kWh.

And add your custom car charging energy sensor in `apps.yaml` in place of the template Wallbox and Zappi regular expression:

**Example**

```yaml
  car_charging_energy: 'sensor.car_energy_used'
```

**car_charging_now** is optional: point it at a sensor that reports the car charging for Predbat to hold the house battery while it does, or leave it commented out (hashed out) in `apps.yaml`:

```yaml
  #car_charging_now:
  #  - off
```

Save the `apps.yaml` file and exit.

In Home Assistant, turn **on** the following Predbat control switches:

- **switch.predbat_car_charging_hold**
- **switch.predbat_car_charging_manual_soc** (for car 0, or **switch.predbat_car_charging_manual_soc_1/2/3** for additional cars)
- **switch.predbat_car_charging_plan_smart**

And turn **off** the Predbat control switch:

- **switch.predbat_octopus_intelligent_charging**

**HA Charging Slot Automation**

In Home Assistant (Settings / Automation & Scenes), create an automation to monitor the Predbat car charging slot sensor and turn the charger on and off according to the Predbat plan
(the numeric entity id's below would need replacing with the appropriate sensor name for your car charger):

```yaml
alias: Car Charging Slot
description: ""
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.predbat_car_charging_slot
actions:
  - if:
      - condition: state
        entity_id: binary_sensor.predbat_car_charging_slot
        state: "off"
    then:
      - type: turn_off
        entity_id: f6de2df0758744aba60f6b5f
        domain: switch
  - if:
      - condition: state
        entity_id: binary_sensor.predbat_car_charging_slot
        state: "on"
    then:
      - type: turn_on
        entity_id: f6de2df0758744aba60f6b5f
        domain: switch
mode: single
```

Finally, for simplicity, add the below entities to your HA Dashboard so you can set them when needed:

- Car Max Charge - input_number.car_max_charge
- Car Manual SoC - input_number.predbat_car_charging_manual_soc_kwh (for car 0)
- For multiple cars, add input_number.predbat_car_charging_manual_soc_kwh_1/2/3 for cars 1/2/3
- Car Charger Plugged in - input_select.car_charger_plugged_in

Annoyingly, you have to calculate the kWh your vehicle has in total by taking the Percentage left in the car / 100 * Total Car Battery capacity.<BR>
For example:

```text
65/100*61.7=40.1
```

Enter '40.1' into 'Car Manual SoC' and '80%' into 'Car Max charge'.

Once the charger is switched to **true** and your Car Max charge (target SoC) % is higher than the kWh currently in the car,
Predbat will plan and charge the car with the kW that are needed to reach the target SoC.

## Example: Separating car charging costs for multiple cars

Predbat provides **predbat.cost_today_car** and **predbat.cost_total_car** which give the cost today and total accumulated cost for all car charging.

If you have multiple cars with a single EV charger then it's not possible to segregate the cost per car.

The following solution will accumulate individual charging costs for each car.

- Create two helper entities of type number to collect the cost per car:

```yaml
input_number.car_car1_cost_today
input_number.car_car2_cost_today
Min = 0
Max = 10000
Step = 0.01
```

  Predbat accumulates cost in pence/cents, etc so the Max value should be big enough to hold the maximum car charging cost per day (e.g. £10/$10/€10).

- Create an automation that triggers when **predbat.cost_today_car** changes value. Then, based upon which car is connected (sensor.car1_connected or sensor.car2_connected in this case), delta of predbat_cost to the appropriate car cost today sensor:

```yaml
alias: Allocate EV Charging Cost
description: ""
triggers:
  - entity_id:
   - predbat.cost_today_car
 trigger: state
actions:
  - variables:
   new_cost: "{{ trigger.to_state.state | float }}"
   old_cost: "{{ trigger.from_state.state | float }}"
   delta: "{{ new_cost - old_cost }}"
  - condition: template
 value_template: "{{ delta > 0 }}"
  - choose:
   - conditions:
    - condition: template
   value_template: "{{ is_state('sensor.car1_connected', 'on') }}"
  sequence:
    - target:
     entity_id: input_number.car_car1_cost_today
   data:
     value: >
    {{ (states('input_number.car_car1_cost_today') | float) + delta
    }}
   action: input_number.set_value
   - conditions:
    - condition: template
   value_template: "{{ is_state('sensor.car2_connected', 'on') }}"
  sequence:
    - target:
     entity_id: input_number.car_car2_cost_today
   data:
     value: >
    {{ (states('input_number.car_car2_cost_today') | float) + delta
    }}
   action: input_number.set_value
mode: single
```

- Finally, create an automation that will reset the cost to 0 at midnight every day:

```yaml
alias: Reset Daily EV Car Costs
description: ""
triggers:
  - at: "00:00:00"
 trigger: time
actions:
  - target:
   entity_id:
  - input_number.car_car1_cost_today
  - input_number.car_car2_cost_today
 data:
   value: 0
 action: input_number.set_value
mode: single
```
