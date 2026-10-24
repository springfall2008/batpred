export type PredbatStatus = {
  calculating: boolean
  updating?: boolean
  battery_html: string

  status: string
  detail: string

  last_updated: string | null
  last_started: string | null

  version: string
  latest_version: string | null
  update_available: boolean

  mode: string
  debug_enable: boolean
  read_only: boolean
  active: boolean
  chat_enabled: boolean
  load_ml_enabled: boolean

  config_ok: boolean
  config_errors: number
}
