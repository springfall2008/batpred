import { useCallback, useEffect, useRef, useState } from 'react'
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
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faRotate, faScaleBalanced } from '@fortawesome/free-solid-svg-icons'

import PlanTable from '../components/PlanTable'
import type { Plan, PlanOverrides } from '../types/plan'
import { formatMajorCurrency } from '../utils/currency'
import { toChartPoints, type ChartPoint } from '../utils/batteryChart'

import './ComparePage.css'

Chart.register(BarController, BarElement, LineController, LineElement, PointElement, LinearScale, Tooltip, Legend)

type TariffComparison = {
  id: string
  name: string
  date: string
  true_cost: number | null
  cost: number | null
  cost10: number | null
  average7: number | null
  average_days: number
  export_kwh: number | null
  import_kwh: number | null
  soc: number | null
  final_iboost: number | null
  final_carbon_g: number | null
  best: boolean
  existing: boolean
  history: Record<string, number>
  rolling7: Record<string, number>
  plan: Plan | null
}

type CompareData = {
  active: boolean
  configured: boolean
  ready: boolean
  generated_at: string
  currency_symbol: string
  show_iboost: boolean
  show_carbon: boolean
  actual: Record<string, number>
  actual_no_car: Record<string, number>
  tariffs: TariffComparison[]
}

const colours = ['#2563eb', '#f59e0b', '#059669', '#dc2626', '#7c3aed', '#0891b2', '#db2777', '#65a30d']
const emptyOverrides: PlanOverrides = {
  manual_charge_times: [], manual_export_times: [], manual_freeze_charge_times: [],
  manual_freeze_export_times: [], manual_demand_times: [], manual_import_rates: [],
  manual_export_rates: [], manual_soc: []
}

function value(value: number | null, suffix = '') {
  return value === null || value === undefined ? '—' : `${value.toFixed(2)}${suffix}`
}

function ComparisonChart({ data, rolling = false }: { data: CompareData, rolling?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!canvasRef.current) return
    const styles = getComputedStyle(document.documentElement)
    const textColour = styles.getPropertyValue('--color-text-secondary').trim() || '#4b5563'
    const gridColour = styles.getPropertyValue('--color-border').trim() || '#e5e7eb'
    const datasets: ChartDataset<'bar' | 'line', ChartPoint[]>[] = data.tariffs
      .map((tariff, index) => ({
        type: rolling ? 'line' as const : 'bar' as const,
        label: tariff.name,
        data: toChartPoints(rolling ? tariff.rolling7 : tariff.history),
        borderColor: colours[index % colours.length],
        backgroundColor: `${colours[index % colours.length]}99`,
        borderWidth: rolling ? 2 : 1,
        pointRadius: 2,
        tension: 0.2
      }))
      .filter((dataset) => dataset.data.length)

    if (!rolling) {
      datasets.push({ type: 'line', label: 'Actual', data: toChartPoints(data.actual), borderColor: '#111827', backgroundColor: '#111827', borderWidth: 2, pointRadius: 2, tension: 0.2 })
      if (Object.keys(data.actual_no_car).length) {
        datasets.push({ type: 'line', label: 'Actual without car', data: toChartPoints(data.actual_no_car), borderColor: '#64748b', backgroundColor: '#64748b', borderWidth: 2, borderDash: [5, 4], pointRadius: 2, tension: 0.2 })
      }
    }

    const configuration: ChartConfiguration<'bar' | 'line', ChartPoint[]> = {
      type: rolling ? 'line' : 'bar',
      data: { datasets },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        interaction: { intersect: false, mode: 'index' },
        plugins: {
          legend: { labels: { color: textColour, usePointStyle: true, padding: 22 } },
          tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${formatMajorCurrency(context.parsed.y ?? 0, data.currency_symbol)}` } }
        },
        scales: {
          x: {
            type: 'linear',
            grid: { color: gridColour },
            ticks: { color: textColour, callback: (tick) => new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' }).format(Number(tick)) }
          },
          y: {
            grid: { color: gridColour },
            ticks: { color: textColour, callback: (tick) => formatMajorCurrency(Number(tick), data.currency_symbol) }
          }
        }
      }
    }
    const chart = new Chart(canvasRef.current, configuration)
    return () => chart.destroy()
  }, [data, rolling])

  return <div className="compare-chart"><canvas ref={canvasRef} /></div>
}

/** Compare configured tariffs using Predbat's stored scenario results. */
export default function ComparePage() {
  const [data, setData] = useState<CompareData | null>(null)
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)

  const load = useCallback(async () => {
    try {
      const response = await fetch('./api/compare', { cache: 'no-store' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      setData(await response.json() as CompareData)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to load tariff comparisons')
    }
  }, [])

  useEffect(() => {
    const initialTimer = window.setTimeout(load, 0)
    const timer = window.setInterval(load, data?.active ? 5000 : 5 * 60 * 1000)
    return () => {
      window.clearTimeout(initialTimer)
      window.clearInterval(timer)
    }
  }, [data?.active, load])

  async function runComparison() {
    setStarting(true)
    setError('')
    try {
      const response = await fetch('./compare', { method: 'POST', body: new URLSearchParams({ run: 'run' }) })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to start tariff comparison')
    } finally {
      setStarting(false)
    }
  }

  return (
    <section className="compare-page">
      <header className="compare-page-header">
        <div><h1>Tariff comparison</h1><p>Compare Predbat's forecast cost and battery plan across your configured tariffs.</p></div>
        <button type="button" disabled={!data?.configured || data?.active || starting} onClick={runComparison}>
          <FontAwesomeIcon icon={data?.active ? faRotate : faScaleBalanced} spin={data?.active} />
          {data?.active || starting ? 'Comparing…' : 'Compare now'}
        </button>
      </header>

      {error && <div className="compare-message is-error" role="alert">Unable to update comparisons: {error}</div>}
      {!data && !error && <div className="compare-message">Loading tariff comparisons…</div>}
      {data && !data.configured && <div className="compare-message">No tariffs are configured. Add entries to <code>compare_list</code> in apps.yaml.</div>}
      {data?.configured && !data.ready && <div className="compare-message">No comparison results yet. Select Compare now to create them.</div>}

      {data?.ready && (
        <>
          <div className="compare-table-card">
            <div className="compare-table-scroll"><table><thead><tr>
              <th>Tariff</th><th>True cost</th><th>Forecast cost</th><th>10% case</th><th>7-day average</th><th>Import</th><th>Export</th><th>Final battery</th>
              {data.show_iboost && <th>iBoost</th>}{data.show_carbon && <th>CO₂</th>}<th>Result</th>
            </tr></thead><tbody>{data.tariffs.map((tariff) => (
              <tr key={tariff.id} className={tariff.best ? 'is-best' : ''}>
                <th><a href={`#compare-${tariff.id}`}>{tariff.name}</a><small>{tariff.id}</small></th>
                <td>{tariff.true_cost === null ? '—' : formatMajorCurrency(tariff.true_cost, data.currency_symbol)}</td>
                <td>{tariff.cost === null ? '—' : formatMajorCurrency(tariff.cost, data.currency_symbol)}</td>
                <td>{tariff.cost10 === null ? '—' : formatMajorCurrency(tariff.cost10, data.currency_symbol)}</td>
                <td>{tariff.average7 === null ? '—' : `${formatMajorCurrency(tariff.average7, data.currency_symbol)} (${tariff.average_days}d)`}</td>
                <td>{value(tariff.import_kwh, ' kWh')}</td><td>{value(tariff.export_kwh, ' kWh')}</td><td>{value(tariff.soc, ' kWh')}</td>
                {data.show_iboost && <td>{value(tariff.final_iboost, ' kWh')}</td>}{data.show_carbon && <td>{value(tariff.final_carbon_g, ' g')}</td>}
                <td><span className="compare-badges">{tariff.best && <b>Best</b>}{tariff.existing && <i>Current</i>}</span></td>
              </tr>
            ))}</tbody></table></div>
          </div>

          <div className="compare-charts">
            <article><h2>Daily true cost</h2><p>Predbat's modelled cost beside your actual daily cost.</p><ComparisonChart data={data} /></article>
            <article><h2>7-day rolling average</h2><p>The average forecast cost smooths out unusual individual days.</p><ComparisonChart data={data} rolling /></article>
          </div>

          <div className="compare-plans">{data.tariffs.map((tariff) => (
            <details id={`compare-${tariff.id}`} key={tariff.id} open={tariff.best}>
              <summary>{tariff.name}{tariff.best && <span>Best result</span>}</summary>
              {tariff.plan ? <PlanTable plan={tariff.plan} overrides={emptyOverrides} debugEnabled={false} readOnly onOverrideSubmitted={() => {}} /> : <p>No detailed plan is available yet.</p>}
            </details>
          ))}</div>
        </>
      )}
    </section>
  )
}
