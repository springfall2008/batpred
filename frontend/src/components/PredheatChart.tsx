import { useEffect, useMemo, useRef, useState } from 'react'
import { Chart, Legend, LinearScale, LineController, LineElement, PointElement, Tooltip } from 'chart.js'

import { useStoredState } from '../hooks/useStoredState'
import type { PredheatChartData } from '../types/charts'
import { getChartWindow, pointsInWindow, toChartPoints } from '../utils/batteryChart'
import { formatChartTimestamp } from '../utils/chartZoom'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Tooltip, Legend)

const series = [
  ['internal_actual', 'Indoor temperature', '#3b82f6', false],
  ['internal_forecast', 'Indoor forecast', '#3b82f6', true],
  ['external_actual', 'Outdoor temperature', '#0d9488', false],
  ['external_forecast', 'Outdoor forecast', '#0d9488', true],
  ['target', 'Target temperature', '#d97706', true],
  ['heat_to', 'Heating target', '#db2777', true]
] as const

/** Show measured temperatures alongside PredHeat's temperature and heating forecasts. */
export default function PredheatChart({ data }: { data: PredheatChartData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [hours, setHours] = useStoredState<number>('predbat-chart-predheat-range', 24, [12, 24, 48])
  const [themeRevision, setThemeRevision] = useState(0)
  const window = useMemo(() => getChartWindow(data.generated_at, hours), [data.generated_at, hours])

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeRevision((revision) => revision + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!canvasRef.current) return
    const styles = getComputedStyle(document.documentElement)
    const text = styles.getPropertyValue('--color-text-secondary').trim() || '#4b5563'
    const grid = styles.getPropertyValue('--color-border').trim() || '#e5e7eb'
    const chart = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        datasets: series.map(([key, label, colour, forecast]) => ({
          label,
          data: pointsInWindow(toChartPoints(data.series[key]), window),
          borderColor: colour,
          backgroundColor: colour,
          borderWidth: 2,
          borderDash: forecast ? [6, 4] : undefined,
          stepped: key === 'target' || key === 'heat_to',
          pointRadius: 0,
          tension: 0.15
        }))
      },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false, parsing: false,
        interaction: { intersect: false, mode: 'nearest' },
        scales: {
          x: {
            type: 'linear', min: window.start, max: window.end, grid: { color: grid },
            ticks: { color: text, maxTicksLimit: 9, callback: (value) => formatChartTimestamp(value, { weekday: 'short', hour: '2-digit', minute: '2-digit' }) }
          },
          y: { grid: { color: grid }, ticks: { color: text, callback: (value) => `${value} °C` } }
        },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            title: (items) => formatChartTimestamp(items[0]?.parsed.x, { weekday: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
            label: (item) => `${item.dataset.label}: ${Number(item.parsed.y).toFixed(1)} °C`
          } }
        }
      }
    })
    return () => chart.destroy()
  }, [data.series, themeRevision, window])

  return (
    <section className="battery-chart-card" aria-labelledby="predheat-chart-heading">
      <div className="battery-chart-header">
        <div><h2 id="predheat-chart-heading">PredHeat temperatures</h2><p>Indoor and outdoor temperatures with PredHeat’s forecast and heating targets. Dashed lines show predictions.</p></div>
        <div className="battery-chart-range" aria-label="PredHeat forecast range">
          {[12, 24, 48].map((range) => <button type="button" className={hours === range ? 'is-active' : ''} aria-pressed={hours === range} onClick={() => setHours(range)} key={range}>{range}h</button>)}
        </div>
      </div>
      <div className="battery-chart-legend" aria-label="Chart series">
        {series.map(([key, label, colour, forecast]) => (
          <span className={`battery-chart-legend-item is-line ${forecast ? 'is-hollow' : ''}`} key={key}>
            <span className="battery-chart-legend-marker" style={{ '--battery-chart-legend-colour': colour } as React.CSSProperties} aria-hidden="true" />{label}
          </span>
        ))}
      </div>
      <div className="battery-chart-canvas"><canvas ref={canvasRef} role="img" aria-label="Indoor and outdoor temperatures, PredHeat forecasts and heating targets" /></div>
    </section>
  )
}
