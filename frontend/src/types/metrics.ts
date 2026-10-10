export type MetricValues = Record<string, number>

export type ApiServiceMetrics = {
  requests: number
  failures: number
  last_success: number
}

export type ControlConflictEvent = {
  at: number
  control?: string
  entity_id?: string
  we_set?: unknown
  now_reads?: unknown
}

export type DashboardMetrics = {
  up: MetricValues
  errors_total: MetricValues
  last_update_timestamp: number
  config_valid: number
  config_warnings: number
  config_errors: Record<string, string>
  plan_valid: number
  plan_age_minutes: number
  battery_soc_percent: number
  battery_soc_kwh: number
  battery_max_kwh: number
  battery_power: number
  grid_power: number
  load_power: number
  pv_power: number
  load_today_kwh: number
  import_today_kwh: number
  export_today_kwh: number
  pv_today_kwh: number
  data_age_days: number
  data_age_required_days: number
  currency_symbol: string
  cost_today: number
  savings_today_pvbat: number
  savings_today_actual: number
  savings_today_predbat: number
  api_services: Record<string, ApiServiceMetrics>
  solcast_api_limit: number
  solcast_api_used: number
  solcast_api_remaining: number
  pv_scaling_worst: number
  pv_scaling_best: number
  pv_scaling_total: number
  control_conflicts_24h: number
  control_conflicts_sustained_total: number
  control_conflicts_events: ControlConflictEvent[]
  control_conflicts_sustained_controls: string[]
}
