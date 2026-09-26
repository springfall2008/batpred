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

import type { CostChartData, CostChartSeries } from '../types/charts'
import type { Plan } from '../types/plan'
import { useStoredState } from '../hooks/useStoredState'
import {
  actionAtTime,
  formatPlanAction,
  hasSeriesData,
  pointsInWindow,
  toChartPoints,
  type ChartPoint
} from '../utils/batteryChart'
import {
  formatMinorCurrency,
  getCostChartWindow,
  latestCostValue,
  plannedSaving
} from '../utils/costChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Filler, Tooltip, Legend)

type CostChartProps = {
  data: CostChartData
  plan: Plan
}

type ComparisonKey = 'actual_import' | 'actual_export' | 'base' | 'base10' | 'optimized10'

type CostLegendItem = {
  key: keyof CostChartSeries
  label: string
  colour: string
  description?: string
}

const primaryLegendItems: CostLegendItem[] = [
  { key: 'actual', label: 'Actual cost', colour: 'var(--color-current)' },
  { key: 'optimized', label: 'Optimised forecast', colour: 'var(--color-danger)' }
]

const comparisonOptions: Array<CostLegendItem & { key: ComparisonKey }> = [
  {
    key: 'actual_import',
    label: 'Import cost',
    colour: '#d97706',
    description: 'Cumulative cost of energy imported from the grid today.'
  },
  {
    key: 'actual_export',
    label: 'Export credit',
    colour: '#059669',
    description: 'Cumulative credit earned from grid exports today, shown below zero.'
  },
  {
    key: 'base',
    label: 'Base forecast',
    colour: '#0891b2',
    description: 'Expected total cost if Predbat takes no further optimising action.'
  },
  {
    key: 'base10',
    label: 'Base forecast (10%)',
    colour: '#7c3aed',
    description: 'Base forecast using Predbat’s pessimistic solar and household-load scenario.'
  },
  {
    key: 'optimized10',
    label: 'Optimised forecast (10%)',
    colour: '#9333ea',
    description: 'Optimised forecast using Predbat’s pessimistic solar and household-load scenario.'
  }
]

function CostLegend({ item, square = false }: { item: CostLegendItem; square?: boolean }) {
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

function makeCostOverlayPlugin(
  plan: Plan,
  start: number,
  end: number,
  now: number,
  nowColour: string,
  zeroColour: string
): Plugin<'line'> {
  return {
    id: 'predbat-cost-overlays',
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
      const zero = scales.y.getPixelForValue(0)
      const nowPixel = scales.x.getPixelForValue(now)

      if (zero >= chartArea.top && zero <= chartArea.bottom) {
        ctx.save()
        ctx.strokeStyle = zeroColour
        ctx.lineWidth = 1.5
        ctx.beginPath()
        ctx.moveTo(chartArea.left, zero)
        ctx.lineTo(chartArea.right, zero)
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

/** Render Predbat's cumulative actual and forecast energy costs. */
export default function CostChart({ data, plan }: CostChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [forecastHours, setForecastHours] = useStoredState<number>('predbat-chart-cost-range', 24, [12, 24, 48])
  const [comparisons, setComparisons] = useState<Set<ComparisonKey>>(new Set())
  const [themeRevision, setThemeRevision] = useState(0)
  const generatedAt = Date.parse(data.generated_at)
  const actualPoints = useMemo(() => toChartPoints(data.series.actual), [data.series.actual])
  const window = useMemo(
    () => getCostChartWindow(actualPoints, data.generated_at, forecastHours),
    [actualPoints, data.generated_at, forecastHours]
  )
  const optimizedPoints = useMemo(
    () => pointsInWindow(toChartPoints(data.series.optimized), window),
    [data.series.optimized, window]
  )
  const basePoints = useMemo(
    () => pointsInWindow(toChartPoints(data.series.base), window),
    [data.series.base, window]
  )
  const current = latestCostValue(actualPoints, generatedAt)
  const optimizedEnd = latestCostValue(optimizedPoints, window.end)
  const baseEnd = latestCostValue(basePoints, window.end)
  const saving = plannedSaving(baseEnd, optimizedEnd)
  const comparisonAvailability = useMemo<Record<ComparisonKey, boolean>>(() => ({
    actual_import: hasSeriesData(data.series.actual_import, window),
    actual_export: hasSeriesData(data.series.actual_export, window),
    base: hasSeriesData(data.series.base, window),
    base10: hasSeriesData(data.series.base10, window),
    optimized10: hasSeriesData(data.series.optimized10, window)
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
    const actualColour = styles.getPropertyValue('--color-current').trim() || '#3b82f6'
    const optimizedColour = styles.getPropertyValue('--color-danger').trim() || '#dc2626'
    const nowColour = styles.getPropertyValue('--color-warning').trim() || '#f59e0b'
    const comparisonColours = Object.fromEntries(comparisonOptions.map((item) => [item.key, item.colour]))

    const datasets: ChartDataset<'line', ChartPoint[]>[] = [
      {
        label: 'Actual cost',
        data: pointsInWindow(actualPoints, window),
        borderColor: actualColour,
        backgroundColor: actualColour,
        borderWidth: 3,
        pointRadius: 0,
        tension: 0.2
      },
      {
        label: 'Optimised forecast',
        data: optimizedPoints,
        borderColor: optimizedColour,
        backgroundColor: optimizedColour,
        borderWidth: 3,
        pointRadius: 0,
        tension: 0.2
      }
    ]

    comparisonOptions.forEach(({ key, label }) => {
      if (!comparisons.has(key) || !comparisonAvailability[key]) {
        return
      }

      datasets.push({
        label,
        data: pointsInWindow(toChartPoints(data.series[key]), window),
        borderColor: comparisonColours[key],
        backgroundColor: comparisonColours[key],
        borderWidth: 1.5,
        borderDash: [5, 5],
        pointRadius: 0,
        tension: 0.15
      })
    })

    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [makeCostOverlayPlugin(plan, window.start, window.end, generatedAt, nowColour, textColour)],
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
                return `${item.dataset.label}: ${formatMinorCurrency(Number(item.parsed.y), data.currency_symbol)}`
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
  }, [actualPoints, comparisonAvailability, comparisons, data, generatedAt, optimizedPoints, plan, themeRevision, window])

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
    <section className="battery-chart-card" aria-labelledby="cost-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="cost-chart-heading">Cost</h2>
          <p>Cumulative energy cost today and Predbat's optimised forecast.</p>
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
        <div><span>Cost so far</span><strong>{formatMinorCurrency(current, data.currency_symbol)}</strong></div>
        <div><span>Forecast total</span><strong>{formatMinorCurrency(optimizedEnd, data.currency_symbol)}</strong></div>
        <div><span>Base total</span><strong>{formatMinorCurrency(baseEnd, data.currency_symbol)}</strong></div>
        <div><span>Planned saving</span><strong>{formatMinorCurrency(saving, data.currency_symbol)}</strong></div>
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {primaryLegendItems.map((item) => <CostLegend item={item} key={item.key} />)}
        {comparisonOptions
          .filter(({ key }) => comparisons.has(key) && comparisonAvailability[key])
          .map((item) => <CostLegend item={item} key={item.key} />)}
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Actual and forecast cumulative energy cost" />
      </div>

      <details className="battery-chart-comparisons">
        <summary>Compare cost breakdowns and forecasts</summary>
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
        <CostLegend
          square
          item={{ key: 'optimized', label: 'Charge period', colour: 'color-mix(in srgb, var(--color-charge) 24%, transparent)' }}
        />
        <CostLegend
          square
          item={{ key: 'base', label: 'Export period', colour: 'color-mix(in srgb, var(--color-export) 24%, transparent)' }}
        />
      </div>
    </section>
  )
}
