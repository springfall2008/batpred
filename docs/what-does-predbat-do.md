# What does Predbat do?

Predbat is a home battery automation program.

It automatically runs every 5 minutes and will update its prediction for the home battery levels for the next period, up to a maximum of 48 hours ahead.
Predbat will automatically decide when to charge and discharge your battery to achieve the best (lowest) cost spend within the parameters you have set.
It uses the solar production forecast from Solcast combined with your historical energy usage to make this prediction.

- The output is a prediction of the battery levels, solar generation, house load, charging activity, discharging activity, costs and import and export amounts based on (by default) 30-minute slots.
- Costs are based on energy pricing data, either manually configured (e.g. 7p from 11pm-4pm and 35p otherwise) or by using the Octopus Energy integration
    - Both import and export rates are supported.
    - Intelligent Octopus is also supported and takes into account allocated charging slots.  
- The solar forecast used is the central scenario from Solcast (50%) with a configurable weighting towards the more pessimistic (10%) scenario.
- Predbat automatically programs your inverter with the appropriate charging and discharging controls. Both Hybrid inverters and AC-coupled inverters are supported by Predbat.
- Automatic planning of export slots is also supported, when enabled Predbat can start a forced discharge of the battery if the export rates are high and you have spare capacity.
- Historical load data is used to predict your consumption, optionally car charging load can be filtered out of this data.
- Predbat can be configured to manage the charging of your EV or to use a Solar Diverter, and take into account these loads on the house during these periods.
- Multiple inverter support is included but depends on all inverters running in lockstep.

## Terminology

### Basic terminology

- **SoC** - State of Charge - the % or kWh level of charge of your battery
- **Target SoC** - The target level that the battery is being charged to, e.g. we target 100% SoC means the battery is being charged to full
- **Charge Limit** - Another word for Target SoC
- **Load** - The energy your home is using to power your equipment e.g. oven, lights or electric car charging
- **Grid** - Your electric supply outside the house
- **Import** - Electricity drawn from the grid to be used in the home or to charge the battery
- **Export** - Electricity from your home from the battery or solar which is sent to the grid
- **Demand** - Demand Mode is when the battery covers the house load and charges from solar, to avoid importing or exporting (some systems call this ECO Mode).
- **Charging** - When your battery is charging, in Predbat this refers to force charge (from the grid).
- **Discharge** - The opposite of charge, when the battery is discharging.
- **Exporting** - When your battery is force discharging to create an export, in Predbat this refers to force export.
- **Export Limit** - When your battery is being force exported the export limit is the % battery level where the discharge will stop if reached.
- **PV** - Solar power that is generated in your home. Can also refer to a prediction of the solar for the day, by default is the 50% scenario (most likely generation).
- **Inverter** - The box that converts DC energy from solar or your battery into AC power for your home and the grid.
The inverter also converts AC power from the grid into DC to charge a battery.
- **Hybrid inverter** - An inverter that can charge a battery from solar directly using DC power as well as charging it from AC power from the grid
- **AC Coupled** - A battery that comes with its own inverter and is always charged or discharged with AC (using an internal inverter)
- **Micro Inverters** - Small inverters that are fitted in line with the DC solar panels and produce AC power on a per-panel basis. Typically used with an AC-coupled battery.
- **Slot** - A period of time where Predbat acts e.g. charging. In Predbat everything is a multiple of 5 minutes
    - Charge slots are always in multiples of the [plan interval duration](energy-rates.md#plan-interval), default is 30 minutes, and align to the interval time boundaries to match the way energy rates are allocated
    - Discharge slots can be any multiple of 5 minutes and always finish on a plan interval (default 30-minute) boundary.
- **Loss** - Refers to energy lost in your system due to heat or other factors.

- **PV10** - A prediction of the 10% scenario for solar, this is like a worst case, occurs 1 in 10 days
- **PV90** - A prediction of the 90% scenario for solar, this is like a best case, occurs 1 in 10 days
- **Base** - Usually refers to the expected outcome if Predbat takes no further action, meaning just what is currently configured on your inverter.
- **Best** - The best plan that Predbat could come up with, as in what it will do (assuming Read-only is off)
- **Actual** - Used to refer to what has already happened in the past.
- **Base10** - The base scenario but with the 10% outcome for Solar and Load (worst case)
- **Charge Limit Base** - This is the target charge % in the Base plan (what is currently set on your inverter)
- **Best10** - The best plan but with the 10% outcome for Solar and Load (worst case)
- **Charge Limit Best** - This is the target charge % in the Best plan (what is currently set on your inverter)
- **Export Limit Best** - This is the target to force export to in % in the Best plan.
- **Best SoC Keep** - The amount of battery you want to keep in the plan that Predbat has made

### Predbat modes

When you first install Predbat it will be in 'Monitor' mode.

You can configure Predbat's mode of operation using the drop-down menu in **select.predbat_mode**.
You will find a full description of [Predbat Modes](customisation.md#predbat-mode) in the Customisation Guide.

Once you are ready for Predbat to take control move this setting to one of the active control modes.

### Predbat status

The current Predbat status is reported in the Home Assistant entity **predbat.status**:

- **Demand** - This is the default, the load will be covered by solar and/or battery. Excess solar will charge the battery or be
exported if the battery is full. This is described as 'Eco' Mode for GivEnergy inverters but other inverters use different terminology.

- **Charging** - The battery charges from the grid and the grid also covers any load. Solar power will also be used to charge the battery.

- **Freeze charging** - The battery is charging but the current battery level (SoC) is frozen (held). Think of it as a charge to the current battery level.
The grid or solar covers any house load. If there is a shortfall of Solar power to meet house load, the excess house load is met from grid import,
but if there is excess Solar power above the house load, the excess solar will be used to charge the battery,

- **Hold charging** - A type of charge where the target SoC % is the same or lower than the current SoC %. This is similar to charge freeze, but it is selected as a result of planning, and cannot be manually selected.

- **No Charge** - A charge where the target SoC % is lower than the current battery SoC level so there will be no charging unless the usage is unexpectedly high.

- **Exporting** - The battery is being force-discharged. The house load will be covered by the battery and any excess is exported to the grid. Any solar generated will be exported.

- **Freeze exporting** - The battery is in demand mode, but with charging disabled.
The battery or solar covers the house load. As charging is disabled, if there is excess solar generated, the current SoC level will be held and the excess solar will be exported.
If there is a shortfall of generated solar power to meet the house load, the battery will discharge to meet the extra load.

- **Hold exporting** - The plan was to force export but the minimum battery level was reached and thus the battery is kept in Demand mode.
If the battery level again gets above the threshold it will be changed back to Export mode.

- **Hold for car** and **Demand, Hold for car** - A car is charging (either Predbat-led or Octopus-led), the battery is in Demand mode,
but is set to prevent discharging into the car (requires **switch.predbat_car_charging_from_battery** to be set to On).

- **Calibration** - The inverter is calibrating the batteries.
On GivEnergy systems the battery state of charge (SoC) level has to be calibrated by performing a full battery discharge and then a full charge
so that the voltage levels associated with empty and full SoC can be determined.
Predbat will pause executing the plan until the calibration automatically finishes - see [Calibration FAQ](faq.md#warn-inverter-is-in-calibration-mode).

- **Error** - If there is a configuration error or other problem, you should check the [Predbat log file](output-data.md#predbat-logfile) for more details.
