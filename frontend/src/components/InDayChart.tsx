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

import type { InDayChartData, InDayChartSeries } from '../types/charts'
import { toChartPoints, type ChartPoint } from '../utils/batteryChart'
import { finalValue, getInDayWindow, latestValueAt } from '../utils/inDayChart'

import './BatteryChart.css'

Chart.register(LineController, LineElement, PointElement, LinearScale, Filler, Tooltip, Legend)

type InDayChartProps = {
  data: InDayChartData
}

type InDayLegendItem = {
  key: keyof InDayChartSeries
  label: string
  colour: string
  description: string
}

const legendItems: InDayLegendItem[] = [
  {
    key: 'actual',
    label: 'Actual load',
    colour: 'var(--color-current)',
    description: 'The cumulative energy your home has used since midnight. This line stops at Now.'
  },
  {
    key: 'predicted',
    label: 'Day-ahead forecast',
    colour: '#0891b2',
    description: 'Predbat’s original estimate of today’s cumulative home energy use, before it saw today’s actual usage.'
  },
  {
    key: 'adjusted',
    label: 'Adjusted forecast',
    colour: 'var(--color-export)',
    description: 'Predbat’s updated estimate for the whole day, combining actual usage so far with an adjusted forecast for the remaining hours.'
  },
  {
    key: 'adjustment_factor',
    label: 'Adjustment',
    colour: '#7c3aed',
    description: 'The percentage correction Predbat currently applies to the remaining load forecast. Positive means it expects more load than originally forecast.'
  }
]

function InDayLegend({ item }: { item: InDayLegendItem }) {
  return (
    <span className="battery-chart-legend-item" tabIndex={0}>
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

function formatAdjustment(value: number | null): string {
  if (value === null) {
    return '—'
  }
  return `${value > 0 ? '+' : ''}${value.toFixed(0)}%`
}

function makeInDayOverlayPlugin(now: number, nowColour: string): Plugin<'line'> {
  return {
    id: 'predbat-inday-overlays',
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart
      const nowPixel = Math.min(Math.max(scales.x.getPixelForValue(now), chartArea.left), chartArea.right)

      ctx.save()
      ctx.fillStyle = 'rgba(15, 23, 42, 0.07)'
      ctx.fillRect(chartArea.left, chartArea.top, nowPixel - chartArea.left, chartArea.bottom - chartArea.top)
      ctx.restore()
    },
    afterDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart
      const nowPixel = scales.x.getPixelForValue(now)

      if (nowPixel < chartArea.left || nowPixel > chartArea.right) {
        return
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

/** Compare today's measured cumulative load with Predbat's original and adjusted forecasts. */
export default function InDayChart({ data }: InDayChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [showAdjustment, setShowAdjustment] = useState(false)
  const [themeRevision, setThemeRevision] = useState(0)
  const generatedAt = Date.parse(data.generated_at)
  const actualAll = useMemo(() => toChartPoints(data.series.actual), [data.series.actual])
  const actualPoints = useMemo(
    () => actualAll.filter((point) => point.x <= generatedAt),
    [actualAll, generatedAt]
  )
  const predictedPoints = useMemo(() => toChartPoints(data.series.predicted), [data.series.predicted])
  const adjustedPoints = useMemo(() => toChartPoints(data.series.adjusted), [data.series.adjusted])
  const adjustmentPoints = useMemo(() => toChartPoints(data.series.adjustment_factor), [data.series.adjustment_factor])
  const window = useMemo(
    () => getInDayWindow([actualAll, predictedPoints, adjustedPoints], generatedAt),
    [actualAll, adjustedPoints, generatedAt, predictedPoints]
  )
  const currentActual = latestValueAt(actualPoints, generatedAt)
  const predictedTotal = finalValue(predictedPoints)
  const adjustedTotal = finalValue(adjustedPoints)
  const currentAdjustment = latestValueAt(adjustmentPoints, generatedAt)
  const adjustmentAvailable = adjustmentPoints.length > 0

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
    const adjustedColour = styles.getPropertyValue('--color-export').trim() || '#7c3aed'
    const nowColour = styles.getPropertyValue('--color-warning').trim() || '#f59e0b'
    const datasets: ChartDataset<'line', ChartPoint[]>[] = [
      {
        label: 'Actual load',
        data: actualPoints,
        borderColor: actualColour,
        backgroundColor: actualColour,
        borderWidth: 3,
        pointRadius: 0
      },
      {
        label: 'Day-ahead forecast',
        data: predictedPoints,
        borderColor: '#0891b2',
        backgroundColor: '#0891b2',
        borderWidth: 2,
        borderDash: [6, 4],
        pointRadius: 0
      },
      {
        label: 'Adjusted forecast',
        data: adjustedPoints,
        borderColor: adjustedColour,
        backgroundColor: adjustedColour,
        borderWidth: 3,
        pointRadius: 0
      }
    ]

    if (showAdjustment && adjustmentAvailable) {
      datasets.push({
        label: 'Adjustment',
        data: adjustmentPoints,
        borderColor: '#7c3aed',
        backgroundColor: '#7c3aed',
        borderWidth: 2,
        borderDash: [3, 4],
        pointRadius: 0,
        yAxisID: 'yAdjustment'
      })
    }

    const configuration: ChartConfiguration<'line', ChartPoint[]> = {
      type: 'line',
      data: { datasets },
      plugins: [makeInDayOverlayPlugin(generatedAt, nowColour)],
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
                return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(Number(value))
              }
            }
          },
          y: {
            beginAtZero: true,
            grid: { color: gridColour },
            ticks: { color: textColour, callback: (value) => `${value} kWh` }
          },
          yAdjustment: {
            display: showAdjustment && adjustmentAvailable,
            position: 'right',
            grid: { drawOnChartArea: false },
            ticks: { color: '#7c3aed', callback: (value) => `${value}%` }
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
                const suffix = item.dataset.yAxisID === 'yAdjustment' ? '%' : ' kWh'
                return `${item.dataset.label}: ${Number(item.parsed.y).toFixed(item.dataset.yAxisID === 'yAdjustment' ? 0 : 2)}${suffix}`
              }
            }
          }
        }
      }
    }

    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [actualPoints, adjustedPoints, adjustmentAvailable, adjustmentPoints, data, generatedAt, predictedPoints, showAdjustment, themeRevision, window])

  return (
    <section className="battery-chart-card" aria-labelledby="inday-chart-heading">
      <div className="battery-chart-header">
        <div>
          <h2 id="inday-chart-heading">In-day load adjustment</h2>
          <p>How today’s measured home energy use changes Predbat’s forecast for the rest of the day.</p>
        </div>
      </div>

      <div className="battery-chart-summary">
        <div><span>Actual so far</span><strong>{formatEnergy(currentActual)}</strong></div>
        <div><span>Original day forecast</span><strong>{formatEnergy(predictedTotal)}</strong></div>
        <div><span>Adjusted day forecast</span><strong>{formatEnergy(adjustedTotal)}</strong></div>
        <div>
          <span className="battery-chart-summary-help" tabIndex={0}>
            Current adjustment
            <span className="battery-chart-help-popup" role="tooltip">The correction Predbat applies to the remaining load forecast based on today’s usage so far.</span>
          </span>
          <strong>{formatAdjustment(currentAdjustment)}</strong>
        </div>
      </div>

      <div className="battery-chart-legend" aria-label="Chart series">
        {legendItems
          .filter(({ key }) => key !== 'adjustment_factor' || (showAdjustment && adjustmentAvailable))
          .map((item) => <InDayLegend item={item} key={item.key} />)}
      </div>

      <div className="battery-chart-canvas">
        <canvas ref={canvasRef} role="img" aria-label="Actual and forecast cumulative home energy use today" />
      </div>

      <details className="battery-chart-comparisons">
        <summary>Show adjustment detail</summary>
        <div>
          <label className={adjustmentAvailable ? '' : 'is-unavailable'} title={adjustmentAvailable ? legendItems[3].description : 'No adjustment history is available today'}>
            <input
              type="checkbox"
              checked={showAdjustment}
              disabled={!adjustmentAvailable}
              onChange={() => setShowAdjustment((current) => !current)}
            />
            <span>Adjustment percentage</span>
            {!adjustmentAvailable && <small>No data today</small>}
          </label>
        </div>
      </details>
    </section>
  )
}
