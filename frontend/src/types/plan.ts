export type PlanReason = {
  code: string
  params: Record<string, unknown>
}

export type PlanRow = {
  time: string
  slot_minute: number

  import_rate: number
  export_rate: number

  import_rate_adjusted: number
  export_rate_adjusted: number
  import_rate_adjust_type?: string
  export_rate_adjust_type?: string
  rate_event_type?: 'octopus_power_down' | 'octopus_power_up' | 'octopus_happy_hour' | 'octopus_free_electricity' | 'axle_import' | 'axle_export' | 'axle_event' | 'energy_event'
  rate_color_import?: string

  state: string
  state_target: string
  state_override: string

  reasons: PlanReason[]

  pv_forecast: number
  pv_forecast10?: number
  load_forecast: number
  load_forecast10?: number
  load_color?: string
  clipped?: number
  extra_load?: string
  show_limit?: string

  car_charging?: number

  iboost?: number
  iboost_change?: number
  iboost_color?: string

  soc_percent: number
  soc_change: number

  cost_change: number
  total_cost: number
  cost_color?: string

  carbon_intensity?: number
  carbon_change?: number
  total_carbon?: number
  carbon_intensity_color?: string
  carbon_color?: string

  description: string
}

export type Plan = {
  rows: PlanRow[]
  reason_templates: Record<string, string>
  currency_symbols?: string | string[]

  soc: number
  soc_max: number
  mode: string
  plan_debug?: boolean
  num_cars: number
  iboost_enable?: boolean
  carbon_enable?: boolean
  car_charging_from_battery: boolean
  car_energy_reported_load: boolean
  description?: string[]
  totals?: {
    total_cost: number
    pv_forecast: number
    load_forecast: number
    soc_percent: number
    clipped?: number
    extra_load?: number
    car_charging?: number
    iboost?: number
    carbon_intensity?: number
    total_carbon?: number
  }
}

export type PlanData = {
  unchanged: boolean
  plan: Plan
  yesterday: Plan | null
  baseline: Plan | null
  overrides: PlanOverrides
  overrides_hash: string
}

export type ManualRateOverride = {
  minutes: number
  rate: number
}

export type ManualSocOverride = {
  minutes: number
  target: number
}

export type PlanOverrides = {
  manual_charge_times: number[]
  manual_export_times: number[]
  manual_freeze_charge_times: number[]
  manual_freeze_export_times: number[]
  manual_demand_times: number[]

  manual_import_rates: ManualRateOverride[]
  manual_export_rates: ManualRateOverride[]

  manual_soc: ManualSocOverride[]
}
