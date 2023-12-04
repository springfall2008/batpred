# Configuration guide

First get the basics set up, ensure you have the inverter controls configured, the historical load data and the solar forecast
in place. Make sure your energy rates are configured correctly for import and export.

If you have an EV try to set up the car charging sensor correctly so the tool can tell what part of your historical load is EV
charging. You might want to also set to the car charging plan so you can predict when your car is plugged in and how much it will charge.

You should try to tune **inverter_loss**, **battery_loss** and **battery_loss_discharge** to the correct % loss for your system
in order to get more accurate predictions. Around 4% for each is good for a hybrid inverter. Also set **inverter_hybrid** to
True or False depending on if you have a Hybrid or AC Coupled battery.

The setting **input_number.metric_battery_cycle** (_expert mode_) can be used to put a cost on using your battery for charging
and discharging. In theory if you think your battery will last say 6000 complete cycles and cost you £4000 and is 9.5kWh then
each cycle is 19kWh and so the cost is £4000 / 19 / 6000 = 3.5p. If you configure this number higher then more expensive plans
will be selected which avoid charging and discharging your battery as much. The default is 2p but can be set to 0 if you want
to turn this off. Note that the cycle cost will not be included in the cost predictions, just taken into account in the planning
stage. _Note: Setting this to a non-zero zero will increase your daily cost, but will reduce your home battery usage._

Cloud coverage is modelled by using difference between PV and PV10 is used to work out a cloud factor, this modulates the PV
output predictions up and down accordingly as if there was passing clouds. This can have an impact on planning, especially for
things like freeze charging which could assume the PV will cover the house load but it might not due to clouds.

Below is a guide to some of the tariff options, in theory most tariffs will work out of the box but still it's worth reviewing your settings.

## Fixed daily rates

- In this case you will just be predicting the battery levels, no charging or discharging is required although it won't hurt if you leave these options enabled.

## Cheap night rate with bad export rate (e.g. Octopus Go, Economy 7 etc)

- In this scenario you will want to charge overnight based on the next days solar forecast.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode      | Control Charge | You want the tool to calculate and control charging |
| input_number.predbat_best_soc_keep |  2.0  | Tweak this to control what battery level you want to keep as a backup in case you use more energy |
| switch.predbat_combine_charge_slots  | True   | Use one big charge slot |

If you are using expert mode then these options maybe worth reviewing:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours      | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| switch.predbat_combine_charge_slots | True  | As you have just one overnight rate then one slot is fine |
| input_number.metric_min_improvement  | 0   | Charge less if it's cost neutral |

## Cheap night rate, with a good export rate (e.g. Intelligent Octopus with Octopus Outgoing)

Follow the instructions from Cheap Night rate above, but also you will want to have automatic discharge when the export rates are profitable.

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode      | Control Charge & Discharge | You want the tool to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  2.0  | Tweak this to control what battery level you want to keep as a backup in case you use more energy |
| switch.predbat_combine_charge_slots  | True   | Use one big charge slot |

If you are using expert mode then these options maybe worth reviewing, otherwise ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours      | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| switch.predbat_combine_charge_slots | ? | Going to False will allow charging at more expensive day rates when it's worth it to export mode |
| input_number.metric_min_improvement  | 0   | Charge less if it's cost neutral |
| input_number.metric_min_improvement_discharge  | 0.1  | Discharge only if there is a profit |
| input_number.metric_battery_cycle  | ?  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  ?  | Can be set non-zero if you want to force a minimum charge level |

## Multiple rates for import and export (e.g. Octopus Flux & Cozy)

Follow the instructions from Cheap Night rate above, but also you will want to have automatic discharge when the export rates are profitable.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode      | Control Charge & Discharge | You want the tool to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  0.5  | Use the full battery without going empty |
| switch.predbat_combine_charge_slots  | True   | Use one big charge slot |

If you are using expert mode then these options maybe worth reviewing, otherwise ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours      | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| switch.predbat_combine_charge_slots | ? | Going to False will allow charging at more expensive day rates when it's worth it to export mode |
| input_number.metric_min_improvement  | 0   | Charge less if it's cost neutral |
| input_number.metric_min_improvement_discharge  | 0.1  | Discharge only if there is a profit |
| input_number.metric_battery_cycle  | ?  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  0  | Don't use non-zero otherwise all slots will be force charging |

## Half hourly variable rates (e.g. Octopus Agile)

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode      | Control Charge & Discharge | You want the tool to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  0.5  | Use the full battery without going empty |
| switch.predbat_combine_charge_slots  | True   | Use one big charge slot |

If you are using expert mode then these options maybe worth reviewing, otherwise ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours      | 24-48 | If you set this to 24 then you will have quicker updates, going to 36/48 for a longer plan |
| input_number.metric_min_improvement  | 0   | Charge less if it's cost neutral |
| input_number.metric_min_improvement_discharge  | 0.1  | Discharge only if there is a profit |
| input_number.metric_battery_cycle  | ?  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  0  | Don't use non-zero otherwise all slots will be force charging |
