import { useCallback, useEffect, useState } from 'react'

import BatteryChart from '../components/BatteryChart'
import BatteryDegradationChart from '../components/BatteryDegradationChart'
import CarbonChart from '../components/CarbonChart'
import CostChart from '../components/CostChart'
import InDayChart from '../components/InDayChart'
import MarginalCostsChart from '../components/MarginalCostsChart'
import LoadMlChart from '../components/LoadMlChart'
import PowerChart from '../components/PowerChart'
import RatesChart from '../components/RatesChart'
import SavingsChart from '../components/SavingsChart'
import SolarChart from '../components/SolarChart'

import type { ChartData } from '../types/charts'
import type { Plan } from '../types/plan'
import { useStoredState } from '../hooks/useStoredState'

import './ChartsPage.css'

type ChartsPageProps = {
  plan: Plan
  loadMlEnabled: boolean
}

const chartNames = ['battery', 'power', 'cost', 'rates', 'inday', 'solar', 'savings', 'degradation', 'marginal', 'carbon', 'loadml'] as const
type ChartName = typeof chartNames[number]

const chartLabels: Record<ChartName, string> = {
  battery: 'Battery',
  power: 'Power',
  cost: 'Cost',
  rates: 'Rates',
  inday: 'In-day',
  solar: 'Solar',
  savings: 'Savings',
  degradation: 'Degradation',
  marginal: 'Marginal costs',
  carbon: 'CO₂',
  loadml: 'Load ML'
}

/** Fetch and display Predbat's modern charts. */
export default function ChartsPage({ plan, loadMlEnabled }: ChartsPageProps) {
  const visibleChartNames = chartNames.filter((chart) => (chart !== 'carbon' || plan.carbon_enable) && (chart !== 'loadml' || loadMlEnabled))
  const [storedSelectedChart, setSelectedChart] = useStoredState<ChartName>(
    'predbat-chart-selected',
    'battery',
    visibleChartNames
  )
  const selectedChart = visibleChartNames.includes(storedSelectedChart) ? storedSelectedChart : 'battery'
  const [data, setData] = useState<ChartData | null>(null)
  const [error, setError] = useState<string | null>(null)

  const fetchChart = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await fetch(`./api/chart_data?chart=${selectedChart}`, {
        cache: 'no-store',
        signal
      })

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }

      const chartData = (await response.json()) as ChartData
      setData(chartData)
      setError(null)
    } catch (fetchError) {
      if (fetchError instanceof DOMException && fetchError.name === 'AbortError') {
        return
      }

      const detail = fetchError instanceof Error ? fetchError.message : 'Unknown error'
      setError(`${chartLabels[selectedChart]} chart data could not be loaded. ${detail}`)
    }
  }, [selectedChart])

  useEffect(() => {
    const controller = new AbortController()
    const initialTimer = window.setTimeout(() => fetchChart(controller.signal), 0)
    const refreshTimer = window.setInterval(() => fetchChart(), 5 * 60 * 1000)

    return () => {
      controller.abort()
      window.clearTimeout(initialTimer)
      window.clearInterval(refreshTimer)
    }
  }, [fetchChart])

  return (
    <div className="charts-page">
      <header className="charts-page-header">
        <div>
          <h1>Charts</h1>
          <p>Clear views of Predbat's history, forecast and scheduled actions.</p>
        </div>

        <div className="charts-page-tabs" aria-label="Chart type">
          {visibleChartNames.map((chart) => (
            <button
              type="button"
              className={selectedChart === chart ? 'is-active' : ''}
              aria-pressed={selectedChart === chart}
              onClick={() => {
                setData(null)
                setError(null)
                setSelectedChart(chart)
              }}
              key={chart}
            >
              {chartLabels[chart]}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <div className="charts-page-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => fetchChart()}>Retry</button>
        </div>
      )}

      {!data && !error && (
        <div className="charts-page-loading">Loading {selectedChart} chart…</div>
      )}

      {data && !data.ready && (
        <div className="charts-page-loading">Predbat is still preparing the {selectedChart} forecast…</div>
      )}

      {data?.ready && data.chart === 'battery' && <BatteryChart data={data} plan={plan} />}
      {data?.ready && data.chart === 'power' && <PowerChart data={data} plan={plan} />}
      {data?.ready && data.chart === 'cost' && <CostChart data={data} plan={plan} />}
      {data?.ready && data.chart === 'rates' && <RatesChart data={data} plan={plan} />}
      {data?.ready && data.chart === 'inday' && <InDayChart data={data} />}
      {data?.ready && data.chart === 'solar' && <SolarChart data={data} />}
      {data?.ready && data.chart === 'savings' && <SavingsChart data={data} />}
      {data?.ready && data.chart === 'degradation' && <BatteryDegradationChart data={data} />}
      {data?.ready && data.chart === 'marginal' && <MarginalCostsChart data={data} />}
      {plan.carbon_enable && data?.ready && data.chart === 'carbon' && <CarbonChart data={data} />}
      {loadMlEnabled && data?.ready && data.chart === 'loadml' && <LoadMlChart data={data} />}
    </div>
  )
}
