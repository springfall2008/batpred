import type { ChartPoint, ChartWindow } from './batteryChart'

/** Show costs from the first actual reading through the selected forecast horizon. */
export function getCostChartWindow(
  actualPoints: ChartPoint[],
  generatedAt: string,
  forecastHours: number
): ChartWindow {
  const now = Date.parse(generatedAt)
  const firstActual = actualPoints[0]?.x

  return {
    start: Number.isFinite(firstActual) ? firstActual : now,
    end: now + forecastHours * 60 * 60 * 1000
  }
}

/** Return the latest value at or before a timestamp. */
export function latestCostValue(points: ChartPoint[], at: number): number | null {
  return points.reduce<number | null>((latest, point) => point.x <= at ? point.y : latest, null)
}

/** Format Predbat's minor currency units as a normal monetary value. */
export function formatMinorCurrency(value: number | null, symbol: string): string {
  if (value === null || !Number.isFinite(value)) {
    return '—'
  }

  const sign = value < 0 ? '-' : ''
  return `${sign}${symbol}${(Math.abs(value) / 100).toFixed(2)}`
}

/** Compare the selected base and optimised forecast totals. */
export function plannedSaving(base: number | null, optimized: number | null): number | null {
  return base === null || optimized === null ? null : base - optimized
}
