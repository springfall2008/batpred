# Configuration guide

First get the basics set up, ensure you have the [inverter controls configured](install.md#inverter-control-integration-install-givtcpsolax-modbus),
you have [configured apps.yaml](apps-yaml.md) to your setup, and the [solar forecast](install.md#solcast-install) is in place.
Make sure your [energy rates](energy-rates.md) are configured correctly for import and export.

If you have an EV try to set up the [car charging sensor](apps-yaml.md#car-charging-integration) correctly so Predbat can tell what part of your historical load is EV charging.
You might want to also set to the [car charging plan](apps-yaml.md#planned-car-charging) so you can predict when your car is plugged in and how much it will charge.

It is recommended that you [create a dashboard page](output-data.md#displaying-output-data) with all the required entities to control Predbat.

This page gives a summary of some of the key configuration settings you should consider in Predbat for different energy tariffs;
the [Predbat customisation guide](customisation.md) details all the Predbat customisation options.

You should try to tune **input_number.predbat_inverter_loss**, **input_number.predbat_battery_loss** and **input_number.predbat_battery_loss_discharge** to the correct % loss
for your system in order to get more accurate predictions. Around 4% for each is good for a hybrid inverter.
Also set **switch.predbat_inverter_hybrid** to True or False depending upon if you have a Hybrid or AC-Coupled battery.

The setting **input_number.predbat_metric_battery_cycle** (_expert mode_) can be used to put a 'virtual cost' in pence per kWh on using your battery for charging and discharging.<BR>
If you configure this number higher then more expensive plans will be selected which avoids charging and discharging your battery as much.
The default is 0.5p (meaning charging and discharging the battery would effectively cost an extra 1p per kWh) but can be set to 0 if you want to turn this feature off.

Below is a guide to some of the electricity tariff options and a set of recommended Predbat settings for each tariff type.
In theory most tariffs will work out of the box but still it's worth reviewing your settings.

## Fixed daily rates

With a fixed daily rate tariff you will just be predicting the battery levels, no charging or discharging is required although it won't hurt if you leave these options enabled.

You should set **select.predbat_mode** to 'Monitor'.

## Cheap night rate with bad export rate (e.g. Octopus Go, Economy 7 etc)

In this scenario you will want to charge overnight based on the next day's solar forecast and don't want Predbat to force discharge your battery.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode | Control Charge | You want Predbat to calculate and control charging |
| input_number.predbat_best_soc_keep |  2.0  | Tweak this to control what battery level you want to keep as a backup in case you use more energy |
| switch.predbat_combine_charge_slots | True | Use one big charge slot |

If you are using expert mode then these options maybe worth reviewing:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| switch.predbat_combine_charge_slots | True  | As you have just one overnight rate then one slot is fine |
| input_number.predbat_metric_min_improvement | 0  | Charge less if it's cost neutral |

You should set **select.predbat_mode** to 'Control charge'

## Cheap night rate, with a good export rate (e.g. Intelligent Octopus with Octopus Outgoing)

Follow the instructions from the _Cheap Night rate_ above, but also you will also want to have automatic discharge occurring when the export rates are profitable.

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode  | Control Charge & Discharge | You want Predbat to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  2.0  | Tweak this to control what battery level you want to keep as a backup in case you use more energy |
| switch.predbat_combine_charge_slots  | True  | Use one big charge slot |

If you are using expert mode then these options maybe worth reviewing, otherwise ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours  | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| switch.predbat_combine_charge_slots | ? | Setting to False will allow charging at more expensive day rates when it's worth it to export more |
| input_number.predbat_metric_min_improvement  | 0   | Charge less if it's cost neutral |
| input_number.predbat_metric_min_improvement_discharge  | 0.1  | Discharge only if there is a profit |
| input_number.predbat_metric_battery_cycle  | ?  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  ?  | Can be set non-zero if you want to force a minimum charge level |

You should set **select.predbat_mode** to 'Control charge & discharge'

## Multiple rates for import and export (e.g. Octopus Flux & Cozy)

Follow the instructions from _Cheap Night_ rate above, but also you will want to have automatic discharge when the export rates are profitable.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode  | Control Charge & Discharge | You want Predbat to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  0.5  | Use the full battery without going empty |
| switch.predbat_combine_charge_slots  | True  | Use one big charge slot |

If you are using expert mode then these options maybe worth reviewing, otherwise ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours  | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| switch.predbat_combine_charge_slots | ? | Setting to False will allow charging at more expensive day rates when it's worth it to export more |
| input_number.predbat_metric_min_improvement  | 0  | Charge less if it's cost neutral |
| input_number.predbat_metric_min_improvement_discharge  | 0.1  | Discharge only if there is a profit |
| input_number.predbat_metric_battery_cycle  | ?  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  0  | Don't use non-zero otherwise all slots will be force charging |

You should set **select.predbat_mode** to 'Control charge & discharge'

## Half hourly variable rates (e.g. Octopus Agile)

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode  | Control Charge & Discharge | You want Predbat to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  0.5  | Use the full battery without going empty |
| switch.predbat_combine_charge_slots  | False  | Don't combine slots for a better result |

If you are using expert mode then these options maybe worth reviewing, otherwise ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours  | 24-48 | If you set this to 24 then you will have quicker updates, going to 36/48 for a longer plan |
| input_number.predbat_metric_min_improvement  | 0  | Charge less if it's cost neutral |
| input_number.predbat_metric_min_improvement_discharge  | 0.1  | Discharge only if there is a profit |
| input_number.predbat_metric_battery_cycle  | ?  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  0  | Don't use non-zero otherwise all slots will be force charging |
| switch.predbat_calculate_fast_plan | False | The fast plan feature reduces accuracy of planning |

You should set **select.predbat_mode** to 'Control charge & discharge'
