# Some words of caution

Predbat is a powerful hobbyist system that can control many home battery and solar systems. While every attempt has been made to make it as easy to use as possible it does require
a certain amount of technical skills.

## Increased energy costs

While Predbat normally will save you money an incorrectly configured Predbat can cause your battery to be badly managed and thus increase your electricity bills, I'd recommend carefully changing what you install is doing
once you have enabled it for the first time.

## Flash memory

Some inverters have a flash memory which has a limited lifespan and so depending on the way register writes are managed controller your inverter can act to reduce this lifespan.
In normal operation, this should not be an issue but if you have a setup that performs a large number of register writes all the time it's possible you could eventually run into
this issue.

- For example, on a current GivEnergy inverter it's assumed around 1 million register writes could be the current usable limit (although firmware changes may increase this limit)
- This would give you around 270 writes per day or around 1 every 5 minutes.
- Each change of mode requires more than one register write, e.g. start time, end time, scheduled charge enable, pause mode etc - this could be around 6 registers.
- This means one change of mode every 30 minutes on average would be okay.
- However given most plans will have larger gaps (often hours) where the battery is either in Demand mode, charging or exporting where registers are not changing it is unlikely to hit this limit.

Given different inverter designs may have different limits it is a wise precaution to avoid your plan being too busy if it doesn't gain you very much.
Things you can do to have a less complex plan include:

- Keep calculating export during charge off
- Set metric battery cycle to a small non-zero value e.g. 0.5
- Ensure inverter losses are set to a representative value
- Turn off charge_low_power mode

**Avoid using balance inverters which can make register changes once or twice a minute unless you are sure this is not an issue**

Predbat creates an entity called **predbat.inverter_register_writes** which can be used to check the total number of writes across all inverters if you divide this by the period of use
and by the number of inverters, you will be able to figure out the actual rate of register writes.
