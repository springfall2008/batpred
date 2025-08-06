# Energy rates

Predbat needs to know what your Import and (optionally) Export rates are so it can plan the optimal way to use your battery.
Your Import and Export rates can be simple flat rates,
more complex time-of-day tariffs (e.g. Economy 7, Octopus Flux),
or daily/half-hourly rates that track electricity market prices (e.g. Octopus Agile or Tracker).

Energy rates are all configured in the `apps.yaml` file that's stored in either the directory `/addon_configs/6adb4f0d_predbat` or `/config/appdaemon/apps/batpred/config/` directory
depending on [what type of Predbat installation method you have used](apps-yaml.md#appsyaml-settings).

You will need to use a file editor within Home Assistant (e.g. either the File editor or Studio Code Server add-ons)
to edit this file - see [editing configuration files within Home Assistant](install.md#editing-configuration-files-in-home-assistant) if you need to install an editor.

There are four different ways of configuring your Energy rates in `apps.yaml`, using [Octopus Energy Direct](#octopus-energy-direct),
using the [Octopus Energy Integration](#octopus-energy-home-assistant-integration), using [Octopus Rates URL's](#octopus-rates-url),
or manually [defining your rates and time periods](#rate-bands-to-manually-configure-energy-rates).

At least one of these methods must be used to define your import and export rates. If you don't then Predbat will assume zero for your energy rates.

## Octopus Energy direct

If your electricity supplier is Octopus Energy then the simplest way to provide Predbat with your electricity pricing information
is to connect Predbat directly to Octopus.

- This method will not work correctly if you have multiple import or export meters.
- A single Octopus Intelligent GO car charger or car is supported.
- Saving sessions are also supported, including auto-enroll.

You should first log into your Octopus account and go to the [Accounts](https://octopus.energy/dashboard/new/accounts/) section and copy your account number e.g. A-1234567.

Then go to the [API Access page](https://octopus.energy/dashboard/new/accounts/personal-details/api-access) where you can copy your API key e.g. sk_live_1as12355...

Put these both into your apps.yaml and you are done.

```yaml
  octopus_api_account: 'XXXXXXXX'
  octopus_api_key: 'sk_live_yyyyyyyy'
```

Free energy sessions:

Predbat can obtain details of Free energy sessions directly from the Octopus Web Site.
As Octopus do not publish an API for this information it has to be done by 'scraping' the website - there may be future issues with this if Octopus ever change the website format.
If you enable free energy sessions in Predbat then sessions will be considered even if you forget to sign-up to them in your Octopus account so be careful!

```yaml
  octopus_free_url: 'http://octopus.energy/free-electricity'
```

## Octopus Energy Home Assistant Integration

### Octopus Energy integration setup

Instead of using Octopus Energy Direct method, Predbat can also work with the [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy/),
this brings greater configurability than the direct method.

The Octopus Energy integration connects to your Octopus Energy account and retrieves the tariffs you are on, and the current tariff rates.
If you change tariff within Octopus the integration will automatically retrieve the updated tariff information, and as tariff prices change, again they are automatically retrieved.

The integration also provides support for Intelligent Octopus charging to support car charging.

Follow the instructions provided in the Octopus Energy integration documentation to install and set up the integration.

Once installed, you will need to configure the integration (go to Settings / Devices & Services / Integrations / Octopus Energy then click 'Configure')
and provide the integration with your 'Octopus API key' (that you obtain from your Octopus account: Personal Details / API access).

**CAUTION** To get detailed energy rates needed by Predbat you need to go into Home Assistant and manually enable the following
Octopus Energy events which are disabled by default when the integration is installed:

```yaml
  event.octopus_energy_electricity_xxxxxxxx_previous_day_rates
  event.octopus_energy_electricity_xxxxxxxx_current_day_rates
  event.octopus_energy_electricity_xxxxxxxx_next_day_rates

  event.octopus_energy_electricity_xxxxxxxx_export_previous_day_rates
  event.octopus_energy_electricity_xxxxxxxx_export_current_day_rates
  event.octopus_energy_electricity_xxxxxxxx_export_next_day_rates

  event.octopus_energy_gas_xxxxxxxx_previous_day_rates
  event.octopus_energy_gas_xxxxxxxx_current_day_rates
  event.octopus_energy_gas_xxxxxxxx_next_day_rates
```  

To enable the above events:

- Go to Settings / Devices & Services / Integrations, choose Octopus Energy, then *xx entities*
- You will see a list of entities (and events) supplied by the Octopus integration
- Click the 'Filter' symbol on the top right-hand corner (a series of lines in a downwards pointing arrow) and make sure all the options are selected
- Then in the left-hand-side search entities box, type "current_day"
- Click on the first event that comes up, check the name is the right one
- Click the cog wheel, then you should see the option to enable the event
- Click the option to enable the event and press 'Update' to make the change

Repeat this for the other events.

The gas rates are only required if you have a gas boiler, and an iBoost, and are [using Predbat to determine whether it's cheaper to heat your hot water with the iBoost or via gas](customisation.md#iboost-energy-rate-filtering)

Verify that the integration is working correctly in Home Assistant by going to Developer Tools / States, and entering 'octopus' in the 'Filter entities' box.
Confirm that the Octopus entities are being populated correctly.

### Configuring Predbat to use the Octopus Energy integration

The following configuration items in apps.yaml are used to configure Predbat to use the Octopus Energy integration.
They are set to a regular expression and should be auto-discovered so that Predbat automatically uses the Octopus Energy integration,
but you can comment out the regular expression lines to disable, or you set them manually.

- **metric_octopus_import** - Import rates from the Octopus Energy integration, should point to the sensor sensor.octopus_energy_electricity_METER_NUMBER_current_rate
- **metric_octopus_export** - Export rates from the Octopus Energy integration, should point to the sensor sensor.octopus_energy_electricity_METER_NUMBER_export_current_rate
- **metric_octopus_gas** - Gas rates from the Octopus Energy integration, should point to the sensor sensor.octopus_energy_gas_METER_NUMBER_current_rate
- **octopus_intelligent_slot** - If you have the Octopus Intelligent Go tariff this should point to the 'slot' sensor binary_sensor.octopus_energy_ACCOUNT_ID_intelligent_dispatching

metric_octopus_gas is (as above) only required to be configured if you are using Predbat to determine whether to heat your hot water via your iBoost or gas.

If you do not have an export rate or are not on the Octopus Go tariff, then the appropriate lines can be commented out in apps.yaml.

### Standing charge

Predbat can also (optionally) include the daily standing charge in cost predictions.
The following configuration item in apps.yaml defaults to obtaining the standing charge from the Octopus Energy integration:

- **metric_standing_charge** - Standing charge in pounds. By default points to the Octopus Energy integration sensor sensor.octopus_energy_electricity_METER_NUMBER_current_standing_charge

You can manually change this to a standing charge in pounds, e.g. 0.50 is 50p, or delete this line from apps.yaml, or set it to zero
if you don't want the standing charge (and only have consumption usage) to be included in Predbat charts and output data.

### Octopus Saving sessions

Predbat can automatically join you to Octopus saving sessions and plan battery activity for the saving session period to maximise your income.

For Predbat to automatically manage Octopus saving sessions the following additional configuration item in apps.yaml is used.
Like the electricity rates, this is set in the apps.yaml template to a regular expression that should auto-discover the Octopus Energy integration.

- **octopus_saving_session** - Indicates if a saving session is active, should point to the sensor binary_sensor.octopus_energy_ACCOUNT_ID_octoplus_saving_sessions.

When a saving session is available it will be automatically joined by Predbat and should then appear as a joined session within the next 30 minutes.

NOTE: Predbat automatically joining the saving session relies upon the event being enabled and Predbat being able to find the saving session event as 'event.octopus_energy_ACCOUNT_ID_octoplus_saving_session_events' - there have been occasions
with some Octopus Integration installations where the event name is different.  If this is the case then the event must be renamed to the correct format that Predbat can function correctly.

In the Predbat plan, for joined saving sessions the energy rates for import and export will be overridden by adding the assumed saving rate to your normal rate.
The assumed rate will be taken from the Octopus Energy integration and converted into pence
using the **octopus_saving_session_octopoints_per_penny** configuration item in apps.yaml (default is 8).

If you normally cut back your house usage during a saving session then you can change **input_number.predbat_load_scaling_saving** to allow Predbat to assume an energy
reduction in this period. E.g. setting to a value of 0.8 would indicate you will use 80% of the normal consumption in that period (a 20% reduction).

As the saving session import and export rates are very high compared to normal Predbat will plan additional export during the saving session period.
If necessary, a pre-charge may happen at some point during the day to maintain the battery right level for the session.

Note that Predbat's operational mode **select.predbat_mode** must be set to either 'Control charge'
or 'Control charge & discharge' for Predbat to be able to manage the battery for the saving session.

If you do not have an export tariff then forced export will not apply and Predbat will just ensure you have enough battery charge to see you through the saving session period.

If you do not want Predbat to automatically join Octopus saving sessions and manage your battery activity for the session,
simply delete or comment out the **octopus_saving_session** entry in apps.yaml.

### Octopus free (power up) events

Predbat can automatically detect Octopus free events and adjust your battery plan according. Note that this is derived from external sources, which do not verify your eligibility for free sessions.

For Predbat to automatically manage Octopus free sessions the following additional configuration item in apps.yaml is used.

Note: **You must have signed up to Octoplus and eligible to benefit from these events**

Like the electricity rates, this is set in the apps.yaml template to a regular expression that should auto-discover the Octopus Energy integration.

**octopus_free_session** - Will point to the free event sensor that is exposed by the Octopus Energy Integration. This event sensor contains the dates/times of
all the free events.

```yaml
  octopus_free_session: 're:(event.octopus_energy_([0-9a-z_]+|)_octoplus_free_electricity_session_events)'
```

Note: **This event may need to be enabled in Home Assistant first** - see [How to Enable Octopus events](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/faq/#there-are-entities-that-are-disabled-why-are-they-disabled-and-how-do-i-enable-them)

If you normally increase your house usage during a free session then you can change **input_number.predbat_load_scaling_free** to allow Predbat to assume an energy
increase in this period. E.g. setting to a value of 1.2 would indicate you will use 20% more energy than normal during this period. (Default is 1.2)

If you do not want Predbat to see these sessions then comment out the **octopus_free_session** setting.

Note: If the above is not working due to a lack of data (via a 3rd party service) Predbat can scrape directly from the Octopus Web Site, this may
not work in future if Octopus ever change the website format. If you enable this then sessions will be considered by Predbat even if you forget to sign-up to free electricity sessions in your Octopus account so be careful!

```yaml
  octopus_free_url: 'http://octopus.energy/free-electricity'
```

## Octopus Rates URL

If you do not wish to use the Octopus Energy integration and are an Octopus Energy customer then you can configure Predbat to get the electricity rates
directly online from the Octopus website.

In apps.yaml configure the following lines:

- **rates_import_octopus_url** to point to the appropriate import tariff URL on the Octopus website
- **rates_export_octopus_url** to point to the export tariff URL

e.g.

```yaml
  rates_import_octopus_url : "https://api.octopus.energy/v1/products/FLUX-IMPORT-23-02-14/electricity-tariffs/E-1R-FLUX-IMPORT-23-02-14-A/standard-unit-rates"
  rates_import_octopus_url : "https://api.octopus.energy/v1/products/AGILE-FLEX-BB-23-02-08/electricity-tariffs/E-1R-AGILE-FLEX-BB-23-02-08-A/standard-unit-rates"

  rates_export_octopus_url: "https://api.octopus.energy/v1/products/FLUX-EXPORT-BB-23-02-14/electricity-tariffs/E-1R-FLUX-EXPORT-BB-23-02-14-A/standard-unit-rates"
  rates_export_octopus_url: "https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/"
  rates_export_octopus_url: "https://api.octopus.energy/v1/products/OUTGOING-FIX-12M-BB-23-02-09/electricity-tariffs/E-1R-OUTGOING-FIX-12M-BB-23-02-09-A/standard-unit-rates/"
```

If you configure the rates_import_octopus_url then Predbat will use this instead of metric_octopus or rates_import.
Similarly, rates_export_octopus_url takes precedence over metric_octopus_export or rates_export.

Configuring the Octopus rates URL is an expert feature and for most users, the Octopus Energy integration is a simpler solution.

## Energidataservice Integration

<https://github.com/MTrab/energidataservice>

If your electricity supplier provides data through the Energi Data Service, you can use the Energidataservice integration to fetch real-time and future electricity pricing data.
This integration allows you to automatically retrieve rates and apply them within your energy management system, including optional tariff adjustments for greater accuracy.

The integration processes hourly pricing data and converts it into 30-minute intervals, making it ideal for scheduling and optimizing energy usage.

## Configuring Predbat to Use the Energidataservice Integration

The following configuration items in apps.yaml are used to configure Predbat to use the Energidataservice integration. These items must be set explicitly to ensure that Predbat retrieves the correct import and export rates.

metric_energidataservice_import - Import rates from the Energidataservice integration. This should point to the sensor that provides hourly import rates, such as sensor.energi_data_service.

metric_energidataservice_export - Export rates from the Energidataservice integration. This should point to the sensor that provides hourly export rates (e.g., solar feed-in rates), such as sensor.energi_data_service_export.

## Other spot sensor integrations

Different spot integrations that include forecast prices may be used.
Because integrations format their attribute data differently, a template sensor is required to convert the attribute data into a new sensor with the correctly formatted attributes: raw_today and raw_tomorrow.

metric_octopus_import: 'sensor.current_buy_electricity_price_raw'
metric_octopus_export: 'sensor.current_sell_electricity_price_raw'

Czech Republic example:
<https://github.com/rnovacek/homeassistant_cz_energy_spot_prices>

```yaml
{% set attributes = states.sensor.current_buy_electricity_price.attributes %}
{% set datetime_dict = zip(attributes.keys() | map('as_datetime', default={}), attributes.values()) | selectattr(0, 'datetime') %}
{% set ns = namespace(output=[]) %}
{% for start_time, price in datetime_dict %}
 {% if start_time < today_at() + timedelta(days=1) %}
   {% set ns.output = ns.output + [{'start': start_time.isoformat(), 'end': (start_time + timedelta(hours=1)).isoformat(), 'value': price | round(5)}] %}
 {% endif %}
{% endfor %}
{{ns.output | sort(attribute='start') }}
```

full code for cz energy spot template:
<https://gist.github.com/ziat007/ae29e26ae257f069520b65f0168c3a6b>

## Rate Bands to manually configure Energy Rates

If you are not an Octopus Energy customer, or you are but your energy rates repeat simply, you can configure your rate bands in apps.yaml using rates_import/rates_export/rates_gas.

Add the following entries to apps.yaml to define the pattern of rates over 24 hours:

```yaml
  rates_import:
    - start: "HH:MM:SS"
      end: "HH:MM:SS"
      rate: pence
  rates_export:
    - start: "HH:MM:SS"
      end: "HH:MM:SS"
      rate: pence
  rates_gas:
    - start: "HH:MM:SS"
      end: "HH:MM:SS"
      rate: pence
```

**start** and **end** are in the time format of "HH:MM:SS" e.g. "12:30:00" and should be aligned to 30 minute slots normally, i.e. end with ":30:00" or ":00:00".
Make sure the start and end times for the different rates cover the whole 24 hour period with no gaps!
**rate** is in pence e.g. 4.2

**day_of_week** Can also be used to control rates on specific days. You can specify one day or multiple days split by a comma.
Note: Day 1 = Monday, 2 = Tuesday .... 7 = Sunday

e.g:

```yaml
  rates_import:
    - rate: 15
      day_of_week: "1,2,3,4,5"
    - rate: 10
      day_of_week: "6,7"
```

start and end can be omitted and Predbat will assume that you are on a single flat-rate tariff.

If there are any gaps in the 24-hour period then a zero rate will be assumed.

The gas rates are only required if you have a gas boiler, or an iBoost, and are using Predbat to determine whether it's cheaper to heat your hot water with the iBoost or via gas.

## Manually over-riding energy rates

You can also override the import or export energy rates (regardless of whether they are set manually or via the Octopus Energy integration) by using the override feature in apps.yaml.

Rate override is used to set the specific date and time period where your rates are different, e.g. an Octopus Power Up session (zero rate for an hour or two),
or the British Gas half-price electricity on Sundays.

Unfortunately, there aren't any API's available to feed this information automatically into Predbat so you will have to define your own input controls and use the [Predbat manual API](manual-api.md#example-solution-to-over-ride-predicted-house-load),
or edit `apps.yaml` manually to set the appropriate rate over-ride dates and times:

```yaml
  rates_import_override:
    - date: "YYYY-MM-DD"
      start: "HH:MM:SS"
      end: "HH:MM:SS"
      rate: pence
  rates_export_override:
    - date: "YYYY-MM-DD"
      start: "HH:MM:SS"
      end: "HH:MM:SS"
      rate: pence
```

Optionally you can add a predicted load scaling factor for these periods using **load_scaling**, for example:

```yaml
  rates_import_override:
    - date: '2024-01-21'
      start: '17:30:00'
      end: '18:30:00'
      rate: 150
      load_scaling: 0.8
```

This instructs Predbat that during a 1-hour period at 5:30-6:30pm on 21st of Jan set the import rate to 150p and assume our load will be 80% of normal (20% lower).

You can also make relative adjustments to your energy rates, e.g. if you want to avoid exporting during peak periods to improve your energy
saving session results you could make a relative adjustment to your export rates using **rate_increment**.
The reason not to just set **rate** is that when an energy saving session is active you do not want to ignore the higher export rate that is automatically provided by Octopus.

In this example we subtract 10p from our export rate during the period that saving sessions normally fall within and thus steer Predbat away from
force exporting during that time. The saving session will still work correctly as a 10p adjustment on rates >100p will have little/no impact.

```yaml
  rates_export_override:
    - start: '17:00:00'
      end: '19:00:00'
      rate_increment: -10
```

You can also use a similar but opposite approach of setting a positive export rate_increment to encourage Predbat to discharge the battery at certain time periods.

If you have a very low overnight rate (such as Octopus Go) and want to ensure your battery is discharged just before the low rate period,
but you don't want to risk the battery running out too early (and importing at a higher rate),
you can add a rate export override for the period you want to discharge just before the low rate period:

```yaml
  rates_export_override:
    - start: '22:30:00'
      end: '23:30:00'
      rate_increment: 10
```

You can also define an import_rate_override with start and end times and only load_scaling (without a rate_increment or rate), e.g. if you just want to apply load scaling to certain defined periods.

- **date** is optional and if specified must be in the date format of "YYYY-MM-DD" e.g. "2023-09-09".
If a date is specified then the rate override applies to that specific date, otherwise, it applies to all dates
- **start** and **end** must be specified in "HH:MM:SS" time format e.g. "12:30:00"
- **load_scaling** is an optional percentage change factor in house load for the period - 1.0 would be no change, 0.8 is 80% of nominal house load,
2.0 would be a 100% increase (i.e. 2x) on normal historic house load
- **rate** is an optional figure in pence to override the rate for the specific period
- **rate_increment** is optional and is the number of pence to add (or subtract) to the reported energy rates during this period

## Rate offsets

If you are on an Agile or Tracker tariff you can tune future unknown energy rates by adjusting the entities **input_number.predbat_metric_future_rate_offset_import** (*expert mode*)
and **input_number.predbat_metric_future_rate_offset_export** (*expert mode*) inside Home Assistant to set the predicted offset for future unknown rates.

## Future Agile energy rates

In the energy market, it's possible to calculate the Octopus Agile rates from around 10am UK time using public data, you can
enable this in apps.yaml for Import, Export or both. This will approximate the next day's rates based on the spot prices.
The approximation is only used until the real Octopus Agile rates are released around 4pm.

- **futurerate_url** - URL of future energy market prices; this should not normally need to be changed
- **futurerate_adjust_import** and **futurerate_adjust_export** - Whether tomorrow's predicted import or export prices should be adjusted based on energy market prices or not.
Set these depending on whether you have an agile tariff for import, export or both
- **futurerate_peak_start** and **futurerate_peak_end** - during the peak period Octopus applies an additional peak-rate price adjustment.
These configuration items enable the peak-rate hours to be adjusted

CAUTION: You may violate the terms and conditions of the Nordpool site if you use this data and as such the authors of
Predbat accepts no responsibility for any violations:

<https://www.nordpoolgroup.com/en/About-us/terms-and-conditions-for-useofwebsite/>

```yaml
  futurerate_url: 'https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices?date=DATE&market=N2EX_DayAhead&deliveryArea=UK&currency=GBP'
  futurerate_adjust_import: True
  futurerate_adjust_export: False
  futurerate_peak_start: "16:00:00"
  futurerate_peak_end: "19:00:00"
```

## Grid Carbon intensity

Predbat can also track Carbon intensity by linking it to an integration which provides this data.

### UK Grid Carbon intensity

The National Grid provides this data, please install this integration: <https://github.com/jfparis/sensor.carbon_intensity_uk>

Once it is active update apps.yaml to link Predbat to the Sensor (if it's not already in your template):

```yaml
  # Carbon Intensity data from National grid
  carbon_intensity: 're:(sensor.carbon_intensity_uk)'
```

By enabling **switch.predbat_carbon_enable** you can view Carbon Intensity [in the predbat plan](predbat-plan-card.md).

Predbat can also [optimise your grid charging based on the Carbon footprint](customisation.md#battery-margins-and-metrics-options) by setting **input_number.predbat_carbon_metric**.

![image](https://github.com/springfall2008/batpred/assets/48591903/292c6625-412a-420a-9bd4-df68a937e93c)
