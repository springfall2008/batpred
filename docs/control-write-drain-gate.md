# Battery control write fencing foundation

This patch does not enable a control selector, automatic backup or a production
handoff. Explicit `control_handoff: {enabled: true}` now attaches a service-issued
epoch before any components are constructed and publishes local drain evidence.
There is no provider selector or controller release. Existing installations have
no gate and retain their current behaviour.

An explicitly attached `base.control_write_gate` binds battery control to one
provider, epoch, configuration revision and monotonic lease. Admission and
closure share a thread lock. Closure is permanent and transition-bound. Every
actual GivEnergy inverter setting POST, including retries, passes admission.
Gateway battery commands and executable schedules pass admission immediately
before MQTT submission, including reconnect and periodic retained-plan paths.
Expired epochs reject new attempts. Neither a wait timeout nor cancellation
proves drain. Previously ambiguous attempts remain latched for that epoch.

## Provider completion evidence

The [GivEnergy API Remote Control Codes contract](https://givenergy.cloud/docs/api/v1#remote-control-codes)
describes a single-device response with a success boolean and the resulting
device value. We infer completion of that individual setting operation only
when `success is True` and the value is a finite nonnegative number or boolean.
Strings, missing fields, malformed success flags, negative values and missing
responses remain uncertain. This deliberately supports a conservative subset
of setting value types. Terminal exceptions for documented rejection codes
-3, -4 and -7 are definitive non-application. Retryable errors remain uncertain,
including -2 despite its documented non-application, because it is currently
handled in the shared retry-body path. Successful later attempts never clear
an earlier uncertain attempt. The optimistic `pending_writes` cache is not
physical readback evidence and is not used by the gate.

MQTT publication completion proves broker acceptance only. Every submitted
battery command or schedule therefore remains uncertain even after its owner
loop future completes. A caller cancellation leaves that future tracked until
it actually completes. A retained schedule already accepted by the broker or
Hub is not revoked by closing this gate and may execute later. The gate cannot
currently certify a physical Hub handoff.

The supported domain is battery inverter control. Gateway `ev/command` and
GivEnergy charger endpoints are independent roles and remain available, as do
Hub display data and query topics. Other inverter providers need their own
transport adapters and completion contracts before supporting handoff.

## Explicit service enrollment

Enrollment is off by default. `control_handoff` accepts only a dictionary with a
strict Boolean `enabled` field. An enabled engine installs a closed sentinel,
then calls the fixed `engine-battery-control` endpoint using `SUPABASE_URL`,
`SUPABASE_KEY` and the instance UUID in `user_id`. Attach failure, malformed data,
missing credentials or an unsupported provider configuration aborts startup
before `Components` exists. There is no legacy fallback, local epoch minting or
later reopening. Reinitializing the same app object also seals and retains its
existing gate, including any pending work, rather than replacing it or opting
back into legacy behavior. The service must pre-enroll one epoch and claim exactly one
process boot, with server-side flag and authority checks also disabled by default.

Active responses bind the lower-case provider ID (`givenergy` or
`predbat_gateway`), UUID epoch, positive integer `config_version`, current UUID
`boot_id` and integer `lease_for_s` from 1 to 90. Deadlines use monotonic request
start, accounting for network delay. Only a same-boot, unchanged binding can
renew a still-live gate. Expiry or closure is permanent, even if a delayed active
response arrives. HTTP redirects are refused. Network errors and HTTP 5xx other
than 503 retain only the remaining original lease; refusal, malformed responses,
409 and 503 close immediately. No exception detail or credential is logged.

The dedicated service worker runs outside component event loops. An exact
`closing` response binds a transition UUID and permanently closes admission.
Only after pending local transports reach zero does it POST `action: drained`
with `instance_id`, `boot_id`, `epoch` and the exact nested `evidence` snapshot:
provider, epoch, configuration revision, transition, boot, closed=true, pending=0
and the actual uncertain count. Nonzero uncertainty remains ambiguous, never
becomes a successful physical drain. The server independently validates the
current engine lease and reservation; expired reports cannot be accepted.
Shutdown and startup exceptions seal before stopping components, without a
success report in cleanup.

Enrolled engine arguments must include `saas_config_version`, emitted from the
SaaS response revision by the operator or legacy image wrapper. Before claiming
a boot, startup rejects an absent or malformed revision. After attaching, it
requires that loaded revision to equal the service-issued revision before
starting the worker or constructing components. Stale configuration therefore
leaves startup's admission sentinel closed. Ordinary, unenrolled startup does
not require this field.

This comparison binds the loaded revision number, not a configuration content
hash or the physical inverter identity. Exact inverter identity and target
readiness still require independent evidence before any write release is
authorised. The paired operator and image-wrapper changes must be deployed
before explicit enrollment; this feature remains disabled by default.

Enrollment requires the selected supported writer to be configured. Every actual
`Components.initialize`, including restarts, rejects eligible unsupported inverter
components before import or construction. GivTCP REST and other provider writers
therefore cannot silently bypass the gate. A real HA credential is also refused:
`Inverter.write_and_poll_value`/switch/option, custom service and MQTT paths can
otherwise reach `HAInterface.call_service` and its external websocket outside the
certified transports. Without a real HA credential that interface only loops
back into registered components, where the supported physical sinks are gated.
Custom inverter `auto_restart` commands are refused because they can invoke
`os.system` outside these transports. Independent charger components and Hub
display/query traffic remain available. This is deliberately limited support,
not general HA, REST or future FoxESS handoff coverage.

## Remaining complete handoff contract

A canary-gated coordinator must durably bind a transition to source epoch,
configuration revision, exact inverter identity and target provider. The source
must close admission before returning evidence. Replayed, expired and mismatched
receipts must be rejected. A fetched configuration revision is not evidence
that the old writer stopped. Process restarts must never reuse an enrolled epoch: a new boot requires explicit
server re-enrollment after reconciliation, with a fresh epoch.

The Hub protocol must acknowledge an epoch/revision-bound physical fence after
old commands have completed and old retained schedules have been invalidated.
Read-only probe leases must be recognised by standby reconciliation and expire
back to parked state. Target initialisation and all write paths must remain
blocked until source release and fresh target readiness are verified. Only then
may the backend commit the provider change and release the target writer.
The service integration only records source process evidence. Joined engine,
backend and physical firmware proofs, target baseline reconciliation, provider
commit/release and power-loss tests remain required, so activation stays unavailable.

`download.get_github_directory_listing` enumerates all files under
`apps/predbat`, so `control_handoff.py` participates in the existing dynamic
module download. Bundled images must include that new file with both importing
modules. No separate hardcoded module manifest was found in this repository.

## Synthetic verification

Python 3.14.8, invoked through `/private/tmp/predbat-hub-tools-venv/bin/python`
(which resolves to the Homebrew interpreter), runs the actual decorated
`GECloudDirect` and actual `GatewayMQTT` sinks with synthetic HTTP and MQTT
transports. Tests race eight threads, close while a POST is outstanding, reject
truthy positional POST retries after closure, validate strict vendor responses,
retain timeout/late-server-application uncertainty, cancel an awaiting Gateway
caller while its owner-loop future is pending, and fence retained-plan reconnect
and periodic republish. Independent EV, display and query traffic remains usable.
The existing Gateway and GivEnergy suites also run unchanged.

From `coverage`, run each registered suite with:

```sh
env PYTHONPATH=../apps/predbat /private/tmp/predbat-hub-tools-venv/bin/python ../apps/predbat/unit_test.py --test control_handoff
env PYTHONPATH=../apps/predbat /private/tmp/predbat-hub-tools-venv/bin/python ../apps/predbat/unit_test.py --test gateway
env PYTHONPATH=../apps/predbat /private/tmp/predbat-hub-tools-venv/bin/python ../apps/predbat/unit_test.py --test ge_cloud
```

The service regression suite additionally invokes actual `PredBat.initialize` and
`terminate`, actual component eligibility, a real worker thread and the real HTTP
client call site with synthetic responses. It verifies startup refusal before
component construction, monotonic lease delays, bound renewals, closure during an
actual Cloud write, ambiguous evidence and shutdown without publication.
