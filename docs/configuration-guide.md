# Configuration guide

First, get the basics set up, ensure you have the [inverter controls configured](install.md#inverter-control-install),
you have [configured apps.yaml](apps-yaml.md) to your setup, and the [solar forecast](install.md#solcast-install) is in place.
Make sure your [energy rates](energy-rates.md) are configured correctly for import and export.

If you have an EV try to set up the [car charging sensor](apps-yaml.md#car-charging-integration) correctly so Predbat can tell what part of your historical load is EV charging.
You might want to also set the [car charging plan](apps-yaml.md#planned-car-charging) so you can predict when your car is plugged in and how much it will charge.

It is recommended that you [create a dashboard page](output-data.md#displaying-output-data) with all the required entities to control Predbat.

This page gives a summary of some of the key configuration settings you should consider in Predbat for different energy tariffs;
the [Predbat customisation guide](customisation.md) details all the Predbat customisation options.

You should try to tune **input_number.predbat_inverter_loss**, **input_number.predbat_battery_loss** and **input_number.predbat_battery_loss_discharge** to the correct % loss
for your system to get more accurate predictions. Around 4% for each is good for a hybrid inverter.

For a Hybrid inverter, the inverter loss includes the loss on inverting PV as well as going from AC to DC when importing. Battery loss charge and discharge are factors to account for the loss
in charging and discharging the battery as DC.

For an AC coupled inverter the inverter loss is just the loss of the battery inverter, if you need to model the loss of your PV inverter then use **input_number.predbat_pv_scaling**
or adjust your Solcast output. Battery loss charge and discharge are factors to account for the loss in charging and discharging the battery as DC.

Also, set **switch.predbat_inverter_hybrid** to True or False depending upon if you have a Hybrid or AC-Coupled battery.

The setting **input_number.predbat_metric_battery_cycle** (_expert mode_) can be used to put a 'virtual cost' in pence per kWh on using your battery for charging and discharging.<BR>
If you configure this number higher then more expensive plans will be selected which avoids charging and discharging your battery as much.
The default is 0.5p (meaning charging and discharging the battery would effectively cost an extra 1p per kWh) but can be set to 0 if you want to turn this feature off.

Below is a guide to some of the electricity tariff options and a set of recommended Predbat settings for each tariff type.
In theory, most tariffs will work out of the box but still, it's worth reviewing your settings.

## Fixed daily rates

With a fixed daily rate tariff you will just be predicting the battery levels, no charging or discharging is required although it won't hurt if you leave these options enabled.

You should set **select.predbat_mode** to 'Monitor'.

## Cheap night rate with a bad export rate (e.g. Economy 7 with SEG)

In this scenario, you will want to charge overnight based on the next day's solar forecast and don't want Predbat to force export (discharge) your battery.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode | Control Charge | You want Predbat to calculate and control charging |
| input_number.predbat_best_soc_keep |  2.0  | Tweak this to control what battery level you want to keep as a backup in case you use more energy |

If you are using expert mode then these options may be worth reviewing:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| input_number.predbat_metric_min_improvement | 0  | Charge less if it's cost neutral |
| input_number.predbat_metric_min_improvement_export  | 3 | Export only if there is a profit |

You should set **select.predbat_mode** to 'Control charge'

## Cheap night rate, with a good export rate (e.g. Go or Intelligent Octopus Go with Octopus Outgoing)

Follow the instructions from the _Cheap Night rate_ above, but you will also want to have automatic export occurring when the export rates are profitable.

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode  | Control Charge & Discharge | You want Predbat to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  2.0  | Tweak this to control what battery level you want to keep as a backup in case you use more energy |

If you are using expert mode then these options may be worth reviewing, otherwise, ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours  | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| input_number.predbat_metric_min_improvement  | 0 | Charge less if it's cost neutral |
| input_number.predbat_metric_min_improvement_export  | 3 | Export only if there is a profit |
| input_number.predbat_metric_battery_cycle  | 0-2 | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  0 | Can be set to non-zero if you want to force a minimum charge level |

You should set **select.predbat_mode** to 'Control charge & discharge'

You may wish to use **rates_export_override** to override the night export rate to zero or turn off **calculate_export_during_charge** and turn on **combine_charge**. 
Either of these options will prevent charge / discharge cycling within the cheap period, which Predbat would see as economically sensible but may not be within terms of use for some Tariff's.

With the overnight charging rate being cheaper than your export rate, you probably want to charge your EV overnight and export all your solar; and not charge the EV from Solar during the day.
Settings for doing this vary by charger manufacturer, but for the Zappi charger, set _export margin_ to a value higher than your inverter can output (e.g. 6000W) to ensure that all solar is exported and not used to charge the EV.

## Multiple rates for import and export (e.g. Octopus Flux & Cozy)

Follow the instructions from the _Cheap Night_ rate above, but also you will want to have automatic export when the export rates are profitable.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode  | Control Charge & Discharge | You want Predbat to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  0  | Use the full battery |

If you are using expert mode then these options may be worth reviewing, otherwise, ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours  | 24 | If you set this to 24 then you will have quicker updates, the cycle repeats itself anyhow |
| input_number.predbat_metric_min_improvement  | 0  | Charge less if it's cost neutral |
| input_number.predbat_metric_min_improvement_export  | 3 | Export only if there is a profit |
| input_number.predbat_metric_battery_cycle  | 0-2  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  0  | Don't use non-zero otherwise all slots will be force charging |

You should set **select.predbat_mode** to 'Control charge & discharge'

## Half hourly variable rates (e.g. Octopus Agile)

Recommended settings - these must be changed in Home Assistant once Predbat is running:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| select.predbat_mode  | Control Charge & Discharge | You want Predbat to calculate and control charging and discharging |
| input_number.predbat_best_soc_keep |  0  | Use the full battery |

If you are using expert mode then these options may be worth reviewing, otherwise, ignore this:

| Item |  Value  | Comment  |
|---------|---------------|-------------|
| input_number.predbat_forecast_plan_hours  | 24-48 | If you set this to 24 then you will have quicker updates, going to 36/48 for a longer plan |
| input_number.predbat_metric_min_improvement  | 0  | Charge less if it's cost neutral |
| input_number.predbat_metric_min_improvement_export  | 3 | Export only if there is a profit |
| input_number.predbat_metric_battery_cycle  | 0-2  | Higher numbers mean less charging and discharging but higher costs |
| input_number.predbat_best_soc_min |  0  | Don't use non-zero otherwise all slots will be force charging |

You should set **select.predbat_mode** to 'Control charge & discharge'
