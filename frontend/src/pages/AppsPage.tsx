import { useEffect, useRef, useState } from 'react'

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
  .apps-section-nav {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: .4rem;
    margin: .75rem 0;
    padding: .65rem .75rem;
    border: 1px solid var(--apps-border);
    border-radius: 8px;
    background: var(--apps-surface);
  }
  .apps-section-nav strong {
    margin-right: .25rem;
    color: var(--apps-muted);
    font-size: .68rem;
    letter-spacing: .04em;
    text-transform: uppercase;
  }
  .apps-section-nav a {
    padding: .32rem .48rem;
    border: 1px solid var(--apps-border);
    border-radius: 5px;
    background: var(--apps-bg);
    color: var(--apps-text);
    font-size: .7rem;
    font-weight: 650;
    text-decoration: none;
  }
  .apps-section-nav a:hover,
  .apps-section-nav a:focus-visible {
    border-color: #3b82f6;
    color: #2563eb;
    outline: none;
  }
  .save-controls { display: none !important; }
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
    top: 0;
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

  /* Nested collections use the same columns at every depth, never a shrinking table. */
  table.apps-settings-table td table { table-layout: fixed; }
  table.apps-settings-table td table > tbody { display: grid; gap: .4rem; }
  table.apps-settings-table td table > tbody > tr {
    display: grid;
    grid-template-columns: minmax(8rem, 1fr) minmax(0, 2fr) 8.5rem;
    align-items: start;
    border-bottom: 1px solid var(--apps-border);
  }
  table.apps-settings-table td table > tbody > tr > td { min-width: 0; padding: .5rem; }
  table.apps-settings-table td table > tbody > tr > td:last-child {
    display: flex; justify-content: flex-end; gap: .3rem; flex-wrap: wrap;
  }
  table.apps-settings-table td table button { margin: 0; }
  table.apps-settings-table tr[data-nested-path$="]"] > td:first-child {
    font-size: 0;
  }
  table.apps-settings-table tr[data-nested-path$="]"] > td:first-child::after {
    content: 'Item ' counter(list-item);
    font-size: .75rem; color: var(--apps-muted); font-weight: 600;
  }
  table.apps-settings-table td table > tbody { counter-reset: list-item; }
  table.apps-settings-table tr[data-nested-path$="]"] { counter-increment: list-item; }
  table.apps-settings-table td table > tbody > tr:has(> td:nth-child(2) > table) > td:nth-child(2) {
    grid-column: 1 / -1; grid-row: 2;
    padding: .5rem .75rem .75rem;
    border: 1px solid var(--apps-border); border-radius: 6px;
    background: color-mix(in srgb, var(--apps-surface) 75%, transparent);
  }
  table.apps-settings-table td table > tbody > tr:has(> td:nth-child(2) > table) > td:last-child {
    grid-column: 3; grid-row: 1;
  }
  table.apps-settings-table td table > tbody > tr[id^="add_anchor_"] {
    display: flex; justify-content: flex-end; border: 0;
  }
  table.apps-settings-table td table > tbody > tr[id^="add_anchor_"] > td:not(:last-child) { display: none; }
  table.apps-settings-table > tbody > tr:has(> td:nth-child(2) > table) {
    display: grid; grid-template-columns: 1fr;
  }
  table.apps-settings-table > tbody > tr:has(> td:nth-child(2) > table) > td:nth-child(2) {
    padding: .75rem;
  }
  table.apps-settings-table > tbody > tr:has(> td:nth-child(2) > table) > td:last-child:empty { display: none; }
  table.apps-settings-table > tbody > tr:has(> td:nth-child(2) > table) > .apps-description {
    grid-row: 2; padding-top: 0;
  }
  /* Keep collection rows spanning the outer four-column table. */
  table.apps-settings-table > tbody { display: block; }
  table.apps-settings-table > thead { display: table; width: 100%; table-layout: fixed; }
  table.apps-settings-table > tbody > tr:not(:has(> td:nth-child(2) > table)) {
    display: table; width: 100%; table-layout: fixed;
  }
  table.apps-settings-table > tbody > tr[data-arg-name]:not(:has(> td:nth-child(2) > table)) > td:nth-child(1) { width: 22%; }
  table.apps-settings-table > tbody > tr[data-arg-name]:not(:has(> td:nth-child(2) > table)) > td:nth-child(2) { width: 36%; }
  table.apps-settings-table > tbody > tr[data-arg-name]:not(:has(> td:nth-child(2) > table)) > td:nth-child(3) { width: 30%; }
  table.apps-settings-table > tbody > tr[data-arg-name]:not(:has(> td:nth-child(2) > table)) > td:nth-child(4) { width: 12%; }
  .apps-comparison-row > td:nth-child(2) > table > tbody > tr[data-nested-path] {
    padding: .75rem; border: 1px solid var(--apps-border); border-radius: 8px;
    background: var(--apps-surface);
  }
  table.apps-settings-table .apps-comparison-row > td:nth-child(2) > table > tbody > tr > td:first-child::after {
    content: 'Comparison ' counter(list-item);
  }
  .apps-comparison-row {
    --group-colour: #e76f51;
    margin-bottom: .75rem;
    border: 1px solid color-mix(in srgb, var(--group-colour) 32%, var(--apps-border));
    border-left: 5px solid var(--group-colour);
    border-radius: 10px;
    background: color-mix(in srgb, var(--group-colour) 4%, var(--apps-surface));
    overflow: hidden;
  }
  table.apps-settings-table > tbody > .apps-comparison-row > td:first-child,
  table.apps-settings-table > tbody > .apps-comparison-row > .apps-description,
  table.apps-settings-table > tbody > .apps-comparison-row > td:last-child:empty {
    display: none;
  }
  table.apps-settings-table > tbody > .apps-comparison-row > td:nth-child(2) {
    padding: 0 !important;
  }
  .apps-comparison-intro {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: .9rem 1rem;
    border-bottom: 1px solid color-mix(in srgb, var(--group-colour) 28%, var(--apps-border));
    background: color-mix(in srgb, var(--group-colour) 11%, var(--apps-surface));
  }
  .apps-comparison-intro h3,
  .apps-comparison-intro p { margin: 0; }
  .apps-comparison-intro h3 { font-size: .92rem; }
  .apps-comparison-intro p { margin-top: .2rem; color: var(--apps-muted); font-size: .74rem; }
  .apps-comparison-count {
    flex: 0 0 auto;
    padding: .28rem .5rem;
    border: 1px solid color-mix(in srgb, var(--group-colour) 35%, var(--apps-border));
    border-radius: 999px;
    background: var(--apps-surface);
    color: var(--apps-muted);
    font-size: .68rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .apps-comparison-row > td:nth-child(2) > table { padding: .85rem; }
  .apps-comparison-row > td:nth-child(2) > table > tbody { gap: .85rem; }
  table.apps-settings-table .apps-comparison-card {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    padding: 0;
    border: 1px solid color-mix(in srgb, var(--group-colour) 34%, var(--apps-border));
    border-left: 4px solid var(--group-colour);
    border-radius: 9px;
    background: var(--apps-surface);
    box-shadow: 0 2px 7px rgb(15 23 42 / .06);
    overflow: hidden;
  }
  table.apps-settings-table .apps-comparison-card > td:first-child {
    display: flex;
    align-items: center;
    min-height: 2.6rem;
    padding: .65rem .8rem;
    background: color-mix(in srgb, var(--group-colour) 8%, var(--apps-surface));
  }
  table.apps-settings-table .apps-comparison-card > td:first-child::after {
    content: attr(data-item-label);
    color: var(--apps-text);
    font-size: .78rem;
    letter-spacing: .02em;
    text-transform: uppercase;
  }
  table.apps-settings-table .apps-comparison-card > td:nth-child(2) {
    grid-column: 1 / -1;
    grid-row: 2;
    margin: 0;
    padding: .35rem .75rem .75rem;
    border: 0;
    border-top: 1px solid var(--apps-border);
    border-radius: 0;
    background: var(--apps-surface);
  }
  table.apps-settings-table .apps-comparison-card > td:last-child {
    grid-column: 2;
    grid-row: 1;
    align-items: center;
    padding: .45rem .7rem;
    background: color-mix(in srgb, var(--group-colour) 8%, var(--apps-surface));
  }
  table.apps-settings-table .apps-comparison-card > td:nth-child(2) > table > tbody {
    gap: 0;
  }
  table.apps-settings-table .apps-comparison-card > td:nth-child(2) > table > tbody > tr {
    grid-template-columns: minmax(8rem, 1fr) minmax(0, 2fr) 8.5rem;
  }
  table.apps-settings-table .apps-rate-group {
    margin: .55rem 0;
    border: 1px solid var(--apps-border);
    border-radius: 8px;
    background: color-mix(in srgb, var(--group-colour) 3%, var(--apps-surface));
    overflow: hidden;
  }
  table.apps-settings-table .apps-rate-group > td:first-child {
    display: flex;
    align-items: center;
    padding: .6rem .7rem;
    background: color-mix(in srgb, var(--group-colour) 7%, var(--apps-surface));
    font-size: 0;
  }
  table.apps-settings-table .apps-rate-group > td:first-child::after {
    content: attr(data-item-label);
    color: var(--apps-text);
    font-size: .75rem;
    font-weight: 700;
  }
  table.apps-settings-table .apps-rate-group > td:nth-child(2) {
    margin: 0;
    padding: .45rem .65rem .65rem;
    border: 0;
    border-top: 1px solid var(--apps-border);
    border-radius: 0;
  }
  table.apps-settings-table .apps-rate-period {
    grid-template-columns: minmax(6rem, .7fr) minmax(0, 2fr) 8.5rem;
    margin-top: .35rem;
    border: 1px solid color-mix(in srgb, var(--apps-border) 85%, transparent);
    border-radius: 6px;
    background: var(--apps-surface);
  }
  table.apps-settings-table .apps-rate-period > td:first-child {
    font-size: 0;
  }
  table.apps-settings-table .apps-rate-period > td:first-child::after {
    content: attr(data-item-label);
    color: var(--apps-muted);
    font-size: .7rem;
    font-weight: 700;
  }
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
  .apps-group-row {
    display: table;
    width: 100%;
    margin-top: .8rem;
    table-layout: fixed;
  }
  .apps-group-row th {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: .8rem .85rem;
    border-top: 1px solid color-mix(in srgb, var(--group-colour) 34%, var(--apps-border));
    border-bottom: 1px solid color-mix(in srgb, var(--group-colour) 34%, var(--apps-border));
    border-left: 5px solid var(--group-colour);
    background: color-mix(in srgb, var(--group-colour) 17%, var(--apps-surface));
    color: var(--apps-text);
    text-align: left;
    font-size: .75rem;
    text-transform: uppercase;
    letter-spacing: .055em;
  }
  .apps-group-row th::after {
    content: attr(data-item-count);
    color: var(--apps-muted);
    font-size: .65rem;
    font-weight: 600;
    letter-spacing: 0;
    text-transform: none;
  }
  .apps-group-row:first-child { margin-top: 0; }
  .apps-group-row:first-child th { border-top: 0; }
  .apps-group-row[id] { scroll-margin-top: 3.25rem; }
  .apps-comparison-heading {
    margin-top: 1.35rem;
  }
  .apps-comparison-heading th {
    border-color: color-mix(in srgb, #e76f51 40%, var(--apps-border));
    border-left-color: #e76f51;
    background: color-mix(in srgb, #e76f51 20%, var(--apps-surface));
  }
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
  const [saveState, setSaveState] = useState({ text: 'No unsaved changes', saveDisabled: true, discardDisabled: true })
  const saveObserverRef = useRef<MutationObserver | null>(null)
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
    saveObserverRef.current?.disconnect()
    if (saveControls) {
      // Keep the header controls in sync while the legacy editor owns staging and saving.
      const syncSaveControls = () => setSaveState({
        text: frameDocument.getElementById('changeCount')?.textContent ?? 'No unsaved changes',
        saveDisabled: (frameDocument.getElementById('saveAllButton') as HTMLButtonElement | null)?.disabled ?? true,
        discardDisabled: (frameDocument.getElementById('discardAllButton') as HTMLButtonElement | null)?.disabled ?? true,
      })
      syncSaveControls()
      saveObserverRef.current = new MutationObserver(syncSaveControls)
      saveObserverRef.current.observe(saveControls, { subtree: true, childList: true, characterData: true, attributes: true })
    }
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

      const comparisonTable = comparisonRow.cells[1]?.querySelector('table')
      const comparisonRows = Array.from(comparisonTable?.querySelectorAll<HTMLTableRowElement>('tr[data-nested-path]') ?? [])
      const comparisonCards = comparisonRows.filter((row) => /^compare_list\[\d+\]$/.test(row.dataset.nestedPath ?? ''))

      if (comparisonTable && !comparisonRow.querySelector('.apps-comparison-intro')) {
        const intro = frameDocument.createElement('div')
        intro.className = 'apps-comparison-intro'
        const copy = frameDocument.createElement('div')
        const title = frameDocument.createElement('h3')
        title.textContent = 'Tariff comparisons'
        const description = frameDocument.createElement('p')
        description.textContent = 'Each tariff is shown separately, with its import and export periods grouped underneath.'
        const count = frameDocument.createElement('span')
        count.className = 'apps-comparison-count'
        count.textContent = comparisonCards.length + ' tariff' + (comparisonCards.length === 1 ? '' : 's')
        copy.append(title, description)
        intro.append(copy, count)
        comparisonTable.before(intro)
      }

      for (const [index, row] of comparisonCards.entries()) {
        const path = row.dataset.nestedPath ?? ''
        const nameRow = comparisonRows.find((candidate) => candidate.dataset.nestedPath === path + '.name')
        const name = nameRow?.cells[1]?.textContent?.trim()
        row.classList.add('apps-comparison-card')
        if (row.cells[0]) row.cells[0].dataset.itemLabel = name ? 'Tariff ' + (index + 1) + ': ' + name : 'Tariff ' + (index + 1)
      }

      for (const row of comparisonRows) {
        const path = row.dataset.nestedPath ?? ''
        const rateGroup = path.match(/^compare_list\[\d+\]\.(rates_import|rates_export)$/)
        const ratePeriod = path.match(/^compare_list\[\d+\]\.(rates_import|rates_export)\[(\d+)\]$/)
        if (rateGroup) {
          row.classList.add('apps-rate-group')
          if (row.cells[0]) row.cells[0].dataset.itemLabel = rateGroup[1] === 'rates_import' ? 'Import rate periods' : 'Export rate periods'
        } else if (ratePeriod) {
          row.classList.add('apps-rate-period')
          if (row.cells[0]) row.cells[0].dataset.itemLabel = 'Period ' + (Number(ratePeriod[2]) + 1)
        }
      }
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
    frameDocument.querySelector('.apps-section-nav')?.remove()
    const sectionLinks: Array<{ id: string, label: string }> = []

    for (const group of CONFIG_GROUPS) {
      const groupRows = rows.filter((row) => row !== comparisonRow && configGroup(row.dataset.argName ?? '') === group)
      if (!groupRows.length) continue

      const sectionId = 'apps-section-' + group.toLowerCase().replace(/[^a-z0-9]+/g, '-')
      const groupRow = body.insertRow()
      groupRow.id = sectionId
      groupRow.className = 'apps-group-row'
      groupRow.dataset.appsGroup = group.toLowerCase()
      const groupHeading = frameDocument.createElement('th')
      groupHeading.colSpan = 4
      groupHeading.textContent = group
      groupHeading.dataset.itemCount = groupRows.length + ' setting' + (groupRows.length === 1 ? '' : 's')
      groupRow.append(groupHeading)
      sectionLinks.push({ id: sectionId, label: group })

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

    if (comparisonRow) {
      const comparisonHeading = body.insertRow()
      comparisonHeading.id = 'apps-section-tariff-comparison'
      comparisonHeading.className = 'apps-group-row apps-comparison-heading'
      comparisonHeading.dataset.appsGroup = 'tariffs'
      const heading = frameDocument.createElement('th')
      heading.colSpan = 4
      heading.textContent = 'Tariff comparison'
      heading.dataset.itemCount = 'Configured scenarios'
      comparisonHeading.append(heading)
      body.append(comparisonRow)
      sectionLinks.push({ id: comparisonHeading.id, label: 'Tariff comparison' })
    }

    if (sectionLinks.length) {
      const nav = frameDocument.createElement('nav')
      nav.className = 'apps-section-nav'
      nav.setAttribute('aria-label', 'Apps sections')
      const label = frameDocument.createElement('strong')
      label.textContent = 'Jump to'
      nav.append(label)
      for (const section of sectionLinks) {
        const link = frameDocument.createElement('a')
        link.href = '#' + section.id
        link.textContent = section.label
        nav.append(link)
      }
      const guide = frameDocument.querySelector('.apps-guide')
      if (guide) guide.after(nav)
      else table.before(nav)
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
    return () => { observer.disconnect(); saveObserverRef.current?.disconnect() }
  }, [])

  return (
    <section className="apps-page">
      <header className="apps-page-header">
        <div>
          <h1>Apps</h1>
          <p>Review and update the active values loaded from apps.yaml.</p>
        </div>
        <div className="apps-page-actions" aria-label="Pending changes">
          <span role="status">{saveState.text}</span>
          <button type="button" disabled={saveState.saveDisabled} onClick={() => frameRef.current?.contentDocument?.getElementById('saveAllButton')?.click()}>Save changes</button>
          <button type="button" disabled={saveState.discardDisabled} onClick={() => frameRef.current?.contentDocument?.getElementById('discardAllButton')?.click()}>Discard changes</button>
        </div>
      </header>
      <iframe ref={frameRef} className="apps-frame" src="./legacy_apps" title="Predbat apps.yaml settings" onLoad={styleEmbeddedPage} />
    </section>
  )
}
