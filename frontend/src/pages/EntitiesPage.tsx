import { useCallback, useEffect, useMemo, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faArrowUpRightFromSquare, faRotate } from '@fortawesome/free-solid-svg-icons'

import { filterEntities, formatEntityState, type EntityState, type EntitySummary } from '../utils/entities'
import './EntitiesPage.css'

const SHOW_ALL_KEY = 'predbat-entities-show-all'

/** Browse Predbat and Home Assistant entities with their live state and attributes. */
export default function EntitiesPage() {
  const [entities, setEntities] = useState<EntitySummary[]>([])
  const [states, setStates] = useState<Record<string, EntityState>>({})
  const [search, setSearch] = useState('')
  const [showAll, setShowAll] = useState(() => localStorage.getItem(SHOW_ALL_KEY) === 'true')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async (includeAll: boolean) => {
    setLoading(true)
    try {
      const [entitiesResponse, statesResponse] = await Promise.all([
        fetch(`./api/entities${includeAll ? '?all=1' : ''}`, { cache: 'no-store' }),
        fetch('./api/state', { cache: 'no-store' })
      ])
      if (!entitiesResponse.ok || !statesResponse.ok) throw new Error(`HTTP ${entitiesResponse.ok ? statesResponse.status : entitiesResponse.status}`)
      setEntities(await entitiesResponse.json() as EntitySummary[])
      setStates(await statesResponse.json() as Record<string, EntityState>)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to load entities')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- initial API load
    load(showAll)
  }, [load, showAll])

  const groups = useMemo(() => {
    const grouped = new Map<string, EntitySummary[]>()
    for (const entity of filterEntities(entities, states, search)) {
      const group = grouped.get(entity.group) ?? []
      group.push(entity)
      grouped.set(entity.group, group)
    }
    return [...grouped.entries()]
  }, [entities, search, states])

  function toggleShowAll() {
    const next = !showAll
    setShowAll(next)
    localStorage.setItem(SHOW_ALL_KEY, String(next))
  }

  return (
    <section className="entities-page">
      <header className="entities-page-header">
        <div><h1>Entities</h1><p>Browse live Predbat and Home Assistant entity states.</p></div>
        <button type="button" onClick={() => load(showAll)} disabled={loading}><FontAwesomeIcon icon={faRotate} /> Refresh</button>
      </header>

      {error && <div className="entities-error" role="alert">{error}</div>}

      <div className="entities-toolbar">
        <input type="search" value={search} placeholder="Search name, ID or state…" aria-label="Search entities" onChange={(event) => setSearch(event.target.value)} />
        <label><input type="checkbox" role="switch" checked={showAll} onChange={toggleShowAll} /> Show all Home Assistant entities</label>
        <span>{groups.reduce((count, [, items]) => count + items.length, 0)} entities</span>
      </div>

      <div className="entities-groups" aria-busy={loading}>
        {groups.map(([group, items], groupIndex) => (
          <details className="entities-group" key={group} open={groupIndex === 0}>
            <summary><strong>{group}</strong><span>{items.length}</span></summary>
            <div className="entities-list">
              {items.map((entity) => {
                const value = states[entity.id]
                const attributes = value?.attributes ?? {}
                return (
                  <details className="entity-row" key={entity.id}>
                    <summary>
                      <span><strong>{entity.name}</strong><code>{entity.id}</code></span>
                      <span>{formatEntityState(value)}</span>
                      <time>{value?.last_updated ? new Date(value.last_updated).toLocaleString() : '—'}</time>
                    </summary>
                    <div className="entity-details">
                      <dl>
                        {Object.entries(attributes).map(([name, attribute]) => <div key={name}><dt>{name}</dt><dd>{typeof attribute === 'object' ? JSON.stringify(attribute) : String(attribute)}</dd></div>)}
                      </dl>
                      <a href={`./entity?entity_id=${encodeURIComponent(entity.id)}`}>History and controls <FontAwesomeIcon icon={faArrowUpRightFromSquare} /></a>
                    </div>
                  </details>
                )
              })}
            </div>
          </details>
        ))}
        {!loading && !groups.length && <div className="entities-empty">No matching entities.</div>}
      </div>
    </section>
  )
}
