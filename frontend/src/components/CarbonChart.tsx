import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Chart,
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

import { useStoredState } from '../hooks/useStoredState'
import type { CarbonChartData, TimestampSeries } from '../types/charts'
import { pointsInWindow, toChartPoints, type ChartPoint } from '../utils/batteryChart'
import { getCostChartWindow, latestCostValue } from '../utils/costChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Tooltip, Legend)

const legends = [
  ['actual', 'Actual emissions', 'var(--color-current)', 'Your cumulative net grid emissions since midnight. Grid export can make this fall.'],
  ['optimized', 'Optimised forecast', 'var(--color-export)', 'Predbat’s expected cumulative emissions when following the optimised plan.'],
  ['base', 'Base forecast', '#d97706', 'Expected cumulative emissions if Predbat takes no further optimising action.'],
  ['intensity', 'Grid intensity', '#7c3aed', 'Forecast grams of CO₂ produced per kWh of grid electricity.']
] as const

function inKg(series: TimestampSeries): ChartPoint[] {
  return toChartPoints(series).map((point) => ({ ...point, y: point.y / 1000 }))
}

function formatKg(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(2)} kg`
}

function nowMarker(now: number, colour: string): Plugin<'line'> {
  return {
    id: 'predbat-carbon-now',
    beforeDatasetsDraw({ ctx, chartArea, scales }) {
      const x = Math.min(Math.max(scales.x.getPixelForValue(now), chartArea.left), chartArea.right)
      ctx.save()
      ctx.fillStyle = 'rgba(15, 23, 42, 0.07)'
      ctx.fillRect(chartArea.left, chartArea.top, x - chartArea.left, chartArea.bottom - chartArea.top)
      ctx.restore()
    },
    afterDatasetsDraw({ ctx, chartArea, scales }) {
      const x = scales.x.getPixelForValue(now)
      ctx.save()
      ctx.strokeStyle = colour
      ctx.setLineDash([5, 4])
      ctx.beginPath()
      ctx.moveTo(x, chartArea.top)
      ctx.lineTo(x, chartArea.bottom)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = colour
      ctx.font = '600 11px sans-serif'
      ctx.fillText('Now', x + 5, chartArea.top + 13)
      ctx.restore()
    }
  }
}

/** Show actual and forecast household CO₂ alongside forecast grid intensity. */
export default function CarbonChart({ data }: { data: CarbonChartData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [forecastHours, setForecastHours] = useStoredState<number>('predbat-chart-carbon-range', 24, [12, 24, 48])
  const [themeRevision, setThemeRevision] = useState(0)
  const now = Date.parse(data.generated_at)
  const actual = useMemo(() => inKg(data.series.actual), [data.series.actual])
  const window = useMemo(() => getCostChartWindow(actual, data.generated_at, forecastHours), [actual, data.generated_at, forecastHours])
  const points = useMemo(() => ({
    actual: pointsInWindow(actual, window),
    optimized: pointsInWindow(inKg(data.series.optimized), window),
    base: pointsInWindow(inKg(data.series.base), window),
    intensity: pointsInWindow(toChartPoints(data.series.intensity), window)
  }), [actual, data.series, window])

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeRevision((revision) => revision + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!canvasRef.current) return
    const styles = getComputedStyle(document.documentElement)
    const colours = {
      actual: styles.getPropertyValue('--color-current').trim() || '#3b82f6',
      optimized: styles.getPropertyValue('--color-export').trim() || '#16a34a',
      base: '#d97706',
      intensity: '#7c3aed'
    }
    const text = styles.getPropertyValue('--color-text-secondary').trim() || '#4b5563'
    const grid = styles.getPropertyValue('--color-border').trim() || '#e5e7eb'
    const datasets: ChartDataset<'line', ChartPoint[]>[] = legends.map(([key, label]) => ({
      label,
      data: points[key],
      borderColor: colours[key],
      backgroundColor: colours[key],
      borderWidth: key === 'actual' || key === 'optimized' ? 3 : 2,
      borderDash: key === 'base' ? [6, 4] : undefined,
      pointRadius: 0,
      tension: 0.15,
      yAxisID: key === 'intensity' ? 'yIntensity' : 'y'
    }))
    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [nowMarker(now, styles.getPropertyValue('--color-warning').trim() || '#f59e0b')],
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        interaction: { intersect: false, mode: 'nearest' },
        scales: {
          x: {
            type: 'linear', min: window.start, max: window.end, grid: { color: grid },
            ticks: { color: text, maxTicksLimit: 9, callback: (value) => new Intl.DateTimeFormat(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' }).format(Number(value)) }
          },
          y: { grid: { color: grid }, ticks: { color: text, callback: (value) => `${value} kg` }, title: { display: true, text: 'Household CO₂', color: text } },
          yIntensity: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: colours.intensity, callback: (value) => `${value} g/kWh` }, title: { display: true, text: 'Grid intensity', color: colours.intensity } }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(Number(items[0]?.parsed.x)),
              label: (item) => `${item.dataset.label}: ${Number(item.parsed.y).toFixed(item.dataset.yAxisID === 'yIntensity' ? 0 : 2)} ${item.dataset.yAxisID === 'yIntensity' ? 'g/kWh' : 'kg'}`
            }
          }
        }
      }
    }
    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [now, points, themeRevision, window])

  const actualNow = latestCostValue(points.actual, now)
  const optimizedEnd = latestCostValue(points.optimized, window.end)
  const baseEnd = latestCostValue(points.base, window.end)
  const intensityNow = latestCostValue(points.intensity, now)

  return (
    <section className="battery-chart-card" aria-labelledby="carbon-chart-heading">
      <div className="battery-chart-header">
        <div><h2 id="carbon-chart-heading">CO₂</h2><p>Actual and forecast carbon emissions from your home’s net grid use.</p></div>
        <div className="battery-chart-range" aria-label="Forecast range">
          {[12, 24, 48].map((hours) => <button type="button" className={forecastHours === hours ? 'is-active' : ''} aria-pressed={forecastHours === hours} onClick={() => setForecastHours(hours)} key={hours}>{hours}h</button>)}
        </div>
      </div>
      <div className="battery-chart-summary">
        <div><span>Actual today</span><strong>{formatKg(actualNow)}</strong></div>
        <div><span>Optimised horizon</span><strong>{formatKg(optimizedEnd)}</strong></div>
        <div><span>Base horizon</span><strong>{formatKg(baseEnd)}</strong></div>
        <div><span>Grid intensity now</span><strong>{intensityNow === null ? '—' : `${intensityNow.toFixed(0)} g/kWh`}</strong></div>
      </div>
      <div className="battery-chart-legend" aria-label="Chart series">
        {legends.map(([key, label, colour, description]) => (
          <span className="battery-chart-legend-item is-line" tabIndex={0} key={key}>
            <span className="battery-chart-legend-marker" style={{ '--battery-chart-legend-colour': colour } as React.CSSProperties} aria-hidden="true" />
            <span>{label}</span><span className="battery-chart-help-popup" role="tooltip">{description}</span>
          </span>
        ))}
      </div>
      <div className="battery-chart-canvas"><canvas ref={canvasRef} role="img" aria-label="Actual and forecast household carbon emissions and grid carbon intensity" /></div>
    </section>
  )
}
