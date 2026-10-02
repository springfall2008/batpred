import { useCallback, useEffect, useMemo, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faChevronRight, faDownload, faRotate } from '@fortawesome/free-solid-svg-icons'

import './InternalsPage.css'

type InternalMember = {
  key: string
  type: string
  value: unknown
  expandable: boolean
  path: string
  size?: number
}

type StackFrame = { file: string; line: number; name: string; code: string }
type AsyncTask = { name: string; state: string; stack: StackFrame[] }
type ThreadInfo = {
  name: string
  id: number
  alive: boolean | null
  daemon: boolean | null
  stack: StackFrame[]
  tasks: AsyncTask[]
}

/** Inspect Predbat runtime objects and thread stacks. */
export default function InternalsPage() {
  const [path, setPath] = useState('predbat')
  const [members, setMembers] = useState<InternalMember[]>([])
  const [threads, setThreads] = useState<ThreadInfo[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const loadObject = useCallback(async (nextPath: string) => {
    setLoading(true)
    try {
      const response = await fetch(`./api/internals?path=${encodeURIComponent(nextPath)}`, { cache: 'no-store' })
      const data = await response.json() as { success: boolean; members?: InternalMember[]; error?: string }
      if (!response.ok || !data.success) throw new Error(data.error ?? `HTTP ${response.status}`)
      setPath(nextPath)
      setMembers(data.members ?? [])
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to load internals')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadThreads = useCallback(async () => {
    try {
      const response = await fetch('./api/internals/threads', { cache: 'no-store' })
      const data = await response.json() as { success: boolean; threads?: ThreadInfo[]; error?: string }
      if (!response.ok || !data.success) throw new Error(data.error ?? `HTTP ${response.status}`)
      setThreads(data.threads ?? [])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to load thread stacks')
    }
  }, [])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- initial API load
    loadObject('predbat')
    loadThreads()
  }, [loadObject, loadThreads])

  const filteredMembers = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return members
    return members.filter((member) => `${member.key} ${member.type} ${String(member.value)}`.toLowerCase().includes(query))
  }, [members, search])

  const pathParts = path.split('::')

  return (
    <section className="internals-page">
      <header className="internals-page-header">
        <div>
          <h1>Internals</h1>
          <p>Inspect Predbat's live object hierarchy and worker threads.</p>
        </div>
        <button type="button" onClick={() => { loadObject(path); loadThreads() }}>
          <FontAwesomeIcon icon={faRotate} /> Refresh
        </button>
      </header>

      {error && <div className="internals-error" role="alert">{error}</div>}

      <section className="internals-card internals-threads">
        <header><div><h2>Thread stacks</h2><p>{threads.length} running threads</p></div></header>
        <div className="internals-thread-list">
          {threads.map((thread) => (
            <details key={thread.id}>
              <summary>
                <span>{thread.name}</span>
                <small>ID {thread.id} · {thread.alive === false ? 'Dead' : 'Alive'}{thread.daemon ? ' · Daemon' : ''} · {thread.stack.length} frames</small>
              </summary>
              <div className="internals-stack">
                {thread.stack.map((frame, index) => (
                  <div key={`${frame.file}:${frame.line}:${index}`}>
                    <span>#{index}</span><strong>{frame.file}:{frame.line}</strong><em>{frame.name}()</em>
                    {frame.code && <code>{frame.code}</code>}
                  </div>
                ))}
                {thread.tasks.map((task) => (
                  <details className="internals-task" key={task.name}>
                    <summary>{task.name} · {task.state}</summary>
                    {task.stack.map((frame, index) => <code key={`${frame.file}:${frame.line}:${index}`}>{frame.file}:{frame.line} in {frame.name}() {frame.code}</code>)}
                  </details>
                ))}
              </div>
            </details>
          ))}
        </div>
      </section>

      <section className="internals-card internals-objects">
        <header>
          <div><h2>Object hierarchy</h2><p>{members.length} members at this level</p></div>
          <div className="internals-object-actions">
            <input type="search" name="search" autoComplete="off" value={search} placeholder="Filter members…" aria-label="Filter object members" onChange={(event) => setSearch(event.target.value)} />
            <a href={`./api/internals/download?path=${encodeURIComponent(path)}`}><FontAwesomeIcon icon={faDownload} /> YAML</a>
          </div>
        </header>

        <nav className="internals-breadcrumb" aria-label="Object path">
          {pathParts.map((part, index) => (
            <span key={`${part}:${index}`}>
              {index > 0 && <FontAwesomeIcon icon={faChevronRight} />}
              <button type="button" onClick={() => loadObject(pathParts.slice(0, index + 1).join('::'))}>{part}</button>
            </span>
          ))}
        </nav>

        <div className="internals-table-wrap" aria-busy={loading}>
          <table className="internals-table">
            <thead><tr><th>Name</th><th>Type</th><th>Value</th></tr></thead>
            <tbody>
              {filteredMembers.map((member) => (
                <tr className={member.expandable ? 'is-expandable' : ''} key={member.path} onClick={() => member.expandable && loadObject(member.path)}>
                  <td>{member.expandable && <FontAwesomeIcon icon={faChevronRight} />}<strong>{member.key}</strong></td>
                  <td><span>{member.type}{member.size !== undefined ? ` · ${member.size}` : ''}</span></td>
                  <td><code>{String(member.value ?? 'None')}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && !filteredMembers.length && <div className="internals-empty">No matching members.</div>}
        </div>
      </section>
    </section>
  )
}
