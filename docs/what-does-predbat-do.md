# What does Predbat do?

The app runs every N minutes (default 5), it will automatically update its prediction for the home battery levels for the next period, up to a maximum of 48 hours. It will automatically find charging slots (up to 10 slots) and if enabled configure them automatically with GivTCP. It uses the solar production forecast from Solcast combined with your historical energy use and your plan charging slots to make a prediction. When enable it will tune the charging percentage (SOC) for each of the slots to achieve the lowest cost possible.

- The output is a prediction of the battery levels, charging slots, discharging slots, costs and import and export amounts.
- Costs are based on energy pricing data, either manually configured (e.g. 7p from 11pm-4pm and 35p otherwise) or by using the Octopus Plugin
    - Both import and export rates are supported.
    - Intelligent Octopus is also supported and takes into account allocated charging slots.  
- The solar forecast used is the central scenario from Solcast but you can also add weighting to the 10% (worst case) scenario, the default is 20% weighting to this.
- The target SOC calculation can be adjusted with a safety margin (minimum battery level and a pence threshold).
- The charging windows and charge level (SOC) can be automatically programmed into the inverter.
- Automatic planning of export slots is also supported, when enabled Batpred can start a forced discharge of the battery if the export rates are high and you have spare capacity.
- Ability to manage reserve % to match SOC % to prevent discharge (if enabled)
- Historical load data is used to predict your consumption, optionally car charging load can be filtered out of this data.

- Multiple inverter support depends on running all inverters in lockstep, that is each will charge at the same time to the same %

## Terminology

### Basic terminology

* **SOC** - State of Charge - the % or kWh level of charge of your battery
* **Target SOC** - The target level that the battery is being charged to, e.g. we target 100% SOC means the battery is being charged to full
* **Load** - The energy your home is using to power your equipment e.g. oven, lights or electric car charging
* **Grid** - Your electric supply outside the house
* **Import** - Electricy drawn from the grid to be used in the home or to charge the battery
* **Export** - Electricy from your home from the battery or solar which is sent to the grid.
* **PV** - Solar power that is generated in your home
* **Inverter** - The box that converts DC energy from solar or from your battery into AC power for your home and the grid. It also converts AC power from the grid into DC to charge a battery.
* **Hybrid inverter** - An inverter that can charge a battery from solar directly using DC power as well as charging it from AC power from the grid.
* **AC Coupled** - A battery that comes with it's own inverter and is always charge or discharged with AC (using an internal inverter)

### Battery terminology

* **ECO Mode** - This is the default, the load will be covered by solar and/or battery. Excess solar will charge the battery or be exported if the battery is full.

* **Charge** - A charge slot where the battery charges from the grid and the grid also covers any load. Solar power will also be used to charge the battery.
* **Charge Freeze** - A charge slot where the current battery level is held and the grid also covers any load. Solar power will also be used to charge the battery.
  
* **Discharge** - A force discharge slot where the battery is exported to the grid. The battery covers the load. Any solar generated will be exported.
* **Discharge Freeze** - A slot where the current battery is held at the current level. The battery covers the load. Any solar generated will be exported. 
