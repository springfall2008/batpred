import type { TimestampSeries } from '../types/charts'

export type PowerPoint = {
  x: number
  y: number
}

/** Convert cumulative kWh readings into the average kW used in each interval. */
export function cumulativeEnergyToPower(series: TimestampSeries): PowerPoint[] {
  const energyPoints = Object.entries(series)
    .map(([timestamp, value]) => ({ x: Date.parse(timestamp), y: Number(value) }))
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
    .sort((left, right) => left.x - right.x)

  return energyPoints.slice(1).map((point, index) => {
    const previous = energyPoints[index]
    const hours = (point.x - previous.x) / (60 * 60 * 1000)
    const energy = Math.max(0, point.y - previous.y)

    return {
      x: point.x,
      y: hours > 0 ? energy / hours : 0
    }
  })
}

/** Keep the Now marker just inside a forecast-only chart. */
export function getPowerChartWindow(generatedAt: string, forecastHours: number) {
  const now = Date.parse(generatedAt)
  return {
    start: now - 30 * 60 * 1000,
    end: now + forecastHours * 60 * 60 * 1000
  }
}

/** Summarise a normalised grid series: negative import and positive export. */
export function gridPowerPeaks(points: PowerPoint[]) {
  if (!points.length) {
    return { import: null, export: null }
  }

  return {
    import: Math.abs(Math.min(0, ...points.map((point) => point.y))),
    export: Math.max(0, ...points.map((point) => point.y))
  }
}
