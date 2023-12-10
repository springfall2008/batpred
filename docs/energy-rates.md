# Energy rates

## Octopus Energy Plugin

If you want to use real pricing data and have Octopus Energy then ensure you have the Octopus Energy plugin installed and working
([https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy/))

The Octopus Energy Plugin also provides support for Intelligent Octopus charging to support car charging.

When **octopus_intelligent_charging** is True and you are on Intelligent Octopus import the car charging plan will be
extracted from Octopus and used for Predbat to plan, and it may charge the home battery using these slots also.

 **CAUTION** To get detailed energy rates needed by Predbat you need to go into Home Assistant and manually enable the following
 events which are disabled by the plugin by default in some versions:

```yaml
    event.octopus_energy_electricity_xxxxxxxx_previous_day_rates
    event.octopus_energy_electricity_xxxxxxxx_current_day_rates
    event.octopus_energy_electricity_xxxxxxxx_next_day_rates

    event.octopus_energy_electricity_xxxxxxxx_export_previous_day_rates
    event.octopus_energy_electricity_xxxxxxxx_export_current_day_rates
    event.octopus_energy_electricity_xxxxxxxx_export_next_day_rates
```  

### Octopus Saving sessions

For Predbat to automatically manage saving sessions you will need to make sure that **octopus_saving_session** is set
in apps.yaml to point to the saving session binary sensor supported by the Octopus Energy plugin (see template apps.yaml
for the default name).

When a saving session is available it will be automatically joined by Predbat and then should appear as a joined session
within the next 30 minutes.

When a saving session has been joined the energy rates for import and export will be overridden by adding the assumed saving rate
to your normal rate. The assumed rate will be taken from the Octopus Energy add-in (v9.1.0 and above) and converted into pence
using the **octopus_saving_session_octopoints_per_penny** configuration item in apps.yaml (default is 8).

As the saving session import and export rates are very high compared to normal you would expect Predbat to export during the entire
period if the battery is large amount, a pre-charge may happen at some point during the day to maintain the right level for the session.

If you are using expert mode, for best results: **switch.predbat_combine_charge_slots** (_expert mode_) should be turned off.
Set **input_number.rate_low_threshold** (_expert mode_) and **input_number.rate_high_threshold** (_expert mode_) to 0 for automatic mode.

For forced export you need to ensure that **switch.predbat_calculate_best_discharge** is enabled and that **switch.predbat_set_discharge_freeze_only** is disabled.
If you do not have an export tariff then forced export will not apply.

## Rate bands

You can manually configure your rate bands (assuming they repeat) using rates_import/rates_export (see below).

## Rate offsets

Note that you can tune future unknown energy rates by adjusting **input_number.predbat_metric_future_rate_offset_import**
(_expert mode_) and **input_number.predbat_metric_future_rate_offset_export** (_expert mode_) inside Home Assistant
to set the predicted offset for future unknown rates.

## Future Agile energy rates

In the energy market it's possible to calculate the Octopus Agile rates from around 10am UK time using public data, you can
enable this in apps.yaml for Import, Export or both. This will approximate next day's rates based on the spot prices.
The approximation is only used until the real Octopus Agile rates are released around 4pm.

CAUTION: You may violate the terms and conditions of the Nordpool site if you use this data and as such the authors of
Predbat accept no responsibility for any violations:

<https://www.nordpoolgroup.com/en/About-us/terms-and-conditions-for-useofwebsite/>

## Nordpool market energy rates

```yaml
futurerate_url: '<https://www.nordpoolgroup.com/api/marketdata/page/325?currency=GBP>'
futurerate_adjust_import: True
futurerate_adjust_export: True
futurerate_peak_start: "16:00:00"
futurerate_peak_end: "19:00:00"
futurerate_peak_premium_import: 14
futurerate_peak_premium_export: 6.5
```
