import { useEffect, useRef, useState } from 'react'

import * as monaco from 'monaco-editor'
import { configureMonacoYaml } from 'monaco-yaml'
import EditorWorker from '../editor.worker?worker'
import YamlWorker from '../yaml.worker?worker'
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

self.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    return label === 'yaml' ? new YamlWorker() : new EditorWorker()
  }
}

/** Edit apps.yaml with schema-aware YAML validation and completion. */
export default function AppsEditorPage() {
  const containerRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null)
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
        fetch('./api/apps_yaml', { cache: 'no-store' }),
        fetch('./api/apps_schema', { cache: 'no-store' }),
        fetch('./api/entities?all=1', { cache: 'no-store' })
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

    const modelUri = monaco.Uri.parse('file:///apps.yaml')
    const yamlSupport = configureMonacoYaml(monaco, {
      customTags: ['!secret scalar'],
      enableSchemaRequest: false,
      schemas: [
        {
          uri: 'predbat://apps.schema.json',
          fileMatch: ['*'],
          schema,
        },
      ],
      validate: true,
      completion: true,
      hover: true,
      format: {},
    })
    const model = monaco.editor.createModel(source.content, 'yaml', modelUri)
    const editor = monaco.editor.create(
      containerRef.current,
      {
        model,

        automaticLayout: true,
        fontSize: 14,

        minimap: {
          enabled: false,
        },

        scrollBeyondLastLine: false,
        tabSize: 2,

        theme:
          document.documentElement.dataset.theme === 'dark'
            ? 'vs-dark'
            : 'vs',

        quickSuggestions: {
          other: true,
          comments: false,
          strings: true,
        },

        suggestOnTriggerCharacters: true,
        wordBasedSuggestions: 'off',

        wordWrap: 'off',
      },
    )
    editorRef.current = editor

    const updateMarkers = () => setHasErrors(monaco.editor.getModelMarkers({ resource: modelUri }).some((marker) => marker.severity === monaco.MarkerSeverity.Error))
    const contentListener = editor.onDidChangeModelContent(() => setDirty(editor.getValue() !== originalRef.current))
    const markerListener = monaco.editor.onDidChangeMarkers((uris) => {
      if (uris.some((uri) => uri.toString() === modelUri.toString())) updateMarkers()
    })
    const themeObserver = new MutationObserver(() => monaco.editor.setTheme(document.documentElement.dataset.theme === 'dark' ? 'vs-dark' : 'vs'))
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })

    return () => {
      themeObserver.disconnect()
      markerListener.dispose()
      contentListener.dispose()
      editor.dispose()
      model.dispose()
        ; (yamlSupport as unknown as { dispose: () => void }).dispose()
      editorRef.current = null
    }
  }, [source, schema])

  async function save() {
    const editor = editorRef.current
    if (!editor || !source || hasErrors || !window.confirm('Saving changes will restart Predbat. Continue?')) return
    setSaving(true)
    setMessage('')
    try {
      const content = editor.getValue()
      const response = await fetch('./api/apps_yaml', {
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
        <div ref={containerRef} className="apps-editor-monaco" aria-label="apps.yaml editor" />
      )}
      {message && <div className="apps-editor-message" role="status">{message}</div>}
    </section>
  )
}
