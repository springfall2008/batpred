import { useEffect, useRef, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faDownload, faPause, faPlay, faRotate } from '@fortawesome/free-solid-svg-icons'

import { mergeLogLines, type LogLine } from '../utils/log'

import './LogPage.css'

type LogFilter = 'all' | 'info' | 'warnings' | 'errors'

type LogResponse = {
  status: 'success' | 'error'
  message?: string
  total_lines: number
  returned_lines: number
  search_matches?: number
  lines: LogLine[]
}

const FILTERS: { value: LogFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'info', label: 'Info' },
  { value: 'warnings', label: 'Warnings' },
  { value: 'errors', label: 'Errors' }
]

/** Display and incrementally refresh the Predbat log. */
export default function LogPage() {
  const viewerRef = useRef<HTMLDivElement>(null)
  const [filter, setFilter] = useState<LogFilter>('warnings')
  const [search, setSearch] = useState('')
  const [lines, setLines] = useState<LogLine[]>([])
  const [paused, setPaused] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [totalLines, setTotalLines] = useState(0)
  const [searchMatches, setSearchMatches] = useState<number | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    let interval: number | undefined
    let lastLine = 0
    let fetching = false

    async function fetchLines(reset: boolean) {
      if (fetching) return
      fetching = true
      try {
        const params = new URLSearchParams({
          filter,
          since: reset ? '0' : String(lastLine),
          max_lines: reset ? '1024' : '200'
        })
        if (search.trim()) params.set('search', search.trim())
        const response = await fetch(`./api/log?${params}`, { cache: 'no-store' })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = await response.json() as LogResponse
        if (data.status !== 'success') throw new Error(data.message ?? 'Unable to load the log')
        if (cancelled) return

        setLines((current) => mergeLogLines(reset ? [] : current, data.lines))
        lastLine = Math.max(lastLine, ...data.lines.map((line) => line.line_number), 0)
        setTotalLines(data.total_lines)
        setSearchMatches(search.trim() ? (data.search_matches ?? data.returned_lines) : null)
        setError('')
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : 'Unable to load the log')
      } finally {
        if (!cancelled) setLoading(false)
        fetching = false
      }
    }

    const delay = window.setTimeout(async () => {
      await fetchLines(true)
      if (!cancelled && !paused) interval = window.setInterval(() => fetchLines(false), 2000)
    }, search ? 350 : 0)

    return () => {
      cancelled = true
      window.clearTimeout(delay)
      if (interval) window.clearInterval(interval)
    }
  }, [filter, search, paused, refreshKey])

  useEffect(() => {
    if (autoScroll && viewerRef.current) viewerRef.current.scrollTop = 0
  }, [lines, autoScroll])

  return (
    <section className="log-page">
      <header className="log-page-header">
        <div>
          <h1>Log</h1>
          <p>{searchMatches === null ? `${lines.length} of ${totalLines} lines` : `${searchMatches} matches`}{paused ? ' · Updates paused' : ' · Live updates'}</p>
        </div>
        <div className="log-page-actions">
          <button type="button" onClick={() => setPaused((value) => !value)}>
            <FontAwesomeIcon icon={paused ? faPlay : faPause} />
            {paused ? 'Resume' : 'Pause'}
          </button>
          <button type="button" onClick={() => setRefreshKey((value) => value + 1)}>
            <FontAwesomeIcon icon={faRotate} />
            Refresh
          </button>
          <a href="./debug_log">
            <FontAwesomeIcon icon={faDownload} />
            Download
          </a>
        </div>
      </header>

      <div className="log-page-toolbar">
        <div className="log-filter" aria-label="Log level">
          {FILTERS.map((item) => (
            <button
              type="button"
              className={filter === item.value ? 'is-active' : ''}
              aria-pressed={filter === item.value}
              key={item.value}
              onClick={() => setFilter(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <input
          type="search"
          value={search}
          aria-label="Search the log"
          placeholder="Search the entire log…"
          onChange={(event) => setSearch(event.target.value)}
        />
        <label>
          <input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} />
          Follow newest
        </label>
      </div>

      {error && <div className="log-page-error" role="alert">Unable to refresh the log: {error}</div>}
      <div className="log-viewer" ref={viewerRef} aria-busy={loading}>
        {loading && !lines.length ? (
          <div className="log-page-empty">Loading log…</div>
        ) : !lines.length ? (
          <div className="log-page-empty">No matching log entries.</div>
        ) : lines.map((line) => (
          <div className={`log-line is-${line.type}`} key={line.line_number}>
            <span className="log-line-number">{line.line_number}</span>
            <time>{line.raw_timestamp}</time>
            <span className="log-line-message">{line.raw_message}</span>
          </div>
        ))}
      </div>
    </section>
  )
}
