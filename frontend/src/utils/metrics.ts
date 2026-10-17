import type { DashboardMetrics, MetricValues } from '../types/metrics'

export function sumMetricValues(values: MetricValues): number {
  return Object.values(values).reduce((total, value) => total + value, 0)
}

export function metricVersion(values: MetricValues): string {
  const key = Object.keys(values)[0] ?? ''
  return key.match(/["']version["']\s*:\s*["']([^"']+)/)?.[1] ?? 'Unknown'
}

export function formatMetricAge(timestamp: number, now = Date.now() / 1000): string {
  if (!timestamp) return 'Never'

  const seconds = Math.max(0, Math.floor(now - timestamp))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m ago`
}

export function metricPowerValues(metrics: Pick<DashboardMetrics, 'battery_power' | 'grid_power' | 'load_power' | 'pv_power'>): number[] {
  return [
    Math.max(0, -metrics.battery_power),
    Math.max(0, metrics.battery_power),
    Math.max(0, metrics.load_power),
    Math.max(0, metrics.pv_power),
    Math.max(0, -metrics.grid_power),
    Math.max(0, metrics.grid_power)
  ]
}

export function metricEnergyValues(metrics: Pick<DashboardMetrics, 'load_today_kwh' | 'pv_today_kwh' | 'import_today_kwh' | 'export_today_kwh'>): number[] {
  return [metrics.load_today_kwh, metrics.pv_today_kwh, metrics.import_today_kwh, metrics.export_today_kwh].map((value) => Math.max(0, value))
}
