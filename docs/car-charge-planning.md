# Car charging planning

There are two ways to plan car charging slots:

- If you have Intelligent Octopus import tariff and the Octopus Energy Plugin - in which case Predbat will use the slots allocated by the plugin in battery prediction
    - Ensure **octopus_intelligent_slot** in `apps.yaml` points to the Intelligent Slot sensor in the Octopus Energy Plugin
    - Set **switch.predbat_octopus_intelligent_charging** to True
    - Information about the car's battery size will also be extracted from the plugin
    - You will need to set the cars current soc sensor, **car_charging_soc** correctly to have accurate results
    - If you set **car_charging_limit** then Batpred can also know if the car's limit is set lower than in Intelligent Octopus
    - Let the Octopus app control when your car charges
- Predbat led charging - Here Predbat plans the charging based on the upcoming low rate slots
    - Ensure **car_charging_limit**, **car_charging_soc** and **car_charging_planned** are set correctly in `apps.yaml`
    - Set **select.predbat_car_charging_plan_time** in the config or in HA to the time you want the car ready by
    - Enable **switch.predbat_car_charging_plan_smart** if you want to use the cheapest slots only. If you leave this disabled then all low rate slots will be used. This may mean you need to use expert mode and change your low rate threshold to configure which slots should be considered if you have a tariff with more than 2 import rates (e.g. flux)
    - Use an automation based on **binary_sensor.predbat_car_charging_slot** to control when your car charges

NOTE: Multiple cars can be planned with Predbat.

See [Car charging filtering](config-yml-settings.md#car-charging-filtering) and [Planned car charging](config-yml-settings.md#planned-car-charging) in the [config.yml settings](config-yml-settings.md) section of the documentation.
