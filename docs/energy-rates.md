# Energy rates

## Octopus Energy Plugin

- If you want to use real pricing data and have Octopus Energy then ensure you have the Octopus Energy plugin installed and working ([https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy/))
- The Octopus Energy Plugin also provides support for Intelligent Octopus charging to support car charging
- When **octopus_intelligent_charging** is True and you are on Intelligent Octopus import the car charging plan will be extracted from Octopus and used for Predbat to plan, and it may charge the home battery using these slots also.

### Octopus Saving sessions

For Predbat to automatically manage saving sessions you will need to make sure that **octopus_saving_session** is set in apps.yaml to point to the saving session binary sensor supported by the Octopus Energy plugin (see template apps.yaml for the default name)

When a saving session is active the energy rates for import and export will be overridden with the assumed rate set in Home Assistant with **input_number.predbat_metric_octopus_saving_rate**, if this rate is 0 then the feature is disabled (default). You should set this to the saving rate published by Octopus for the session (currently there is no sensor for this rate).

For best results ensure **switch.predbat_combine_charge_slots** is turned off and that you have enough windows (**input_number.predbat_calculate_max_windows**) available to allow Predbat to charge before the event if need be, e.g. a value of 48 would normally be suitable. Set **input_number.rate_low_threshold** and **input_number.rate_high_threshold** to 0 for automatic mode.

## Rate bands

- you can configure your rate bands (assuming they repeat) using rates_import/rates_export (see below)

## Rate offsets

- Note that you can tune future unknown energy rates by adjusting **input_number.predbat_metric_future_rate_offset_import** and **input_number.predbat_metric_future_rate_offset_export** inside Home Assistant to set the predicted offset for future unknown rates.
