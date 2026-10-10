export type PowerFlowData = {
  grid_power: number
  battery_power: number
  pv_power: number
  load_power: number
  house_power: number

  soc_percent: number

  grid_importing: boolean
  battery_charging: boolean
  battery_discharging: boolean
  pv_generating: boolean
  sun_state: string | null
  currency_symbols?: string | string[]
  pv_forecast_today: number | null

  totals: {
    load_today: number | null
    pv_today: number | null
    import_today: number | null
    export_today: number | null
    cost_today: number
  }

  weather: {
    state: string | null
    temperature: number | null
    temperature_unit: string
  } | null

  car: {
    configured: boolean
    power: number
    inside_clamp: boolean
    charging: boolean
    status: string | null
    soc: number | null
    energy_today: number | null
  }

  ashp: {
    power: number | null
    status: string | null
    energy_today: number | null
  } | null
}
