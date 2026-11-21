# LuxPower VPP component

`apps/predbat/luxpower.py` is a fail-closed client for LuxPower VPP Partner API
v0.1. It is intentionally a foundation rather than a working inverter adapter.

The component requires `luxpower_asset_id` and `luxpower_base_url`. It remains
passive unless the relevant capability is explicitly enabled:

- `luxpower_binding_ready`
- `luxpower_telemetry_ready`
- `luxpower_control_ready`

Partner operations additionally require `luxpower_partner_token`. Supplying a
token does not open a readiness gate.

The component is imported only when configured. Invalid configuration leaves
LuxPower inactive and reports a component load error while PredBat continues
starting its other components. Binding status is retained by the cloud account
setup and is not an engine configuration option.

The client exposes device discovery, asset binding, latest telemetry, dispatch
submit, dispatch status and dispatch cancellation. Dispatch validation enforces
the documented single-command shape, UUID idempotency key, UTC control window,
allowed modes and mode-specific `powerLimitW` rules before making a request.

LuxPower VPP v0.1 does not define minimum or target state of charge. The
component therefore provides no state-of-charge control and must not be treated
as a complete PredBat inverter adapter until LuxPower confirms the missing
safeguard and releases partner telemetry/control for testing.
