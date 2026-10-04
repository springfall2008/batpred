import type { PlanRow } from '../types/plan'

export type OverviewAction = {
  label: string
  startsAt: string | null
  endsAt: string | null
  limit: number | null
}

type Point = {
  x: number
  y: number
}

type Rect = Point & {
  width: number
  height: number
}

const ACTION_LABELS: Record<string, string> = {
  Chrg: 'Charge',
  HoldChrg: 'Hold Charge',
  FrzChrg: 'Freeze Charge',
  Exp: 'Export',
  HoldExp: 'Hold Export',
  FrzExp: 'Freeze Export',
  'Hold for car': 'Hold for Car',
  'Demand, Hold for car': 'Hold for Car',
  'Hold for iBoost': 'Hold for iBoost',
  'Demand, Hold for iBoost': 'Hold for iBoost',
  'No Charge': 'No Charge',
  Demand: 'Demand'
}

/** Expand Predbat's compact plan states into dashboard labels. */
export function formatOverviewAction(state: string): string {
  return ACTION_LABELS[state] ?? state ?? 'Unknown'
}

/** Return the current plan action and the next distinct action. */
export function getOverviewActions(rows: PlanRow[], now = new Date()): { current: OverviewAction; next: OverviewAction } {
  if (rows.length === 0) {
    return {
      current: { label: 'No plan available', startsAt: null, endsAt: null, limit: null },
      next: { label: 'No scheduled action', startsAt: null, endsAt: null, limit: null }
    }
  }

  const currentIndex = rows.findLastIndex((row) => new Date(row.time) <= now)
  const resolvedIndex = currentIndex >= 0 ? currentIndex : 0
  const currentRow = rows[resolvedIndex]
  const currentLabel = formatOverviewAction(currentRow.state)
  const nextOffset = rows.slice(resolvedIndex + 1).findIndex((row) => formatOverviewAction(row.state) !== currentLabel)
  const nextIndex = nextOffset >= 0 ? resolvedIndex + 1 + nextOffset : -1
  const nextRow = nextIndex >= 0 ? rows[nextIndex] : undefined
  const currentActionRows = nextIndex >= 0 ? rows.slice(resolvedIndex, nextIndex) : rows.slice(resolvedIndex)
  const currentTarget = currentActionRows[currentActionRows.length - 1]?.state_target?.trim()
  const parsedCurrentLimit = currentTarget ? Number(currentTarget) : Number.NaN
  const current = {
    label: currentLabel,
    startsAt: currentRow.time,
    endsAt: nextRow?.time ?? null,
    limit: Number.isNaN(parsedCurrentLimit) ? null : parsedCurrentLimit
  }

  if (!nextRow) {
    return {
      current,
      next: { label: 'No scheduled action', startsAt: null, endsAt: null, limit: null }
    }
  }

  const nextLabel = formatOverviewAction(nextRow.state)
  const followingRows = rows.slice(nextIndex)
  const endOffset = followingRows.findIndex((row) => formatOverviewAction(row.state) !== nextLabel)
  const actionRows = endOffset >= 0 ? followingRows.slice(0, endOffset) : followingRows
  const finalTarget = actionRows[actionRows.length - 1]?.state_target?.trim()
  const parsedLimit = finalTarget ? Number(finalTarget) : Number.NaN

  return {
    current,
    next: {
      label: nextLabel,
      startsAt: nextRow.time,
      endsAt: endOffset >= 0 ? followingRows[endOffset].time : null,
      limit: Number.isNaN(parsedLimit) ? null : parsedLimit
    }
  }
}

/** Select the matching transparent house render without changing its dimensions. */
export function getOverviewSceneKey(sunState: string | null, hasCar: boolean, hasHeatPump: boolean, hasSnow = false): string {
  const time = sunState === 'below_horizon' ? 'night' : 'day'
  const features = [hasCar ? 'car' : '', hasHeatPump ? 'heat-pump' : ''].filter(Boolean)
  const scene = features.length > 0 ? [time, ...features] : [time, 'house']
  if (hasSnow) scene.push('snow')
  return scene.join('-')
}

/** Map a point on the 1500 x 1200 house artwork into the stage connector SVG. */
export function mapOverviewImagePoint(point: Point, imageSize: Point, imageRect: Rect, stageRect: Rect, stageViewBox: Point): Point {
  const renderedX = imageRect.x - stageRect.x + point.x / imageSize.x * imageRect.width
  const renderedY = imageRect.y - stageRect.y + point.y / imageSize.y * imageRect.height

  return {
    x: renderedX * stageViewBox.x / stageRect.width,
    y: renderedY * stageViewBox.y / stageRect.height
  }
}

/** Use a muted headline whenever the displayed power rounds to zero watts. */
export function getOverviewPowerTone(watts: number, activeTone = ''): string {
  return Math.round(Math.abs(watts)) === 0 ? 'is-muted' : activeTone
}

/** Format the optional carbon figures shown beneath the Grid card values. */
export function getOverviewCarbonValues(
  enabled: boolean,
  row: Pick<PlanRow, 'carbon_intensity' | 'total_carbon'> | undefined
): { intensity: string | null; total: string | null } {
  if (!enabled || !row) return { intensity: null, total: null }

  return {
    intensity: typeof row.carbon_intensity === 'number' && Number.isFinite(row.carbon_intensity)
      ? `${row.carbon_intensity.toFixed(0)} gCO₂/kWh`
      : null,
    total: typeof row.total_carbon === 'number' && Number.isFinite(row.total_carbon)
      ? `${row.total_carbon.toFixed(2)} kg CO₂`
      : null
  }
}

export type OverviewWeatherEffect = 'clouds' | 'fog' | 'rain' | 'snow' | 'storm'

/** Map Home Assistant weather states to the lightweight scene effect. */
export function getOverviewWeatherEffect(state: string | null): OverviewWeatherEffect | null {
  const value = state?.trim().toLowerCase() ?? ''
  if (value.includes('lightning') || value.includes('thunder')) return 'storm'
  if (value.includes('snow') || value.includes('hail')) return 'snow'
  if (value.includes('rain') || value.includes('pour')) return 'rain'
  if (value.includes('fog')) return 'fog'
  if (value.includes('cloud')) return 'clouds'
  return null
}

/** Convert common charger states into the three labels used by the Overview card. */
export function formatCarStatus(status: string | null, charging: boolean): string {
  if (charging) return 'Charging'
  if (!status || ['unknown', 'unavailable', 'none'].includes(status.trim().toLowerCase())) return 'Unavailable'

  const normalised = status.trim().toLowerCase()
  if (normalised.includes('unplug') || normalised.includes('disconnect')) return 'Unplugged'
  if (normalised.includes('charg')) return 'Charging'
  if (normalised.includes('connect') || normalised.includes('plug') || normalised.includes('waiting') || normalised.includes('paused')) return 'Connected'

  return status.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

const WEATHER_LABELS: Record<string, string> = {
  'clear-night': 'Clear night',
  cloudy: 'Cloudy',
  exceptional: 'Exceptional weather',
  fog: 'Fog',
  hail: 'Hail',
  lightning: 'Lightning',
  'lightning-rainy': 'Lightning and rain',
  partlycloudy: 'Partly cloudy',
  pouring: 'Pouring rain',
  rainy: 'Rainy',
  snowy: 'Snowy',
  'snowy-rainy': 'Snow and rain',
  sunny: 'Sunny',
  windy: 'Windy',
  'windy-variant': 'Windy and cloudy'
}

/** Expand Home Assistant's weather condition codes into readable labels. */
export function formatWeatherStatus(status: string | null): string {
  const normalised = status?.trim().toLowerCase() ?? ''
  if (!normalised || ['unknown', 'unavailable', 'none'].includes(normalised)) return 'Unavailable'
  return WEATHER_LABELS[normalised] ?? formatOverviewStatus(status)
}

/** Make a Home Assistant state readable while preserving device-specific wording. */
export function formatOverviewStatus(status: string | null): string {
  if (!status || ['unknown', 'unavailable', 'none'].includes(status.trim().toLowerCase())) return 'Unavailable'
  return status.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}
