import { useCallback, useEffect, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faRotate } from '@fortawesome/free-solid-svg-icons'

import './DiscoveryPage.css'

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }
type Catalogue = { [key: string]: JsonValue }

const META_KEYS = new Set(['schema_version', 'generated', 'components', 'observations'])

function label(value: string) {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase())
}

function Fields({ value }: { value: JsonValue }) {
  if (value === null || typeof value !== 'object') return <span>{String(value ?? 'None')}</span>
  if (Array.isArray(value)) {
    if (!value.length) return <span>None</span>
    return <div className="discovery-values">{value.map((item, index) => <Fields value={item} key={index} />)}</div>
  }
  return (
    <dl className="discovery-fields">
      {Object.entries(value).map(([key, item]) => (
        <div key={key}><dt>{label(key)}</dt><dd><Fields value={item} /></dd></div>
      ))}
    </dl>
  )
}

/** Show Predbat's observed hardware and service catalogue. */
export default function DiscoveryPage() {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null)
  const [raw, setRaw] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const response = await fetch(`./api/discovery${raw ? '?raw=1' : ''}`, { cache: 'no-store' })
      const data = await response.json() as Catalogue & { error?: string }
      if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`)
      setCatalogue(data)
      setError('')
    } catch (reason) {
      setCatalogue(null)
      setError(reason instanceof Error ? reason.message : 'Unable to load the discovery catalogue')
    } finally {
      setLoading(false)
    }
  }, [raw])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- load when the view changes
    load()
  }, [load])

  const sections = catalogue
    ? Object.entries(catalogue).filter(([key, value]) => !META_KEYS.has(key) && Array.isArray(value)) as [string, JsonValue[]][]
    : []
  const components = catalogue?.components
  const observations = catalogue?.observations
  const conflicts = observations && !Array.isArray(observations) && typeof observations === 'object' && Array.isArray(observations.conflicts) ? observations.conflicts : []

  return (
    <section className="discovery-page">
      <header className="discovery-header">
        <div><h1>Discovery</h1><p>Hardware and services detected by Predbat.</p></div>
        <button type="button" onClick={load}><FontAwesomeIcon icon={faRotate} /> Refresh</button>
      </header>

      <div className="discovery-toolbar">
        <div>
          <strong>Schema {String(catalogue?.schema_version ?? '—')}</strong>
          <span>Generated {String(catalogue?.generated ?? '—')}</span>
        </div>
        <label><input type="checkbox" checked={raw} onChange={(event) => setRaw(event.target.checked)} /> Show raw values</label>
      </div>

      {raw && <div className="discovery-warning" role="alert"><strong>Raw view.</strong> Serial numbers, MPANs and account identifiers are visible. This view is not safe to share.</div>}
      {error && <div className="discovery-error" role="alert">{error}</div>}
      {loading && <div className="discovery-empty">Loading discovery catalogue…</div>}

      {catalogue && !loading && (
        <>
          <div className={conflicts.length ? 'discovery-error' : 'discovery-clear'}>
            {conflicts.length ? `${conflicts.length} discovery conflict${conflicts.length === 1 ? '' : 's'} found.` : 'No discovery conflicts found.'}
            {conflicts.length > 0 && <Fields value={conflicts} />}
          </div>

          {components && !Array.isArray(components) && typeof components === 'object' && (
            <section><h2>Components</h2><div className="discovery-grid">
              {Object.entries(components).map(([name, value]) => <article key={name}><h3>{label(name)}</h3><Fields value={value} /></article>)}
            </div></section>
          )}

          {sections.filter(([, records]) => records.length).map(([name, records]) => (
            <section key={name}>
              <h2>{label(name)} <span>{records.length}</span></h2>
              <div className="discovery-grid">
                {records.map((record, index) => {
                  const id = record && !Array.isArray(record) && typeof record === 'object' ? record.device_id : null
                  return <article key={`${String(id ?? name)}-${index}`}><h3>{String(id ?? `${label(name)} ${index + 1}`)}</h3><Fields value={record} /></article>
                })}
              </div>
            </section>
          ))}

          <details className="discovery-document"><summary>Full document</summary><pre>{JSON.stringify(catalogue, null, 2)}</pre></details>
        </>
      )}
    </section>
  )
}
