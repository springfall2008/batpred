# Battery control write fencing foundation

This patch does not enable a control selector, automatic backup or a production
handoff. There is no backend acknowledgement publisher, runtime configuration
switch or controller release. Existing installations have no gate and retain
their current behaviour.

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

## Remaining complete handoff contract

A canary-gated coordinator must durably bind a transition to source epoch,
configuration revision, exact inverter identity and target provider. The source
must close admission before returning evidence. Replayed, expired and mismatched
receipts must be rejected. A fetched configuration revision is not evidence
that the old writer stopped. Process restarts must reconstruct closed epochs
rather than silently reverting to a controller with no gate.

The Hub protocol must acknowledge an epoch/revision-bound physical fence after
old commands have completed and old retained schedules have been invalidated.
Read-only probe leases must be recognised by standby reconciliation and expire
back to parked state. Target initialisation and all write paths must remain
blocked until source release and fresh target readiness are verified. Only then
may the backend commit the provider change and release the target writer.
No such physical fence or durable receipt exists in this patch, so activation
must remain unavailable.

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
