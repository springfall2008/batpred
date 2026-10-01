import { useEffect, useState } from 'react'

import type { DashboardMetrics } from '../types/metrics'
import { formatMajorCurrency } from '../utils/currency'
import MetricsCharts from './MetricsCharts'
import { formatMetricAge, metricVersion, sumMetricValues } from '../utils/metrics'

import './MetricsPanel.css'

function formatUptime(value: string | null, currentTime: Date | null) {
  if (!value || !currentTime) return 'Unknown'

  const minutes = Math.max(0, Math.floor((currentTime.getTime() - new Date(value).getTime()) / 60000))
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  const mins = minutes % 60

  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

function MetricCard({ label, value, detail, tone }: { label: string; value: string; detail?: string; tone?: 'good' | 'warning' | 'bad' }) {
  return (
    <div className="metrics-card">
      <span>{label}</span>
      <strong className={tone ? `is-${tone}` : ''}>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  )
}

/** Display operational metrics that complement the main dashboard cards. */
export default function MetricsPanel({ lastStarted }: { lastStarted: string | null }) {
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null)
  const [error, setError] = useState(false)
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null)

  useEffect(() => {
    let active = true

    async function refresh() {
      try {
        const response = await fetch('./metrics/json', { cache: 'no-store' })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = await response.json() as DashboardMetrics
        if (active) {
          setMetrics(data)
          setRefreshedAt(new Date())
          setError(false)
        }
      } catch {
        if (active) setError(true)
      }
    }

    refresh()
    const timer = window.setInterval(refresh, 30_000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

  if (!metrics) {
    return error ? <section className="metrics-panel metrics-panel-message">Dashboard metrics are unavailable.</section> : null
  }

  const errors = sumMetricValues(metrics.errors_total)
  const services = Object.entries(metrics.api_services).sort(([first], [second]) => first.localeCompare(second))
  const conflicts = [...metrics.control_conflicts_events].reverse()
  const quotaPercent = metrics.solcast_api_limit > 0 ? Math.min(100, metrics.solcast_api_used / metrics.solcast_api_limit * 100) : 0

  return (
    <section className="metrics-panel" aria-labelledby="metrics-heading">
      <header className="metrics-panel-header">
        <div>
          <h2 id="metrics-heading">Performance and health</h2>
          <p>Energy, cost and service metrics collected by Predbat.</p>
        </div>
        <small>{error ? 'Refresh failed · showing previous values' : refreshedAt ? `Updated ${refreshedAt.toLocaleTimeString()}` : ''}</small>
      </header>

      <div className="metrics-section">
        <h3>System health</h3>
        <div className="metrics-grid">
          <MetricCard label="Predbat" value={Object.values(metrics.up).some(Boolean) ? 'Running' : 'Stopped'} detail={`Version ${metricVersion(metrics.up)}`} tone={Object.values(metrics.up).some(Boolean) ? 'good' : 'bad'} />
          <MetricCard label="Configuration" value={metrics.config_valid ? 'Valid' : 'Invalid'} detail={metrics.config_warnings ? `${metrics.config_warnings} warnings` : 'No warnings'} tone={metrics.config_valid ? (metrics.config_warnings ? 'warning' : 'good') : 'bad'} />
          <MetricCard label="Plan" value={metrics.plan_valid ? 'Valid' : 'Stale'} detail={`${metrics.plan_age_minutes.toFixed(0)} minutes old`} tone={metrics.plan_valid ? 'good' : 'warning'} />
          <MetricCard label="Uptime" value={formatUptime(lastStarted, refreshedAt)} />
          <MetricCard label="Errors" value={errors.toFixed(0)} tone={errors ? 'warning' : 'good'} />
          <MetricCard label="Load history" value={`${metrics.data_age_days.toFixed(1)} days`} detail={`${metrics.data_age_required_days.toFixed(0)} days required`} tone={metrics.data_age_days >= metrics.data_age_required_days ? 'good' : 'warning'} />
        </div>
      </div>

      <MetricsCharts metrics={metrics} />

      <div className="metrics-section">
        <h3>Cost and savings</h3>
        <div className="metrics-grid metrics-grid-compact">
          <MetricCard label="Cost today" value={formatMajorCurrency(metrics.cost_today / 100, metrics.currency_symbol)} />
          <MetricCard label="Solar and battery saving" value={formatMajorCurrency(metrics.savings_today_pvbat / 100, metrics.currency_symbol)} tone="good" />
          <MetricCard label="Predbat saving" value={formatMajorCurrency(metrics.savings_today_predbat / 100, metrics.currency_symbol)} tone="good" />
          <MetricCard label="Yesterday's actual cost" value={formatMajorCurrency(metrics.savings_today_actual / 100, metrics.currency_symbol)} />
        </div>
      </div>

      <div className="metrics-section-row">
        <div className="metrics-section">
          <h3>API services</h3>
          <div className="metrics-table-wrap">
            {services.length ? (
              <table>
                <thead><tr><th>Service</th><th>Requests</th><th>Failures</th><th>Last success</th></tr></thead>
                <tbody>
                  {services.map(([name, service]) => (
                    <tr key={name}><td>{name}</td><td>{service.requests.toFixed(0)}</td><td className={service.failures ? 'is-warning' : ''}>{service.failures.toFixed(0)}</td><td>{formatMetricAge(service.last_success)}</td></tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="metrics-empty">No API calls recorded yet.</p>}
          </div>
        </div>

        <div className="metrics-section">
          <h3>Solar forecasting</h3>
          <div className="metrics-grid metrics-grid-compact">
            <div className="metrics-card metrics-quota-card">
              <span>Solcast API quota</span>
              <strong>{metrics.solcast_api_used.toFixed(0)} / {metrics.solcast_api_limit.toFixed(0)}</strong>
              <div className="metrics-quota" aria-label={`${quotaPercent.toFixed(0)}% of Solcast quota used`}><span style={{ width: `${quotaPercent}%` }} /></div>
              <small>{metrics.solcast_api_remaining.toFixed(0)} remaining</small>
            </div>
            <MetricCard label="Worst-day scaling" value={metrics.pv_scaling_worst.toFixed(2)} />
            <MetricCard label="Best-day scaling" value={metrics.pv_scaling_best.toFixed(2)} />
            <MetricCard label="Total adjustment" value={metrics.pv_scaling_total.toFixed(2)} />
          </div>
        </div>
      </div>

      <div className="metrics-section">
        <h3>Control conflicts</h3>
        <div className="metrics-grid metrics-conflict-summary">
          <MetricCard label="Changes outside Predbat · 24h" value={metrics.control_conflicts_24h.toFixed(0)} tone={metrics.control_conflicts_24h ? 'warning' : 'good'} />
          <MetricCard label="Repeatedly changed controls" value={metrics.control_conflicts_sustained_total.toFixed(0)} detail={metrics.control_conflicts_sustained_controls.join(', ')} tone={metrics.control_conflicts_sustained_total ? 'bad' : 'good'} />
        </div>
        {conflicts.length > 0 && (
          <div className="metrics-table-wrap metrics-conflict-table">
            <table>
              <thead><tr><th>When</th><th>Control</th><th>Entity</th><th>Predbat set</th><th>Current value</th></tr></thead>
              <tbody>
                {conflicts.map((event, index) => (
                  <tr key={`${event.at}-${event.entity_id}-${index}`}><td>{formatMetricAge(event.at)}</td><td>{event.control ?? '—'}</td><td>{event.entity_id ?? '—'}</td><td>{event.we_set == null ? '—' : String(event.we_set)}</td><td>{event.now_reads == null ? '—' : String(event.now_reads)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
