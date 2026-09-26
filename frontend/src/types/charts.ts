export type TimestampSeries = Record<string, number>

export type BatteryChartSeries = {
  actual: TimestampSeries
  optimized: TimestampSeries
  base: TimestampSeries
  base10: TimestampSeries
  optimized10: TimestampSeries
  predicted_h1: TimestampSeries
  predicted_h8: TimestampSeries
  charge_limit_base: TimestampSeries
  charge_limit_optimized: TimestampSeries
  export_limit_optimized: TimestampSeries
  record: TimestampSeries
}

export type BatteryChartData = {
  chart: 'battery'
  ready: boolean
  generated_at: string
  soc_max: number
  series: BatteryChartSeries
}

export type PowerChartSeries = {
  battery: TimestampSeries
  solar: TimestampSeries
  grid: TimestampSeries
  load: TimestampSeries
  iboost_energy: TimestampSeries
}

export type PowerChartData = {
  chart: 'power'
  ready: boolean
  generated_at: string
  series: PowerChartSeries
}

export type CostChartSeries = {
  actual: TimestampSeries
  actual_import: TimestampSeries
  actual_export: TimestampSeries
  base: TimestampSeries
  optimized: TimestampSeries
  base10: TimestampSeries
  optimized10: TimestampSeries
}

export type CostChartData = {
  chart: 'cost'
  ready: boolean
  generated_at: string
  currency_symbol: string
  currency_unit: string
  series: CostChartSeries
}

export type RatesChartSeries = {
  import: TimestampSeries
  export: TimestampSeries
  gas: TimestampSeries
  actual_hourly: TimestampSeries
  actual_today: TimestampSeries
}

export type RatesChartData = {
  chart: 'rates'
  ready: boolean
  generated_at: string
  currency_symbol: string
  currency_unit: string
  series: RatesChartSeries
}

export type InDayChartSeries = {
  actual: TimestampSeries
  predicted: TimestampSeries
  adjusted: TimestampSeries
  adjustment_factor: TimestampSeries
}

export type InDayChartData = {
  chart: 'inday'
  ready: boolean
  generated_at: string
  series: InDayChartSeries
}

export type SolarChartSeries = {
  actual: TimestampSeries
  forecast_history: TimestampSeries
  forecast_history_calibrated: TimestampSeries
  forecast: TimestampSeries
  forecast_low: TimestampSeries
  forecast_high: TimestampSeries
  forecast_calibrated: TimestampSeries
  energy_actual: TimestampSeries
  energy_forecast: TimestampSeries
}

export type SolarChartData = {
  chart: 'solar'
  ready: boolean
  generated_at: string
  series: SolarChartSeries
}

export type SavingsChartSeries = {
  daily_predbat: TimestampSeries
  daily_pv_battery: TimestampSeries
  daily_cost: TimestampSeries
  total_predbat: TimestampSeries
  total_pv_battery: TimestampSeries
}

export type SavingsChartData = {
  chart: 'savings'
  ready: boolean
  generated_at: string
  currency_symbol: string
  series: SavingsChartSeries
}

export type BatteryDegradationInverter = {
  id: number
  nominal: TimestampSeries
  calculated: TimestampSeries
  degradation: TimestampSeries
}

export type BatteryDegradationChartData = {
  chart: 'degradation'
  ready: boolean
  generated_at: string
  automatic_scaling: boolean
  inverters: BatteryDegradationInverter[]
}

export type MarginalCostLevel = {
  id: string
  label: string
  kwh: number
  current_cost: number | null
  cheap: boolean
  moderate: boolean
  series: TimestampSeries
}

export type MarginalCostsChartData = {
  chart: 'marginal'
  ready: boolean
  generated_at: string
  currency_unit: string
  levels: MarginalCostLevel[]
  grid_import: TimestampSeries
  grid_export: TimestampSeries
}

export type CarbonChartData = {
  chart: 'carbon'
  ready: boolean
  generated_at: string
  series: {
    actual: TimestampSeries
    base: TimestampSeries
    optimized: TimestampSeries
    intensity: TimestampSeries
  }
}

export type LoadMlChartData = {
  chart: 'loadml'
  ready: boolean
  generated_at: string
  car_configured: boolean
  series: {
    energy_actual: TimestampSeries
    energy_predicted_h1: TimestampSeries
    energy_predicted_h8: TimestampSeries
    energy_forecast: TimestampSeries
    power_actual: TimestampSeries
    power_actual_less_car: TimestampSeries
    car_power: TimestampSeries
    power_forecast: TimestampSeries
    power_history: TimestampSeries
    power_history_h1: TimestampSeries
    power_history_h8: TimestampSeries
    pv_actual: TimestampSeries
    pv_forecast: TimestampSeries
    temperature: TimestampSeries
  }
}

export type ChartData = BatteryChartData | PowerChartData | CostChartData | RatesChartData | InDayChartData | SolarChartData | SavingsChartData | BatteryDegradationChartData | MarginalCostsChartData | CarbonChartData | LoadMlChartData
