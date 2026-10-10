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

export function isCarBatteryProtected(row: PlanRow, plan: Plan) {
  return isCarCharging(row) && !plan.car_charging_from_battery && plan.car_energy_reported_load
}
