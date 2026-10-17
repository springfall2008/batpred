import { useCallback, useEffect, useMemo, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faCircleInfo, faRotate } from '@fortawesome/free-solid-svg-icons'

import { CONFIG_GROUPS, configGroup } from '../utils/configGroups'
import { analyseTariff, type TariffAnalysis } from '../utils/tariffHelper'
import type { RatesChartData } from '../types/charts'

import './ConfigPage.css'

type ConfigItem = {
  name: string
  friendly_name: string
  description: string
  entity: string
  type: string
  value: string | number | boolean
  default: string | number | boolean
  unit: string
  min: number | null
  max: number | null
  step: number | null
  options: Array<string | number>
}

/** Edit the live Home Assistant controls generated from Predbat configuration. */
export default function ConfigPage() {
  const [items, setItems] = useState<ConfigItem[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState('')
  const [error, setError] = useState('')
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({})
  const [tariff, setTariff] = useState<TariffAnalysis | null>(null)
  const [tariffError, setTariffError] = useState('')
  const [tariffSelection, setTariffSelection] = useState<Set<string>>(new Set())
  const [tariffMessage, setTariffMessage] = useState('')

  const load = useCallback(async () => {
    try {
      const response = await fetch('./api/config', { cache: 'no-store' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json() as { items: ConfigItem[], errors?: Record<string, string> }
      setItems(data.items)
      setValidationErrors(data.errors ?? {})
      setError('')

      try {
        const ratesResponse = await fetch('./api/chart_data?chart=rates', { cache: 'no-store' })
        if (!ratesResponse.ok) throw new Error(`HTTP ${ratesResponse.status}`)
        const rates = await ratesResponse.json() as RatesChartData
        const analysis = analyseTariff(data.items, rates)
        setTariff(analysis)
        setTariffSelection(new Set(analysis?.suggestions.map(({ name }) => name) ?? []))
        setTariffError(analysis ? '' : 'Predbat has not published enough future import-rate data to identify the tariff.')
      } catch (reason) {
        setTariff(null)
        setTariffError(reason instanceof Error ? reason.message : 'Unable to analyse tariff rates')
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to load configuration')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- initial API load
    load()
  }, [load])

  async function postChanges(changes: Array<{ item: ConfigItem, value: string | number | boolean }>) {
    const body = new URLSearchParams()
    changes.forEach(({ item, value }) => body.set(item.entity.replace('.', '__'), item.type === 'switch' ? (value ? 'on' : 'off') : String(value)))
    const response = await fetch('./api/config', { method: 'POST', body })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
  }

  async function save(item: ConfigItem, value: string | number | boolean) {
    setSaving(item.entity)
    setError('')
    try {
      await postChanges([{ item, value }])
      setItems((current) => current.map((entry) => entry.entity === item.entity ? { ...entry, value } : entry))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to save configuration')
    } finally {
      setSaving('')
    }
  }

  async function applyTariffSuggestions() {
    if (!tariff) return
    const changes = tariff.suggestions
      .filter(({ name }) => tariffSelection.has(name))
      .flatMap((suggestion) => {
        const item = items.find(({ name }) => name === suggestion.name)
        return item ? [{ item, value: suggestion.value }] : []
      })
    if (!changes.length) return

    setSaving('tariff-helper')
    setError('')
    setTariffMessage('')
    try {
      await postChanges(changes)
      setTariffMessage(`Applied ${changes.length} reviewed ${changes.length === 1 ? 'change' : 'changes'}.`)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to apply tariff suggestions')
    } finally {
      setSaving('')
    }
  }

  function toggleTariffSuggestion(name: string) {
    setTariffSelection((current) => {
      const next = new Set(current)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const visibleItems = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return items
    return items.filter((item) => `${item.friendly_name} ${item.entity} ${item.type}`.toLowerCase().includes(query))
  }, [items, search])
  const groupedItems = CONFIG_GROUPS.map((group) => ({
    group,
    items: visibleItems.filter((item) => configGroup(item.name) === group)
  })).filter(({ items: groupItems }) => groupItems.length)

  return (
    <section className="config-page">
      <header className="config-page-header">
        <div><h1>Config</h1><p>Live Predbat controls exposed through Home Assistant.</p></div>
        <button type="button" onClick={load}><FontAwesomeIcon icon={faRotate} /> Refresh</button>
      </header>

      <div className="config-toolbar">
        <input type="search" name="search" autoComplete="off" value={search} placeholder="Filter settings…" aria-label="Filter settings" onChange={(event) => setSearch(event.target.value)} />
        <span>{visibleItems.length} of {items.length} settings</span>
      </div>

      <section className="tariff-helper" aria-labelledby="tariff-helper-heading">
        <header><div><h2 id="tariff-helper-heading">Tariff helper</h2><p>Predbat reviews your current controls and the next 48 hours of import and export prices. Nothing changes until you apply the selected suggestions.</p></div>{tariff && <strong>{tariff.profile}</strong>}</header>
        {tariff ? <>
          <div className="tariff-helper-rates">
            <span>Import range <b>{tariff.importMin.toFixed(1)}–{tariff.importMax.toFixed(1)} {tariff.unit}</b></span>
            <span>Export range <b>{tariff.exportMin === null ? 'Unavailable' : `${tariff.exportMin.toFixed(1)}–${tariff.exportMax?.toFixed(1)} ${tariff.unit}`}</b></span>
          </div>
          {tariff.suggestions.length ? <div className="tariff-helper-review">
            {tariff.suggestions.map((suggestion) => {
              const item = items.find(({ name }) => name === suggestion.name)
              return <label key={suggestion.name}>
                <input type="checkbox" checked={tariffSelection.has(suggestion.name)} disabled={saving === 'tariff-helper'} onChange={() => toggleTariffSuggestion(suggestion.name)} />
                <span><strong>{item?.friendly_name ?? suggestion.name}</strong><small>{String(suggestion.current)} → {String(suggestion.value)} · {suggestion.reason}</small></span>
              </label>
            })}
            <button type="button" disabled={saving === 'tariff-helper' || !tariffSelection.size} onClick={applyTariffSuggestions}>{saving === 'tariff-helper' ? 'Applying…' : `Apply ${tariffSelection.size} selected`}</button>
          </div> : <p className="tariff-helper-result">Your available controls already match the suggested settings for this tariff.</p>}
          {tariffMessage && <p className="tariff-helper-result" role="status">{tariffMessage}</p>}
        </> : <p className="tariff-helper-result">{tariffError || 'Analysing tariff…'}</p>}
      </section>

      {error && <div className="config-error" role="alert">Unable to update configuration: {error}</div>}
      {Object.keys(validationErrors).length > 0 && (
        <div className="config-validation-errors" role="alert">
          <strong>Predbat found {Object.keys(validationErrors).length} {Object.keys(validationErrors).length === 1 ? 'error' : 'errors'} in apps.yaml</strong>
          <ul>
            {Object.entries(validationErrors).map(([name, message]) => <li key={name}><b>{name}</b>: {message}</li>)}
          </ul>
          <a href="./apps">Open apps.yaml settings</a>
        </div>
      )}
      {loading ? <div className="config-empty">Loading configuration…</div> : (
        <div className="config-groups">
          {groupedItems.map(({ group, items: groupItems }) => <section className="config-group" key={group}>
            <header><h2>{group}</h2><span>{groupItems.length} settings</span></header>
            <div className="config-table-wrap"><table className="config-table">
              <thead><tr><th>Setting</th><th>Current</th><th>Default</th><th>Control</th></tr></thead>
              <tbody>
              {groupItems.map((item) => {
                const changed = String(item.value) !== String(item.default)
                const disabled = saving === item.entity || saving === 'tariff-helper'
                return (
                  <tr key={item.entity} className={validationErrors[item.name] ? 'has-error' : ''}>
                    <td><div className="config-setting-name"><strong>{item.friendly_name}</strong><span className="config-help" tabIndex={0} aria-label={item.description} data-tooltip={item.description}><FontAwesomeIcon icon={faCircleInfo} /></span></div><small>{item.entity}</small>{validationErrors[item.name] && <span className="config-item-error">{validationErrors[item.name]}</span>}</td>
                    <td className={changed ? 'is-changed' : ''}>{String(item.value)} {item.unit}</td>
                    <td>{String(item.default)} {item.unit}</td>
                    <td>
                      {item.type === 'switch' ? (
                        <label className="config-switch">
                          <input type="checkbox" checked={Boolean(item.value)} disabled={disabled} onChange={(event) => save(item, event.target.checked)} />
                          <i aria-hidden="true" /><span>{item.value ? 'On' : 'Off'}</span>
                        </label>
                      ) : item.type === 'select' ? (
                        <select name={item.name} aria-label={item.friendly_name} autoComplete="off" value={String(item.value)} disabled={disabled} onChange={(event) => save(item, event.target.value)}>
                          {item.options.map((option) => <option key={String(option)} value={String(option)}>{String(option) || 'None'}</option>)}
                        </select>
                      ) : item.type === 'input_number' || item.type === 'number' ? (
                        <input type="number" name={item.name} aria-label={item.friendly_name} autoComplete="off" defaultValue={Number(item.value)} min={item.min ?? undefined} max={item.max ?? undefined} step={item.step ?? undefined} disabled={disabled} onBlur={(event) => Number(event.target.value) !== Number(item.value) && save(item, Number(event.target.value))} />
                      ) : <span>{String(item.value)}</span>}
                    </td>
                  </tr>
                )
              })}
              </tbody>
            </table></div>
          </section>)}
          {!visibleItems.length && <div className="config-empty">No settings match that filter.</div>}
        </div>
      )}
    </section>
  )
}
