import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Chart,
  Filler,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
  type ChartConfiguration,
  type ChartDataset,
  type Plugin
} from 'chart.js'

import type { RatesChartData, RatesChartSeries } from '../types/charts'
import type { Plan } from '../types/plan'
import { useStoredState } from '../hooks/useStoredState'
import {
  actionAtTime,
  formatPlanAction,
  getChartWindow,
  hasSeriesData,
  pointsInWindow,
  toChartPoints,
  type ChartPoint
} from '../utils/batteryChart'
import { formatRate } from '../utils/currency'
import { currentRate, upcomingRateExtremes } from '../utils/ratesChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Filler, Tooltip, Legend)

type RatesChartProps = {
  data: RatesChartData
  plan: Plan
}

type ComparisonKey = 'gas' | 'actual_hourly' | 'actual_today'

type RateLegendItem = {
  key: keyof RatesChartSeries
  label: string
  colour: string
  description?: string
}

const primaryLegendItems: RateLegendItem[] = [
  { key: 'import', label: 'Import rate', colour: 'var(--color-current)' },
  { key: 'export', label: 'Export rate', colour: 'var(--color-success)' }
]

const comparisonOptions: Array<RateLegendItem & { key: ComparisonKey }> = [
  {
    key: 'gas',
    label: 'Gas rate',
    colour: '#d97706',
    description: 'Forecast gas tariff supplied to Predbat.'
  },
  {
    key: 'actual_hourly',
    label: 'Hourly average cost',
    colour: '#0891b2',
    description: 'Measured average electricity cost per kWh for each recent hour.'
  },
  {
    key: 'actual_today',
    label: 'Daily average cost',
    colour: '#64748b',
    description: 'Running average electricity cost per kWh for today.'
  }
]

function RateLegend({ item, square = false }: { item: RateLegendItem; square?: boolean }) {
  return (
    <span className={`battery-chart-legend-item power-chart-legend-item ${square ? 'is-square' : ''}`}>
      <span
        className="battery-chart-legend-marker"
        style={{ '--battery-chart-legend-colour': item.colour } as React.CSSProperties}
        aria-hidden="true"
      />
      <span>{item.label}</span>
    </span>
  )
}

function formatRateValue(value: number | null, currencyUnit: string): string {
  return value === null ? '—' : formatRate(value, currencyUnit)
}

function makeRatesOverlayPlugin(
  plan: Plan,
  start: number,
  end: number,
  now: number,
  nowColour: string,
  zeroColour: string
): Plugin<'line'> {
  return {
    id: 'predbat-rates-overlays',
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart
      const nowPixel = Math.min(Math.max(scales.x.getPixelForValue(now), chartArea.left), chartArea.right)

      ctx.save()
      ctx.fillStyle = 'rgba(15, 23, 42, 0.07)'
      ctx.fillRect(chartArea.left, chartArea.top, nowPixel - chartArea.left, chartArea.bottom - chartArea.top)

      plan.rows.forEach((row, index) => {
        const rowStart = Date.parse(row.time)
        const nextStart = plan.rows[index + 1] ? Date.parse(plan.rows[index + 1].time) : rowStart + 30 * 60 * 1000
        const bandStart = Math.max(rowStart, start)
        const bandEnd = Math.min(nextStart, end)
        const state = row.state.toLowerCase()

        if (bandEnd <= bandStart || (!state.includes('chrg') && !state.includes('charge') && !state.includes('exp'))) {
          return
        }

        ctx.fillStyle = state.includes('exp') ? 'rgba(124, 58, 237, 0.08)' : 'rgba(37, 99, 235, 0.08)'
        const left = scales.x.getPixelForValue(bandStart)
        const right = scales.x.getPixelForValue(bandEnd)
        ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top)
      })
      ctx.restore()
    },
    afterDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart
      const zeroPixel = scales.y.getPixelForValue(0)
      const nowPixel = scales.x.getPixelForValue(now)

      if (zeroPixel >= chartArea.top && zeroPixel <= chartArea.bottom) {
        ctx.save()
        ctx.strokeStyle = zeroColour
        ctx.lineWidth = 1.5
        ctx.beginPath()
        ctx.moveTo(chartArea.left, zeroPixel)
        ctx.lineTo(chartArea.right, zeroPixel)
        ctx.stroke()
        ctx.restore()
      }

      ctx.save()
      ctx.strokeStyle = nowColour
      ctx.lineWidth = 1.5
      ctx.setLineDash([5, 4])
      ctx.beginPath()
      ctx.moveTo(nowPixel, chartArea.top)
      ctx.lineTo(nowPixel, chartArea.bottom)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = nowColour
      ctx.font = '600 11px sans-serif'
      ctx.fillText('Now', nowPixel + 5, chartArea.top + 13)
      ctx.restore()
    }
  }
}

/** Render forecast import/export tariffs and optional measured average rates. */
export default function RatesChart({ data, plan }: RatesChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [forecastHours, setForecastHours] = useStoredState<number>('predbat-chart-rates-range', 24, [12, 24, 48])
  const [comparisons, setComparisons] = useState<Set<ComparisonKey>>(new Set())
  const [themeRevision, setThemeRevision] = useState(0)
  const generatedAt = Date.parse(data.generated_at)
  const window = useMemo(
    () => getChartWindow(data.generated_at, forecastHours),
    [data.generated_at, forecastHours]
  )
  const importPoints = useMemo(
    () => pointsInWindow(toChartPoints(data.series.import), window),
    [data.series.import, window]
  )
  const exportPoints = useMemo(
    () => pointsInWindow(toChartPoints(data.series.export), window),
    [data.series.export, window]
  )
  const currentImport = currentRate(importPoints, generatedAt)
  const currentExport = currentRate(exportPoints, generatedAt)
  const extremes = upcomingRateExtremes(importPoints, exportPoints, generatedAt, window.end)
  const comparisonAvailability = useMemo<Record<ComparisonKey, boolean>>(() => ({
    gas: hasSeriesData(data.series.gas, window),
    actual_hourly: hasSeriesData(data.series.actual_hourly, window),
    actual_today: hasSeriesData(data.series.actual_today, window)
  }), [data.series, window])

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeRevision((revision) => revision + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!canvasRef.current) {
      return
    }

    const styles = getComputedStyle(document.documentElement)
    const textColour = styles.getPropertyValue('--color-text-secondary').trim() || '#4b5563'
    const gridColour = styles.getPropertyValue('--color-border').trim() || '#e5e7eb'
    const importColour = styles.getPropertyValue('--color-current').trim() || '#3b82f6'
    const exportColour = styles.getPropertyValue('--color-success').trim() || '#16a34a'
    const nowColour = styles.getPropertyValue('--color-warning').trim() || '#f59e0b'

    const datasets: ChartDataset<'line', ChartPoint[]>[] = [
      {
        label: 'Import rate',
        data: importPoints,
        borderColor: importColour,
        backgroundColor: importColour,
        borderWidth: 3,
        pointRadius: 0,
        stepped: true
      },
      {
        label: 'Export rate',
        data: exportPoints,
        borderColor: exportColour,
        backgroundColor: exportColour,
        borderWidth: 3,
        pointRadius: 0,
        stepped: true
      }
    ]

    comparisonOptions.forEach(({ key, label, colour }) => {
      if (!comparisons.has(key) || !comparisonAvailability[key]) {
        return
      }

      datasets.push({
        label,
        data: pointsInWindow(toChartPoints(data.series[key]), window),
        borderColor: colour,
        backgroundColor: colour,
        borderWidth: 1.5,
        borderDash: [5, 5],
        pointRadius: 0,
        stepped: true
      })
    })

    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [makeRatesOverlayPlugin(plan, window.start, window.end, generatedAt, nowColour, textColour)],
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        interaction: { intersect: false, mode: 'nearest' },
        scales: {
          x: {
            type: 'linear',
            min: window.start,
            max: window.end,
            grid: { color: gridColour },
            ticks: {
              color: textColour,
              maxTicksLimit: 9,
              callback(value) {
                return new Intl.DateTimeFormat(undefined, {
                  weekday: 'short',
                  hour: '2-digit',
                  minute: '2-digit'
                }).format(Number(value))
              }
            }
          },
          y: {
            grid: { color: gridColour },
            ticks: {
              color: textColour,
              callback(value) {
                return `${value}${data.currency_unit}`
              }
            }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title(items) {
                return new Intl.DateTimeFormat(undefined, {
                  weekday: 'short',
                  day: 'numeric',
                  month: 'short',
                  hour: '2-digit',
                  minute: '2-digit'
                }).format(Number(items[0]?.parsed.x))
              },
              label(item) {
                return `${item.dataset.label}: ${formatRate(Number(item.parsed.y), data.currency_unit)}`
              },
              footer(items) {
                const action = actionAtTime(plan.rows, Number(items[0]?.parsed.x))
                return action ? `Plan: ${formatPlanAction(action)}` : ''
              }
            }
          }
        }
      }
    }

    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [comparisonAvailability, comparisons, data, exportPoints, generatedAt, importPoints, plan, themeRevision, window])

  function toggleComparison(key: ComparisonKey) {
    setComparisons((currentComparisons) => {
      const nextComparisons = new Set(currentComparisons)
      if (nextComparisons.has(key)) {
        nextComparisons.delete(key)
      } else {
        nextComparisons.add(key)
      }
      return nextComparisons
    })
  }

  return (
    <section className="battery-chart-card" aria-labelledby="rates-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="rates-chart-heading">Rates</h2>
          <p>Import and export tariffs alongside Predbat's scheduled actions.</p>
        </div>

        <div className="battery-chart-range" aria-label="Forecast range">
          {[12, 24, 48].map((hours) => (
            <button
              type="button"
              className={forecastHours === hours ? 'is-active' : ''}
              aria-pressed={forecastHours === hours}
              onClick={() => setForecastHours(hours)}
              key={hours}
            >
              {hours}h
            </button>
          ))}
        </div>
      </div>

      <div className="battery-chart-summary">
        <div><span>Current import</span><strong>{formatRateValue(currentImport, data.currency_unit)}</strong></div>
        <div><span>Current export</span><strong>{formatRateValue(currentExport, data.currency_unit)}</strong></div>
        <div><span>Lowest upcoming import</span><strong>{formatRateValue(extremes.lowestImport, data.currency_unit)}</strong></div>
        <div><span>Highest upcoming export</span><strong>{formatRateValue(extremes.highestExport, data.currency_unit)}</strong></div>
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {primaryLegendItems.map((item) => <RateLegend item={item} key={item.key} />)}
        {comparisonOptions
          .filter(({ key }) => comparisons.has(key) && comparisonAvailability[key])
          .map((item) => <RateLegend item={item} key={item.key} />)}
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Forecast import and export electricity rates" />
      </div>

      <details className="battery-chart-comparisons">
        <summary>Compare measured averages and gas</summary>
        <div>
          {comparisonOptions.map((item) => (
            <label
              className={comparisonAvailability[item.key] ? '' : 'is-unavailable'}
              key={item.key}
              title={comparisonAvailability[item.key] ? item.description : 'No data in this range'}
            >
              <input
                type="checkbox"
                checked={comparisons.has(item.key)}
                disabled={!comparisonAvailability[item.key]}
                onChange={() => toggleComparison(item.key)}
              />
              <span>{item.label}</span>
              {!comparisonAvailability[item.key] && <small>No data in this range</small>}
            </label>
          ))}
        </div>
      </details>

      <div className="battery-chart-band-key" aria-label="Plan action shading">
        <RateLegend
          square
          item={{ key: 'import', label: 'Charge period', colour: 'color-mix(in srgb, var(--color-charge) 24%, transparent)' }}
        />
        <RateLegend
          square
          item={{ key: 'export', label: 'Export period', colour: 'color-mix(in srgb, var(--color-export) 24%, transparent)' }}
        />
      </div>
    </section>
  )
}
