# Shared `MockBase` for standalone CLI harnesses

**Date:** 2026-07-29
**Status:** Approved

## Problem

Eleven production modules each define a near-identical `MockBase` class whose only purpose is
to stand in for the PredBat base object when the module is run standalone from the command
line (`python fox.py --key=...`). All are marked `# pragma: no cover`.

| Module | Class | Notes |
|--------|-------|-------|
| `axle.py` | `MockBase` | adds `config_root`, `fatal_error`, `had_errors`, `components`, `now_utc_exact`, `call_notify` |
| `deye.py` | `MockBase` | |
| `enphase.py` | `MockBase` | adds `fatal_error`, `had_errors` |
| `fox.py` | `MockBase` | narrow `get_arg(key, default)`; naive `midnight_utc` |
| `gecloud.py` | `MockBase` | adds `config_root`, `plan_interval_minutes`, `ha_interface` |
| `kraken.py` | `KrakenMockBase` | takes `user_id=`; naive `midnight_utc`; sole user of `import json` |
| `octopus.py` | `MockBase` | adds `config_root`, `plan_interval_minutes`, `now_utc_exact`; has a `QuietMockBase` subclass |
| `sigenergy.py` | `MockBase` | takes `readonly=`; non-mutating `dashboard_item` |
| `solax.py` | `MockBase` | narrow `get_arg(key, default)`; `local_tz = timezone.utc` |
| `solis.py` | `MockBase` | persisting `get_arg`/`set_arg`; naive `midnight_utc` |
| `teslemetry.py` | `MockBase` | persisting `get_arg`/`set_arg` |

Roughly 90% of each class is identical: `get_state_wrapper`, `set_state_wrapper`, `log`,
`dashboard_item`, `get_arg`, `set_arg`, and the bulk of `__init__`. The divergence is
incidental rather than intentional — each was copied from a sibling and then drifted.

The drift has costs beyond duplication. Modules whose `MockBase` omits `fatal_error`,
`had_errors` or `components` will raise `AttributeError` if a `ComponentBase` property that
reads them is ever exercised from the CLI path. And the most widely-copied `dashboard_item`
mutates its caller's `attributes` dict before storing it (see Behaviour Unifications below).

## Scope

### In scope

The eleven CLI test-double classes listed above.

### Explicitly out of scope

- **`tests/test_hainterface_common.py:MockBase`** and the other test-local mocks in
  `test_solcast.py`, `test_load_ml.py`, `test_solis.py`. These share a name but mock a
  different surface with test-specific behaviour, and are already shared within the test
  suite where it matters.
- **`web_mcp.py:1293`** — an inline mock with an unrelated surface (`raw_plan`, `soc_kw`,
  `is_running`); it has no `get_state_wrapper`, `get_arg`, `log` or `dashboard_item`. There
  is nothing to share.
- **`self.record_status(...)` calls in `octopus.py`.** These calls (around
  `octopus.py:2067-2334`) are inside `class Octopus`, a PredBat mixin composed alongside
  `Output` in the main `PredBat` class, where `Output.record_status` genuinely exists at
  runtime. They are unrelated to `OctopusAPI`/`ComponentBase` or to `self.base`, and out of
  scope for this work.

## Design

### New module: `apps/predbat/mock_base.py`

A single shared class, mirroring the `component_base.py` file convention. It is a **concrete
class, not an ABC** — being directly instantiable is the point; subclasses only add extras.

### Constructor

```python
def __init__(self, config_root="./temp_predbat", local_tz=None, **kwargs):
```

`local_tz` defaults to the machine's local timezone; `solax` passes `timezone.utc` to preserve
its current behaviour.

Sets the full superset of attributes, derived from every `self.base.*` dereference across
`component_base.py` and the eleven modules:

`local_tz`, `now_utc`, `now_utc_exact`, `midnight_utc`, `minutes_now`, `prefix`, `args`,
`entities`, `config_root`, `plan_interval_minutes`, `fatal_error`, `had_errors`,
`components` (`None`), `num_cars`, `currency_symbols`, `arg_errors`.

Applying the superset to every module is purely additive: it closes the latent
`AttributeError` gaps without changing any behaviour that works today. `components = None`
keeps `ComponentBase.storage` resolving to `None`, so the disk cache stays skipped for
standalone runs — matching current behaviour.

Surplus `**kwargs` are stored into `self.args`, which absorbs `kraken`'s `user_id=` case
without a bespoke subclass. **Only kwargs whose value is not `None` are stored.** This
matters: `kraken` guards with `if user_id:` before setting `self.args["user_id"]`, and
`oauth_mixin.py:118` reads it back as `args.get("user_id", "")`. Storing a `None` would put
`None` where `""` is expected. Filtering `None` is exactly behaviour-preserving for the
`KrakenMockBase(user_id=None)` call site, while still allowing a legitimate `False` or `0`
argument to be stored.

### Methods

`get_state_wrapper`, `set_state_wrapper`, `log`, `dashboard_item`, `get_arg`, `set_arg`,
`call_notify`, `record_status`, `get_ha_config`, `get_history_wrapper`.

Each signature takes the **widest form observed**, so no existing caller breaks:

- `get_state_wrapper(entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False)`
  — including axle's `attribute=` lookup support.
- `set_state_wrapper(entity_id, state, attributes=None, app=None, required_unit=None)` —
  accepts **both** `app` and `required_unit`, because the modules disagree on which they
  declare and `ComponentBase.set_state_wrapper` passes `required_unit`.
- `get_arg(arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None)`
  — the full `ComponentBase` signature, superseding the narrow `(key, default)` forms in
  `fox` and `solax`.

### Behaviour unifications

Three deliberate changes, all confined to CLI-harness behaviour and output:

1. **`get_arg`/`set_arg` persistence.** Unify on the `solis`/`teslemetry` form: `set_arg`
   writes to `self.args`, `get_arg` reads it back. For the nine modules whose `get_arg`
   returned the bare default, `self.args` stayed empty in practice, so results are unchanged.

2. **`dashboard_item` attribute printing.** Unify on: elide `options` to `"..."` in a
   **copy**, and serialise with `json.dumps(..., default=str)`.

   The prevailing implementation does `attributes["options"] = "..."` — mutating the
   caller's dict, which is then passed to `set_state_wrapper` and **stored corrupted**. This
   is a genuine latent bug; `sigenergy` already avoids it with a display copy and the shared
   version adopts that approach. `default=str` (currently only in `axle`) prevents a
   `TypeError` when a component publishes a datetime.

   Cosmetic consequence: `axle` CLI output will now elide `options` as the other modules
   already do. `deye`'s old `dashboard_item` printed no attributes at all, so it gains
   attribute printing outright rather than merely eliding `options`.

3. **`set_arg` logging.** `axle` and `sigenergy` print a terser line; they adopt the common
   form that resolves the referenced entity's state. More informative, CLI-only.

4. **Timezone-aware `midnight_utc`.** `fox`, `kraken` and `solis` build `midnight_utc` from a
   naive `datetime.now()` while their `now_utc` is timezone-aware — so any
   `now_utc - midnight_utc` arithmetic raises `TypeError: can't subtract offset-naive and
   offset-aware datetimes`. The shared base derives `midnight_utc` from the aware `now_utc`,
   fixing this. `sigenergy` and `solax` set neither attribute today and gain both.

### Per-module changes

Each module keeps a module-level `MockBase` name so its call sites and the
`from teslemetry import MockBase` import in `tests/test_teslemetry.py:1458` keep working.

**Five** modules collapse to a plain re-export:

```python
from mock_base import MockBase   # deye, enphase, fox, solis, teslemetry
```

**Five** need a small subclass:

- `axle.py` — `config_root="./temp_axle"`
- `gecloud.py` — `config_root="./temp_gecloud"` **and** `ha_interface = MockHAInterface()`,
  which `GECloudDirect` dereferences at `gecloud.py:1037`. `MockHAInterface` is
  gecloud-specific and stays in `gecloud.py`.
- `octopus.py` — `config_root="./temp_octopus"`; its existing `QuietMockBase(MockBase)`
  subclass is unaffected and continues to work.
- `sigenergy.py` — keeps its `readonly=` parameter, which pre-seeds
  `switch.predbat_set_read_only` in `self.entities`.
- `solax.py` — `local_tz=timezone.utc`, preserving its deliberate use of UTC rather than the
  machine's local timezone.

**One** alias: `kraken.py` binds `KrakenMockBase = MockBase` so its `user_id=` call site is
untouched (the `**kwargs`-into-`args` behaviour covers it).

Total: 5 + 5 + 1 = 11 modules.

Expected net: 588 duplicated lines replaced by a ~120-line shared module plus ~40 lines of
subclasses.

## Testing

Per CLAUDE.md all new code needs unit tests. A new `tests/test_mock_base.py`, registered in
`TEST_REGISTRY` in `unit_test.py`, covers `MockBase` itself:

- the attribute superset is present after construction, and `config_root` is overridable
- `**kwargs` land in `self.args` (the `kraken` `user_id=` path)
- a `None`-valued kwarg is **not** stored (the `KrakenMockBase(user_id=None)` path)
- `get_arg`/`set_arg` round-trip; `get_arg` returns the supplied default for unset keys
- **`dashboard_item` does not mutate the caller's `attributes` dict** — the latent bug above;
  this is the test that earns its keep
- `dashboard_item` serialises a datetime attribute without raising (the `default=str` path)
- `get_state_wrapper` raw / `attribute=` / default-fallback paths
- `set_state_wrapper` accepts both `app=` and `required_unit=`
- `midnight_utc` is timezone-aware and `now_utc - midnight_utc` does not raise
- the five subclasses set their distinguishing state (`axle`/`gecloud`/`octopus`
  `config_root`, `gecloud` `ha_interface`, `sigenergy` `readonly=` seeding the read-only
  switch, `solax` `local_tz` being UTC)

**Not tested:** the CLI harness functions that construct a `MockBase`
(`test_fox_api`, `test_solis_api`, `test_axle_api`, etc.). Those are `# pragma: no cover`
test hooks that hit live vendor APIs; they are excluded from coverage by design and gain
nothing from unit tests.

The existing `test_teslemetry.py:1456` `get_arg` test must continue to pass unchanged — it
is the regression check that the persistence unification preserved `teslemetry`'s behaviour.

## Verification

- `./run_all` passes (output saved to a file, then grepped, per CLAUDE.md).
- `./run_pre_commit` passes. Audited: **`kraken.py` is the only module whose top-level
  `import json` exists solely for its mock** — every other `json` reference in that file is
  `response.json()` or a string literal, so the import must be deleted or Flake8 F401 fires.
  All other modules use `json` and `datetime` outside their mock and keep their imports.
  `sigenergy.py:2528` also has a redundant function-local `import json` that disappears with
  the class.
- Each refactored module still imports cleanly and its `if __name__ == "__main__"` path is
  intact.
- 100% docstring coverage (`interrogate`) on the new module and its subclasses.
