import type { ChartPoint } from './batteryChart'

export type InDayWindow = {
  start: number
  end: number
}

/** Find the time span covered by today's cumulative load series. */
export function getInDayWindow(pointSets: ChartPoint[][], generatedAt: number): InDayWindow {
  const timestamps = pointSets.flatMap((points) => points.map((point) => point.x))
  if (!timestamps.length) {
    return { start: generatedAt - 12 * 60 * 60 * 1000, end: generatedAt + 12 * 60 * 60 * 1000 }
  }

  const start = Math.min(...timestamps)
  const end = Math.max(...timestamps)
  return end > start ? { start, end } : { start: start - 30 * 60 * 1000, end: end + 30 * 60 * 1000 }
}

/** Return the latest value at or before a timestamp. */
export function latestValueAt(points: ChartPoint[], at: number): number | null {
  return points.reduce<number | null>((latest, point) => point.x <= at ? point.y : latest, null)
}

/** Return the final cumulative value in a series. */
export function finalValue(points: ChartPoint[]): number | null {
  return points.length ? points[points.length - 1].y : null
}
