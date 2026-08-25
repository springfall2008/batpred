# Predbat dev environment

Runs Predbat from this checkout against a disposable Home Assistant instance,
pre-populated with dummy inverter entities, with onboarding and the long-lived
access token created automatically - no manual setup.

## Usage

```sh
dev/up.py
```

or, without the auto browser-open:

```sh
docker compose -f docker-compose.dev.yml up --build
```

- Home Assistant: <http://localhost:8123/> (login `dev` / `devdevdev1`)
- Predbat web UI: <http://localhost:5052/>

Dummy entities (battery SOC, charge/discharge rate, inverter mode, today's
energy sensors, etc.) live under `dev/ha_config/packages/predbat_dummy.yaml` -
edit their `initial` values, or change them live under Developer Tools > States
in the HA UI, to exercise different scenarios. Predbat's config for the
container is `dev/apps.dev.yaml.tmpl`.

## Hot reload

`apps/predbat/` is bind-mounted into the predbat container, and Predbat's own
standalone runner (`hass.py`) watches those files and exits when one changes.
The container is set to restart automatically, so editing and saving a `.py`
file picks up the change within a few seconds - no rebuild needed.

## Resetting

`dev/ha_config` and `dev/shared` are bind-mounted so HA's onboarding state and
the minted token persist across restarts (`docker compose up` again just re-mints
a token). For a fully clean instance:

```sh
docker compose -f docker-compose.dev.yml down -v
rm -rf dev/ha_config/.storage dev/ha_config/*.log dev/ha_config/*.db dev/shared
```

## Known limitations

- Onboarding is driven through Home Assistant's private (undocumented)
  `/api/onboarding` + websocket `auth/long_lived_access_token` sequence, in
  `dev/bootstrap.py`. It's the same flow the HA frontend uses, but if a future
  HA release changes it, that's the file to fix.
- There's currently no way to replay a user's bug-report `predbat_debug.yaml`
  into this container - that file only captures Predbat's own derived state,
  not raw HA entity states. Reproducing a bug through this dev HA instance is a
  planned follow-up.
