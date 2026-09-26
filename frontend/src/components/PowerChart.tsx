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

import type { PowerChartData } from '../types/charts'
import type { Plan } from '../types/plan'
import { useStoredState } from '../hooks/useStoredState'
import { formatPlanAction, pointsInWindow, toChartPoints, type ChartPoint } from '../utils/batteryChart'
import { cumulativeEnergyToPower, getPowerChartWindow, gridPowerPeaks } from '../utils/powerChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Filler, Tooltip, Legend)

type PowerChartProps = {
  data: PowerChartData
  plan: Plan
}

type PowerSeriesKey = 'battery' | 'solar' | 'grid' | 'load' | 'iboost'

type PowerLegendItem = {
  key: PowerSeriesKey
  label: string
  colour: string
}

const legendItems: PowerLegendItem[] = [
  {
    key: 'solar',
    label: 'Solar generation',
    colour: 'var(--color-flow-solar)'
  },
  {
    key: 'load',
    label: 'Home demand',
    colour: 'var(--color-flow-home)'
  },
  {
    key: 'battery',
    label: 'Battery power',
    colour: 'var(--color-flow-battery)'
  },
  {
    key: 'grid',
    label: 'Grid power',
    colour: 'var(--color-flow-grid)'
  },
  {
    key: 'iboost',
    label: 'iBoost heating',
    colour: 'var(--color-hold-iboost)'
  }
]

function PowerLegendItem({ item, square = false }: { item: PowerLegendItem; square?: boolean }) {
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

function formatPower(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(2)} kW`
}

function maximum(points: ChartPoint[]): number | null {
  return points.length ? Math.max(...points.map((point) => point.y)) : null
}

function makePowerOverlayPlugin(
  plan: Plan,
  start: number,
  end: number,
  now: number,
  nowColour: string,
  zeroColour: string
): Plugin<'line'> {
  return {
    id: 'predbat-power-overlays',
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart
      const nowPixel = scales.x.getPixelForValue(now)

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

      ctx.save()
      ctx.strokeStyle = zeroColour
      ctx.lineWidth = 1.5
      ctx.beginPath()
      ctx.moveTo(chartArea.left, zero)
      ctx.lineTo(chartArea.right, zero)
      ctx.stroke()

      ctx.strokeStyle = nowColour
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

function powerDirection(key: PowerSeriesKey, value: number): string {
  if (key === 'battery') {
    return value < 0 ? 'charging' : value > 0 ? 'discharging' : 'idle'
  }
  if (key === 'grid') {
    return value < 0 ? 'importing' : value > 0 ? 'exporting' : 'idle'
  }
  if (key === 'solar') {
    return 'generating'
  }
  if (key === 'load') {
    return 'home demand'
  }
  return 'water heating'
}

/** Render Predbat's optimised power forecast with explicit flow direction. */
export default function PowerChart({ data, plan }: PowerChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [forecastHours, setForecastHours] = useStoredState<number>('predbat-chart-power-range', 24, [12, 24, 48])
  const [themeRevision, setThemeRevision] = useState(0)
  const generatedAt = Date.parse(data.generated_at)
  const window = useMemo(
    () => getPowerChartWindow(data.generated_at, forecastHours),
    [data.generated_at, forecastHours]
  )

  const points = useMemo(() => ({
    battery: pointsInWindow(toChartPoints(data.series.battery), window),
    solar: pointsInWindow(toChartPoints(data.series.solar), window),
    grid: pointsInWindow(toChartPoints(data.series.grid), window),
    load: pointsInWindow(toChartPoints(data.series.load), window),
    iboost: pointsInWindow(cumulativeEnergyToPower(data.series.iboost_energy), window)
  }), [data.series, window])
  const hasIBoost = points.iboost.some((point) => point.y > 0)
  const gridPeaks = gridPowerPeaks(points.grid)

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
    const colours: Record<PowerSeriesKey, string> = {
      solar: styles.getPropertyValue('--color-flow-solar').trim() || '#f59e0b',
      load: styles.getPropertyValue('--color-flow-home').trim() || '#16a34a',
      battery: styles.getPropertyValue('--color-flow-battery').trim() || '#2563eb',
      grid: styles.getPropertyValue('--color-flow-grid').trim() || '#6b7280',
      iboost: styles.getPropertyValue('--color-hold-iboost').trim() || '#0284c7'
    }
    const textColour = styles.getPropertyValue('--color-text-secondary').trim() || '#4b5563'
    const gridColour = styles.getPropertyValue('--color-border').trim() || '#e5e7eb'
    const nowColour = styles.getPropertyValue('--color-warning').trim() || '#f59e0b'
    const visibleItems = legendItems.filter((item) => item.key !== 'iboost' || hasIBoost)

    const datasets: ChartDataset<'line', ChartPoint[]>[] = visibleItems.map((item) => ({
      label: item.label,
      data: points[item.key],
      borderColor: colours[item.key],
      backgroundColor: colours[item.key],
      borderWidth: item.key === 'solar' || item.key === 'load' ? 3 : 2,
      pointRadius: 0,
      tension: 0.2
    }))

    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [makePowerOverlayPlugin(plan, window.start, window.end, generatedAt, nowColour, textColour)],
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
                return `${value} kW`
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
                const key = visibleItems[item.datasetIndex]?.key ?? 'load'
                const value = Number(item.parsed.y)
                return `${item.dataset.label}: ${Math.abs(value).toFixed(2)} kW · ${powerDirection(key, value)}`
              },
              footer(items) {
                const timestamp = Number(items[0]?.parsed.x)
                const row = [...plan.rows].reverse().find((candidate) => Date.parse(candidate.time) <= timestamp)
                return row ? `Plan: ${formatPlanAction(row.state)}` : ''
              }
            }
          }
        }
      }
    }

    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [data, generatedAt, hasIBoost, plan, points, themeRevision, window])

  return (
    <section className="battery-chart-card" aria-labelledby="power-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="power-chart-heading">Power</h2>
          <p>Predbat's optimised forecast for generation, demand and energy flow.</p>
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
        <div><span>Peak solar</span><strong>{formatPower(maximum(points.solar))}</strong></div>
        <div><span>Peak home demand</span><strong>{formatPower(maximum(points.load))}</strong></div>
        <div><span>Peak grid import</span><strong>{formatPower(gridPeaks.import)}</strong></div>
        <div><span>Peak grid export</span><strong>{formatPower(gridPeaks.export)}</strong></div>
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {legendItems
          .filter((item) => item.key !== 'iboost' || hasIBoost)
          .map((item) => <PowerLegendItem item={item} key={item.key} />)}
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Predicted solar, home, battery and grid power" />
      </div>

      <div className="battery-chart-band-key" aria-label="Plan action shading">
        <PowerLegendItem
          square
          item={{
            key: 'battery',
            label: 'Charge period',
            colour: 'color-mix(in srgb, var(--color-charge) 24%, transparent)'
          }}
        />
        <PowerLegendItem
          square
          item={{
            key: 'grid',
            label: 'Export period',
            colour: 'color-mix(in srgb, var(--color-export) 24%, transparent)'
          }}
        />
      </div>
    </section>
  )
}
