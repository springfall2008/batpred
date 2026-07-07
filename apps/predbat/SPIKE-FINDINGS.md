# Spike: shared-component `ctx` shape (Octopus)

**Branch:** `spike/octopus-ctx-shape` off `main` @ b1d2614c. Throwaway proof, not the Phase-2 refactor.

**Question:** can one shared component instance serve N tenants by threading a
per-tenant context through `run()`, given components already separate lifecycle
(`start()`) from per-tick work (`run(seconds, first)`)?

**Answer: yes, the shape holds.** `spike_octopus_ctx.py` drives ONE
`SharedOctopusService` through two tenants and proves 6 invariants (all PASS):
no credential bleed, independent staleness clocks, command-queue isolation,
zero per-tenant state on the shared object, a single fleet-wide rate budget,
and isolated per-tenant data. The port of the real hot path (staleness
due-logic `octopus.py:423`, command drain `:490`, cred access `:356`) was
mechanical — `self.X` → `ctx.X`.

## State partition (from `OctopusAPI.initialize`, octopus.py:354)

| Field | Verdict |
|-------|---------|
| `api_key`, `api` | per-tenant cred → **ctx** (but see friction 2) |
| `account_id`, `mpan` | per-tenant identity → **ctx** |
| `graphql_token`, `graphql_expiration` | per-tenant auth → **ctx** |
| `account_data`, `tariffs`, `saving_sessions`, `saving_sessions_to_join`, `intelligent_devices`, `free_electricity_events` | per-tenant data → **ctx** |
| `tariff_fetched_at`, `device_fetched_at` | per-tenant staleness clocks → **ctx** |
| `commands` | per-tenant UI queue → **ctx** |
| `automatic` | per-tenant config → **ctx** |
| `requests_total`, `failures_total` | per-tenant metrics → **ctx** (or aggregate fleet-wide) |
| `_product_info_cache` | **shared** → stays on the service |
| HTTP session / connector | **shared** connection pool → stays on the service |

## Blast radius (measured on real octopus.py)

**29 of 49 `OctopusAPI` methods** reference per-tenant `self` state or
`self.base` → each needs a `ctx` parameter. That's the per-component cost;
Octopus is mid-sized, so budget a similar-or-larger surface for gecloud
(bigger), and smaller for solcast/fox/kraken. Fleet ≈ 8 fetch components.

## Frictions the happy-path spike glosses over (the real cost)

1. **Event routing needs tenant identity.** `select_event`/`number_event`
   (octopus.py:382/399) are called by the component event dispatcher, which
   today routes by entity-prefix filtering to a single instance. Shared
   components require the dispatcher to resolve entity → tenant and pass the
   right `ctx`. This is a one-time change in the routing layer (`components.py`),
   not per-component — but it's a prerequisite, and it's exactly what the parent
   spec's central dispatcher already has to do for events.

2. **`OctopusEnergyApiClient` fuses transport + identity.** It's constructed
   with `api_key` AND owns the aiohttp `session` (octopus.py:319-343). To share
   the connection pool while varying creds per request, split it into a shared
   transport (session/connector) and per-call auth. `async_refresh_token` /
   `async_graphql_query` (which read `self.api` + `self.graphql_token`) are the
   fiddly methods. Moderate, and it recurs per vendor client (gecloud, solis…).

3. **`self.base` → `ctx.base` is fine, because base is already per-tenant.**
   The 7 methods using `self.base` (sensor publish / HA reads) just resolve
   against `ctx.base`. No new machinery — the tenant already owns its base/state
   sink. This is the reassuring part: the write-out path doesn't fight sharing.

## Verdict

The loop/step boundary was the hard architectural bone and it's already done —
you were right. Sharing is a **mechanical `ctx`-threading refactor** (~29
method signatures for Octopus), plus two one-time structural changes: dispatcher
tenant-routing (friction 1, shared across all components) and splitting each
vendor client's transport from its identity (friction 2, per vendor). No
re-architecture. This is a clean upstream PR with Trefor, same collaboration
shape as `plan_once`.

**Recommended Phase-2 sequencing:** land the shadow soak first; then take this
partition + the dispatcher tenant-routing change into a proper Phase-2 spec.
Start the real refactor on Octopus (cleanest, and its rates are already
tenant-independent per tariff — biggest density win for least risk).
