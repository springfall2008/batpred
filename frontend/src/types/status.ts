export type PredbatStatus = {
    calculating: boolean
    battery_html: string

    status: string
    detail: string

    last_updated: string | null
    last_started: string | null

    version: string

    mode: string
    debug_enable: boolean
    read_only: boolean
    active: boolean

    config_ok: boolean
    config_errors: number
}