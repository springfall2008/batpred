import type { ChartPoint } from './batteryChart'

export type SolarChartWindow = {
  start: number
  end: number
}

export type SolarPowerWindow = SolarChartWindow & {
  forecastDays: number
}

/** Show today alone, or a calendar range that includes history, today and tomorrow. */
export function getSolarChartWindow(generatedAt: string, days: number): SolarChartWindow {
  const start = new Date(generatedAt)
  start.setHours(0, 0, 0, 0)
  start.setDate(start.getDate() - Math.max(0, days - 1))

  const end = new Date(generatedAt)
  end.setHours(0, 0, 0, 0)
  end.setDate(end.getDate() + 1)

  return { start: start.getTime(), end: end.getTime() }
}

/** Show available forecast days and fill the selected range with history. */
export function getSolarPowerWindow(generatedAt: string, days: number, forecastEnd: number): SolarPowerWindow {
  const now = new Date(generatedAt)
  const today = new Date(now)
  today.setHours(0, 0, 0, 0)
  const lastForecast = new Date(forecastEnd)
  const forecastDays = Number.isFinite(forecastEnd) && forecastEnd >= now.getTime()
    ? Math.min(days, Math.round(
      (Date.UTC(lastForecast.getFullYear(), lastForecast.getMonth(), lastForecast.getDate())
        - Date.UTC(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000
    ) + 1)
    : 0

  if (days === 1) {
    return { start: now.getTime(), end: now.getTime() + 24 * 60 * 60 * 1000, forecastDays }
  }

  const futureDays = Math.max(1, forecastDays)

  const start = new Date(today)
  start.setDate(start.getDate() - (days - futureDays))
  const end = new Date(today)
  end.setDate(end.getDate() + futureDays)

  return { start: start.getTime(), end: end.getTime(), forecastDays }
}

/** Join the forecast recorded in the past to the current forecast from now onwards. */
export function combineSolarForecast(history: ChartPoint[], current: ChartPoint[], now: number): ChartPoint[] {
  return [
    ...history.filter((point) => point.x <= now),
    ...current.filter((point) => point.x > now)
  ].sort((left, right) => left.x - right.x)
}

/** Return the highest power reading, or null when the selected range is empty. */
export function peakSolarPower(points: ChartPoint[]): number | null {
  return points.length ? Math.max(...points.map((point) => point.y)) : null
}

/** Sum the final cumulative energy reached on each local calendar day. */
export function sumDailyCumulative(points: ChartPoint[]): number | null {
  if (!points.length) return null

  const dailyTotals = new Map<string, number>()
  points.forEach((point) => {
    const date = new Date(point.x)
    const day = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
    dailyTotals.set(day, Math.max(dailyTotals.get(day) ?? 0, point.y))
  })

  return Array.from(dailyTotals.values()).reduce((total, value) => total + value, 0)
}

/** Compare cumulative generation with the forecast energy expected in the same period. */
export function solarAccuracy(actual: number | null, forecast: number | null) {
  if (actual === null || forecast === null) {
    return { difference: null, achieved: null }
  }

  return {
    difference: actual - forecast,
    achieved: forecast > 0 ? actual / forecast * 100 : null
  }
}
