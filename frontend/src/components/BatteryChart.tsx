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

import type { BatteryChartData, BatteryChartSeries } from '../types/charts'
import type { Plan } from '../types/plan'
import { useStoredState } from '../hooks/useStoredState'
import {
  actionAtTime,
  formatPlanAction,
  getChartWindow,
  hasSeriesData,
  pointsInWindow,
  predictionEndTime,
  toChartPoints,
  type ChartPoint
} from '../utils/batteryChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Filler, Tooltip, Legend)

type BatteryChartProps = {
  data: BatteryChartData
  plan: Plan
}

type ComparisonKey =
  | 'base'
  | 'base10'
  | 'optimized10'
  | 'predicted_h1'
  | 'predicted_h8'
  | 'charge_limit_base'
  | 'record'

type LegendItem = {
  key: string
  label: string
  description: string
  colour: string
}

const primaryLegendItems: LegendItem[] = [
  {
    key: 'actual',
    label: 'Actual',
    description: 'Battery energy measured so far today.',
    colour: 'var(--color-current)'
  },
  {
    key: 'optimized',
    label: 'Optimised forecast',
    description: 'Expected battery energy if Predbat follows its current lowest-cost plan.',
    colour: 'var(--color-danger)'
  },
  {
    key: 'charge-limit',
    label: 'Charge limit',
    description: 'The battery energy target Predbat plans to charge to during a charge window.',
    colour: 'var(--color-charge)'
  },
  {
    key: 'export-limit',
    label: 'Export limit',
    description: 'The battery energy target Predbat plans to discharge to during an export window.',
    colour: 'var(--color-export)'
  }
]

const comparisonOptions: Array<LegendItem & { key: ComparisonKey }> = [
  {
    key: 'base',
    label: 'Base forecast',
    description: 'Expected battery energy if Predbat takes no further action and leaves the inverter on its current settings.',
    colour: '#0891b2'
  },
  {
    key: 'base10',
    label: 'Base forecast (10%)',
    description: 'The base forecast using Predbat’s pessimistic solar and household-load scenario.',
    colour: '#d97706'
  },
  {
    key: 'optimized10',
    label: 'Optimised forecast (10%)',
    description: 'The optimised plan using Predbat’s pessimistic solar and household-load scenario.',
    colour: '#9333ea'
  },
  {
    key: 'predicted_h1',
    label: 'Previous +1h prediction',
    description: 'What earlier plans predicted the battery level would be one hour later, aligned with the time that prediction was for.',
    colour: '#ea580c'
  },
  {
    key: 'predicted_h8',
    label: 'Previous +8h prediction',
    description: 'What earlier plans predicted the battery level would be eight hours later, aligned with the time that prediction was for.',
    colour: '#7c3aed'
  },
  {
    key: 'charge_limit_base',
    label: 'Base charge limit',
    description: 'The charge target already configured on the inverter before Predbat applies its optimised plan.',
    colour: '#059669'
  },
  {
    key: 'record',
    label: 'Prediction end',
    description: 'A vertical marker showing where Predbat’s recorded prediction data ends.',
    colour: '#64748b'
  }
]

function LegendHelpItem({ item, square = false }: { item: LegendItem; square?: boolean }) {
  return (
    <span
      className={`battery-chart-legend-item ${square ? 'is-square' : ''}`}
      tabIndex={0}
      aria-label={`${item.label}: ${item.description}`}
    >
      <span
        className="battery-chart-legend-marker"
        style={{ '--battery-chart-legend-colour': item.colour } as React.CSSProperties}
        aria-hidden="true"
      />
      <span>{item.label}</span>
      <span className="battery-chart-help-popup" role="tooltip">{item.description}</span>
    </span>
  )
}

function formatEnergy(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(2)} kWh`
}

function latestValue(points: ChartPoint[], at: number): number | null {
  return points.reduce<number | null>((latest, point) => {
    return point.x <= at ? point.y : latest
  }, null)
}

function makeChartOverlayPlugin(
  plan: Plan,
  start: number,
  end: number,
  now: number,
  predictionEnd: number | null,
  textColour: string
): Plugin<'line'> {
  return {
    id: 'predbat-chart-overlays',
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart
      const rows = plan.rows

      ctx.save()

      // Distinguish history from the forecast without obscuring the Actual line.
      const nowPixel = Math.min(Math.max(scales.x.getPixelForValue(now), chartArea.left), chartArea.right)
      ctx.fillStyle = 'rgba(15, 23, 42, 0.07)'
      ctx.fillRect(chartArea.left, chartArea.top, nowPixel - chartArea.left, chartArea.bottom - chartArea.top)

      rows.forEach((row, index) => {
        const rowStart = Date.parse(row.time)
        const nextStart = rows[index + 1] ? Date.parse(rows[index + 1].time) : rowStart + 30 * 60 * 1000
        const bandStart = Math.max(rowStart, start)
        const bandEnd = Math.min(nextStart, end)

        if (bandEnd <= bandStart) {
          return
        }

        const state = row.state.toLowerCase()
        if (!state.includes('chrg') && !state.includes('charge') && !state.includes('exp')) {
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

      function drawTimeMarker(timestamp: number, label: string, colour: string, alignRight = false) {
        if (timestamp < start || timestamp > end) {
          return
        }

        const x = scales.x.getPixelForValue(timestamp)
        ctx.save()
        ctx.strokeStyle = colour
        ctx.lineWidth = 1.5
        ctx.setLineDash([5, 4])
        ctx.beginPath()
        ctx.moveTo(x, chartArea.top)
        ctx.lineTo(x, chartArea.bottom)
        ctx.stroke()
        ctx.setLineDash([])
        ctx.fillStyle = colour
        ctx.font = '600 11px sans-serif'
        ctx.textAlign = alignRight ? 'right' : 'left'
        ctx.fillText(label, x + (alignRight ? -5 : 5), chartArea.top + 13)
        ctx.restore()
      }

      drawTimeMarker(now, 'Now', textColour)
      if (predictionEnd !== null) {
        drawTimeMarker(predictionEnd, 'Prediction ends', '#64748b', true)
      }
    }
  }
}

/** Render the interactive battery history and forecast chart. */
export default function BatteryChart({ data, plan }: BatteryChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [forecastHours, setForecastHours] = useStoredState<number>('predbat-chart-battery-range', 24, [12, 24, 48])
  const [comparisons, setComparisons] = useState<Set<ComparisonKey>>(new Set())
  const [themeRevision, setThemeRevision] = useState(0)

  const generatedAt = Date.parse(data.generated_at)
  const window = useMemo(
    () => getChartWindow(data.generated_at, forecastHours),
    [data.generated_at, forecastHours]
  )
  const actualPoints = useMemo(() => toChartPoints(data.series.actual), [data.series.actual])
  const optimizedPoints = useMemo(
    () => pointsInWindow(toChartPoints(data.series.optimized), window),
    [data.series.optimized, window]
  )

  const current = latestValue(actualPoints, generatedAt)
  const futurePoints = optimizedPoints.filter((point) => point.x >= generatedAt)
  const forecastMinimum = futurePoints.length
    ? Math.min(...futurePoints.map((point) => point.y))
    : null
  const horizonEnd = futurePoints.at(-1)?.y ?? null
  const nextAction = plan.rows.find(
    (row) => Date.parse(row.time) >= generatedAt && row.state.toLowerCase() !== 'demand'
  )
  const predictionEnd = useMemo(
    () => predictionEndTime(data.series.record),
    [data.series.record]
  )
  const comparisonAvailability = useMemo<Record<ComparisonKey, boolean>>(() => ({
    base: hasSeriesData(data.series.base, window),
    base10: hasSeriesData(data.series.base10, window),
    optimized10: hasSeriesData(data.series.optimized10, window),
    predicted_h1: hasSeriesData(data.series.predicted_h1, window),
    predicted_h8: hasSeriesData(data.series.predicted_h8, window),
    charge_limit_base: hasSeriesData(data.series.charge_limit_base, window, true),
    record: predictionEnd !== null && predictionEnd >= window.start && predictionEnd <= window.end
  }), [data.series, predictionEnd, window])

  function unavailableReason(key: ComparisonKey): string {
    if (key === 'charge_limit_base') {
      return 'No baseline charge window in this range'
    }

    if (key === 'record' && predictionEnd !== null) {
      return 'Prediction continues beyond this range'
    }

    return 'No data in this range'
  }

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
    const chargeColour = styles.getPropertyValue('--color-charge').trim() || '#2563eb'
    const exportColour = styles.getPropertyValue('--color-export').trim() || '#7c3aed'
    const nowColour = styles.getPropertyValue('--color-warning').trim() || '#f59e0b'

    const datasets: ChartDataset<'line', ChartPoint[]>[] = [
      {
        label: 'Actual',
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
      },
      {
        label: 'Charge limit',
        data: pointsInWindow(toChartPoints(data.series.charge_limit_optimized), window),
        borderColor: chargeColour,
        backgroundColor: chargeColour,
        borderWidth: 1.5,
        borderDash: [6, 4],
        pointRadius: 0,
        stepped: true
      },
      {
        label: 'Export limit',
        data: pointsInWindow(toChartPoints(data.series.export_limit_optimized), window),
        borderColor: exportColour,
        backgroundColor: exportColour,
        borderWidth: 1.5,
        borderDash: [2, 4],
        pointRadius: 0,
        stepped: true
      }
    ]

    comparisonOptions.forEach(({ key, label, colour }) => {
      if (!comparisons.has(key) || !comparisonAvailability[key] || key === 'record') {
        return
      }

      datasets.push({
        label,
        data: pointsInWindow(toChartPoints(data.series[key] as BatteryChartSeries[ComparisonKey]), window),
        borderColor: colour,
        backgroundColor: colour,
        borderWidth: 1.5,
        borderDash: [5, 5],
        pointRadius: 0,
        tension: 0.15,
        stepped: key.includes('limit')
      })
    })

    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [
        makeChartOverlayPlugin(
          plan,
          window.start,
          window.end,
          generatedAt,
          comparisons.has('record') && comparisonAvailability.record ? predictionEnd : null,
          nowColour
        )
      ],
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
            beginAtZero: true,
            suggestedMax: data.soc_max,
            grid: { color: gridColour },
            ticks: {
              color: textColour,
              callback(value) {
                return `${value} kWh`
              }
            }
          }
        },
        plugins: {
          legend: {
            display: false
          },
          tooltip: {
            callbacks: {
              title(items) {
                const timestamp = Number(items[0]?.parsed.x)
                return new Intl.DateTimeFormat(undefined, {
                  weekday: 'short',
                  day: 'numeric',
                  month: 'short',
                  hour: '2-digit',
                  minute: '2-digit'
                }).format(timestamp)
              },
              label(item) {
                const value = Number(item.parsed.y)
                const percentage = data.soc_max > 0 ? ` · ${Math.round((value / data.soc_max) * 100)}%` : ''
                return `${item.dataset.label}: ${value.toFixed(2)} kWh${percentage}`
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
  }, [actualPoints, comparisonAvailability, comparisons, data, generatedAt, optimizedPoints, plan, predictionEnd, themeRevision, window])

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
    <section className="battery-chart-card" aria-labelledby="battery-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="battery-chart-heading">Battery</h2>
          <p>Recent state of charge and Predbat's optimised forecast.</p>
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
        <div><span>Current</span><strong>{formatEnergy(current)}</strong></div>
        <div><span>Forecast minimum</span><strong>{formatEnergy(forecastMinimum)}</strong></div>
        <div>
          <span
            className="battery-chart-summary-help"
            tabIndex={0}
            aria-label="Battery at chart end: forecast battery energy at the end of the selected chart range."
          >
            Battery at chart end
            <span className="battery-chart-help-popup" role="tooltip">
              Forecast battery energy at the end of the selected {forecastHours}-hour range.
            </span>
          </span>
          <strong>{formatEnergy(horizonEnd)}</strong>
        </div>
        <div>
          <span>Next action</span>
          <strong>{nextAction ? formatPlanAction(nextAction.state) : 'No scheduled action'}</strong>
          {nextAction && <small>{new Date(nextAction.time).toLocaleString()}</small>}
        </div>
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {primaryLegendItems.map((item) => <LegendHelpItem item={item} key={item.key} />)}
        {comparisonOptions
          .filter(({ key }) => comparisons.has(key) && comparisonAvailability[key])
          .map((item) => <LegendHelpItem item={item} key={item.key} />)}
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Battery state of charge history and forecast" />
      </div>

      <details className="battery-chart-comparisons">
        <summary>Compare other forecasts</summary>
        <div>
          {comparisonOptions.map((item) => (
            <label
              className={comparisonAvailability[item.key] ? '' : 'is-unavailable'}
              key={item.key}
              title={comparisonAvailability[item.key] ? item.description : unavailableReason(item.key)}
            >
              <input
                type="checkbox"
                checked={comparisons.has(item.key)}
                disabled={!comparisonAvailability[item.key]}
                onChange={() => toggleComparison(item.key)}
              />
              <span>{item.label}</span>
              {!comparisonAvailability[item.key] && (
                <small>{unavailableReason(item.key)}</small>
              )}
            </label>
          ))}
        </div>
      </details>

      <div className="battery-chart-band-key" aria-label="Plan action shading">
        <LegendHelpItem
          square
          item={{
            key: 'charge-period',
            label: 'Charge period',
            description: 'Background shading marks a period when Predbat plans to charge the battery.',
            colour: 'color-mix(in srgb, var(--color-charge) 24%, transparent)'
          }}
        />
        <LegendHelpItem
          square
          item={{
            key: 'export-period',
            label: 'Export period',
            description: 'Background shading marks a period when Predbat plans to export energy from the battery.',
            colour: 'color-mix(in srgb, var(--color-export) 24%, transparent)'
          }}
        />
      </div>
    </section>
  )
}
