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
  type ChartDataset
} from 'chart.js'

import { useStoredState } from '../hooks/useStoredState'
import type { BatteryDegradationChartData } from '../types/charts'
import { pointsInWindow, toChartPoints, type ChartPoint } from '../utils/batteryChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Tooltip, Legend)

const colours = [
  ['#0891b2', '#dc2626', '#d97706'],
  ['#7c3aed', '#059669', '#ca8a04'],
  ['#2563eb', '#be123c', '#9333ea'],
  ['#475569', '#0f766e', '#c2410c']
]

const descriptions = {
  nominal: 'The battery capacity specified by the manufacturer.',
  calculated: 'The usable capacity Predbat has estimated from recent charging data.',
  degradation: 'The percentage of the original capacity Predbat estimates has been lost.'
}

function latest(points: ChartPoint[]): number | null {
  return points.at(-1)?.y ?? null
}

/** Show Predbat's recent battery capacity estimates. */
export default function BatteryDegradationChart({ data }: { data: BatteryDegradationChartData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [rangeDays, setRangeDays] = useStoredState<number>('predbat-chart-degradation-range', 28, [7, 14, 28])
  const [themeRevision, setThemeRevision] = useState(0)
  const window = useMemo(() => ({
    start: Date.parse(data.generated_at) - rangeDays * 24 * 60 * 60 * 1000,
    end: Date.parse(data.generated_at)
  }), [data.generated_at, rangeDays])
  const inverters = useMemo(() => data.inverters.map((inverter) => ({
    ...inverter,
    nominal: pointsInWindow(toChartPoints(inverter.nominal), window),
    calculated: pointsInWindow(toChartPoints(inverter.calculated), window),
    degradation: pointsInWindow(toChartPoints(inverter.degradation), window)
  })), [data.inverters, window])

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeRevision((revision) => revision + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!canvasRef.current) return

    const styles = getComputedStyle(document.documentElement)
    const textColour = styles.getPropertyValue('--color-text-secondary').trim() || '#4b5563'
    const gridColour = styles.getPropertyValue('--color-border').trim() || '#e5e7eb'
    const datasets: ChartDataset<'line', ChartPoint[]>[] = inverters.flatMap((inverter, index) => {
      const suffix = data.inverters.length > 1 ? ` ${inverter.id + 1}` : ''
      const palette = colours[index % colours.length]
      return [
        { label: `Nominal capacity${suffix}`, data: inverter.nominal, borderColor: palette[0], yAxisID: 'y' },
        { label: `Estimated capacity${suffix}`, data: inverter.calculated, borderColor: palette[1], yAxisID: 'y' },
        { label: `Degradation${suffix}`, data: inverter.degradation, borderColor: palette[2], yAxisID: 'y1' }
      ].map((dataset) => ({
        ...dataset,
        backgroundColor: dataset.borderColor,
        borderWidth: 3,
        pointRadius: dataset.data.length === 1 ? 4 : 1,
        pointHoverRadius: 4,
        stepped: true,
        spanGaps: true
      }))
    })
    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        interaction: { intersect: false, mode: 'index' },
        scales: {
          x: {
            type: 'linear',
            min: window.start,
            max: window.end,
            grid: { color: gridColour },
            ticks: {
              color: textColour,
              maxTicksLimit: 10,
              callback: (value) => new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' }).format(Number(value))
            }
          },
          y: {
            beginAtZero: false,
            grid: { color: gridColour },
            ticks: { color: textColour, callback: (value) => `${Number(value).toFixed(1)} kWh` },
            title: { display: true, text: 'Capacity', color: textColour }
          },
          y1: {
            position: 'right',
            beginAtZero: true,
            grid: { drawOnChartArea: false },
            ticks: { color: textColour, callback: (value) => `${Number(value).toFixed(1)}%` },
            title: { display: true, text: 'Degradation', color: textColour }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short' }).format(Number(items[0]?.parsed.x)),
              label: (item) => `${item.dataset.label}: ${Number(item.parsed.y).toFixed(1)}${item.dataset.yAxisID === 'y1' ? '%' : ' kWh'}`
            }
          }
        }
      }
    }

    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [data.inverters.length, inverters, themeRevision, window])

  return (
    <section className="battery-chart-card" aria-labelledby="degradation-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="degradation-chart-heading">Battery degradation</h2>
          <p>Predbat's estimate of usable battery capacity over time.</p>
        </div>
        <div className="battery-chart-range" aria-label="History range">
          {[7, 14, 28].map((days) => (
            <button type="button" className={rangeDays === days ? 'is-active' : ''} aria-pressed={rangeDays === days} onClick={() => setRangeDays(days)} key={days}>{days}d</button>
          ))}
        </div>
      </div>

      <div className="battery-chart-summary">
        {inverters.map((inverter) => (
          <div key={inverter.id}>
            <span>{data.inverters.length > 1 ? `Battery ${inverter.id + 1}` : 'Battery'}</span>
            <strong>{latest(inverter.degradation)?.toFixed(1) ?? '—'}% degradation</strong>
            <small>{latest(inverter.calculated)?.toFixed(1) ?? '—'} of {latest(inverter.nominal)?.toFixed(1) ?? '—'} kWh usable</small>
          </div>
        ))}
        <div>
          <span>Automatic capacity scaling</span>
          <strong>{data.automatic_scaling ? 'Enabled' : 'Disabled'}</strong>
          <small>{data.automatic_scaling ? 'Predbat adjusts usable capacity' : 'Current estimate is advisory'}</small>
        </div>
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {inverters.flatMap((inverter, index) => {
          const suffix = data.inverters.length > 1 ? ` ${inverter.id + 1}` : ''
          return (['nominal', 'calculated', 'degradation'] as const).map((key, colourIndex) => (
            <span className="battery-chart-legend-item is-line" tabIndex={0} key={`${inverter.id}-${key}`}>
              <span className="battery-chart-legend-marker" style={{ '--battery-chart-legend-colour': colours[index % colours.length][colourIndex] } as React.CSSProperties} aria-hidden="true" />
              <span>{key === 'calculated' ? 'Estimated capacity' : key[0].toUpperCase() + key.slice(1)}{suffix}</span>
              <span className="battery-chart-help-popup" role="tooltip">{descriptions[key]}</span>
            </span>
          ))
        })}
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Battery capacity and degradation history" />
      </div>
    </section>
  )
}
