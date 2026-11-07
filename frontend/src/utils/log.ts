export type LogLine = {
  line_number: number
  raw_timestamp: string
  raw_message: string
  raw_full_line: string
  type: 'error' | 'warning' | 'info' | 'log'
}

export function mergeLogLines(current: LogLine[], incoming: LogLine[], limit = 1024) {
  const lines = new Map(current.map((line) => [line.line_number, line]))
  incoming.forEach((line) => lines.set(line.line_number, line))
  return [...lines.values()].sort((a, b) => b.line_number - a.line_number).slice(0, limit)
}
