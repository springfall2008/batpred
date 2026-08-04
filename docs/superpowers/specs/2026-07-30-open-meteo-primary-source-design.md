# Open-Meteo as primary solar source with Forecast.solar fallback

Date: 2026-07-30

## Motivation

Investigation of a customer report ("solar constantly under reports") found that Forecast.solar
carried a consistent low bias for that site: over seven logged days it forecast 127.2 kWh against
168.7 kWh actually generated, low on seven days out of seven, giving PV calibration an
`average_day_scaling` of 1.32.

The investigation ruled out every Predbat-side cause:

- The `watts` to kWh integration in `download_forecast_solar_data()` reproduces Forecast.solar's own
  `watt_hours_day` to within 0.35%, so no energy is lost in parsing.
- The caps in `pv_calibration()` do not clip the planner's data. They apply only to
  `pv_estimateCL`/`pv_estimate10`/`pv_estimate90`, which annotate the published sensor attributes;
  `solcast.py` returns the uncapped `pv_forecast_minute_adjusted` to the planner.
- The customer's configuration is correct. Reconstructing the actual generation profile from the
  logged slot adjustments fits the configured azimuth of 85 (WSW) far better than south or east
  (SSE 0.00067 against 0.00586 and 0.01920). Open-Meteo GTI at the same tilt, azimuth and kWp
  predicts 182.9 kWh across the same seven days against 168.7 kWh actual, confirming the declared
  6.44 kWp is right.

Open-Meteo is therefore worth evaluating as the primary source for affected sites. Predbat can
already fetch Open-Meteo, but only as a failure fallback via `forecast_solar_open_meteo_backup`,
which fires only when Forecast.solar returns no data. A biased-but-successful response never
triggers it.

This design adds a single setting that reverses the order, so a site can be switched to Open-Meteo
without restructuring its `forecast_solar` configuration.

Note on cost: the Open-Meteo path calls the Ensemble API for P10. Ensemble is available on the free
tier, rate limited, so evaluating a small number of customers needs no paid plan.

## The setting

A new top-level boolean, `forecast_solar_open_meteo_first`, defaulting to `False`:

```yaml
forecast_solar:
  - latitude: 54.81306
    longitude: -1.38647
    declination: 32
    azimuth: 85
    azimuth_zero_south: True
    kwp: 6.44
    api_key: xxxx

forecast_solar_open_meteo_first: True
```

The existing `forecast_solar_open_meteo_backup` is unchanged in both type and behaviour. When
`forecast_solar_open_meteo_first` is set it implies the reverse fallback, so it works standalone and
`forecast_solar_open_meteo_backup` becomes irrelevant.

Rationale for a sibling boolean over extending the existing flag into a mode: the existing setting is
declared `{"type": "boolean"}` in `APPS_SCHEMA` and is documented as a boolean in two places.
Extending it to boolean-or-string requires back-compat handling for no functional gain. A separate
flag is also easier to toggle per customer during an evaluation.

## Behaviour

`fetch_pv_forecast()` gains a branch ahead of the existing `self.forecast_solar` branch:

```python
if self.forecast_solar and self.forecast_solar_open_meteo_first:
    primary_configs = self.open_meteo_forecast if self.open_meteo_forecast else self.forecast_solar
    pv_forecast_data, max_kwh = await self.download_open_meteo_data(configs=primary_configs)
    divide_by = 30.0
    create_pv10 = True
    if not pv_forecast_data:
        pv_forecast_data, max_kwh = await self.download_forecast_solar_data()
elif self.forecast_solar:
    # existing branch, unchanged
```

The config-selection line mirrors the existing backup path: an explicit `open_meteo_forecast` section
wins if present, so Open-Meteo-specific options such as `shading_factors` remain available;
otherwise the `forecast_solar` entries are reused directly.

Forecast.solar is not called at all while Open-Meteo succeeds. This is deliberate: it keeps the
comparison clean and stops consuming the site's Forecast.solar quota.

### Why config reuse needs no translation

`download_open_meteo_data()` and `download_forecast_solar_data()` read the same keys (`latitude`,
`longitude`, `postcode`, `declination`, `azimuth`, `kwp`, `efficiency`) and apply `azimuth_zero_south`
identically, both calling `convert_azimuth()` only when the flag is absent or false. Both return
`max_kwh` as `kwp * efficiency`. An `api_key` present in a `forecast_solar` entry is ignored by the
Open-Meteo path. `shading_factors` is optional and simply absent when reusing `forecast_solar`
entries.

The azimuth convention is the one silent-failure risk in this reuse: a mismatch would mis-orient
every array without raising an error, so it is covered by a dedicated test.

### Fallback trigger

`not pv_forecast_data`, the same condition the existing backup path uses. A biased-but-successful
Open-Meteo response does not fall back, matching the existing semantics.

## Calibration during a source change

PV calibration derives `past_day_forecast` from up to seven days of `sensor.predbat_pv_forecast_h0`
history. Immediately after the setting is flipped that history still holds Forecast.solar values,
so `average_day_scaling` blends the two sources until the window rolls over.

The chosen handling is to accept the settling period rather than reset or partition the history.
Calibration self-corrects within seven days, and resetting would instead disable calibration
entirely until three valid days accumulate.

To make the transition visible rather than silent, the active source name is persisted through the
existing Storage abstraction and a warning is logged when it differs from the previous run,
stating that calibration will settle over the following seven days.

Practical consequence: an A/B comparison should not be judged until seven days after the switch.

## Testing

Mirroring the two existing backup tests in `tests/test_solcast.py`:

- `test_fetch_pv_forecast_open_meteo_first_used_when_set` — Open-Meteo returns data; assert it is
  used and `download_forecast_solar_data` is never called.
- `test_fetch_pv_forecast_open_meteo_first_falls_back_on_failure` — Open-Meteo returns nothing;
  assert Forecast.solar data is used.
- `test_fetch_pv_forecast_open_meteo_first_ignored_when_unset` — flag off; assert existing ordering
  is intact.
- `test_fetch_pv_forecast_open_meteo_first_preserves_azimuth_zero_south` — a `forecast_solar` entry
  with `azimuth_zero_south: True` reaches the Open-Meteo request with the azimuth unconverted.
- `test_fetch_pv_forecast_open_meteo_first_logs_source_change` — source change emits the warning.

## Documentation

Extend the existing "Open-Meteo backup for Forecast.solar" section in `docs/apps-yaml.md` and the
matching section in `docs/install.md`, covering the new setting, the reversed fallback, the fact
that Forecast.solar is not called while Open-Meteo succeeds, and the seven-day calibration settling
period.

## Out of scope

- The cap inconsistency where published `pv_estimateCL`/`10`/`90` attributes are capped to
  nameplate/observed peak while the planner's data is not. Worth fixing separately; it is the most
  likely explanation for a user reporting a forecast apparently pinned near a fixed ceiling.
- The possible double-derate where `kwp * efficiency` is sent to Forecast.solar, which applies its
  own internal system losses. Unverified, roughly 5%, and affects only the Forecast.solar path.
- Any change to `forecast_solar_open_meteo_backup`.
- Reducing Open-Meteo request volume (coordinate rounding for cache sharing, computing GTI locally
  from GHI/DNI/DHI, decoupling the Ensemble refresh cadence). Relevant only at fleet scale.
