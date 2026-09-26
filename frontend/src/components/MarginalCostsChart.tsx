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
import type { MarginalCostsChartData } from '../types/charts'
import { pointsInWindow, toChartPoints, type ChartPoint } from '../utils/batteryChart'
import { formatRate } from '../utils/currency'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Tooltip, Legend)

const colours = ['#0891b2', '#d97706', '#dc2626', '#7c3aed']

function nowMarker(now: number, colour: string): Plugin<'line'> {
  return {
    id: 'predbat-marginal-now',
    beforeDatasetsDraw({ ctx, chartArea, scales }) {
      const x = scales.x.getPixelForValue(now)
      ctx.save()
      ctx.fillStyle = 'rgba(15, 23, 42, 0.07)'
      ctx.fillRect(chartArea.left, chartArea.top, Math.max(0, x - chartArea.left), chartArea.bottom - chartArea.top)
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

/** Show the extra cost of consuming more energy at different times. */
export default function MarginalCostsChart({ data }: { data: MarginalCostsChartData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [rangeHours, setRangeHours] = useStoredState<number>('predbat-chart-marginal-range', 24, [24, 72, 168])
  const [themeRevision, setThemeRevision] = useState(0)
  const now = Date.parse(data.generated_at)
  const window = useMemo(() => ({
    start: now - (rangeHours - 12) * 60 * 60 * 1000,
    end: now + 12 * 60 * 60 * 1000
  }), [now, rangeHours])

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
    const nowColour = styles.getPropertyValue('--color-warning').trim() || '#f59e0b'
    const datasets: ChartDataset<'line', ChartPoint[]>[] = data.levels.map((level, index) => ({
      label: `${level.label} (+${level.kwh} kWh)`,
      data: pointsInWindow(toChartPoints(level.series), window),
      borderColor: colours[index],
      backgroundColor: colours[index],
      borderWidth: 2.5,
      pointRadius: 0,
      stepped: true
    }))

    datasets.push(
      {
        label: 'Import rate',
        data: pointsInWindow(toChartPoints(data.grid_import), window),
        borderColor: '#2563eb',
        backgroundColor: '#2563eb',
        borderWidth: 1.5,
        borderDash: [5, 5],
        pointRadius: 0,
        stepped: true
      },
      {
        label: 'Export rate',
        data: pointsInWindow(toChartPoints(data.grid_export), window),
        borderColor: '#16a34a',
        backgroundColor: '#16a34a',
        borderWidth: 1.5,
        borderDash: [5, 5],
        pointRadius: 0,
        stepped: true
      }
    )

    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [nowMarker(now, nowColour)],
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
              callback: (value) => new Intl.DateTimeFormat(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' }).format(Number(value))
            }
          },
          y: {
            grid: { color: gridColour },
            ticks: { color: textColour, callback: (value) => `${value}${data.currency_unit}` },
            title: { display: true, text: `${data.currency_unit}/kWh`, color: textColour }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(Number(items[0]?.parsed.x)),
              label: (item) => `${item.dataset.label}: ${formatRate(Number(item.parsed.y), data.currency_unit)}`
            }
          }
        }
      }
    }

    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [data, now, themeRevision, window])

  return (
    <section className="battery-chart-card" aria-labelledby="marginal-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="marginal-chart-heading">Marginal costs</h2>
          <p>The cost of using more energy than forecast after Predbat adjusts the plan.</p>
        </div>
        <div className="battery-chart-range" aria-label="Chart range">
          {[[24, '24h'], [72, '3d'], [168, '7d']].map(([hours, label]) => (
            <button type="button" className={rangeHours === hours ? 'is-active' : ''} aria-pressed={rangeHours === hours} onClick={() => setRangeHours(hours as number)} key={hours}>{label}</button>
          ))}
        </div>
      </div>

      <div className="battery-chart-summary">
        {data.levels.map((level) => (
          <div key={level.id}>
            <span>{level.label} consumption · +{level.kwh} kWh</span>
            <strong>{level.current_cost == null ? '—' : formatRate(level.current_cost, data.currency_unit)}</strong>
            <small>{level.cheap ? 'Cheap now' : level.moderate ? 'Moderate now' : 'High now'}</small>
          </div>
        ))}
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {data.levels.map((level, index) => (
          <span className="battery-chart-legend-item is-line" tabIndex={0} key={level.id}>
            <span className="battery-chart-legend-marker" style={{ '--battery-chart-legend-colour': colours[index] } as React.CSSProperties} aria-hidden="true" />
            <span>{level.label} (+{level.kwh} kWh)</span>
            <span className="battery-chart-help-popup" role="tooltip">The effective cost per kWh if an extra {level.kwh} kWh is used during this time window.</span>
          </span>
        ))}
        <span className="battery-chart-legend-item power-chart-legend-item is-line"><span className="battery-chart-legend-marker" style={{ '--battery-chart-legend-colour': '#2563eb' } as React.CSSProperties} /><span>Import rate</span></span>
        <span className="battery-chart-legend-item power-chart-legend-item is-line"><span className="battery-chart-legend-marker" style={{ '--battery-chart-legend-colour': '#16a34a' } as React.CSSProperties} /><span>Export rate</span></span>
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Historical and forecast marginal energy costs" />
      </div>
    </section>
  )
}
