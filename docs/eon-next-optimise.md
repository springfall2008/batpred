# E.ON Next Optimise pricing

The optional `eon_optimise` component reads published import and export prices
directly from the UK E.ON Next Optimise app's Amber backend. It does not enable
SmartShift, schedule an EV or send battery commands. This is not the Australian
Amber public API and does not use an Australian Amber API key.

## Relationship to Kraken

Predbat's Kraken component authenticates with the supplier, discovers the active
tariff and retrieves dated import/export rates through REST or GraphQL. It then
publishes rate sensors and connects them to the existing optimiser through
`metric_octopus_import` and `metric_octopus_export`.

Optimise follows that same consumer contract, but obtains its rates from the
Optimise app backend with Cognito authentication. It requires both price
channels. It does not infer Optimise prices from historical Kraken rates.

## Configuration

Install the Python dependencies in `requirements.txt`, including
`pycognito==2024.5.1`. An add-on release must include this dependency in its
runtime image before enabling the component; updating Python source alone does
not install packages in an existing add-on image.

In `apps.yaml`, inside the Predbat configuration:

```yaml
  eon_optimise_enable: true
  eon_optimise_email: !secret eon_optimise_email
  eon_optimise_password: !secret eon_optimise_password
```

In your private `secrets.yaml`:

```yaml
eon_optimise_email: "your-optimise-app-login"
eon_optimise_password: "your-optimise-app-password"
```

Use credentials that can sign in to the Optimise app. Never commit this file.
The email and password are marked as secrets in Predbat's component registry,
so they use its existing configuration-redaction mechanism. Cognito tokens are
kept in memory and authentication runs outside the component's event loop.

Enable one owner of the pricing inputs. Disable Kraken or another native tariff
component if it would also set those inputs. Opting into this component connects
both inputs to its sensors, replacing existing manual sensor bindings. Keep the
existing standing-charge configuration: this endpoint does not return a
standing charge, so this component neither guesses nor overwrites one.

Remove `rates_import_octopus_url` and `rates_export_octopus_url` keys entirely,
including empty entries. They take precedence over sensors in Predbat. The
component and planner reject these settings, a configured `kraken_provider`,
or a configured Octopus API account/key pair rather than silently using another
tariff. The planner also checks that both sensor bindings still belong to E.ON.

## Published entities

With the default `predbat` prefix:

- `sensor.predbat_eon_optimise_import_rates`
- `sensor.predbat_eon_optimise_export_rates`
- `sensor.predbat_eon_optimise_status`

Rate sensor states are the number of available periods, as with Kraken. Their
`rates` attributes contain `valid_from`, `valid_to`, `value_inc_vat`,
`source_quality` and the supplier's `provider_estimate` flag. Prices use p/kWh;
negative prices remain valid. Export is normalised once into export earnings.
Account-specific boosters already included in the response are not added again.

`current` means a successful recent response covers the present interval in
both channels. `cached` means the last response is still usable after a failed
refresh. `unavailable` means the freshness or coverage check failed. A published
future price is a supplier forecast, not final settlement or a final bill.

## Freshness and missing prices

Poll every five minutes, with at most one authentication retry after HTTP 401.
Failed authentication and transport messages are sanitised. Repeated failures
do not cause a tight retry loop. Both channels are validated before replacement;
a shortened forecast replaces the old future rather than retaining its tail.

Cache price fields and the original observation time through Predbat's Storage
component, using a login-specific hashed cache key. Do not persist credentials
or authentication tokens in the price cache. Responses expire after 15 minutes;
a restart or failed fetch never resets their age. Missing current coverage or
expired data publishes empty rate lists with an explicit unavailable status.

The planner checks freshness and current import/export coverage before starting
a new calculation, and rejects missing current sensor prices before basic-rate
fallback. This blocks a new calculation; it does not cancel commands already
sent to an inverter.

For gaps beyond the current interval, use the same shared `rate_replicate`
behaviour as Kraken: copy the previous day's available rates, or use the last
available rate where no matching interval exists. Predbat's configured future
rate adjustments still apply. Replicated intervals remain marked as estimates
in the planner; they are not published as new supplier rates. Shortened forecasts
replace the supplier data, then this common estimation fills future gaps.
Unlike Kraken's longer-lived cache, this app feed expires after 15 minutes
because its prices can change during the day. No new fixed fallback price is
introduced, and the planning horizon is not restricted to published intervals.

## Scope and validation

The feature is disabled by default. It requires no custom Home Assistant price
integration. The existing HA integration can remain running for comparison or
research, but only the selected native component should own planner inputs.

The app backend is not a guaranteed public API. **Live testing is limited to
the contributor's own E.ON Next Optimise account in their UK supply region.
Other accounts and regions have not been tested.** Multi-site selection and
interactive MFA challenges are not implemented. An MFA/login challenge produces an authentication error rather
than requesting or logging a one-time code.

Registered `eon_optimise` tests cover sign/negative-price handling, credentials
redaction metadata, partial and duplicate channels, polling, cache restoration,
expiry, missing current prices, shortened coverage and bounded 401 retry.
The initial read-only live probe matched all 97 returned periods per channel
against the existing Home Assistant feed, with zero price differences.
