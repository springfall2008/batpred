# Comparing Energy Tariffs

While it is quite easy to go off and compare your historical usage against various energy tariffs with existing apps, it is much harder to perform a what-if analysis
as the way you control your battery would be different depending on the tariff.

For this reason, Predbat offers an energy rate comparison to allow you to know if you are on the best available tariff or when it might be time to switch.

Once you have given Predbat a list of tariffs that you want to compare then it will update its predictions daily and keep a history of these over time.

If you do decide to switch to Octopus Energy after using this feature please consider using my referral link so we can both save some money: <https://share.octopus.energy/jolly-eel-176>

## Limitations

Keep in mind this is an approximation of costs for the following 24-hour period and the reality could be different. In particular, car charging costs is unlikely to reflect
the true picture as it will only be planned after you plug in. Smart tariffs like Octopus Intelligent GO will give you extra cheap car slots which can also not be
predicted right now. When changing tariffs, you should use your judgment, this data is only a helpful guide.

## Configuring the tariff's to compare

First, you need to tell Predbat which tariffs you want to compare, you should list all the tariffs you realistically might want to switch between, including your
current tariff to act as a baseline.

Below is a suggestion of various tariff combinations from Octopus Intelligent (valid Feb 2025) against region A (please please -A with your region code if you decide
to use this template). [Region Codes](https://energy-stats.uk/dno-region-codes-explained/)

As well as Octopus rate URLs (rates_import_octopus_url/rates_export_octopus_url) you can use manual rates (rates_import/rates_export),
Octopus integration rates (metric_octopus_import/metric_octopus_export) and Energi Data service rates (metric_energidataservice_import/metric_energidataservice_export).

Each tariff must be given an ID which will be used to create a sensor to track predicted cost over time, the full name is used in the description of that sensor and on
the web page.  The ID can contain alphanumeric characters or underscores; do not use slashes, commas or other special characters in the ID or predbat will crash when running the compare!

If you do not set an import or export rate for a particular tariff then your existing energy rates will be used.

```yaml
  # Tariff comparison feature
  # Adjust this list to the tariffs you want to compare, include your current tariff also
  # Octopus region code (https://energy-stats.uk/dno-region-codes-explained/)
  octopus_region: "A"
  compare_list:
    - id: 'current'
      name: 'Current Tariff'
    - id: 'cap_seg'
      name: 'Price cap import/Seg export'
      rates_import:
        - rate: 24.86
      rates_export:
        - rate: 4.1  
    - id: 'igo_fixed'
      name: 'Intelligent GO import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/INTELLI-BB-VAR-23-03-01/electricity-tariffs/E-1R-INTELLI-BB-VAR-23-03-01-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-{octopus_region}/standard-unit-rates/'
    - id: 'igo_agile'
      name: 'Intelligent GO import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/INTELLI-BB-VAR-23-03-01/electricity-tariffs/E-1R-INTELLI-BB-VAR-23-03-01-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-{octopus_region}/standard-unit-rates/'
    - id: 'go_fixed'
      name: 'GO import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/GO-VAR-BB-23-02-07/electricity-tariffs/E-1R-GO-VAR-BB-23-02-07-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-{octopus_region}/standard-unit-rates/'
    - id: 'go_agile'
      name: 'GO import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/GO-VAR-BB-23-02-07/electricity-tariffs/E-1R-GO-VAR-BB-23-02-07-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-{octopus_region}/standard-unit-rates/'
    - id: 'agile_fixed'
      name: 'Agile import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-{octopus_region}/standard-unit-rates/'
    - id: 'agile_agile'
      name: 'Agile import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-{octopus_region}/standard-unit-rates/'
    - id: 'flux'
      name: 'Flux import/Export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/FLUX-IMPORT-23-02-14/electricity-tariffs/E-1R-FLUX-IMPORT-23-02-14-{octopus_region}/standard-unit-rates'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/FLUX-EXPORT-BB-23-02-14/electricity-tariffs/E-1R-FLUX-EXPORT-BB-23-02-14-{octopus_region}/standard-unit-rates'
    - id: 'cosy_fixed'
      name: 'Cosy import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{octopus_region}/standard-unit-rates'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-{octopus_region}/standard-unit-rates/'
    - id: 'cosy_agile'
      name: 'Cosy import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{octopus_region}/standard-unit-rates'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-{octopus_region}/standard-unit-rates/'
    - id: 'snug_fixed'
      name: 'Snug import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/SNUG-24-11-07/electricity-tariffs/E-1R-SNUG-24-11-07-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-{octopus_region}/standard-unit-rates/'
    - id: 'iflux'
      name: 'Intelligent Flux import/Export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/INTELLI-FLUX-IMPORT-23-07-14/electricity-tariffs/E-1R-INTELLI-FLUX-IMPORT-23-07-14-{octopus_region}/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/INTELLI-FLUX-EXPORT-23-07-14/electricity-tariffs/E-1R-INTELLI-FLUX-EXPORT-23-07-14-{octopus_region}/standard-unit-rates/'
```

## Running a comparison

By default, the comparison will be run at Midnight every night and saved for the entire day.

You can view the comparison on the Predbat web interface under the 'Compare' tab and also manually trigger a new comparison by hitting the 'Run' button or by turning on **switch.predbat_compare_active**.

When a compare is running **switch.predbat_compare_active** will be turned on, otherwise it will be off.

Predbat will highlight which tariff may be the best cost-wise for the next 24-hour period based on the plan optimisation metric you have defined. The metric
includes the value of the contents of your battery and iBoost that has been diverted during this period.

The predicted cost is also shown, but keep in mind ending the day with an empty battery may be cheaper today but cost more tomorrow.

![image](https://github.com/user-attachments/assets/399866a1-7d86-457d-b525-7c2e1fdf683b)

![image](https://github.com/user-attachments/assets/b7c7f9a3-8a80-4abf-a08c-4da62b9258fe)

## Comparison sensors

For each tariff a new sensor is created in Home Assistant called **predbat.compare_tariff_id** where **id** is the ID name you entered above. This sensor will track the cost
as its main value and many details about the prediction in its attributes.

You can create charts from these sensors to show how the different tariffs compare on a daily basis.

![image](https://github.com/user-attachments/assets/6d5c30f6-822f-4d9c-b4a6-701c0b676c61)
