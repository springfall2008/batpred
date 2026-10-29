import { useEffect, useState, type CSSProperties } from 'react'
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faBatteryQuarter, faBatteryHalf, faBatteryThreeQuarters, faHouse, faMoon, faSolarPanel, faSun } from '@fortawesome/free-solid-svg-icons'
import type { IconDefinition } from '@fortawesome/fontawesome-svg-core'
import GridIcon from '../components/GridIcon'
import { COLOURS, DEFAULT_THEME, type ColourKey, type ThemeSettings } from '../hooks/useThemeSettings'
import './ThemePage.css'

type Props = {
  saved: ThemeSettings | null
  draft: ThemeSettings
  setDraft: (value: ThemeSettings) => void
  dirty: boolean
  error: string
  conflict: boolean
  save: (overwrite?: boolean) => Promise<void>
  reload: () => Promise<void>
  cancel: () => void
}

const PRESETS = [
  ['predbat', 'Predbat', 'The familiar Predbat colours'],
  ['retro', 'Retro', 'A classic green-screen look'],
  ['custom', 'Custom', 'Make the colours your own']
] as const
const COLOUR_ICONS: Record<ColourKey, IconDefinition | null> = {
  'battery-low': faBatteryQuarter, 'battery-medium': faBatteryHalf, 'battery-high': faBatteryThreeQuarters,
  charge: faBatteryHalf, export: null, 'flow-solar': faSolarPanel, 'flow-grid': null, 'flow-home': faHouse
}

function contrast(a: string, b: string) {
  const luminance = (colour: string) => {
    const channels = colour.slice(1).match(/../g)?.map(value => parseInt(value, 16) / 255) ?? [0, 0, 0]
    const linear = channels.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
  }
  const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (lighter + 0.05) / (darker + 0.05)
}

export default function ThemePage({ saved, draft, setDraft, dirty, error, conflict, save, reload, cancel }: Props) {
  const [mode, setMode] = useState<'light' | 'dark'>('light')
  const [saving, setSaving] = useState(false)
  const [, refreshDefaults] = useState(0)
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => refreshDefaults(value => value + 1))
    return () => window.cancelAnimationFrame(frame)
  }, [draft.preset])
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const styles = getComputedStyle(document.documentElement)
  const colour = (key: ColourKey, palette: 'light' | 'dark' = mode) => draft.colours[palette][key] ?? styles.getPropertyValue(`--default-${palette}-${key}`).trim()
  const { low, medium } = draft.socBoundaries
  const valid = Number.isInteger(low) && Number.isInteger(medium) && low >= 0 && low < medium && medium <= 100
  const setColour = (key: ColourKey, palette: 'light' | 'dark', value: string) => {
    setMode(palette)
    setDraft({ ...draft, colours: { ...draft.colours, [palette]: { ...draft.colours[palette], [key]: value } } })
  }
  const submit = async (overwrite = false) => { setSaving(true); await save(overwrite); setSaving(false) }
  const customPreview = Object.fromEntries((['light', 'dark'] as const).flatMap(palette =>
    (['charge', 'export', 'flow-grid', 'flow-home'] as const).map(key => [`--preview-${palette}-${key}`, draft.colours[palette][key]])
  )) as CSSProperties
  const socPreview = draft.preset === 'custom' ? {
    '--soc-preview-low': colour('battery-low'),
    '--soc-preview-medium': colour('battery-medium'),
    '--soc-preview-high': colour('battery-high')
  } as CSSProperties : undefined

  return <section className="theme-page">
    <header><h1>Theme</h1><p>Choose a look for your dashboard and set your battery colour ranges. Changes preview here until saved.</p></header>
    {error && <p className="theme-message" role="alert">{error}</p>}
    {!saved ? error ? <button type="button" onClick={() => void reload()}>Retry</button> : <p>Loading theme settings…</p> : <>
      <div className="theme-actions"><span role="status">{dirty ? 'Unsaved changes' : 'All changes saved'}</span><button type="button" onClick={cancel} disabled={!dirty || saving}>Cancel</button><button type="button" onClick={() => void submit()} disabled={!dirty || !valid || saving || conflict}>Save</button></div>
      {conflict && <div className="theme-message" role="alert"><p>Theme settings changed on another device. Reload them or overwrite them with your edits.</p><button type="button" onClick={() => void reload()}>Reload saved settings</button> <button type="button" disabled={!valid || saving} onClick={() => void submit(true)}>Overwrite newer settings</button></div>}
      <section className="theme-section"><h2>Choose a theme</h2><p>Each theme includes a light and dark appearance.</p>
        <div className="theme-presets" aria-label="Theme choices">{PRESETS.map(([key, name, description]) => <button type="button" className={`theme-preset theme-preset-${key}`} style={key === 'custom' ? customPreview : undefined} disabled={saving} aria-pressed={draft.preset === key} onClick={() => setDraft({ ...draft, preset: key })} key={key}>
          <span className="theme-preset-preview" aria-hidden="true"><span /><span /><span /><span /></span><strong>{name}</strong><small>{description}</small>
        </button>)}</div>
      </section>
      {draft.preset === 'custom' && <section className="theme-section"><h2>Custom colours</h2>
        <p>Choose colours for each appearance.</p>
        <div className="theme-colour-table-wrap"><table className="theme-colour-table">
          <colgroup><col /><col /><col className="theme-colour-light" /><col className="theme-colour-dark" /></colgroup>
          <thead><tr><th scope="col" aria-label="Category" /><th scope="col">Colour</th><th scope="col" className="theme-colour-light"><FontAwesomeIcon icon={faSun} aria-hidden="true" /> Light</th><th scope="col" className="theme-colour-dark"><FontAwesomeIcon icon={faMoon} aria-hidden="true" /> Dark</th></tr></thead>
          {([['Energy flows', COLOURS.slice(3)], ['Battery levels', COLOURS.slice(0, 3)]] as const).map(([heading, entries]) => <tbody key={heading}>
            {entries.map(([key, label], index) => <tr key={key}>
              {index === 0 && <th className="theme-colour-group" scope="rowgroup" rowSpan={entries.length}><span>{heading}</span></th>}
              <th scope="row"><span className="theme-colour-name"><span className="theme-colour-icon" aria-hidden="true">{COLOUR_ICONS[key] ? <FontAwesomeIcon icon={COLOUR_ICONS[key]!} /> : <GridIcon />}</span>{label}</span></th>
              {(['light', 'dark'] as const).map(palette => {
                const value = colour(key, palette)
                const background = styles.getPropertyValue(`--default-${palette}-bg`).trim()
                const warning = /^#[0-9a-f]{6}$/i.test(value) && /^#[0-9a-f]{6}$/i.test(background) && contrast(value, background) < 3
                const warningId = `theme-warning-${palette}-${key}`
                return <td className="theme-colour-cell" data-palette={palette} key={palette}>
                  <input type="color" aria-label={`${label} ${palette}`} aria-describedby={warning ? warningId : undefined} disabled={saving} value={value} onChange={event => setColour(key, palette, event.target.value)} />
                  {warning && <small id={warningId}>Low contrast</small>}
                </td>
              })}
            </tr>)}
          </tbody>)}
        </table></div>
        <div className="theme-reset-colours">{(['light', 'dark'] as const).map(palette => <button type="button" key={palette} disabled={saving} onClick={() => { setMode(palette); setDraft({ ...draft, colours: { ...draft.colours, [palette]: {} } }) }}>Reset {palette} colours</button>)}</div>
      </section>}
      <section className="theme-section" style={socPreview}><h2>Battery SOC levels</h2><p>Move the handles to set where each colour range ends.{draft.preset === 'custom' && ` Showing the ${mode} palette.`}</p>
        <div className="soc-range-labels"><span><i className="soc-swatch low" />Low <strong>0–{low}%</strong></span><span><i className="soc-swatch medium" />Medium <strong>{low + 1}–{medium}%</strong></span><span><i className="soc-swatch high" />High <strong>{medium === 100 ? 'None' : `${medium + 1}–100%`}</strong></span></div>
        <div className="soc-slider" style={{ background: `linear-gradient(to right, var(--soc-preview-low, var(--color-battery-low)) 0%, var(--soc-preview-low, var(--color-battery-low)) ${low}%, var(--soc-preview-medium, var(--color-battery-medium)) ${low}%, var(--soc-preview-medium, var(--color-battery-medium)) ${medium}%, var(--soc-preview-high, var(--color-battery-high)) ${medium}%, var(--soc-preview-high, var(--color-battery-high)) 100%)` }}>
          <input type="range" aria-label="Low SOC boundary" aria-valuetext={`${low}%`} min="0" max="100" step="1" disabled={saving} value={low} onChange={event => setDraft({ ...draft, socBoundaries: { ...draft.socBoundaries, low: Math.min(Number(event.target.value), medium - 1) } })} />
          <input type="range" aria-label="Medium SOC boundary" aria-valuetext={`${medium}%`} min="0" max="100" step="1" disabled={saving} value={medium} onChange={event => setDraft({ ...draft, socBoundaries: { ...draft.socBoundaries, medium: Math.max(Number(event.target.value), low + 1) } })} />
        </div>
        <div className="soc-slider-scale"><span>0%</span><span>100%</span></div>
        {!valid && <p role="alert">The low boundary must be below the medium boundary.</p>}
        <button type="button" disabled={saving} onClick={() => setDraft({ ...draft, socBoundaries: DEFAULT_THEME.socBoundaries })}>Reset battery levels</button>
      </section>
    </>}
  </section>
}
