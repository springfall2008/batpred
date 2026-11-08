import { useEffect, useRef, useState } from 'react'

import { basicSetup } from 'codemirror'
import { EditorState, Compartment } from '@codemirror/state'
import { EditorView, hoverTooltip, keymap } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { yaml, yamlLanguage } from '@codemirror/lang-yaml'
import { indentUnit } from '@codemirror/language'
import { oneDark } from '@codemirror/theme-one-dark'
import { linter } from '@codemirror/lint'
import { stateExtensions } from '@invopop/codemirror-json-schema'
import { yamlSchemaHover } from '@invopop/codemirror-json-schema/yaml'
import { createAppsYamlLinter, createAppsYamlCompletion, appsYamlHoverOptions } from '../utils/appsEditor'
import { addEntitySuggestions, type HaEntity } from '../utils/appsSchema'

import './AppsEditorPage.css'

type AppsYamlResponse = {
  content: string
  checksum: string
}

type SaveResponse = {
  checksum?: string
  error?: string
}

/** Edit apps.yaml with schema-aware YAML validation and completion. */
export default function AppsEditorPage() {
  const containerRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<EditorView | null>(null)
  const originalRef = useRef('')
  const [source, setSource] = useState<AppsYamlResponse | null>(null)
  const [schema, setSchema] = useState<Record<string, unknown> | null>(null)
  const [dirty, setDirty] = useState(false)
  const [hasErrors, setHasErrors] = useState(false)
  const [message, setMessage] = useState('')
  const [loadingError, setLoadingError] = useState('')
  const [saving, setSaving] = useState(false)

  async function load() {
    try {
      const [yamlResponse, schemaResponse, entitiesResponse] = await Promise.all([
        fetch('../api/apps_yaml', { cache: 'no-store' }),
        fetch('../api/apps_schema', { cache: 'no-store' }),
        fetch('../api/entities?all=1', { cache: 'no-store' })
      ])
      if (!yamlResponse.ok || !schemaResponse.ok) throw new Error(`HTTP ${yamlResponse.ok ? schemaResponse.status : yamlResponse.status}`)
      const yaml = await yamlResponse.json() as AppsYamlResponse
      const appSchema = await schemaResponse.json() as Record<string, unknown>
      const entities = entitiesResponse.ok ? await entitiesResponse.json() as HaEntity[] : []
      setLoadingError('')
      setSource(yaml)
      setSchema(addEntitySuggestions(appSchema, entities) as Record<string, unknown>)
      originalRef.current = yaml.content
      setDirty(false)
    } catch (error) {
      setLoadingError(error instanceof Error ? error.message : 'Unable to load apps.yaml')
    }
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- initial API load
    load()
  }, [])

  useEffect(() => {
    if (!containerRef.current || !source || !schema) return

    const theme = new Compartment()
    const currentTheme = () => document.documentElement.dataset.theme === 'dark' ? oneDark : EditorView.theme({}, { dark: false })
    const validate = createAppsYamlLinter()
    const editor = new EditorView({
      parent: containerRef.current,
      state: EditorState.create({
        doc: source.content,
        extensions: [
          basicSetup,
          yaml(),
          stateExtensions(schema),
          yamlLanguage.data.of({ autocomplete: createAppsYamlCompletion() }),
          hoverTooltip(yamlSchemaHover(appsYamlHoverOptions)),
          linter(view => {
            const diagnostics = validate(view)
            setHasErrors(diagnostics.some(diagnostic => diagnostic.severity === 'error'))
            return diagnostics
          }),
          indentUnit.of('  '),
          EditorState.tabSize.of(2),
          keymap.of([indentWithTab]),
          theme.of(currentTheme()),
          EditorView.contentAttributes.of({ 'aria-label': 'apps.yaml editor' }),
          EditorView.updateListener.of(update => {
            if (update.docChanged) setDirty(update.state.doc.toString() !== originalRef.current)
          }),
        ],
      }),
    })
    editorRef.current = editor

    const themeObserver = new MutationObserver(() => editor.dispatch({ effects: theme.reconfigure(currentTheme()) }))
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })

    return () => {
      themeObserver.disconnect()
      editor.destroy()
      editorRef.current = null
    }
  }, [source, schema])

  async function save() {
    const editor = editorRef.current
    if (!editor || !source) return
    const errors = createAppsYamlLinter()(editor).some(diagnostic => diagnostic.severity === 'error')
    setHasErrors(errors)
    if (errors || !window.confirm('Saving changes will restart Predbat. Continue?')) return
    setSaving(true)
    setMessage('')
    try {
      const content = editor.state.doc.toString()
      const response = await fetch('../api/apps_yaml', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, checksum: source.checksum })
      })
      const result = await response.json() as SaveResponse
      if (!response.ok || !result.checksum) throw new Error(result.error ?? `HTTP ${response.status}`)
      originalRef.current = content
      setSource({ content, checksum: result.checksum })
      setDirty(false)
      setMessage('apps.yaml saved. A backup was created.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Unable to save apps.yaml')
    } finally {
      setSaving(false)
    }
  }

  function revert() {
    if (!dirty || window.confirm('Discard your unsaved changes?')) load()
  }

  return (
    <section className="apps-editor-page">
      <header className="apps-editor-header">
        <div>
          <h1>Apps.yaml editor</h1>
          <p>Schema-aware completion and validation for Predbat configuration.</p>
        </div>
        <div className="apps-editor-actions">
          <span className={hasErrors ? 'is-error' : ''}>{hasErrors ? 'Fix validation errors before saving' : dirty ? 'Unsaved changes' : 'No unsaved changes'}</span>
          <button type="button" className="secondary" disabled={!dirty || saving} onClick={revert}>Revert</button>
          <button type="button" disabled={!dirty || hasErrors || saving} onClick={save}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </header>

      {loadingError ? (
        <div className="apps-editor-error" role="alert">Unable to load the editor: {loadingError} <button type="button" onClick={load}>Retry</button></div>
      ) : !source || !schema ? (
        <div className="apps-editor-message">Loading apps.yaml…</div>
      ) : (
        <div ref={containerRef} className="apps-editor-codemirror" aria-label="apps.yaml editor" />
      )}
      {message && <div className="apps-editor-message" role="status">{message}</div>}
    </section>
  )
}
