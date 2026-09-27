import type { RatesChartData } from '../types/charts'

type ConfigValue = string | number | boolean

export type TariffSuggestion = {
  name: string
  value: ConfigValue
  current: ConfigValue
  reason: string
}

export type TariffAnalysis = {
  profile: 'Dynamic' | 'Time of use' | 'Flat'
  importMin: number
  importMax: number
  exportMin: number | null
  exportMax: number | null
  unit: string
  suggestions: TariffSuggestion[]
}

type Setting = { name: string, value: ConfigValue }

function futureValues(series: Record<string, number>, generatedAt: string): number[] {
  const start = Date.parse(generatedAt) - 30 * 60 * 1000
  const end = start + 48 * 60 * 60 * 1000
  const entries = Object.entries(series)
    .map(([timestamp, value]) => [Date.parse(timestamp), Number(value)] as const)
    .filter(([timestamp, value]) => Number.isFinite(timestamp) && Number.isFinite(value))
    .sort(([left], [right]) => left - right)
  const previous = entries.filter(([timestamp]) => timestamp < start).at(-1)
  return [...(previous ? [previous[1]] : []), ...entries.filter(([timestamp]) => timestamp >= start && timestamp <= end).map(([, value]) => value)]
}

/** Classify the active tariff and return reviewable changes to existing controls. */
export function analyseTariff(settings: Setting[], rates: RatesChartData): TariffAnalysis | null {
  const imports = futureValues(rates.series.import, rates.generated_at)
  if (!imports.length) return null
  const exports = futureValues(rates.series.export, rates.generated_at)
  const importMin = Math.min(...imports)
  const importMax = Math.max(...imports)
  const importDistinct = new Set(imports.map((value) => value.toFixed(1))).size
  const profile = importMin < 0 || importDistinct >= 8
    ? 'Dynamic'
    : importMax - importMin >= 4 || importDistinct > 1 ? 'Time of use' : 'Flat'
  const exportMin = exports.length ? Math.min(...exports) : null
  const exportMax = exports.length ? Math.max(...exports) : null
  const exportVariable = exportMin !== null && exportMax !== null && exportMax - exportMin >= 3
  const targets: Array<[string, boolean, string]> = profile === 'Dynamic'
    ? [
        ['combine_charge_slots', false, 'Keep individual price slots available on a frequently changing import tariff.'],
        ['set_charge_low_power', false, 'Concentrate charging into the cheapest dynamic-price periods.']
      ]
    : [
        ['combine_charge_slots', true, 'Treat adjacent low-rate periods as one practical charging window.'],
        ['set_charge_low_power', true, 'Spread charging smoothly across the available low-rate window.']
      ]

  if (exports.length) {
    targets.push(
      ['combine_export_slots', !exportVariable, exportVariable ? 'Keep individual export peaks available to the optimiser.' : 'Combine adjacent periods because export pricing is broadly flat.'],
      ['set_export_low_power', !exportVariable, exportVariable ? 'Concentrate export into the highest-value periods.' : 'Use gentler export power when there is little price advantage to exporting faster.']
    )
  }

  const byName = new Map(settings.map((setting) => [setting.name, setting]))
  const suggestions = targets.flatMap(([name, value, reason]) => {
    const setting = byName.get(name)
    return setting && Boolean(setting.value) !== value
      ? [{ name, current: setting.value, value, reason }]
      : []
  })

  return { profile, importMin, importMax, exportMin, exportMax, unit: `${rates.currency_unit}/kWh`, suggestions }
}
