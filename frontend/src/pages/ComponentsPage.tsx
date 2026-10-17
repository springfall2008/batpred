import { useCallback, useEffect, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faPen, faRotate, faServer } from '@fortawesome/free-solid-svg-icons'

import './ComponentsPage.css'

type ComponentStatus = 'active' | 'error' | 'disabled'

type ComponentSetting = {
  name: string
  required: boolean
  value: string
}

type ComponentInfo = {
  id: string
  name: string
  status: ComponentStatus
  can_restart: boolean
  last_updated: string
  error: string | null
  error_count: number | null
  entity_count: number | null
  event_filter: string | null
  settings: ComponentSetting[]
}

type ComponentsResponse = { components: ComponentInfo[] }

const STATUS_LABELS: Record<ComponentStatus, string> = {
  active: 'Active',
  error: 'Error',
  disabled: 'Disabled'
}

/** Show component health and link configuration to the schema-aware editor. */
export default function ComponentsPage() {
  const [components, setComponents] = useState<ComponentInfo[]>([])
  const [showDisabled, setShowDisabled] = useState(() => sessionStorage.getItem('showDisabledComponents') === 'true')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [restarting, setRestarting] = useState('')

  const load = useCallback(async () => {
    try {
      const response = await fetch('./api/components', { cache: 'no-store' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json() as ComponentsResponse
      setComponents(data.components)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to load components')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- initial API load
    load()
    const timer = window.setInterval(load, 60000)
    return () => window.clearInterval(timer)
  }, [load])

  function updateShowDisabled(checked: boolean) {
    setShowDisabled(checked)
    sessionStorage.setItem('showDisabledComponents', String(checked))
  }

  async function restart(component: ComponentInfo) {
    setRestarting(component.id)
    setError('')
    try {
      const body = new URLSearchParams({ component: component.id })
      const response = await fetch('./component_restart', { method: 'POST', body })
      const result = await response.json() as { message?: string }
      if (!response.ok) throw new Error(result.message ?? `HTTP ${response.status}`)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to restart component')
    } finally {
      setRestarting('')
    }
  }

  const counts = components.reduce<Record<ComponentStatus, number>>(
    (result, component) => ({ ...result, [component.status]: result[component.status] + 1 }),
    { active: 0, error: 0, disabled: 0 }
  )
  const visibleComponents = showDisabled ? components : components.filter((component) => component.status !== 'disabled')

  return (
    <section className="components-page">
      <header className="components-page-header">
        <div>
          <h1>Components</h1>
          <p>Health and configuration for Predbat integrations and services.</p>
        </div>
        <button type="button" onClick={load}>
          <FontAwesomeIcon icon={faRotate} /> Refresh
        </button>
      </header>

      <div className="component-summary" aria-label="Component status summary">
        {(['error', 'active', 'disabled'] as ComponentStatus[]).map((status) => (
          <div className={`is-${status}`} key={status}>
            <strong>{counts[status]}</strong>
            <span>{STATUS_LABELS[status]}</span>
          </div>
        ))}
        <label>
          <input type="checkbox" checked={showDisabled} onChange={(event) => updateShowDisabled(event.target.checked)} />
          Show disabled components
        </label>
      </div>

      {error && <div className="components-error" role="alert">Unable to update components: {error}</div>}
      {loading ? <div className="components-empty">Loading components…</div> : (
        <div className="components-grid">
          {visibleComponents.map((component) => (
            <article className={`component-card is-${component.status}`} key={component.id}>
              <header>
                <span className="component-icon"><FontAwesomeIcon icon={faServer} /></span>
                <div>
                  <h2>{component.name}</h2>
                  <span className={`component-status is-${component.status}`}><i />{STATUS_LABELS[component.status]}</span>
                </div>
                <div className="component-actions">
                  {component.can_restart && (
                    <button type="button" disabled={restarting === component.id} onClick={() => restart(component)}>
                      <FontAwesomeIcon icon={faRotate} /> {restarting === component.id ? 'Restarting…' : 'Restart'}
                    </button>
                  )}
                  <a href="./apps_editor"><FontAwesomeIcon icon={faPen} /> Edit</a>
                </div>
              </header>

              <dl className="component-meta">
                <div><dt>Last updated</dt><dd>{component.last_updated || 'Never'}</dd></div>
                {component.error_count !== null && <div><dt>Errors</dt><dd className={component.error_count ? 'is-error' : ''}>{component.error_count}</dd></div>}
                {component.entity_count !== null && <div><dt>Entities</dt><dd>{component.entity_count}</dd></div>}
              </dl>

              {component.error && <p className="component-error-detail">{component.error}</p>}

              {component.settings.length > 0 && (
                <details>
                  <summary>Configuration ({component.settings.length})</summary>
                  <div className="component-settings">
                    {component.settings.map((setting) => (
                      <div key={setting.name}>
                        <span>{setting.name}{setting.required && <em>Required</em>}</span>
                        <strong>{setting.value}</strong>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
