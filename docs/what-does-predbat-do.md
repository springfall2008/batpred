# What does Predbat do?

Predbat is a home battery automation program.

It automatically runs every 5 minutes and will update its prediction for the home battery levels for the next period, up to a maximum of 48 hours ahead.
Predbat will automatically decide when to charge and discharge your battery to achieve the best (lowest) cost spend within the parameters you have set.
It uses the solar production forecast from Solcast combined with your historical energy usage to make this prediction.

- The output is a prediction of the battery levels, charging slots, discharging slots, costs and import and export amounts.
- Costs are based on energy pricing data, either manually configured (e.g. 7p from 11pm-4pm and 35p otherwise) or by using the Octopus Energy integration
    - Both import and export rates are supported.
    - Intelligent Octopus is also supported and takes into account allocated charging slots.  
- The solar forecast used is the central scenario from Solcast (50%) with a configurable weighting towards the more pessimistic (10%) scenario.
- The charging and discharging controls are automatically programmed into the inverter.
- Automatic planning of export slots is also supported, when enabled Predbat can start a forced discharge of the battery if the export rates are high and you have spare capacity.
- Historical load data is used to predict your consumption, optionally car charging load can be filtered out of this data.
- Predbat can be configured to manage the charging of your EV or use of a Solar Diverter and take into account of these loads on the house during these periods.
- Multiple inverter support is included but depends on all inverters running in lockstep.

## Terminology

### Basic terminology

- **SoC** - State of Charge - the % or kWh level of charge of your battery
- **Target SoC** - The target level that the battery is being charged to, e.g. we target 100% SoC means the battery is being charged to full
- **Load** - The energy your home is using to power your equipment e.g. oven, lights or electric car charging
- **Grid** - Your electric supply outside the house
- **Import** - Electricity drawn from the grid to be used in the home or to charge the battery
- **Export** - Electricity from your home from the battery or solar which is sent to the grid.
- **PV** - Solar power that is generated in your home
- **Inverter** - The box that converts DC energy from solar or from your battery into AC power for your home and the grid.
The inverter also converts AC power from the grid into DC to charge a battery.
- **Hybrid inverter** - An inverter that can charge a battery from solar directly using DC power as well as charging it from AC power from the grid.
- **AC Coupled** - A battery that comes with it's own inverter and is always charged or discharged with AC (using an internal inverter)
- **Slot** - A period of time where Predbat performs an action e.g. charging. In Predbat everything is a multiple of 5 minutes.
    - Charge slots are always in multiples of 30 minutes and align to a 30-minute boundary to match the way energy rates are allocated.
    - Discharge slots can be any multiple of 5 minutes and always finish on a 30-minute boundary.

### Predbat modes

When you first install Predbat it will be in 'Monitor' mode.

You can configure Predbat's mode of operation using the drop down menu in **select.predbat_mode**.
You will find a full description of [Predbat Modes](customisation.md#predbat-mode) in the Customisation Guide.

Once you are ready for Predbat to take control move this setting to one of the active control modes.

### Predbat status

The current Predbat status is reported in the Home Assistant entity **predbat.status**:

- **Idle** - This is the default, the load will be covered by solar and/or battery. Excess solar will charge the battery or be
exported if the battery is full. This is described as 'Eco' Mode for GivEnergy inverters but other inverters use different terminology.

- **Charging** - The battery charges from the grid and the grid also covers any load. Solar power will also be used to charge the battery.

- **Freeze charging** - The battery is charging but the current battery level (SoC) is is frozen (held). Think of it as a charge to the current battery level.
The grid or solar covers any house load. If there is a shortfall of Solar power to meet house load, the excess house load is met from grid import,
but if there is excess Solar power above house load, the excess solar will be used to charge the battery,

- **Hold charging** - A type of charge where the target SoC % is the same as the current SoC %, effectively the same as a charge freeze (but without being explicitly selected).

- **No Charge** - A charge where the target SoC % is lower than the current battery SoC level so there will be no charging unless the usage is unexpectedly high.

- **Discharging** - The battery is being force-discharged. The house load will be covered by the battery and any excess is exported to the grid. Any solar generated will be exported.

- **Freeze discharging** - The battery is in Discharge mode, the same as Idle (Eco) mode, but with charging disabled.
The battery or solar covers the house load. As charging is disabled, if there is excess solar generated, the current SoC level will be held and the excess solar will be exported.
If there is a shortfall of solar power to meet house load, the battery will discharge.

- **Calibration** - The inverter is calibrating the batteries.
On GivEnergy systems the battery state of charge (SoC) level has to be calibrated by performing a full battery discharge then a full charge
so that the voltage levels associated with empty and full SoC can be determined.
Predbat will pause executing the plan until the calibration automatically finishes - see [Calibration FAQ](faq.md#warn-inverter-is-in-calibration-mode).

- **Error** - There is a configuration error or other problem, you should check the [Predbat AppDaemon log file](output-data.md#predbat-logfile) for more details.
