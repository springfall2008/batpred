import { useEffect, useMemo, useRef, useState } from 'react'
import {
  BarController,
  BarElement,
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

import type { SavingsChartData, SavingsChartSeries } from '../types/charts'
import { useStoredState } from '../hooks/useStoredState'
import { pointsInWindow, toChartPoints, type ChartPoint } from '../utils/batteryChart'
import { formatMajorCurrency } from '../utils/currency'
import { alignedSavingsAxisBounds } from '../utils/savingsChart'

import './BatteryChart.css'

Chart.register(BarController, BarElement, LineController, LineElement, PointElement, LinearScale, Tooltip, Legend)

type SavingsLegendItem = {
  key: keyof SavingsChartSeries
  label: string
  colour: string
  description: string
  bar?: boolean
}

const legendItems: SavingsLegendItem[] = [
  {
    key: 'daily_predbat',
    label: 'Predbat saving',
    colour: '#f59e0b',
    description: 'Money saved each day compared with the configured simple charging baseline.',
    bar: true
  },
  {
    key: 'daily_pv_battery',
    label: 'Solar and battery saving',
    colour: '#0891b2',
    description: 'Money saved each day compared with having no solar panels or battery.',
    bar: true
  },
  {
    key: 'daily_cost',
    label: 'Actual cost',
    colour: '#dc2626',
    description: 'The actual net energy cost for each day.',
    bar: true
  },
  {
    key: 'total_predbat',
    label: 'Total Predbat saving',
    colour: '#ca8a04',
    description: 'Running total saved by Predbat compared with the configured simple charging baseline.'
  },
  {
    key: 'total_pv_battery',
    label: 'Total solar and battery saving',
    colour: '#059669',
    description: 'Running total saved by the solar and battery system compared with grid-only energy use.'
  }
]

function latestValue(points: ChartPoint[]): number | null {
  return points.at(-1)?.y ?? null
}

function SavingsLegend({ item }: { item: SavingsLegendItem }) {
  return (
    <span className={`battery-chart-legend-item ${item.bar ? 'is-square' : 'is-line'}`} tabIndex={0}>
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

/** Render daily costs and savings with their cumulative totals. */
export default function SavingsChart({ data }: { data: SavingsChartData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [rangeDays, setRangeDays] = useStoredState<number>('predbat-chart-savings-range', 28, [7, 14, 28])
  const [themeRevision, setThemeRevision] = useState(0)
  const window = useMemo(() => ({
    start: Date.parse(data.generated_at) - rangeDays * 24 * 60 * 60 * 1000,
    end: Date.parse(data.generated_at)
  }), [data.generated_at, rangeDays])
  const points = useMemo(() => Object.fromEntries(
    Object.entries(data.series).map(([key, series]) => [key, pointsInWindow(toChartPoints(series), window)])
  ) as Record<keyof SavingsChartSeries, ChartPoint[]>, [data.series, window])

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
    const dailyDatasets: ChartDataset<'bar' | 'line', ChartPoint[]>[] = legendItems.filter((item) => item.bar).map((item) => ({
      type: 'bar',
      label: item.label,
      data: points[item.key],
      borderColor: item.colour,
      backgroundColor: `${item.colour}b3`,
      borderWidth: 1,
      order: 2,
      yAxisID: 'y'
    }))
    const totalDatasets: ChartDataset<'bar' | 'line', ChartPoint[]>[] = legendItems.filter((item) => !item.bar).map((item) => ({
      type: 'line',
      label: item.label,
      data: points[item.key],
      borderColor: item.colour,
      backgroundColor: item.colour,
      borderWidth: 3,
      pointRadius: points[item.key].length === 1 ? 4 : 0,
      pointHoverRadius: 4,
      showLine: true,
      spanGaps: true,
      tension: 0.2,
      order: 1,
      yAxisID: 'y1'
    }))
    const dailyValues = dailyDatasets.flatMap((dataset) => dataset.data.map((point) => point.y))
    const totalValues = totalDatasets.flatMap((dataset) => dataset.data.map((point) => point.y))
    const axisBounds = alignedSavingsAxisBounds(dailyValues, totalValues)
    const configuration: ChartConfiguration<'bar' | 'line', ChartPoint[]> = {
      type: 'bar',
      data: { datasets: [...dailyDatasets, ...totalDatasets] },
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
              callback(value) {
                return new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' }).format(Number(value))
              }
            }
          },
          y: {
            min: axisBounds.daily.min,
            max: axisBounds.daily.max,
            grid: { color: gridColour },
            ticks: { color: textColour, callback: (value) => formatMajorCurrency(Number(value), data.currency_symbol) },
            title: { display: true, text: 'Daily', color: textColour }
          },
          y1: {
            position: 'right',
            min: axisBounds.total.min,
            max: axisBounds.total.max,
            grid: { drawOnChartArea: false },
            ticks: { color: textColour, callback: (value) => formatMajorCurrency(Number(value), data.currency_symbol) },
            title: { display: true, text: 'Running total', color: textColour }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title(items) {
                return new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short' }).format(Number(items[0]?.parsed.x))
              },
              label(item) {
                return `${item.dataset.label}: ${formatMajorCurrency(Number(item.parsed.y), data.currency_symbol)}`
              }
            }
          }
        }
      }
    }

    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [data.currency_symbol, points, themeRevision, window])

  return (
    <section className="battery-chart-card" aria-labelledby="savings-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="savings-chart-heading">Savings</h2>
          <p>Daily energy costs and the savings delivered by Predbat, solar and battery storage.</p>
        </div>
        <div className="battery-chart-range" aria-label="History range">
          {[7, 14, 28].map((days) => (
            <button type="button" className={rangeDays === days ? 'is-active' : ''} aria-pressed={rangeDays === days} onClick={() => setRangeDays(days)} key={days}>
              {days}d
            </button>
          ))}
        </div>
      </div>

      <div className="battery-chart-summary">
        <div><span>Latest Predbat saving</span><strong>{formatMajorCurrency(latestValue(points.daily_predbat) ?? 0, data.currency_symbol)}</strong></div>
        <div><span>Latest solar and battery saving</span><strong>{formatMajorCurrency(latestValue(points.daily_pv_battery) ?? 0, data.currency_symbol)}</strong></div>
        <div><span>Total Predbat saving</span><strong>{formatMajorCurrency(latestValue(points.total_predbat) ?? 0, data.currency_symbol)}</strong></div>
        <div><span>Total solar and battery saving</span><strong>{formatMajorCurrency(latestValue(points.total_pv_battery) ?? 0, data.currency_symbol)}</strong></div>
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {legendItems.map((item) => <SavingsLegend item={item} key={item.key} />)}
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Daily and cumulative energy savings" />
      </div>
    </section>
  )
}
