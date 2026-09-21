# Predbat REST API

Predbat supports a REST API operated via its Web Interface, this is normally intended for use when Predbat runs in a Docker or Standalone rather than in Home Assistant.

## /api/state - Method GET

entity_id -> The entity to get, if not specified all entities are returned

Example:

/api/state?entity_id=predbat.status

```json
{
  "state": "Demand",
  "attributes": {"friendly_name": "Status", "detail": "", "icon": "mdi:information", "last_updated": "2025-02-23 20:49:57.855074"}
}
```

## /api/state - Method POST

Sets the state of an entity, this is done by changing its value without calling the service API, this means Predbat will not notice the change
Not normally recommended

You must post in 'json' as follows

```json
{
   "entity_id": "predbat.status",
   "state": "Hello",
   "attributes": {"friendly_name" : "Fire"}
}
```

## /api/service - Method POST

This is the recommended way to change a Predbat value, it will either make the change via Home Assistant or directly via Predbat if not connected.

You must post in 'json' with the service name and the service data, as per Home Assistant services:

```json
{
    "service": "switch/turn_on",
    "data": {"entity_id": "switch.predbat_expert_mode"}
}
```

The response body is `true` on success, `false`/`null` on failure (via Home Assistant) or when the call was made in standalone mode against a service Predbat doesn't simulate itself.

For a write that a Hub-connected component (e.g. the Gateway) rejected, or could not confirm, the response body is instead a JSON object:

```json
{
    "success": false,
    "error": "GatewayMQTT: inverter command rejected: not_polled (CH0000A001)",
    "outcome_unknown": false
}
```

`error` is a human-readable reason for the failure. `outcome_unknown` tells you whether the write may still have taken effect: `false` means the write was rejected before anything was sent to the device (e.g. the target wasn't found, or telemetry was stale), so it's safe to fix the underlying condition and retry. `true` means the outcome is genuinely unknown — for example the device's acknowledgement timed out, or the publish itself failed — and the write may or may not have landed. **Do not blindly retry when `outcome_unknown` is `true`**; check the entity's state first, since retrying an unconfirmed write can repeat one that already succeeded.
