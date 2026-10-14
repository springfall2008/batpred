# Physical inverter gateways, runtime foundation

The grid contains physical inverters. Each inverter has configured gateway
endpoints, with one effective primary and zero or more configured secondary
endpoints. A Hub is a gateway for supported manufacturers, not a manufacturer
or a second copy of the inverter. Independent display and EV roles do not grant
battery control. Secondary means standby, without automatic takeover.

This increment wires configuration capture and mutation admission into the real
Cloud and Hub components. It is **not an enabled selector**: there is no config,
server response or apps.yaml installer for the runtime yet. Production source
control continues through the existing instance-wide lease. No provider,
credential, pairing, firmware mode or enrollment is changed.

## Runtime contract

`GatewayEndpoint` separates a stable endpoint UUID from its provider and a
server-owned credential reference UUID. `InverterRoute` identifies the physical
inverter by manufacturer and complete canonical serial, and references primary
and secondary endpoint UUIDs. Physical GivEnergy serials are case-insensitive;
other manufacturer adapters remain unavailable rather than inheriting that rule.

`InverterGatewayRuntime` freezes the inventory and sorts physical slots by full
identity. The current components support one endpoint per provider, so a second
Cloud account or second Hub of the same provider is explicitly rejected. Future
multiple-endpoint adapters must select by endpoint UUID at the transport, not
silently collapse IDs into provider labels.

The real `GECloudDirect.async_automatic_config` and
`GatewayMQTT.automatic_config` run into task-local configuration batches. Their
provider-local `set_arg` hooks keep both configured gateways and their mappings,
including standby mappings, without disabling Cloud when the Hub discovers an
inverter. Only the effective primary contributes each optimiser slot. The final
argument dictionary is published once after all selected mappings exist. Any
subsequent entity-binding change requires a new boot and seals that provider's
control gates. An unavailable secondary leaves the primary's gate alone.

The actual Cloud POST-attempt wrapper uses the complete request serial. The
actual Hub MQTT boundary parses the command and requires a complete selected
serial, an allowed per-inverter command and its own live gate. Neither suffix
aliases nor gateway-wide commands confer authority. HTTP retries re-enter
admission, and ambiguous outcomes stay latched. MQTT broker completion remains
uncertain physical completion. Independent gates cannot be shared between two
inverters. The existing instance-wide gate and this runtime cannot mix.

Gateway rate caps resolve global optimiser slots through the physical inventory,
not the Hub's local discovery order. Background Cloud defaults skip secondary
units. In routed mode the engine does not send gateway-wide read-only/reset
commands or executable plans: physical mode belongs to the signed coordinator
protocol and the existing plan lacks a proven per-inverter scope. Raw retained
schedule attempts are refused at the transport too. Display and EV topics retain
their separate admission path.

The runtime uses the private `__inverter_gateway_runtime` member, excluded by the
existing debug-export policy. There is no secret material in endpoint IDs or
inventory records, and tests exercise the actual support export.

## Explicit limits and next integration

The constructor's externally supplied gates are an internal test/integration
boundary, not authority issued by this patch. Ownership of endpoint credentials,
source and target readiness, saved preference loading, credential rotation and
physical fence receipts must be verified by service-only SQL and HTTPS before a
runtime is installed before any components. No normal config toggle can create
a gate, reopen one or replace an effective primary.

The next bounded integration must:

1. Persist physical inverters, owned gateway endpoints and their full-identity
   associations, with requested and effective primary IDs separate. Preserve both
   credentials and dormant Hub pairing transactionally. User preference changes
   cannot update effective authority.
2. Issue an immutable loaded-config/credential-reference/route binding before
   claiming the startup boot. Initially support one physical inverter, Cloud and
   Hub both saved, retaining the existing exact source lease and parked Hub proof.
   Refuse missing or changed loaded bindings before any lease or boot mutation.
3. Attach before component construction and only install the selected current
   primary after actual service validation. Extend the real producer/loader/TLS
   proof through the selected physical transport sink, including wrong credential,
   wrong endpoint, stale preference, replay, expiry and concurrent startup cases.
4. Complete exclusive selection handoff using source transport drain plus signed
   physical Hub fence, actual target readiness and an immutable target engine
   receipt. Only then atomically change effective authority. Cancellation cannot
   reopen a closed source. Target activation remains unavailable meanwhile.
5. Extend per-inverter leases and coordinator reservations beyond the current
   instance-wide source session. Multiple physical inverters need explicit meter
   ownership/deduplication, aggregate actuation adapters, per-endpoint component
   instances and scoped executable plans before general enablement.

Cloud EMS/vendor-Gateway aggregation, extra PV-only sources, incomplete mappings,
Hub suffix collisions and shared/derived multi-inverter site-meter mappings are
rejected by this increment. The mixed-slot test uses independent synthetic
measurements; it proves actual mapping and dispatch admission, not physical meter
ownership or a completed multi-inverter production controller.

## Local verification

From this checkout:

```sh
PYTHONPATH=apps/predbat /private/tmp/predbat-hub-tools-venv/bin/python -m pytest \
  apps/predbat/tests/test_inverter_gateways.py \
  apps/predbat/tests/test_control_handoff.py \
  apps/predbat/tests/test_gateway_display.py \
  apps/predbat/tests/test_gateway.py \
  -k 'not test_ev_soc_battery_size_through_get_arg' -q
```

The excluded pre-existing function requires the custom project runner's
`my_predbat` argument and is not an available pytest fixture. The remaining
actual Gateway suite and existing source-gate/display regressions run in this
invocation. Provider and broker responses are synthetic, with no customer keys,
connections or commands.

Optional automatic mappings retain the provider's existing semantics. An omitted
key leaves the configured value or consumer default in place. An explicit
`None` removes an unsupported mapping when no selected slot supplies a value;
it cannot revive a stale standby control entity. For mixed selected providers,
a supplied value replaces only its own physical slot, while an omitted slot
retains its existing configured value. Rediscovery cannot change the composed
bindings within the same boot. Regression tests use actual Cloud discovery and
`Inverter.refresh_config` to verify explicit 1500 W and zero-export caps, a
discovered site cap, and the absent-cap consumer default.
