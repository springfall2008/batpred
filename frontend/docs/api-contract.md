# Modern UI API contract

The frontend calls Predbat using relative URLs. Do not turn these into root
URLs: Home Assistant ingress mounts Predbat below a changing path.

This document records the fields whose meaning is easy to misread. It is not a
replacement for the backend implementation; update it with a fixture and a
test when a new field affects the UI.

## `GET ./api/plan_data`

Returns the active plan, previous-day plan, baseline plan, and manual
overrides. The current plan is in `plan.rows`.

### Important row fields

| Field | Meaning |
| --- | --- |
| `time` | ISO timestamp for the start of the half-hour slot. |
| `slot_minute` | Minute index used by manual overrides. |
| `state` | The final action for the slot. This alone is not sufficient for split actions. |
| `state_text` | First legacy display action, supplied for a split charge/export slot. |
| `state2_text` | Second legacy display action. When both display fields describe actions, render both. |
| `state_target` | Planned battery target for the action. |
| `reasons` | Structured explanation data rendered in action tooltips. |
| `import_rate`, `export_rate` | Base tariff rates in the configured minor currency unit. |
| `*_rate_adjusted` | Rate after Predbat losses or an event adjustment. |
| `*_rate_adjust_type` | Includes `future` for a predicted rate and `saving` for an energy event. |
| `rate_event_type` | Event type used for the plan badge, such as `octopus_power_down` or `octopus_happy_hour`. |

### Split charge and export actions

Predbat can charge and export in one half-hour slot. In that case `state` is
normally the final export state, while `state_text` and `state2_text` preserve
both display actions. The Modern Plan table must show both actions rather than
using `state` alone.

Use [the split-action fixture](../tests/fixtures/plan-data-split-actions.json)
when changing this behaviour.

## Other frequently used endpoints

| Endpoint | Used for |
| --- | --- |
| `GET ./api/status` | Predbat status, calculation state and update state. |
| `GET ./api/power_flow` | Live power-flow and overview data. |
| `GET ./api/chart_data?chart=<name>` | Chart series. |
| `GET/POST ./api/config` | User-facing configuration page. |
| `GET/POST ./api/apps_yaml` | Raw `apps.yaml` editor. |
| `GET ./api/apps_schema` | Editor completion, validation and hover descriptions. |
| `GET ./api/entities?all=1` | Home Assistant entity completion. |

The power-flow response includes `updated_at`, a Unix timestamp in seconds marking the last completed
inverter-data refresh. It stays unchanged between refreshes and is `null` before the first one.
Overview uses it for the age of the power snapshot; it does not represent individual sensor timestamps
or the freshness of independently updated weather and daily totals.
