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
import type { LoadMlChartData } from '../types/charts'
import { pointsInWindow, toChartPoints, type ChartPoint } from '../utils/batteryChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Tooltip, Legend)

type View = 'energy' | 'power'

const descriptions: Record<string, string> = {
  'Actual load': 'The load measured by Predbat.',
  'Prediction made 1 hour earlier': 'What the load model predicted one hour before each reading.',
  'Prediction made 8 hours earlier': 'What the load model predicted eight hours before each reading.',
  'Current ML forecast': 'Predbat’s current machine-learned load forecast.',
  'Actual load, excluding car': 'Measured house load with configured car charging removed to match what the model predicts.',
  'Car charging': 'Power used by the configured car charger.',
  'PV actual': 'Solar power measured by the inverter.',
  'PV forecast': 'Predbat’s current solar power forecast.',
  Temperature: 'Predbat’s temperature forecast, shown on the right axis.',
  'Learned load history': 'The load model’s estimate recorded at each historical point.',
  'History predicted 1 hour earlier': 'The load model’s estimate made one hour before each historical point.',
  'History predicted 8 hours earlier': 'The load model’s estimate made eight hours before each historical point.'
}

function marker(now: number, colour: string): Plugin<'line'> {
  return {
    id: 'predbat-load-ml-now',
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

function latest(points: ChartPoint[], at: number): number | null {
  return points.reduce<number | null>((value, point) => point.x <= at ? point.y : value, null)
}

/** Show Predbat's machine-learned load history and forecast. */
export default function LoadMlChart({ data }: { data: LoadMlChartData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [view, setView] = useStoredState<View>('predbat-chart-load-ml-view', 'energy', ['energy', 'power'])
  const [days, setDays] = useStoredState<number>('predbat-chart-load-ml-range', 7, [1, 3, 7])
  const [history, setHistory] = useStoredState<'show' | 'hide'>('predbat-chart-load-ml-history', 'hide', ['show', 'hide'])
  const [themeRevision, setThemeRevision] = useState(0)
  const now = Date.parse(data.generated_at)
  const window = useMemo(() => ({ start: now - days * 86400000, end: now + 48 * 3600000 }), [days, now])
  const points = useMemo(() => Object.fromEntries(
    Object.entries(data.series).map(([key, series]) => [key, pointsInWindow(toChartPoints(series), window)])
  ) as Record<keyof LoadMlChartData['series'], ChartPoint[]>, [data.series, window])
  const useAdjustedLoad = data.car_configured && points.power_actual_less_car.length > 0
  const showCar = data.car_configured && points.car_power.length > 0

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeRevision((revision) => revision + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!canvasRef.current) return
    const styles = getComputedStyle(document.documentElement)
    const colours = {
      actual: styles.getPropertyValue('--color-current').trim() || '#2563eb',
      forecast: '#dc2626', h1: '#d97706', h8: '#7c3aed',
      adjusted: styles.getPropertyValue('--color-export').trim() || '#16a34a',
      car: '#db2777', pv: styles.getPropertyValue('--color-flow-solar').trim() || '#ca8a04',
      pvForecast: '#f59e0b', temperature: '#65a30d'
    }
    const definitions = view === 'energy'
      ? [
          ['Actual load', points.energy_actual, colours.actual],
          ['Prediction made 1 hour earlier', points.energy_predicted_h1, colours.h1],
          ['Prediction made 8 hours earlier', points.energy_predicted_h8, colours.h8],
          ['Current ML forecast', points.energy_forecast, colours.forecast]
        ] as const
      : [
          [useAdjustedLoad ? 'Actual load, excluding car' : 'Actual load', useAdjustedLoad ? points.power_actual_less_car : points.power_actual, colours.actual],
          ['Current ML forecast', points.power_forecast, colours.forecast],
          ...(showCar ? [['Car charging', points.car_power, colours.car] as const] : []),
          ['PV actual', points.pv_actual, colours.pv],
          ['PV forecast', points.pv_forecast, colours.pvForecast],
          ['Temperature', points.temperature, colours.temperature],
          ...(history === 'show' ? [
            ['Learned load history', points.power_history, colours.forecast] as const,
            ['History predicted 1 hour earlier', points.power_history_h1, colours.h1] as const,
            ['History predicted 8 hours earlier', points.power_history_h8, colours.h8] as const
          ] : [])
        ] as const
    const datasets: ChartDataset<'line', ChartPoint[]>[] = definitions
      .filter(([, series]) => series.length)
      .map(([label, series, colour]) => ({
        label,
        data: series,
        borderColor: colour,
        backgroundColor: colour,
        borderWidth: label === 'Actual load' || label === 'Actual load, excluding car' || label === 'Current ML forecast' ? 3 : 2,
        borderDash: label.includes('predicted') || label === 'PV forecast' ? [6, 4] : undefined,
        pointRadius: 0,
        tension: 0.2,
        yAxisID: label === 'Temperature' ? 'temperature' : 'y'
      }))
    const text = styles.getPropertyValue('--color-text-secondary').trim() || '#4b5563'
    const grid = styles.getPropertyValue('--color-border').trim() || '#e5e7eb'
    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [marker(now, styles.getPropertyValue('--color-warning').trim() || '#f59e0b')],
      options: {
        animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
        interaction: { intersect: false, mode: 'nearest' },
        scales: {
          x: { type: 'linear', min: window.start, max: window.end, grid: { color: grid }, ticks: { color: text, maxTicksLimit: 9, callback: (value) => new Intl.DateTimeFormat(undefined, days === 1 ? { hour: '2-digit', minute: '2-digit' } : { weekday: 'short', day: 'numeric', hour: '2-digit' }).format(Number(value)) } },
          y: { beginAtZero: true, grid: { color: grid }, ticks: { color: text, callback: (value) => `${value} ${view === 'energy' ? 'kWh' : 'kW'}` } },
          temperature: { display: view === 'power', position: 'right', grid: { drawOnChartArea: false }, ticks: { color: colours.temperature, callback: (value) => `${value} °C` } }
        },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            title: (items) => new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(Number(items[0]?.parsed.x)),
            label: (item) => `${item.dataset.label}: ${Number(item.parsed.y).toFixed(2)} ${item.dataset.yAxisID === 'temperature' ? '°C' : view === 'energy' ? 'kWh' : 'kW'}`
          } }
        }
      }
    }
    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [days, history, now, points, showCar, themeRevision, useAdjustedLoad, view, window])

  const legendItems = view === 'energy'
    ? [['Actual load', 'var(--color-current)'], ['Prediction made 1 hour earlier', '#d97706'], ['Prediction made 8 hours earlier', '#7c3aed'], ['Current ML forecast', '#dc2626']]
    : [[useAdjustedLoad ? 'Actual load, excluding car' : 'Actual load', 'var(--color-current)'], ['Current ML forecast', '#dc2626'], ...(showCar ? [['Car charging', '#db2777']] : []), ['PV actual', 'var(--color-flow-solar)'], ['PV forecast', '#f59e0b'], ['Temperature', '#65a30d'], ...(history === 'show' ? [['Learned load history', '#dc2626'], ['History predicted 1 hour earlier', '#d97706'], ['History predicted 8 hours earlier', '#7c3aed']] : [])]
  const summary = view === 'energy'
    ? [['Actual now', latest(points.energy_actual, now), 'kWh'], ['Forecast now', latest(points.energy_forecast, now), 'kWh']]
    : [['Load now', latest(useAdjustedLoad ? points.power_actual_less_car : points.power_actual, now), 'kW'], ['Forecast now', latest(points.power_forecast, now), 'kW'], ['PV now', latest(points.pv_actual, now), 'kW'], ['Temperature', latest(points.temperature, now), '°C']]

  return (
    <section className="battery-chart-card" aria-labelledby="load-ml-chart-heading">
      <div className="battery-chart-header">
        <div><h2 id="load-ml-chart-heading">Machine-learned load</h2><p>Compare Predbat’s learned household demand with measured load and its current forecast.</p></div>
        <div className="solar-chart-controls">
          <div className="battery-chart-range" aria-label="Load ML measurement">
            <button type="button" className={view === 'energy' ? 'is-active' : ''} aria-pressed={view === 'energy'} onClick={() => setView('energy')}>Energy</button>
            <button type="button" className={view === 'power' ? 'is-active' : ''} aria-pressed={view === 'power'} onClick={() => setView('power')}>Power</button>
          </div>
          <div className="battery-chart-range" aria-label="Load ML chart range">
            {[1, 3, 7].map((range) => <button type="button" className={days === range ? 'is-active' : ''} aria-pressed={days === range} onClick={() => setDays(range)} key={range}>{range}d</button>)}
          </div>
        </div>
      </div>
      <div className="battery-chart-summary">
        {summary.map(([label, value, unit]) => <div key={String(label)}><span>{label}</span><strong>{value === null ? '—' : `${Number(value).toFixed(2)} ${unit}`}</strong></div>)}
      </div>
      <div className="battery-chart-legend" aria-label="Chart series">
        {legendItems.map(([label, colour]) => <span className="battery-chart-legend-item is-line" tabIndex={0} key={label}><span className="battery-chart-legend-marker" style={{ '--battery-chart-legend-colour': colour } as React.CSSProperties} aria-hidden="true" /><span>{label}</span><span className="battery-chart-help-popup" role="tooltip">{descriptions[label]}</span></span>)}
      </div>
      <div className="battery-chart-canvas"><canvas ref={canvasRef} role="img" aria-label="Machine-learned load history and forecast" /></div>
      {view === 'power' && <details className="battery-chart-comparisons"><summary>Compare earlier predictions</summary><div><label><input type="checkbox" checked={history === 'show'} onChange={() => setHistory(history === 'show' ? 'hide' : 'show')} /><span>Show predictions recorded at the time</span></label></div></details>}
    </section>
  )
}
