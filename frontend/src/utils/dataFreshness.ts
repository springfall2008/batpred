/** Classify source snapshots, never the time their HTTP response arrived. */
export function snapshotState(timestamp: number | null, now: number, maxAge: number, failed = false): 'current' | 'stale' | 'unavailable' {
  if (timestamp === null || !Number.isFinite(timestamp) || timestamp <= 0 || timestamp > now + 60_000) return 'unavailable'
  return failed || now - timestamp > maxAge ? 'stale' : 'current'
}

/** Format a source timestamp for a compact freshness notice. */
export function snapshotAge(timestamp: number | null, now: number): string {
  if (timestamp === null || !Number.isFinite(timestamp) || timestamp <= 0) return 'time unknown'
  const minutes = Math.max(0, Math.floor((now - timestamp) / 60_000))
  if (minutes === 0) return 'less than a minute ago'
  if (minutes < 60) return `${minutes} min ago`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ago`
}
