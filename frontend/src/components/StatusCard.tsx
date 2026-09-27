import './StatusCard.css'
import { MODERN_UI_VERSION } from '../version'

type StatusCardProps = {
  status: string
  mode: string
  version: string

  lastUpdated: string | null
  lastStarted: string | null

  configOk: boolean

  active: boolean
  readOnly: boolean
  debugEnabled: boolean

  onModeChange: (value: string) => void
  onActiveChange: (value: boolean) => void
  onReadOnlyChange: (value: boolean) => void
  onDebugChange: (value: boolean) => void
}

// Relative labels refresh when the parent supplies the next status poll.
function formatTimeAgo(value: string | null) {
  if (!value) return 'Unknown'

  const date = new Date(value)
  const now = new Date()

  const seconds = Math.floor((now.getTime() - date.getTime()) / 1000)

  if (seconds < 60) {
    return 'Just now'
  }

  const minutes = Math.floor(seconds / 60)

  if (minutes < 60) {
    return `${minutes} min ago`
  }

  const hours = Math.floor(minutes / 60)

  if (hours < 24) {
    return `${hours}h ago`
  }

  const days = Math.floor(hours / 24)

  return `${days}d ago`
}

function formatUptime(value: string | null) {
  if (!value) return 'Unknown'

  const started = new Date(value)
  const now = new Date()

  const minutes = Math.floor((now.getTime() - started.getTime()) / 60000)

  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  const mins = minutes % 60

  if (days > 0) {
    return `${days}d ${hours}h`
  }

  if (hours > 0) {
    return `${hours}h ${mins}m`
  }

  return `${mins}m`
}

function getStatusHealth(status: string) {
  const normalized = status.toLowerCase()

  if (normalized.includes('error') || normalized.includes('unhealthy')) {
    return 'error'
  }

  if (normalized.includes('warn')) {
    return 'warning'
  }

  return 'ok'
}

function StatusCard({
  status,
  mode,
  version,
  lastUpdated,
  lastStarted,
  configOk,
  active,
  readOnly,
  debugEnabled,
  onModeChange,
  onActiveChange,
  onReadOnlyChange,
  onDebugChange
}: StatusCardProps) {
  const statusHealth = getStatusHealth(status)

  return (
    <section className="status-card">
      {/* Card heading */}
      <div className="status-card-header">
        <h2>Predbat Status</h2>

        <span className="status-card-version">
          Predbat {version} · Modern UI {MODERN_UI_VERSION}
        </span>
      </div>

      {/* Current status */}
      <div className="status-card-status-section">
        <span className="status-card-label">Status</span>

        <div className="status-card-status-row">
          <span className={`status-dot status-${statusHealth}`} />

          <strong className="status-card-status">{status}</strong>
        </div>
      </div>

      {/* Runtime/status information */}
      <div className="status-card-details">
        {/* Mode */}
        <div className="status-card-detail">
          <span>Mode</span>

          <select
            className="status-card-select"
            value={mode}
            onChange={(event) => onModeChange(event.target.value)}
          >
            <option value="Monitor">Monitor</option>

            <option value="Control charge">Control charge</option>

            <option value="Control charge & discharge">Control charge & discharge</option>
          </select>
        </div>

        {/* Config health */}
        <div className="status-card-detail">
          <span>Config</span>

          <strong className={configOk ? 'config-ok' : 'config-error'}>
            {configOk ? 'OK' : 'Error'}
          </strong>
        </div>

        {/* Last update */}
        <div className="status-card-detail">
          <span>Plan Updated</span>

          <strong>{formatTimeAgo(lastUpdated)}</strong>
        </div>

        {/* Uptime */}
        <div className="status-card-detail">
          <span>Uptime</span>

          <strong>{formatUptime(lastStarted)}</strong>
        </div>
      </div>

      {/* Interactive controls */}
      <div className="status-card-controls">
        {/* Predbat calculation/activity state */}
        <div className="status-control-row">
          <div className="status-control-text">
            <span className="status-control-label">Predbat Active</span>

            <span className="status-control-description">{active ? 'Calculating' : 'Idle'}</span>
          </div>

          <label className="toggle-switch">
            <input
              type="checkbox"
              checked={active}
              onChange={(event) => onActiveChange(event.target.checked)}
            />
            <span className="toggle-slider" />
          </label>
        </div>

        {/* Read-only mode */}
        <div className="status-control-row">
          <div className="status-control-text">
            <span className="status-control-label">Read Only</span>

            <span className="status-control-description">{readOnly ? 'Enabled' : 'Disabled'}</span>
          </div>

          <label className="toggle-switch">
            <input
              type="checkbox"
              checked={readOnly}
              onChange={(event) => onReadOnlyChange(event.target.checked)}
            />
            <span className="toggle-slider" />
          </label>
        </div>

        {/* Debug output */}
        <div className="status-control-row">
          <div className="status-control-text">
            <span className="status-control-label">Debug</span>

            <span className="status-control-description">
              {debugEnabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>

          <label className="toggle-switch">
            <input
              type="checkbox"
              checked={debugEnabled}
              onChange={(event) => onDebugChange(event.target.checked)}
            />
            <span className="toggle-slider" />
          </label>
        </div>
      </div>
    </section>
  )
}

export default StatusCard
