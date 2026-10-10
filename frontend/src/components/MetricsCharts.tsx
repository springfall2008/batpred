import { useEffect, useRef, useState } from 'react'
import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  DoughnutController,
  LinearScale,
  Tooltip
} from 'chart.js'

import type { DashboardMetrics } from '../types/metrics'
import { metricEnergyValues, metricPowerValues } from '../utils/metrics'

Chart.register(ArcElement, BarController, BarElement, CategoryScale, DoughnutController, LinearScale, Tooltip)

function cssColour(styles: CSSStyleDeclaration, name: string, fallback: string): string {
  return styles.getPropertyValue(name).trim() || fallback
}

/** Display the live battery and power charts plus today's cumulative energy totals. */
export default function MetricsCharts({ metrics }: { metrics: DashboardMetrics }) {
  const socRef = useRef<HTMLCanvasElement>(null)
  const powerRef = useRef<HTMLCanvasElement>(null)
  const energyRef = useRef<HTMLCanvasElement>(null)
  const [themeRevision, setThemeRevision] = useState(0)

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeRevision((revision) => revision + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!socRef.current || !powerRef.current || !energyRef.current) return

    const styles = getComputedStyle(document.documentElement)
    const colours = {
      charge: cssColour(styles, '--color-charge', '#2563eb'),
      discharge: cssColour(styles, '--color-freeze-charge', '#0891b2'),
      load: cssColour(styles, '--color-flow-home', '#16a34a'),
      solar: cssColour(styles, '--color-flow-solar', '#f59e0b'),
      import: cssColour(styles, '--color-danger', '#dc2626'),
      export: cssColour(styles, '--color-export', '#7c3aed'),
      battery: cssColour(styles, '--color-battery-high', '#22c55e'),
      track: cssColour(styles, '--color-battery-track', '#e5e7eb'),
      border: cssColour(styles, '--color-border', '#e5e7eb'),
      text: cssColour(styles, '--color-text', '#111827'),
      muted: cssColour(styles, '--color-text-muted', '#6b7280')
    }
    const socPercent = Math.min(100, Math.max(0, metrics.battery_soc_percent || 0))
    const powerValues = metricPowerValues(metrics)
    const energyValues = metricEnergyValues(metrics)
    const barColours = [colours.charge, colours.discharge, colours.load, colours.solar, colours.import, colours.export]
    const energyColours = [colours.load, colours.solar, colours.import, colours.export]

    const socChart = new Chart(socRef.current, {
      type: 'doughnut',
      data: {
        labels: ['Battery charge', 'Available capacity'],
        datasets: [{ data: [socPercent, 100 - socPercent], backgroundColor: [colours.battery, colours.track], borderWidth: 0 }]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        cutout: '72%',
        plugins: { legend: { display: false }, tooltip: { enabled: false } }
      },
      plugins: [{
        id: 'metrics-battery-centre',
        afterDraw(chart) {
          const { ctx, chartArea } = chart
          const x = (chartArea.left + chartArea.right) / 2
          const y = (chartArea.top + chartArea.bottom) / 2
          ctx.save()
          ctx.textAlign = 'center'
          ctx.textBaseline = 'middle'
          ctx.fillStyle = colours.text
          ctx.font = '700 1.8rem Inter, sans-serif'
          ctx.fillText(`${socPercent.toFixed(0)}%`, x, y - 11)
          ctx.fillStyle = colours.muted
          ctx.font = '0.82rem Inter, sans-serif'
          ctx.fillText(`${metrics.battery_soc_kwh.toFixed(1)} / ${metrics.battery_max_kwh.toFixed(1)} kWh`, x, y + 17)
          ctx.restore()
        }
      }]
    })

    const powerChart = new Chart(powerRef.current, {
      type: 'bar',
      data: {
        labels: ['Battery charge', 'Battery discharge', 'Load', 'PV', 'Grid import', 'Grid export'],
        datasets: [{ data: powerValues, backgroundColor: barColours, borderRadius: 4 }]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (item) => `${Number(item.parsed.x).toFixed(2)} kW` } }
        },
        scales: {
          x: {
            beginAtZero: true,
            grid: { color: colours.border },
            ticks: { color: colours.muted },
            title: { display: true, text: 'kW', color: colours.muted }
          },
          y: { grid: { display: false }, ticks: { color: colours.text } }
        }
      }
    })

    const energyChart = new Chart(energyRef.current, {
      type: 'bar',
      data: {
        labels: ['Load', 'PV generation', 'Grid import', 'Grid export'],
        datasets: [{ data: energyValues, backgroundColor: energyColours, borderRadius: 4 }]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (item) => `${Number(item.parsed.x).toFixed(1)} kWh` } }
        },
        scales: {
          x: {
            beginAtZero: true,
            grid: { color: colours.border },
            ticks: { color: colours.muted },
            title: { display: true, text: 'kWh', color: colours.muted }
          },
          y: { grid: { display: false }, ticks: { color: colours.text } }
        }
      }
    })

    return () => {
      socChart.destroy()
      powerChart.destroy()
      energyChart.destroy()
    }
  }, [metrics, themeRevision])

  return (
    <>
      <div className="metrics-section">
        <h3>Battery status</h3>
        <div className="metrics-chart-grid">
          <div className="metrics-chart-card metrics-chart-card-soc">
            <h4>State of charge</h4>
            <div className="metrics-chart-canvas"><canvas ref={socRef} role="img" aria-label="Battery state of charge" /></div>
          </div>
          <div className="metrics-chart-card">
            <h4>Live power</h4>
            <div className="metrics-chart-canvas"><canvas ref={powerRef} role="img" aria-label="Live battery, home, solar and grid power" /></div>
          </div>
        </div>
      </div>

      <div className="metrics-section">
        <h3>Energy today</h3>
        <p className="metrics-section-description">Running totals since midnight.</p>
        <div className="metrics-chart-card">
          <div className="metrics-chart-canvas metrics-chart-canvas-energy"><canvas ref={energyRef} role="img" aria-label="Energy totals for today" /></div>
        </div>
      </div>
    </>
  )
}
