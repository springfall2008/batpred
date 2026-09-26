import type { MetricValues } from '../types/metrics'

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
