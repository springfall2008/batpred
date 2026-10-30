import type { PowerFlowData } from '../types/powerFlow'

/** Derive display totals and estimated routes from measured node totals. */
export function resolvePowerFlow(data: PowerFlowData, subtractDevices: boolean) {
  const car = data.car.configured ? Math.max(0, data.car.power) : 0
  const heatPump = Math.max(0, data.ashp?.power ?? 0)
  const totalHome = Math.max(0, data.load_power) + (data.car.inside_clamp ? 0 : car)
  const home = subtractDevices ? Math.max(0, totalHome - car - heatPump) : totalHome
  const solar = Math.max(0, data.pv_power)
  const charging = data.battery_charging ? Math.abs(data.battery_power) : 0
  const discharging = data.battery_discharging ? Math.abs(data.battery_power) : 0
  const exporting = data.grid_importing ? 0 : Math.abs(data.grid_power)
  // shortcut: route splits are inferred with household demand served first; use measured routes if the API gains them.
  const solarHome = Math.min(solar, totalHome)
  const solarBattery = Math.min(Math.max(0, solar - solarHome), charging)
  const solarGrid = Math.min(Math.max(0, solar - solarHome - solarBattery), exporting)
  const batteryHome = Math.min(discharging, Math.max(0, totalHome - solarHome))
  const batteryGrid = Math.min(Math.max(0, discharging - batteryHome), Math.max(0, exporting - solarGrid))
  const gridBattery = data.grid_importing ? Math.min(Math.abs(data.grid_power), Math.max(0, charging - solarBattery)) : 0
  const gridHome = data.grid_importing ? Math.max(0, Math.abs(data.grid_power) - gridBattery) : 0
  return { home, car, heatPump, solarHome, solarBattery, solarGrid, batteryHome, batteryGrid, gridBattery, gridHome }
}

/** Cap particle speed while making higher power visibly faster. */
export function getFlowDuration(power: number) {
  return `${Math.max(0.8, 2.8 - Math.min(Math.abs(power), 3000) / 1500).toFixed(2)}s`
}
