import { useEffect, useRef, useState } from 'react'

export const COLOURS = [
  ['battery-low', 'Battery low'], ['battery-medium', 'Battery medium'], ['battery-high', 'Battery high'],
  ['charge', 'Charging'], ['export', 'Exporting'], ['flow-solar', 'Solar'], ['flow-grid', 'Grid'], ['flow-home', 'Home']
] as const
export type ColourKey = typeof COLOURS[number][0]
export type ThemeSettings = {
  version: 1
  revision: number
  preset: 'predbat' | 'retro' | 'custom'
  colours: Record<'light' | 'dark', Partial<Record<ColourKey, string>>>
  socBoundaries: { low: number; medium: number }
}
export const DEFAULT_THEME: ThemeSettings = { version: 1, revision: 0, preset: 'predbat', colours: { light: {}, dark: {} }, socBoundaries: { low: 20, medium: 50 } }
const normaliseTheme = (value: ThemeSettings): ThemeSettings => (value.preset as string) === 'coastal' ? { ...value, preset: 'predbat' } : value.preset ? value : { ...value, preset: Object.keys(value.colours.light).length || Object.keys(value.colours.dark).length ? 'custom' : 'predbat' }
declare global { interface Window { __predbatTheme?: ThemeSettings } }

function cacheTheme(value: ThemeSettings) {
  try {
    const prefix = window.location.pathname.match(/^(.*\/)ui(?:\/|$)/)?.[1] ?? '/'
    localStorage.setItem('predbat-theme-settings:' + prefix, JSON.stringify(value))
  } catch { /* The shared settings remain available from the server. */ }
}

/** Load shared preferences, retaining unsaved edits when another device saves. */
export function useThemeSettings() {
  const [saved, setSaved] = useState<ThemeSettings | null>(null)
  const [draft, setDraft] = useState<ThemeSettings>(() => window.__predbatTheme ?? DEFAULT_THEME)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const dirty = saved !== null && JSON.stringify(draft) !== JSON.stringify(saved)
  const dirtyRef = useRef(false)
  const savedRef = useRef(saved)
  useEffect(() => { dirtyRef.current = dirty; savedRef.current = saved }, [dirty, saved])

  useEffect(() => {
    let cancelled = false
    async function refresh() {
      try {
        const response = await fetch('../api/theme', { cache: 'no-store' })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const incoming = normaliseTheme(await response.json() as ThemeSettings)
        if (cancelled) return
        if (savedRef.current && incoming.revision < savedRef.current.revision) return
        cacheTheme(incoming)
        if (savedRef.current && incoming.revision === savedRef.current.revision) { setError(''); return }
        if (dirtyRef.current && savedRef.current && incoming.revision !== savedRef.current.revision) setConflict(true)
        else if (!dirtyRef.current) { setSaved(incoming); setDraft(incoming); setConflict(false) }
        setError('')
      } catch {
        if (!cancelled) setError('Unable to load shared theme settings.')
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 30_000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    const root = document.documentElement
    root.dataset.preset = draft.preset
    for (const mode of ['light', 'dark'] as const) {
      for (const [key] of COLOURS) {
        const value = draft.preset === 'custom' ? draft.colours[mode][key] : undefined
        if (value) root.style.setProperty(`--user-${mode}-${key}`, value)
        else root.style.removeProperty(`--user-${mode}-${key}`)
      }
    }
    // Existing canvas charts watch this attribute to refresh their colours.
    if (root.dataset.theme) root.setAttribute('data-theme', root.dataset.theme)
  }, [draft])

  async function reload() {
    try {
      const response = await fetch('../api/theme', { cache: 'no-store' })
      if (!response.ok) throw new Error()
      const incoming = normaliseTheme(await response.json() as ThemeSettings)
      cacheTheme(incoming)
      savedRef.current = incoming; dirtyRef.current = false
      setSaved(incoming); setDraft(incoming); setConflict(false); setError('')
    } catch { setError('Unable to load shared theme settings.') }
  }

  async function save(overwrite = false) {
    try {
      let revision = draft.revision
      if (overwrite) {
        const latest = await fetch('../api/theme', { cache: 'no-store' })
        if (!latest.ok) throw new Error()
        revision = (await latest.json() as ThemeSettings).revision
      }
      const response = await fetch('../api/theme', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...draft, revision }) })
      if (response.status === 409) { setConflict(true); return }
      if (!response.ok) throw new Error()
      const result = normaliseTheme(await response.json() as ThemeSettings)
      cacheTheme(result)
      savedRef.current = result; dirtyRef.current = false
      setSaved(result); setDraft(result); setConflict(false); setError('')
    } catch { setError('Unable to save theme settings. Your changes are still available here.') }
  }

  return { saved, draft, setDraft, dirty, error, conflict, save, reload, cancel: () => { if (saved) setDraft(saved); setConflict(false) } }
}
