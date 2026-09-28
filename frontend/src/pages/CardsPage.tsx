import { useEffect, useMemo, useRef, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faArrowUpFromBracket, faBatteryHalf, faBolt, faBullseye, faCar, faCheck, faClipboard, faClock, faCoins, faHouse } from '@fortawesome/free-solid-svg-icons'

import { buildGlanceSummaryYaml, buildPlanSummaryYaml, getPlanSummaryEntities } from '../utils/cards'
import { formatEntityState, type EntityState, type EntitySummary } from '../utils/entities'
import './CardsPage.css'

/** Preview copy-ready Home Assistant cards using this Predbat instance's entity IDs. */
export default function CardsPage() {
  const [entities, setEntities] = useState<EntitySummary[]>([])
  const [states, setStates] = useState<Record<string, EntityState>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState<'plan' | 'glance' | null>(null)
  const planYamlRef = useRef<HTMLTextAreaElement>(null)
  const glanceYamlRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    Promise.all([
      fetch('./api/entities', { cache: 'no-store' }),
      fetch('./api/state', { cache: 'no-store' })
    ]).then(async ([entitiesResponse, statesResponse]) => {
      if (!entitiesResponse.ok || !statesResponse.ok) throw new Error(`HTTP ${entitiesResponse.ok ? statesResponse.status : entitiesResponse.status}`)
      setEntities(await entitiesResponse.json() as EntitySummary[])
      setStates(await statesResponse.json() as Record<string, EntityState>)
      setError('')
    }).catch((reason) => {
      setError(reason instanceof Error ? reason.message : 'Unable to load Predbat entities')
    }).finally(() => setLoading(false))
  }, [])

  const rows = useMemo(() => getPlanSummaryEntities(entities), [entities])
  const planYaml = useMemo(() => buildPlanSummaryYaml(entities), [entities])
  const glanceYaml = useMemo(() => buildGlanceSummaryYaml(entities), [entities])

  async function copyYaml(yaml: string, source: 'plan' | 'glance', textarea: HTMLTextAreaElement | null) {
    textarea?.select()
    try {
      await navigator.clipboard.writeText(yaml)
    } catch {
      document.execCommand('copy')
    }
    setCopied(source)
    window.setTimeout(() => setCopied(null), 1800)
  }

  const statusEntity = rows.find((row) => row.suffix === 'status')
  const statusState = statusEntity ? states[statusEntity.id] : undefined
  const status = `${String(statusState?.state ?? '')} ${String(statusState?.attributes?.detail ?? '')}`.toLowerCase()
  const statusIcon = status.includes('hold for car') ? faCar : status.startsWith('charg') ? faBatteryHalf : status.startsWith('export') ? faArrowUpFromBracket : faHouse
  const glanceIcons = [statusIcon, faBatteryHalf, faBolt, faClock, faBullseye, faClock, faBullseye, faCoins]

  return (
    <section className="cards-page">
      <header className="cards-page-header">
        <div><h1>Cards</h1><p>Ready-to-use Home Assistant cards for your Predbat dashboard.</p></div>
      </header>

      {error && <div className="cards-error" role="alert">Unable to load entity IDs: {error}</div>}
      {loading && <div className="cards-loading">Loading your Predbat entities…</div>}

      {!loading && rows.length > 0 && (
        <article className="card-example">
          <div className="card-example-heading">
            <div><h2>Plan summary</h2><p>Current status, battery level, rates and the next planned charge or export.</p></div>
          </div>

          <div className="card-example-layout">
            <div>
              <h3>Preview</h3>
              <section className="ha-card-preview" aria-label="Plan summary card preview">
                <h4>Predbat Plan Summary</h4>
                <dl>
                  {rows.map((row) => (
                    <div key={row.id}><dt>{row.name}</dt><dd>{formatEntityState(states[row.id])}</dd></div>
                  ))}
                </dl>
              </section>
            </div>

            <div>
              <div className="card-yaml-heading">
                <h3>YAML</h3>
                <button type="button" onClick={() => copyYaml(planYaml, 'plan', planYamlRef.current)}>
                  <FontAwesomeIcon icon={copied === 'plan' ? faCheck : faClipboard} /> {copied === 'plan' ? 'Copied' : 'Copy YAML'}
                </button>
              </div>
              <textarea ref={planYamlRef} className="card-yaml" value={planYaml} readOnly aria-label="Plan summary card YAML" />
            </div>
          </div>

          <p className="card-instructions">In Home Assistant, edit a dashboard, choose <strong>Add card</strong>, select <strong>Manual</strong>, then paste this YAML.</p>
        </article>
      )}

      {!loading && rows.length > 0 && (
        <article className="card-example">
          <div className="card-example-heading">
            <div><h2>Glance summary</h2><p>A compact overview with a status icon that follows what Predbat is currently doing.</p></div>
          </div>

          <div className="card-example-layout">
            <div>
              <h3>Preview</h3>
              <section className="ha-card-preview" aria-label="Glance summary card preview">
                <h4>Predbat Summary</h4>
                <div className="ha-glance-grid">
                  {rows.map((row, index) => (
                    <div key={row.id}>
                      <FontAwesomeIcon icon={glanceIcons[index]} />
                      <strong>{formatEntityState(states[row.id])}</strong>
                      <span>{row.name}</span>
                    </div>
                  ))}
                </div>
              </section>
            </div>

            <div>
              <div className="card-yaml-heading">
                <h3>YAML</h3>
                <button type="button" onClick={() => copyYaml(glanceYaml, 'glance', glanceYamlRef.current)}>
                  <FontAwesomeIcon icon={copied === 'glance' ? faCheck : faClipboard} /> {copied === 'glance' ? 'Copied' : 'Copy YAML'}
                </button>
              </div>
              <textarea ref={glanceYamlRef} className="card-yaml" value={glanceYaml} readOnly aria-label="Glance summary card YAML" />
            </div>
          </div>

          <p className="card-instructions">The status icon changes automatically: house for Demand, battery charging for Charging, transmission tower for Exporting and car for Hold for car.</p>
        </article>
      )}

      {!loading && !error && rows.length === 0 && <div className="cards-error" role="alert">No Predbat entities were found.</div>}
    </section>
  )
}
