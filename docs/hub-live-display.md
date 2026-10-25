# Optional measured Hub display feed

<!-- cspell:ignore PYTHONPATH -->

This GivEnergy Cloud adapter uses a provider-independent wire contract. The
companion SaaS and firmware integrations supply the saved independent display
preference. FoxESS measured data remains unavailable.
No deployment, enrollment or customer provider change is included.

Producer opt-in requires both `gateway_display_only: true` and the new boolean
`gateway_live_display: true` (default false), plus `gateway_device_id` and an
explicit `gateway_inverter_serial` string/list. The component registry exposes
the flag, but SaaS must bind the saved preference and exact physical selection
to these arguments plus `gateway_live_display_binding`. The binding must have
exactly enabled/device_id/provider/source_id fields matching the paired Hub and
canonical selection. Missing, invalid or disabled bindings cannot produce usable
actuals. The original raw publisher remains restricted to `predbat_data`.

A separate metadata-only publisher constructs a fixed device `/config` topic
and whitelist-only `{live_display: binding}` body. It requires strict display
role, valid paired device identity and matching topic base. It publishes on
reconnect and every 60 seconds, sending `{enabled:false}` when live opt-in is
absent/off/invalid. These sends are deliberately **non-retained**: replacing the
shared topic's retained full configuration would erase passive cutover intent,
mesh keys or other desired settings. Only this display metadata bypasses battery
write gating; no bus/control configuration is accepted and generic raw config
publication is still blocked. Failures retry without advancing dedup state.

`predbat/devices/<paired Hub>/predbat_data` gains `live_display` only when opted
in. Version 1 contains `v: 1`, `enabled: true`, `device_id`, `provider`, `source_id`,
and `available`. Complete actuals add integer `observed_at` (UTC epoch seconds),
`soc` (0..100), `capacity_wh` (1..1000000), `battery_w` (+charge), `grid_w`
(+export), `solar_w` and `load_w`. Signed powers are bounded to +/-1000000 W;
solar/load are non-negative. Genuine zero is valid. Missing values are omitted,
never filled from sensors or plan `block_soc` forecasts. An unavailable message
clears the receiver's previous actuals.

Source identity is SHA256 of UTF-8 `provider + "\n" + "\n".join(serials)`, where
serials are the sorted, unique uppercase selected physical inverter serials.
Selections contain 1..16 serials, each `[A-Za-z0-9_-]{1,32}`. Case-insensitive
duplicates are rejected. Current provider is `givenergy`. This binds selection,
not authority: firmware separately requires authorized device `/config` opt-in
with that exact provider/source/device binding. Existing instance and staff MQTT
principals can publish `/config`; it is not service-exclusive. Telemetry alone
cannot enable display or change the binding. Server-issued preference/selection
configuration is emitted by the separate SaaS follow-up; this branch validates
it before publication. If opting out removes the Gateway component altogether, the stopped instance
cannot send a final disable. Matching firmware treats valid non-retained metadata
as a **150-second monotonic lease**, renewed by this 60-second publisher. It
expires the external role independently of actual-data freshness after the last
enable received, applied on the next display-state tick. Telemetry never renews it; retained enable replay is rejected;
explicit disable is immediate when delivered. This preserves shared retained
configuration and ordinary passive/local UI without changing control state.
The customer save-to-engine-config-reload delay is separate, so this is not an
immediate save-time revocation guarantee. The paired SaaS lifecycle verifier
exercises real preference SQL save, config output, Components.start/stop, TLS MQTT
and firmware decoder/projection with deterministic lease time.

Only one registered inverter provider may be active. Raw GE API samples are
captured before the existing null-preserving merge, so old fields cannot gain a
new timestamp. A sample must be complete, timezone stamped, at most 300 seconds
old, and no more than 30 seconds ahead. The adapter requires the exact GE battery
set at one known site. EMS, Gateway and extra PV compositions are unavailable.
Multiple direct batteries require one explicitly common meter identity; grid
and house load are counted once, battery/PV power summed, actual SoC capacity
weighted. Different or unknown meters, mismatched sites, sample skew above 60
seconds, absent nominal capacity/voltage or unsupported providers are unavailable.
This deliberately leaves some real systems unsupported until topology semantics
are proven. It does not guess aggregation or use configured fallback capacity.

Live display publication includes a 60-second heartbeat even when readings are
unchanged. The compact complete MQTT packet budget is 1400 bytes, including UTF-8
topic and 16 bytes for protocol header/QoS overhead. Optional marginal matrices
are omitted if needed; an oversized packet is not published, so receiver expiry
applies. Ordinary local-controller publishing stays on its existing JSON path.

Validation:

```sh
# From coverage/, includes the new assertions through the existing registry:
python ../apps/predbat/unit_test.py --test gateway > /tmp/live-display-registry.log 2>&1
# From repository root, export actual publisher bytes for firmware host proof:
PYTHONPATH=apps/predbat python apps/predbat/tests/test_live_display.py /tmp/live-display-wire.json
# Optionally supply actual synthetic SaaS-helper output as the second path:
PYTHONPATH=apps/predbat python apps/predbat/tests/test_live_display.py /tmp/live-display-wire.json /tmp/hub-live-display-backend-fixture.json
```

The fixture is wholly synthetic, with no network or customer credentials. The
firmware repository's `docs/hub-live-display.md` documents the matching decoder
and actual DisplayState projection proof. Impact reports for initialize,
`async_get_inverter_status`, `_publish_predbat_data`, and the gateway test runner
were LOW in the indexed reference, with the publisher called by Gateway.run.
The initial index was a reference checkout; source call sites were also
inspected. The final exact-worktree refreshed index maps the expected Gateway
run/reconnect/publication paths and GE sample capture. Its aggregate change scan
reports HIGH because Gateway.run participates in eight indexed execution flows,
despite LOW individual pre-edit impacts. Existing control publishing and read-only
handlers are unchanged, and the full gateway CLI regression group passes. This
aggregate warning requires review and is not a customer deployment clearance.
Affected paths are raw GE observation capture, optional display publication and
component argument registration. Controller handoff and boot attachment paths are
unchanged.

## Independent display off

The protected SaaS `gateway_display_visibility` preference is separate from
`gateway_display_only`. A normal Hub controller can keep every polling/control
path while `gateway_display_enabled: false` hides operating data on its screen.
The publisher sends only device/instance-bound, revision-ordered non-retained
visibility metadata, fresh for 120 seconds, every 60 seconds and on reconnect.
No physical control fields use this path.

Explicit Cloud false retains a metadata-only component for reconnect delivery,
blocks the final display-data sink, and disables the external measured feed.
Default-unset Cloud has no optional Gateway component. Firmware persists false
independently of the unchanged 150-second external feed lease. A saved preference
is not evidence that an offline Hub applied it. These changes do not implement
per-inverter primary/secondary selection or automatic takeover.
