import type { PlanRow } from '../types/plan'
import type { TimestampSeries } from '../types/charts'

export type ChartPoint = {
  x: number
  y: number
}

export type ChartWindow = {
  start: number
  end: number
}

/** Convert Predbat's timestamp-keyed data to sorted Chart.js points. */
export function toChartPoints(series: TimestampSeries): ChartPoint[] {
  return Object.entries(series)
    .map(([timestamp, value]) => ({ x: Date.parse(timestamp), y: Number(value) }))
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
    .sort((left, right) => left.x - right.x)
}

/** Show recent actual history alongside the selected forecast horizon. */
export function getChartWindow(generatedAt: string, forecastHours: number): ChartWindow {
  const now = Date.parse(generatedAt)
  const hour = 60 * 60 * 1000

  return {
    start: now - 12 * hour,
    end: now + forecastHours * hour
  }
}

export function pointsInWindow(points: ChartPoint[], window: ChartWindow): ChartPoint[] {
  return points.filter((point) => point.x >= window.start && point.x <= window.end)
}

/** Return the first timestamp outside Predbat's recorded prediction window. */
export function predictionEndTime(series: TimestampSeries): number | null {
  return toChartPoints(series).find((point) => point.y > 0)?.x ?? null
}

/** Check whether a series contains a meaningful point in the selected range. */
export function hasSeriesData(
  series: TimestampSeries,
  window: ChartWindow,
  requireNonZero = false
): boolean {
  return pointsInWindow(toChartPoints(series), window).some(
    (point) => !requireNonZero || point.y > 0
  )
}

/** Resolve the plan action in force at a chart timestamp. */
export function actionAtTime(rows: PlanRow[], timestamp: number): string | null {
  let action: string | null = null

  for (const row of rows) {
    if (Date.parse(row.time) > timestamp) {
      break
    }

    action = row.state
  }

  return action
}

/** Expand Predbat's compact internal plan states for display. */
export function formatPlanAction(state: string): string {
  const labels: Record<string, string> = {
    Chrg: 'Charge',
    HoldChrg: 'Hold Charge',
    FrzChrg: 'Freeze Charge',
    Exp: 'Export',
    HoldExp: 'Hold Export',
    FrzExp: 'Freeze Export',
    'Chrg/Exp': 'Charge / Export'
  }

  return labels[state] ?? state
}
