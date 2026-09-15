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

    state: string
    state_target: string
    state_override: string

    reasons: PlanReason[]

    pv_forecast: number
    load_forecast: number

    car_charging_from_battery: boolean
    car_energy_reported_load: boolean
    car_charging?: number

    soc_percent: number
    soc_change: number

    cost_change: number
    total_cost: number

    description: string
}

export type Plan = {
    rows: PlanRow[]
    reason_templates: Record<string, string>

    soc: number
    soc_max: number
    mode: string
}

export type PlanData = {
    unchanged: boolean
    plan: Plan
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