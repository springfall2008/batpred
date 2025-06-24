# Some words of caution

Predbat is a powerful hobbyist system that can control many home battery and solar systems. While every attempt has been made to make it as easy to use as possible, it does require a certain amount of technical skill.

## Increased energy costs

While Predbat will normally save you money, an incorrectly configured system can cause your battery to be poorly managed and may increase your electricity bills. I recommend carefully reviewing what your installation is doing once you have enabled it
for the first time.

## Flash memory

Some inverters use flash memory with a limited lifespan. Depending on how register writes are managed, the controller can reduce this lifespan. In normal operation, this should not be an issue, but if your setup performs a large number of register
writes continuously, you could eventually encounter problems.

- For example, on a current GivEnergy inverter, it's estimated that the usable limit is around 1 million register writes (although firmware changes may increase this limit).
- This would allow approximately 270 writes per day, or one every 5 minutes.
- Each change of inverter mode requires multiple register writes — e.g. charge start time, end time, scheduled charge enable, set battery pause mode, etc. — which could total around 6 registers.
- This means one change of mode every 30 minutes, on average, would be acceptable.
- However, as most plans include longer intervals (often hours) where the battery is in Demand mode, Charging, or Exporting — during which registers are not updated — it is unlikely that this limit will be exceeded.

As different inverter designs may have different limits, it's wise to avoid making your plan too complex if it doesn't result in meaningful gains.

Things you can do to have a less complex plan include:

- Keep 'calculate export within charge slots' off
- Set metric battery cycle to a small non-zero value e.g. 0.5
- Set a metric min improvement export to a small value e.g. 5p
- Ensure inverter losses are set to a representative value
- Turn off charge_low_power mode

**Avoid using balance inverters ('switch.predbat_balance_inverters_enable') which can make register changes once or twice a minute unless you are sure this is not an issue**

Predbat creates an entity called **predbat.inverter_register_writes** which can be used to check the total number of writes across all inverters. If you divide this by the period of use
and by the number of inverters, you will be able to figure out the actual rate of register writes - see the [Simple inverter writes dashboard](output-data.md#inverter-data).
