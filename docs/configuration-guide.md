# Configuration guide

First get the basics set up, ensure you have the inverter controls configured, the historical load data and the solar forecast in place. Make sure your energy rates are configured correctly for import and export.

If you have an EV try to set up the car charging sensor correctly so the tool can tell what part of your historical load is EV charging. You might want to also set to the car charging plan so you can predict when your car is plugged in and how much it will charge.

You should try to tune **inverter_loss**, **battery_loss** and **battery_loss_discharge** to the correct % loss for your system in order to get more accurate predictions. Around 4% for each is good for a hybrid inverter. Also set **inverter_hybrid** to True or False depending on if you have a Hybrid or AC Coupled battery.

The setting **input_number.metric_battery_cycle** can be used to put a cost on using your battery for charging and discharging. In theory if you think your battery will last say 6000 complete cycles and cost you £4000 and is 9.5kWh then each cycle is 19kWh and so the cost is £4000 / 19 / 6000 = 3.5p. If you configure this number higher then more expensive plans will be selected which avoid charging and discharging your battery as much. The default is 3p but can be set to 0 if you want to turn this off. Note that the cycle cost will not be included in the cost predictions, just taken into account in the planning stage.

A new experimental feature that tries to model cloud coverage by modulating the PV output can be enabled with **switch.predbat_metric_cloud_enable**. When enabled the difference between PV and PV10 is used to work out a cloud factor, this modulates the PV output predictions up and down accordingly as if there was passing clouds. This can have an impact on planning, especially for things like freeze charging which could assume the PV will cover the house load but it might not due to clouds.

## Fixed daily rates

- In this case you will just be predicting the battery levels, no charging or discharging is required although it won't hurt if you leave these options enabled.

## Cheap night rate with bad export rate (e.g. Octopus Go, Economy 7 etc)

- In this scenario you will want to charge overnight based on the next days solar forecast.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

```yaml
set_soc_enable - True              # Allow the tool to configure the charge %
set_reserve_enable - True          # Use the reserve to stop fluctuations in the charge % when charging
set_reserve_hold - True            # Means if you don't need to charge then charging won't be enabled
calculate_best_charge - True       # You want the tool to calculate charging
combine_charge_slots - True        # As you have just one overnight rate then one slot is fine
metric_min_improvement - 0         # Charge less if it's cost neutral
set_charge_window - True           # You want to have Predbat control the charge window
best_soc_keep - 2.0                # Tweak this to control what battery level you want to keep as a backup in case you use more energy
best_soc_min - 0.0                 # You can also set this to best_soc_keep if you don't want charging to be turned off overnight when it's not required
rate_low_threshold - 0.8           # Consider a 20% reduction in rates or more as a low rate
forecast_plan_hours - 24           # In apps.yml set this to 24 hours to match the repeating rates
calculate_discharge_first - False  # You probably only want to discharge any excess as export rates are poor
```

## Cheap night rate, with a good export rate (e.g. Intelligent Octopus with Octopus Outgoing)

Follow the instructions from Cheap Night rate above, but also you will want to have automatic discharge when the export rates are profitable.

```yaml
calculate_best_discharge - True        # Enable discharge calculation
calculate_discharge_first - True       # Give priority to discharge when it's profitable
combine_charge_slots - True            # Keep one big charge slot
combine_discharge_slots - False        # For fixed export rate you have to break up the discharge slots
set_discharge_window - True            # Allow the tool to control the discharge slots
metric_min_improvement - 0             # Charge less if it's cost neutral
metric_min_improvement_discharge - 0   # Discharge even if cost neutral, as you often need many slots to see the improvement
rate_high_threshold: 0                 # Automatic high rate selection
set_discharge_freeze - True            # Allow Predbat to hold the current battery level rather than just discharge
set_charge_freeze - False              # Allow Predbat to hold the current battery level rather than just charge
calculate_max_windows - 96             # Set to 96 for best results, but if you have host performance issues you can reduce this to 48 or 32
predbat_metric_battery_cycle - ?       # You can set this to maybe 2-5p if you want to avoid cycling the battery too much
```

predbat_set_discharge_freeze_only - ?? # If you set Freeze only to True then excess solar will be exported, set to False if you want forced export as well

## Multiple rates for import and export (e.g. Octopus Flux & Cozy)

Follow the instructions from Cheap Night rate above, but also you will want to have automatic discharge when the export rates are profitable.

Recommended settings - these must be changed in Home Assistant once Predbat is running:

```yaml
calculate_best_discharge - True        # Enable discharge calculation
calculate_discharge_first - True       # Give priority to discharge when it's profitable
combine_charge_slots - True            # Keep one big charge slot
combine_discharge_slots - True         # As these rates have fixed longer periods then a single slot is fine
set_discharge_window - True            # Allow the tool to control the discharge slots
metric_min_improvement - 0             # Charge less if it's cost neutral
metric_min_improvement_discharge - 0.1 # Make sure discharge only happens if it makes a profit
rate_low_threshold: 0.8                # Select rates 20 % below average only
rate_high_threshold: 1.2               # Export rates 20 % above average only
metric_battery_cycle - ?               # You can set this to maybe 2-5p if you want to avoid cycling the battery too much
set_discharge_freeze - True            # Allow Predbat to hold the current battery level rather than just discharge
set_charge_freeze - True               # Allow Predbat to hold the current battery level rather than just charge
```

## Half hourly variable rates (e.g. Octopus Agile)

Recommended settings - these must be changed in Home Assistant once Predbat is running:

```yaml
calculate_best_discharge - True        # Enable discharge calculation
calculate_discharge_first - True       # Give priority to discharge when it's profitable
set_discharge_window - True            # Allow the tool to control the discharge slots
combine_discharge_slots - False        # Split into 30 minute chunks for best optimisation
metric_min_improvement - 0             # Charge less if it's cost neutral
metric_min_improvement_discharge - 0.1 # Make sure discharge only happens if it makes a profit
rate_low_match_export - False          # Start with this at False but you can try it as True if you want to charge at higher rates to export even more
rate_low_threshold: 0                  # Automatic rate selection (can also be tuned manually to find the rates you want)
rate_high_threshold: 0                 # Automatic rate selection (can also be tuned manually to find the rates you want)
calculate_max_windows - 32             # Normally 32 is enough, but you can try more or less to optimise runtime vs results
set_discharge_freeze - True            # Allow Predbat to hold the current battery level rather than just discharge
set_charge_freeze - True               # Allow Predbat to hold the current battery level rather than just charge
forecast_plan_hours - 48               # In apps.yml set this to 48 hours to consider a full cycle plan
```

If you have a fixed export rate then follow the above guidance.
