import { useEffect, useMemo, useRef, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faCheck, faClipboard } from '@fortawesome/free-solid-svg-icons'

import { buildPlanSummaryYaml, getPlanSummaryEntities } from '../utils/cards'
import { formatEntityState, type EntityState, type EntitySummary } from '../utils/entities'
import './CardsPage.css'

/** Preview copy-ready Home Assistant cards using this Predbat instance's entity IDs. */
export default function CardsPage() {
  const [entities, setEntities] = useState<EntitySummary[]>([])
  const [states, setStates] = useState<Record<string, EntityState>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const yamlRef = useRef<HTMLTextAreaElement>(null)

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
  const yaml = useMemo(() => buildPlanSummaryYaml(entities), [entities])

  async function copyYaml() {
    yamlRef.current?.select()
    try {
      await navigator.clipboard.writeText(yaml)
    } catch {
      document.execCommand('copy')
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }

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
                <button type="button" onClick={copyYaml}>
                  <FontAwesomeIcon icon={copied ? faCheck : faClipboard} /> {copied ? 'Copied' : 'Copy YAML'}
                </button>
              </div>
              <textarea ref={yamlRef} className="card-yaml" value={yaml} readOnly aria-label="Plan summary card YAML" />
            </div>
          </div>

          <p className="card-instructions">In Home Assistant, edit a dashboard, choose <strong>Add card</strong>, select <strong>Manual</strong>, then paste this YAML.</p>
        </article>
      )}

      {!loading && !error && rows.length === 0 && <div className="cards-error" role="alert">No Predbat entities were found.</div>}
    </section>
  )
}
