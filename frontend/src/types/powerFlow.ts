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

  car: {
    configured: boolean
    power: number
    inside_clamp: boolean
    charging: boolean
  }
}
