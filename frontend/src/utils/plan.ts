import type { Plan, PlanRow } from '../types/plan'

export type PlanView = 'plan' | 'yesterday' | 'baseline'

export function selectPlanView(
  view: PlanView,
  plan: Plan,
  yesterday: Plan | null,
  baseline: Plan | null
) {
  return view === 'plan' ? plan : view === 'yesterday' ? yesterday : baseline
}

export function shouldShowPlanDebug(view: PlanView, plan: Plan, dashboardDebugEnabled: boolean) {
  return view === 'plan' && (dashboardDebugEnabled || plan.plan_debug === true)
}

export function isCarCharging(row: PlanRow) {
  return (row.car_charging ?? 0) > 0.001
}

/**
 * Predbat retains "Demand" as the raw state while a charging car protects the
 * battery from discharging. The structured reason is the authoritative
 * user-facing action for that row.
 */
export function getEffectivePlanState(row: PlanRow): string {
  return row.reasons?.some((reason) => reason.code === 'hold_for_car')
    ? 'Hold for car'
    : row.state
}

/**
 * A car-charging hold can occupy the first part of a slot that subsequently
 * exports. The raw state keeps only the final export action, so consumers
 * need the structured reason to preserve both visible actions.
 */
export function hasCarHoldBeforeExport(row: PlanRow) {
  return row.reasons?.some((reason) => reason.code === 'hold_for_car_before_export') ?? false
}

export function isCarBatteryProtected(row: PlanRow, plan: Plan) {
  return isCarCharging(row) && !plan.car_charging_from_battery && plan.car_energy_reported_load
}
