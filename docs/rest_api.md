# Predbat REST API

Predbat supports a REST API operated via its Web Interface, this is normally intended for use when Predbat runs in a Docker or Standalone rather than in Home Assistant

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

This is the recommended way to change a Predbat value, it will either make the change via home assistant or directly via Predbat if not connected.

You must post in 'json' with the service name and the service data, as per Home Assistant services:

```json
{
    "service": "switch/turn_on",
    "data": {"entity_id": "switch.predbat_expert_mode"}
}
```
