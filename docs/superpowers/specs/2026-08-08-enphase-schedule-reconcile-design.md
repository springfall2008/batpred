# Enphase Schedule Reconcile — Design

Date: 2026-08-08
Status: Approved (design review complete)

## Goal

Replace the per-family, incrementally-patched write path in `enphase.py`
(`_write_schedule` / `_put_schedule_with_conflict_retry` /
`_create_schedule_with_conflict_retry` / `_write_and_activate` /
`_is_schedule_pending` / the pending-debounce mechanism) with a single
reconcile function, driven by issue [#4461](https://github.com/springfall2008/batpred/issues/4461).

## Background

#4428 fixed same-family sibling conflicts (retry-once-and-prune on PUT).
This session's work on #4461 patched three more things on top of that
(create-path retry, a pending-status debounce, a bugfix for the debounce's
interaction with activation) and, while investigating, surfaced a fourth,
structural gap: a family that is *disabled* can leave a stale, still-live
window on the cloud that a *different* family's write then collides with -
none of the per-family retry/prune machinery looks across families. Each fix
revealed a new problem in a different place - the signal to stop patching and
reconsider the architecture (see `systematic-debugging` Phase 4.5).

## Decision

One function, `apply_battery_schedule(site_id)`, called from two places -
the periodic component poll (`run()`, on a new 5-minute refresh tier) and the
write-switch event handler - instead of only the latter. Both paths converge
on the same logic.

```
apply_battery_schedule(site_id):
    for attempt in 1..3:
        ok = _reconcile_once(site_id)
        if ok: return True
        if attempt < 3: sleep(short jittered backoff)
    return False   # give up - next periodic run or trigger tries again from scratch

_reconcile_once(site_id):
    read desired state from HA entities + fresh GET /schedules (always, no
    cached-state trust between passes)
    compute desired {enabled, start, end, limit} per family (cfg/dtg/rbd)
    determine which families differ from the fresh cloud read
    Phase 1 - cleanup, across ALL families before any writes:
        delete a family's existing schedule when: it should now be disabled,
        OR it has an extra/untracked sibling, OR (see "Update strategy"
        below) more than one family is changing this pass
        also prune any duplicate sibling within a family regardless
    Phase 2 - converge, only after phase 1 has run for every family:
        PUT-in-place if a matching id survived cleanup and only the
        window/limit changed; POST a new one if none exists; activate
        (batterySettings PUT) if needed
    also converge the reserve (profile PUT)
    return whether every step this pass succeeded
```

### Retry/backoff policy

Up to 3 attempts per `apply_battery_schedule` call, short jittered backoff
between attempts (a few seconds), then give up for this call and rely on the
next periodic run (5 minutes) or the next explicit trigger. No attempt is
made to track *why* a write failed (pending, conflict, transient error)
across calls - a fresh, unconditional retry from a clean re-read is simpler
and was what the removed debounce mechanism was trying to approximate badly.

### Update strategy: when to delete-then-recreate vs. PUT-in-place

PUT-in-place (update the existing schedule by id) is preferred when it's
safe - it avoids a brief window where a family has no schedule at all.
It is **not** safe whenever more than one family is changing in the same
pass: a new window for family A can overlap the *old*, not-yet-updated
window of family B even when neither family's *new* windows overlap each
other (e.g. charge 02:00-03:30 -> 03:00-04:30 and export 04:00-05:00 ->
05:00-06:00 in the same pass: new-charge overlaps old-export). Computing
actual interval overlaps to decide case-by-case is unnecessary complexity;
counting how many families have a pending change this pass is enough:

- Exactly one family changing -> PUT-in-place is safe (nothing else is
  moving, so no new overlap can be introduced).
- Two or more families changing -> delete all of them first (phase 1), then
  recreate via POST (phase 2). This costs nothing extra in the common
  Charging<->Exporting oscillation case from #4461, since one of the two
  families there is already being disabled (delete-required anyway) and the
  other has no id yet (create-required anyway).

DTG and RBD share the same `export_start`/`export_end` window and are
mutually exclusive (`export_soc < 99` vs `== 99` - see
`apply_battery_schedule`'s export-family selection), so they can never both
be live with different windows. The only real cross-family overlap risk is
CFG vs. the export family (DTG or RBD) - a single pairwise relationship, not
a three-way cycle between all three families.

### What is removed

`_put_schedule_with_conflict_retry`, `_create_schedule_with_conflict_retry`
(two near-duplicate retry helpers -> one outer retry), `_write_and_activate`,
`_is_schedule_pending`, `_schedule_pending_debounce_active`,
`schedule_pending_since` tracking (in `initialize()` and `get_schedules()`),
`ENPHASE_SCHEDULE_PENDING_DEBOUNCE_MINUTES`.

### What stays

- The HTTP failure log's method + caller-supplied context (`request_json`'s
  `context` parameter) - orthogonal, still valuable for diagnosing whatever
  does still fail.
- Write-failure -> dashboard visibility (`_note_schedule_write_result`,
  `schedule_write_failed`, `count_errors`, the dedicated
  `binary_sensor.predbat_enphase_<site>_schedule_write_ok` sensor) -
  orthogonal to *how* we retry; fires off `apply_battery_schedule`'s final
  give-up instead of scattered per-call-site failure paths.
- `_delete_schedule`, `_activate_cfg_mode` / `_activate_dtg_mode` /
  `_activate_rbd_mode` (via `_activate_control_mode`), `set_reserve`,
  `get_schedules` (simplified - no more pending-status bookkeeping),
  `_prune_sibling_schedules` (generalised into the phase-1 cleanup step).

## Testing

Unit tests (`test_enphase_api.py`) replace most of the per-family
write-helper tests with tests against `_reconcile_once`'s two phases and the
outer retry loop directly: phase ordering (delete before any write), the
delete-vs-PUT-in-place decision rule (single-family vs multi-family change),
the cross-family overlap scenario from this design discussion, retry-then-
succeed, retry-then-give-up, and reserve convergence. The write-failure
tracking tests stay conceptually the same, retargeted at the new call sites.

A live test harness (`test_schedule_reconcile` or similar, alongside the
existing manual `test_enphase_api` / `test_write_schedule` /
`test_write_reserve` functions at the bottom of `enphase.py`, gated
`# pragma: no cover` and invoked directly against a real account) drives a
scripted sequence of schedule changes - including a deliberate
charge/export-both-moving-simultaneously case - against the real Enphase
cloud, using windows between midnight and 05:00 so nothing it does can
actually trigger a real charge or export on the battery it's run against.
