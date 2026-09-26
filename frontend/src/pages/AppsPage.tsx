import { useEffect, useRef } from 'react'

import { CONFIG_GROUPS, configGroup } from '../utils/configGroups'

import './AppsPage.css'

type SchemaProperty = {
  description?: string
  enum?: unknown[]
}

type AppsSchema = {
  $defs?: {
    predbatApp?: {
      properties?: Record<string, SchemaProperty>
    }
  }
}

const FRAME_STYLES = `
  :root { color-scheme: light; }
  body {
    --apps-bg: #f5f7fa;
    --apps-surface: #fff;
    --apps-border: #dfe3ea;
    --apps-text: #172033;
    --apps-muted: #667085;
    height: auto !important;
    margin: 0 !important;
    padding: .75rem !important;
    border: 0 !important;
    background: var(--apps-bg) !important;
    color: var(--apps-text) !important;
  }
  body.dark-mode {
    --apps-bg: #111827;
    --apps-surface: #1f2937;
    --apps-border: #374151;
    --apps-text: #f3f4f6;
    --apps-muted: #aeb6c5;
    color-scheme: dark;
  }
  .apps-guide {
    margin: .75rem 0;
    padding: .8rem 1rem;
    border: 1px solid #90b8ed;
    border-left: 4px solid #3b82f6;
    border-radius: 8px;
    background: color-mix(in srgb, #3b82f6 8%, var(--apps-surface));
    color: var(--apps-text);
    font-size: .8rem;
    line-height: 1.45;
  }
  .apps-guide strong { display: block; margin-bottom: .15rem; }
  .save-controls {
    position: sticky;
    top: 0;
    z-index: 30;
    margin: 0 0 .75rem !important;
    padding: .7rem .85rem !important;
    border-color: var(--apps-border) !important;
    background: color-mix(in srgb, var(--apps-surface) 94%, transparent) !important;
    box-shadow: 0 4px 16px rgb(15 23 42 / 8%);
    backdrop-filter: blur(8px);
  }
  table.apps-settings-table {
    width: 100%;
    min-width: 760px;
    border: 1px solid var(--apps-border);
    border-spacing: 0;
    border-radius: 10px;
    overflow: hidden;
    background: var(--apps-surface);
    color: var(--apps-text);
    font-size: .76rem;
    table-layout: fixed;
  }
  table.apps-settings-table > thead > tr > th {
    position: sticky;
    top: 58px;
    z-index: 20;
    padding: .7rem .75rem;
    border-bottom: 1px solid var(--apps-border);
    background: var(--apps-surface) !important;
    color: var(--apps-muted);
    text-align: left;
    font-size: .64rem;
    text-transform: uppercase;
    letter-spacing: .04em;
  }
  table.apps-settings-table > thead > tr > th:nth-child(1) { width: 22%; }
  table.apps-settings-table > thead > tr > th:nth-child(2) { width: 36%; }
  table.apps-settings-table > thead > tr > th:nth-child(3) { width: 30%; }
  table.apps-settings-table > thead > tr > th:nth-child(4) { width: 12%; }
  table.apps-settings-table td table {
    width: 100%;
    min-width: 0;
    border: 0;
    border-collapse: collapse;
    background: transparent;
  }
  table.apps-settings-table td table td {
    padding: .25rem;
    border: 0;
    background: transparent;
  }
  table.apps-settings-table td table td:last-child,
  table.apps-settings-table td table button {
    width: auto;
    white-space: nowrap;
  }
  .apps-comparison-row > .apps-description,
  .apps-comparison-row > td:last-child { display: none; }
  .apps-comparison-row > td:nth-child(2) { padding: .8rem; }
  .apps-comparison-row > td:nth-child(2) > table > tbody {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: .75rem;
    counter-reset: comparison;
  }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    padding: .7rem;
    border: 1px solid color-mix(in srgb, var(--group-colour) 45%, var(--apps-border));
    border-radius: 8px;
    background: color-mix(in srgb, var(--group-colour) 4%, var(--apps-surface));
    counter-increment: comparison;
  }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] > td:first-child { display: none; }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] > td:nth-child(2) { min-width: 0; }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] > td:nth-child(2)::before {
    content: 'Comparison ' counter(comparison);
    display: block;
    margin-bottom: .45rem;
    color: var(--apps-muted);
    font-size: .65rem;
    font-weight: 700;
    letter-spacing: .04em;
    text-transform: uppercase;
  }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] > td:last-child {
    grid-column: 2;
    grid-row: 1;
    padding-left: .5rem;
  }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] table tr:not([id^="add_anchor_"]) > td {
    border-bottom: 1px solid color-mix(in srgb, var(--apps-border) 75%, transparent);
    vertical-align: top;
  }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] table tr:not([id^="add_anchor_"]) > td:first-child {
    font-weight: 700;
  }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[id^="add_anchor_"] {
    grid-column: 1 / -1;
    display: flex;
    justify-content: flex-end;
  }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[id^="add_anchor_"] > td:not(:last-child) { display: none; }
  tr[data-apps-group] { --group-colour: #64748b; }
  tr[data-apps-group="system"] { --group-colour: #3b82f6; }
  tr[data-apps-group="solar"] { --group-colour: #d99a00; }
  tr[data-apps-group="load"] { --group-colour: #0f9f8f; }
  tr[data-apps-group="battery"] { --group-colour: #17a864; }
  tr[data-apps-group="plan"] { --group-colour: #a855f7; }
  tr[data-apps-group="inverter"] { --group-colour: #6366f1; }
  tr[data-apps-group="car"] { --group-colour: #168aad; }
  tr[data-apps-group="tariffs"] { --group-colour: #e76f51; }
  tr[data-apps-group="heating"] { --group-colour: #ef4444; }
  tr[data-apps-group="iboost"] { --group-colour: #f97316; }
  tr[data-apps-group] > td {
    padding: .65rem .75rem;
    border: 0;
    border-bottom: 1px solid color-mix(in srgb, var(--apps-border) 70%, transparent);
    background: color-mix(in srgb, var(--group-colour) 5%, var(--apps-surface));
    color: var(--apps-text);
    vertical-align: middle;
    overflow-wrap: anywhere;
  }
  tr[data-apps-group] > td:first-child {
    border-left: 4px solid var(--group-colour);
    font-weight: 700;
  }
  tr[data-apps-group]:hover > td { background: color-mix(in srgb, var(--group-colour) 10%, var(--apps-surface)); }
  .apps-group-row th {
    padding: .65rem .75rem;
    border-top: 1px solid var(--apps-border);
    border-bottom: 1px solid var(--apps-border);
    background: color-mix(in srgb, var(--group-colour) 13%, var(--apps-surface));
    color: var(--apps-text);
    text-align: left;
    font-size: .7rem;
    text-transform: uppercase;
    letter-spacing: .04em;
  }
  .apps-group-row:first-child th { border-top: 0; }
  .apps-description { color: var(--apps-muted) !important; line-height: 1.4; font-weight: 400; }
  .edit-input, .apps-choice-input {
    width: min(100%, 28rem) !important;
    box-sizing: border-box;
    padding: .45rem .55rem !important;
    border: 1px solid var(--apps-border) !important;
    border-radius: 5px !important;
    background: var(--apps-surface) !important;
    color: var(--apps-text) !important;
  }
  .edit-button, .save-button, .cancel-button, .save-all-button, .discard-all-button { border-radius: 5px !important; }
  .row-changed > td { background: color-mix(in srgb, #fbbf24 16%, var(--apps-surface)) !important; }
  @media (max-width: 700px) {
    body { padding: .5rem !important; overflow-x: auto; }
    .apps-guide { min-width: 700px; }
  }
`

/** Host Predbat's structured apps.yaml settings editor inside the modern shell. */
export default function AppsPage() {
  const frameRef = useRef<HTMLIFrameElement>(null)
  const schemaRef = useRef<Record<string, SchemaProperty>>({})

  function styleEmbeddedPage() {
    const frameDocument = frameRef.current?.contentDocument
    if (!frameDocument) return

    frameDocument.querySelector('.menu-bar')?.remove()
    const dark = document.documentElement.dataset.theme === 'dark'
    frameDocument.documentElement.classList.toggle('dark-mode', dark)
    frameDocument.body.classList.toggle('dark-mode', dark)

    if (!frameDocument.getElementById('modern-apps-frame-style')) {
      const style = frameDocument.createElement('style')
      style.id = 'modern-apps-frame-style'
      style.textContent = FRAME_STYLES
      frameDocument.head.append(style)
    }

    const saveControls = frameDocument.getElementById('saveControls')
    if (saveControls && !frameDocument.querySelector('.apps-guide')) {
      const guide = frameDocument.createElement('aside')
      guide.className = 'apps-guide'
      guide.innerHTML = '<strong>Editing apps.yaml</strong>Use Edit to stage a value, then save all changes together. Suggested fields such as web_ui and timezone offer known choices while still accepting a custom value. Hover over a setting name or read its Description column for guidance.'
      saveControls.after(guide)
    }

    const table = frameDocument.querySelector('table')
    const body = table?.tBodies[0]
    if (!table || !body) return
    table.classList.add('apps-settings-table')

    const rows = Array.from(body.rows).filter((row) => row.dataset.argName)
    const comparisonRow = rows.find((row) => row.dataset.argName === 'compare_list')
    if (comparisonRow) {
      comparisonRow.classList.add('apps-comparison-row')
      comparisonRow.cells[1].colSpan = 3
    }
    const header = Array.from(body.rows).find((row) => !row.dataset.argName)
    if (header && !table.tHead) table.createTHead().append(header)
    const headerRow = table.tHead?.rows[0]
    if (headerRow && !headerRow.querySelector('.apps-description-heading')) {
      const heading = frameDocument.createElement('th')
      heading.className = 'apps-description-heading'
      heading.textContent = 'Description'
      headerRow.insertBefore(heading, headerRow.lastElementChild)
    }

    body.querySelectorAll('.apps-group-row').forEach((row) => row.remove())
    for (const group of CONFIG_GROUPS) {
      const groupRows = rows.filter((row) => configGroup(row.dataset.argName ?? '') === group)
      if (!groupRows.length) continue

      const groupRow = body.insertRow()
      groupRow.className = 'apps-group-row'
      groupRow.dataset.appsGroup = group.toLowerCase()
      const groupHeading = frameDocument.createElement('th')
      groupHeading.colSpan = 4
      groupHeading.textContent = `${group} · ${groupRows.length}`
      groupRow.append(groupHeading)

      for (const row of groupRows) {
        const name = row.dataset.argName ?? ''
        const property = schemaRef.current[name]
        const description = property?.description ?? 'Custom apps.yaml option.'
        row.dataset.appsGroup = group.toLowerCase()
        row.cells[0]?.setAttribute('title', description)

        let descriptionCell = row.querySelector<HTMLTableCellElement>('.apps-description')
        if (!descriptionCell) {
          descriptionCell = frameDocument.createElement('td')
          descriptionCell.className = 'apps-description'
          row.insertBefore(descriptionCell, row.lastElementChild)
        }
        descriptionCell.textContent = description
        body.append(row)

        const options = name === 'web_ui'
          ? property?.enum?.filter((value): value is string => typeof value === 'string') ?? ['legacy', 'modern']
          : name === 'timezone' && typeof Intl.supportedValuesOf === 'function'
            ? Intl.supportedValuesOf('timeZone')
            : []
        const editButton = row.querySelector<HTMLButtonElement>('.edit-button')
        if (!options.length || !editButton || editButton.dataset.choiceEditor) continue
        editButton.dataset.choiceEditor = 'true'
        editButton.addEventListener('click', () => queueMicrotask(() => {
          const input = frameDocument.getElementById(`input_${row.id.replace('row_', '')}`) as HTMLInputElement | null
          if (!input) return
          const list = frameDocument.createElement('datalist')
          list.id = `options_${row.id}`
          list.append(...options.map((value) => {
            const option = frameDocument.createElement('option')
            option.value = value
            return option
          }))
          input.classList.add('apps-choice-input')
          input.setAttribute('list', list.id)
          input.setAttribute('placeholder', 'Choose or enter a custom value')
          input.after(list)
        }))
      }
    }
  }

  useEffect(() => {
    let active = true
    fetch('./api/apps_schema', { cache: 'no-store' })
      .then((response) => response.ok ? response.json() as Promise<AppsSchema> : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((schema) => {
        if (!active) return
        schemaRef.current = schema.$defs?.predbatApp?.properties ?? {}
        styleEmbeddedPage()
      })
      .catch(() => undefined)
    return () => { active = false }
  }, [])

  useEffect(() => {
    const observer = new MutationObserver(styleEmbeddedPage)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  return (
    <section className="apps-page">
      <header className="apps-page-header">
        <h1>Apps</h1>
        <p>Review and update the active values loaded from apps.yaml.</p>
      </header>
      <iframe ref={frameRef} className="apps-frame" src="./legacy_apps" title="Predbat apps.yaml settings" onLoad={styleEmbeddedPage} />
    </section>
  )
}
