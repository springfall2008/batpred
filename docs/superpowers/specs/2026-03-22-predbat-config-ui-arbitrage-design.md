# Predbat: Config UI & Arbitrage Engine — Design Spec

**Date:** 2026-03-22
**Status:** Approved
**Author:** Ian (via Claude Code brainstorming session)

---

## Overview

Two independently deliverable enhancements to Predbat:

1. **`custom_components/predbat_config`** — A companion Home Assistant custom integration providing a native Config Flow (setup wizard) and Options Flow (ongoing settings UI), eliminating the need for new users to edit `apps.yaml` directly.
2. **`apps/predbat/arbitrage.py`** — A new arbitrage engine module integrated into Predbat's existing prediction loop, enabling target-driven grid import/export optimisation on dynamic tariffs (Octopus Agile).

---

## Goals

- Improve setup experience for **new users**: guided wizard, no YAML editing required
- Improve configuration experience for **existing users**: organised, HA-native settings UI
- Add **genuine arbitrage capability**: deliberate grid import-for-profit, not just self-consumption optimisation
- Keep the architecture **low risk**: companion integration touches no Predbat core logic; arbitrage engine integrates cleanly into the existing planning loop

---

## Architecture

### Two Deliverables, Zero Coupling

```
custom_components/predbat_config     apps/predbat/arbitrage.py
─────────────────────────────────    ──────────────────────────
Thin HA integration                  New Predbat module
Config Flow + Options Flow UI        Arbitrage scheduling engine
Reads/writes apps.yaml               Called from plan.py loop
No prediction logic                  No HA UI code
```

The companion integration communicates with Predbat solely via the filesystem (`apps.yaml`) and HA entity states. Predbat does not depend on the companion integration to function. Both are independently installable via HACS.

---

## Deliverable 1: `custom_components/predbat_config`

### Config Flow (New User Setup Wizard)

Triggered when the user adds the integration via Settings → Integrations → Add Integration → Predbat Config.

| Step | Content |
|------|---------|
| 1 — Inverter | Dropdown of 28 supported inverter brands/types. Selecting one copies the matching template as the base `apps.yaml`. |
| 2 — Battery | Capacity (kWh), max charge rate (kW), max discharge rate (kW), minimum SoC reserve (%). |
| 3 — Energy Tariff | Tariff type selector. For Agile: auto-detect Octopus Energy HA integration or prompt for API key. For other types: configure import/export rate windows. |
| 4 — Solar Forecast | Optional. Auto-detect Solcast HA integration if present; otherwise prompt for API key or allow skip. |
| 5 — Arbitrage | Optional. "Optimise for arbitrage profit?" If yes, set daily profit target (£/day). Can be skipped and configured later. |
| 6 — Review & Save | Summary of all choices. On confirm: write completed `apps.yaml` to Predbat addon config directory and trigger reload. |

On save failure (e.g. config directory not found), the flow surfaces a clear error message pointing the user to the Predbat addon config path. No partial writes occur.

### Options Flow (Ongoing Configuration)

Accessible via Settings → Integrations → Predbat Config → Configure.

Settings are organised into sections, shown progressively based on what is enabled:

**Section 1 — Battery**
Capacity, charge/discharge limits, reserve SoC, battery loss %, degradation parameters. Advanced items (e.g. charge curve compensation) hidden behind "Show expert settings".

**Section 2 — Solar**
Solcast API key, site details, forecast scaling factor, panel orientation overrides.

**Section 3 — Tariffs**
Import/export rate source (Octopus integration, direct API, or manual). Rate override windows. Export cap settings.

**Section 4 — Arbitrage** *(shown only if Agile tariff detected)*
- Daily profit target (£) — primary control
- Weekly profit target (£) — alternative framing
- Battery arbitrage reserve (% of battery capacity available for arbitrage vs self-consumption)
- Read-only display: today's projected arbitrage gain, current opportunity score

**Section 5 — EV Charging**
Car charging slots, smart charging toggle, charger type.

**Section 6 — Heat Pump** *(shown only if predheat enabled)*
Heat pump entity links, temperature targets, heating forecast.

**Section 7 — Advanced / Expert**
Remaining `apps.yaml` items not covered above, organised by category. Hidden unless expert mode is enabled.

### Implementation Notes

- Uses `ruamel.yaml` (already a Predbat dependency) to make surgical edits to `apps.yaml`, preserving existing comments and structure.
- All saves immediately rewrite affected keys in `apps.yaml`. Predbat detects the file change via its existing hot-reload mechanism — no restart required.
- Tested with `pytest-homeassistant-custom-component`.

---

## Deliverable 2: `apps/predbat/arbitrage.py`

### Purpose

Enable deliberate grid import-for-export-profit scheduling on dynamic (Agile) tariffs, guided by a user-defined daily or weekly profit target.

### Inputs (all already available in Predbat)

| Input | Source |
|-------|--------|
| Agile import rates (30-min, 24–48hr horizon) | `fetch.py` → Octopus API |
| Agile export rates (30-min, 24–48hr horizon) | `fetch.py` → Octopus API |
| Solar forecast (30-min slots) | `solcast.py` |
| Predicted load (30-min slots) | `load_predictor.py` |
| Current battery SoC and capacity | `inverter.py` |
| Daily/weekly profit target (£) | `apps.yaml` / HA entity |

### What the Engine Computes

**1. Opportunity Scoring**
For each future 30-min import/export slot pair, calculate the net spread after round-trip battery efficiency losses. Rank pairs by profitability.

**2. Target-Driven Scheduling**
Working forward from now, select the minimum set of charge/export slot pairs needed to hit the profit target. Constraints:
- Battery capacity limits
- Solar headroom (don't charge when solar will fill the battery anyway)
- Arbitrage reserve % (portion of battery capacity ring-fenced for arbitrage vs self-consumption)

**3. Confidence Weighting**
Slot pairs further in the forecast horizon are discounted by:
- Solcast confidence intervals (wider = lower weight)
- Agile rate uncertainty (rates beyond 23:00 are estimated, not confirmed)

**4. Plan Integration**
Outputs a set of charge/discharge slot overrides fed into the existing `plan.py` scheduling engine. Arbitrage decisions appear in the normal Predbat plan view alongside self-consumption decisions. The engine does not replace the optimiser — it provides constraints and hints to it.

### New HA Entities

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.predbat_arbitrage_projected_gain` | Sensor (£) | Today's projected arbitrage profit |
| `sensor.predbat_arbitrage_opportunity_score` | Sensor (0–100) | Quality of current arbitrage conditions |
| `sensor.predbat_arbitrage_weekly_gain` | Sensor (£) | Rolling 7-day actual arbitrage gain |
| `binary_sensor.predbat_arbitrage_active` | Binary Sensor | True when executing an arbitrage charge or discharge |

### Error Handling

| Condition | Behaviour |
|-----------|-----------|
| Rates unavailable for some slots | Skip affected slots, log warning; self-consumption optimisation continues normally |
| Solcast unavailable | Confidence weighting defaults to 50%; arbitrage proceeds conservatively |
| Profit target unachievable | Engine does its best; `predbat_arbitrage_projected_gain` reflects maximum achievable; no error raised |

### Integration Point

`arbitrage.py` is called from within the existing `plan.py` prediction loop. It is a standalone module with no circular imports. Unit tested against synthetic rate/forecast fixtures in the existing `tests/` framework.

---

## Data Flow

### Configuration

```
User (Options Flow UI)
    → custom_components/predbat_config
    → surgical edit of apps.yaml (ruamel.yaml, preserving comments)
    → Predbat hot-reload detects file change
    → Predbat reloads config, applies changes
```

### Arbitrage

```
Octopus rate API  → fetch.py    ─┐
Solcast API       → solcast.py  ─┤→ arbitrage.py → plan.py → execute.py → inverter
load_predictor.py              ─┘         │
                                          └→ output.py → new HA sensor entities
```

---

## Out of Scope (Phase 2)

- **Backtesting dashboard** — historical arbitrage performance view in the web UI
- **Full `custom_components` migration** — restructuring Predbat core as a native HA integration
- **Non-Agile arbitrage** — time-of-use and flat tariff arbitrage optimisation
- **Multi-property / VPP** — coordinated arbitrage across multiple installations

---

## Testing Strategy

| Component | Approach |
|-----------|---------|
| `arbitrage.py` | Unit tests with synthetic rate/forecast fixtures in `tests/` |
| `custom_components/predbat_config` | `pytest-homeassistant-custom-component` test harness |
| Existing Predbat tests | No changes required |

---

## Open Questions

None — all design decisions resolved in brainstorming session.
