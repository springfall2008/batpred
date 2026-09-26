import type { ChartPoint } from './batteryChart'

/** Return the latest tariff value active at a given time. */
export function currentRate(points: ChartPoint[], at: number): number | null {
  return points.reduce<number | null>((latest, point) => point.x <= at ? point.y : latest, null)
}

/** Summarise future import and export opportunities in the selected chart range. */
export function upcomingRateExtremes(
  importPoints: ChartPoint[],
  exportPoints: ChartPoint[],
  start: number,
  end: number
) {
  const upcomingImports = importPoints.filter((point) => point.x >= start && point.x <= end)
  const upcomingExports = exportPoints.filter((point) => point.x >= start && point.x <= end)

  return {
    lowestImport: upcomingImports.length
      ? Math.min(...upcomingImports.map((point) => point.y))
      : null,
    highestExport: upcomingExports.length
      ? Math.max(...upcomingExports.map((point) => point.y))
      : null
  }
}
