# What does Predbat do?

Predbat runs every 5 minutes and will automatically update its prediction for the home battery levels for the next period, up to a maximum of 48 hours. It will automatically decide when to charge and discharge your battery to achieve the best cost metric within the parameters you have set. It uses the solar production forecast from Solcast combined with your historical energy use to make this prediction.

- The output is a prediction of the battery levels, charging slots, discharging slots, costs and import and export amounts.
- Costs are based on energy pricing data, either manually configured (e.g. 7p from 11pm-4pm and 35p otherwise) or by using the Octopus Plugin
    - Both import and export rates are supported.
    - Intelligent Octopus is also supported and takes into account allocated charging slots.  
- The solar forecast used is the central scenario from Solcast (50%) with a weighting towards the more pessimistic (10%) scenario.
- The charging and discharging controls are automatically programmed into the inverter.
- Automatic planning of export slots is also supported, when enabled Batpred can start a forced discharge of the battery if the export rates are high and you have spare capacity.
- Historical load data is used to predict your consumption, optionally car charging load can be filtered out of this data.
- Predbat can be configured to manage the charging of your EV and take into account its load on the house during these periods.
- Multiple inverter support depends on running all inverters in lockstep.

## Terminology

### Basic terminology

- **SOC** - State of Charge - the % or kWh level of charge of your battery
- **Target SOC** - The target level that the battery is being charged to, e.g. we target 100% SOC means the battery is being charged to full
- **Load** - The energy your home is using to power your equipment e.g. oven, lights or electric car charging
- **Grid** - Your electric supply outside the house
- **Import** - Electricy drawn from the grid to be used in the home or to charge the battery
- **Export** - Electricy from your home from the battery or solar which is sent to the grid.
- **PV** - Solar power that is generated in your home
- **Inverter** - The box that converts DC energy from solar or from your battery into AC power for your home and the grid. It also converts AC power from the grid into DC to charge a battery.
- **Hybrid inverter** - An inverter that can charge a battery from solar directly using DC power as well as charging it from AC power from the grid.
- **AC Coupled** - A battery that comes with it's own inverter and is always charge or discharged with AC (using an internal inverter)
- **Slot** - A period of time where Predbat performs an action e.g. charging. In Predbat everything is a multiple of 5 minutes.
    - Charge slots are always multiples of 30 minutes and align to a 30-minute boundary to match the way energy rates are allocated.
    - Discharge slots can be any multiple of 5 minutes and always finish on a 30-minute boundary.

### Predbat modes

The current Predbat mode is reported in **predbat.status**

- **Idle** - This is the default, the load will be covered by solar and/or battery. Excess solar will charge the battery or be exported if the battery is full. This will be described as ECO Mode for Givenergy inverters but other inverters use different terminology.

- **Charge** - The battery charges from the grid and the grid also covers any load. Solar power will also be used to charge the battery.
- **Charge Freeze** - The current battery level is held and the grid/solar covers any load. Solar power will also be used to charge the battery.
- **Hold Charge** - A type of charge where the target % is the same as the current %, effectively the same as a charge freeze (but without being explicitly selected)
- **No Charge** - A charge where the target % is lower than the current battery level so there will be no charging unless the usage is unexpectedly high.

- **Discharge** - A force discharge slot where the battery is exported to the grid. The battery covers the load. Any solar generated will be exported.
- **Discharge Freeze** - A slot where the battery covers the load but charging is disabled, thus any solar generated will be exported.

- **Error** - There is a configuration error or other problem, you should check the AppDaemon log in home assistant for more details (Settings, System, Logs, AppDaemon)
