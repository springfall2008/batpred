# Comparing Energy Tariff's

While it is quite easy to go off and compare your historical usage against various energy tariff's with existing apps, it is much harder to perform a what-if analysis
as the way you control your battery would be different depending on the tariff.

For this reason Predbat offers an energy rate comparison to allow you to know if you are on the best available tariff or when it might be time to switch.

Once you have given Predbat a list of tariff's that you want to compare then it will update its predictions daily and keep a history of these over time.

## Configuring the tariff's to compare

First you need to tell Predbat which tariff's you want to compare, you should list all the tariff's you realistically might want to switch between, including your
current tariff to act as a baseline.

Below is a suggestion of various tariff combinations from Octopus Intelligent (valid Feb 2025) against region A (please please -A with your region code if you decide
to use this template). [Region Codes](https://energy-stats.uk/dno-region-codes-explained/)

As well as Octopus rate URLs you can use manual rates (rates_import/rates_export), other types of rates from sensors will be added in future releases.

Each tariff must be given an ID which will be used to create a sensor to track predicted cost over time, the full name is used in the description of that sensor and on
the web page.

If you do not set an import or export rate for a particular tariff then your existing energy rates will be used.

```yaml
  # Tariff comparison feature
  # Adjust this list to the tariffs you want to compare, include your current tariff also
  compare_list:
    - id: 'cap_none'
      name: 'Price cap import/No export'
      rates_import:
         - rate 24.86
      rates_export:
         - rate 0
    - id: 'igo_fixed'
      name: 'Intelligent GO import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/INTELLI-BB-VAR-23-03-01/electricity-tariffs/E-1R-INTELLI-BB-VAR-23-03-01-A/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-A/standard-unit-rates/'
    - id: 'igo_agile'
      name: 'Intelligent GO import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/INTELLI-BB-VAR-23-03-01/electricity-tariffs/E-1R-INTELLI-BB-VAR-23-03-01-A/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/'
    - id: 'go_fixed'
      name: 'GO import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/GO-VAR-BB-23-02-07/electricity-tariffs/E-1R-GO-VAR-BB-23-02-07-A/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-A/standard-unit-rates/'
    - id: 'go_agile'
      name: 'GO import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/GO-VAR-BB-23-02-07/electricity-tariffs/E-1R-GO-VAR-BB-23-02-07-A/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/'
    - id: 'agile_fixed'
      name: 'Agile import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-A/standard-unit-rates/'
    - id: 'agile_agile'
      name: 'Agile import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/'
    - id: 'flux'
      name: 'Flux import/Export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/FLUX-IMPORT-23-02-14/electricity-tariffs/E-1R-FLUX-IMPORT-23-02-14-A/standard-unit-rates'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/FLUX-EXPORT-BB-23-02-14/electricity-tariffs/E-1R-FLUX-EXPORT-BB-23-02-14-A/standard-unit-rates'
    - id: 'cosy_fixed'
      name: 'Cosy import/Fixed export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-A/standard-unit-rates'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/OUTGOING-VAR-BB-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-BB-24-10-26-A/standard-unit-rates/'
    - id: 'cosy_agile'
      name: 'Cosy import/Agile export'
      rates_import_octopus_url: 'https://api.octopus.energy/v1/products/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-A/standard-unit-rates'
      rates_export_octopus_url: 'https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/'
```

## Running a comparison

By default the comparison will be run at Midnight every night and saved for the entire day.

You can view the comparison on the Predbat web interface under the 'Compare' tab and also manually trigger a new comparison by hitting the 'Run' button.

Predbat will highly which tariff maybe the Best cost wise for the next 24-hour period based on the plan optimisation metric you have defined. The metric
includes the value of the contents of your battery, any iBoost that has been diverted and also the impact of any keep settings, carbon or self-sufficency settings.

The predicted cost is also shown, but keep in mind ending the day with an empty battery maybe cheaper today but cost more tomorrow.

![image](https://github.com/user-attachments/assets/ed170b51-7f00-4bb3-a036-059b5a96b512)

## Comparison sensors

For each tariff a new sensor is created in Home Assistant called **predbat.compare_tariff_<id>** where <id> is the ID name you entered above. This sensor will track the cost
as its main value and many details about the prediction in its attributes.

You can create charts from these sensors to show how the different tariff's could compare on a daily basis.

![image](https://github.com/user-attachments/assets/6d5c30f6-822f-4d9c-b4a6-701c0b676c61)
