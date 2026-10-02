import { Fragment, useEffect, useState, useRef, type CSSProperties } from 'react'

import {
  FontAwesomeIcon,
  FontAwesomeLayers
} from '@fortawesome/react-fontawesome'

import {
  faArrowRight,
  faArrowUpFromBracket,
  faArrowTrendUp,
  faBan,
  faBolt,
  faCar,
  faCircleInfo,
  faHouse,
  faPause,
  faSnowflake,
  faWater,
  faCheck,
  faChevronDown,
  faRotateLeft
} from '@fortawesome/free-solid-svg-icons'

import type { IconDefinition } from '@fortawesome/fontawesome-svg-core'

import NordPoolIcon from './NordPoolIcon'

import type { Plan, PlanOverrides } from '../types/plan'
import { formatMajorCurrency, resolveCurrencySymbols } from '../utils/currency'

import './PlanTable.css'

type PlanTableProps = {
  plan: Plan
  overrides: PlanOverrides
  debugEnabled: boolean
  readOnly?: boolean
  onOverrideSubmitted: () => void
}

type PlanRow = Plan['rows'][number]

function cellColour(colour?: string): CSSProperties | undefined {
  return colour ? { '--plan-cell-colour': colour } as CSSProperties : undefined
}

type ActionType =
  | 'charge'
  | 'hold-charge'
  | 'freeze-charge'
  | 'export'
  | 'hold-export'
  | 'freeze-export'
  | 'hold-car'
  | 'hold-iboost'
  | 'no-charge'
  | 'demand'
  | 'unknown'

type OverrideAction =
  | 'Manual Demand'
  | 'Manual Charge'
  | 'Manual Export'
  | 'Manual Freeze Charge'
  | 'Manual Freeze Export'

const OVERRIDE_OPTIONS: {
  action: OverrideAction
  label: string
  icon: IconDefinition
}[] = [
    {
      action: 'Manual Demand',
      label: 'Demand',
      icon: faHouse
    },
    {
      action: 'Manual Charge',
      label: 'Charge',
      icon: faBolt
    },
    {
      action: 'Manual Export',
      label: 'Export',
      icon: faArrowUpFromBracket
    },
    {
      action: 'Manual Freeze Charge',
      label: 'Freeze Charge',
      icon: faSnowflake
    },
    {
      action: 'Manual Freeze Export',
      label: 'Freeze Export',
      icon: faSnowflake
    }
  ]

function getManualOverride(slotMinute: number, overrides: PlanOverrides): OverrideAction | null {
  if (overrides.manual_charge_times.includes(slotMinute)) {
    return 'Manual Charge'
  }

  if (overrides.manual_export_times.includes(slotMinute)) {
    return 'Manual Export'
  }

  if (overrides.manual_demand_times.includes(slotMinute)) {
    return 'Manual Demand'
  }

  if (overrides.manual_freeze_charge_times.includes(slotMinute)) {
    return 'Manual Freeze Charge'
  }

  if (overrides.manual_freeze_export_times.includes(slotMinute)) {
    return 'Manual Freeze Export'
  }

  return null
}

function formatOverrideTime(value: string) {
  const date = new Date(value)

  const day = date.toLocaleDateString('en-GB', {
    weekday: 'short'
  })

  const time = date.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  })

  return `${day} ${time}`
}

type RateType = 'import' | 'export'

function getManualRateOverride(slotMinute: number, type: RateType, overrides: PlanOverrides) {
  const rates = type === 'import' ? overrides.manual_import_rates : overrides.manual_export_rates

  return rates.find((override) => override.minutes === slotMinute) ?? null
}

type RateCellProps = {
  row: PlanRow
  type: RateType
  overrides: PlanOverrides
  isPast: boolean
  debugEnabled: boolean
  currencyMinor: string
  onOverrideSubmitted: () => void
  open: boolean
  onToggle: () => void
  onClose: () => void
}

function RateCell({
  row,
  type,
  overrides,
  isPast,
  debugEnabled,
  currencyMinor,
  onOverrideSubmitted,
  open,
  onToggle,
  onClose
}: RateCellProps) {
  const override = getManualRateOverride(row.slot_minute, type, overrides)

  const rate = type === 'import' ? row.import_rate : row.export_rate

  const adjustedRate = type === 'import' ? row.import_rate_adjusted : row.export_rate_adjusted

  const isPredicted = (type === 'import' ? row.import_rate_adjust_type : row.export_rate_adjust_type) === 'future'

  const [value, setValue] = useState('')

  const [saving, setSaving] = useState(false)

  const [error, setError] = useState<string | null>(null)

  const controlRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) {
      return
    }

    function handlePointerDown(event: PointerEvent) {
      if (controlRef.current && !controlRef.current.contains(event.target as Node)) {
        onClose()
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        onClose()
      }
    }

    document.addEventListener('pointerdown', handlePointerDown)

    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)

      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, onClose])

  function openEditor() {
    if (isPast) {
      return
    }

    /*
     * Only initialise the input when opening,
     * otherwise clicking the current editor closed
     * would unnecessarily reset its value.
     */
    if (!open) {
      setValue(String(override?.rate ?? rate))

      setError(null)
    }

    onToggle()
  }

  async function sendOverride(
    action: 'Set Import' | 'Clear Import' | 'Set Export' | 'Clear Export',
    overrideRate: number
  ) {
    setSaving(true)
    setError(null)

    const formData = new FormData()

    formData.append('time', formatOverrideTime(row.time))

    formData.append('action', action)

    formData.append('rate', String(overrideRate))

    try {
      const response = await fetch('./rate_override', {
        method: 'POST',
        body: formData
      })

      const result = (await response.json()) as {
        success?: boolean
        message?: string
      }

      if (!response.ok || result.success === false) {
        throw new Error(result.message || `HTTP ${response.status}`)
      }

      onClose()

      /*
       * App immediately refreshes status.
       * Predbat then recalculates and the global
       * calculating overlay takes over.
       */
      onOverrideSubmitted()
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'

      setError(`Unable to update rate: ${message}`)
    } finally {
      setSaving(false)
    }
  }

  async function setRate(event: React.FormEvent) {
    event.preventDefault()

    const numericRate = Number(value)

    /*
     * Negative electricity prices are legitimate,
     * so deliberately don't enforce min="0".
     */
    if (!Number.isFinite(numericRate)) {
      setError('Enter a valid rate.')

      return
    }

    await sendOverride(type === 'import' ? 'Set Import' : 'Set Export', numericRate)
  }

  async function clearRate() {
    if (!override) {
      return
    }

    await sendOverride(
      type === 'import' ? 'Clear Import' : 'Clear Export',

      /*
       * Predbat looks up the stored value itself,
       * but sending the actual value mirrors the
       * legacy implementation.
       */
      override.rate
    )
  }

  return (
    <td
      className={['plan-number', 'plan-rate-cell', type === 'import' && row.rate_color_import ? 'has-plan-cell-colour' : ''].filter(Boolean).join(' ')}
      style={cellColour(type === 'import' ? row.rate_color_import : undefined)}
    >
      <div ref={controlRef} className="plan-rate-control">
        <button
          type="button"
          className={['plan-rate-button', override ? 'is-manual' : ''].filter(Boolean).join(' ')}
          onClick={openEditor}
          aria-disabled={isPast}
          aria-label={
            override
              ? `${type} rate ${rate.toFixed(2)} ${currencyMinor} per kWh, manually overridden`
              : `${type} rate ${rate.toFixed(2)} ${currencyMinor} per kWh`
          }
        >
          <span className="plan-rate-values">
            <span className="plan-value-with-indicator">
              {type === 'import' && row.rate_color_import && (
                <span
                  className="plan-value-indicator"
                  style={{ backgroundColor: row.rate_color_import }}
                  aria-hidden="true"
                />
              )}
              <span>{rate.toFixed(2)}{currencyMinor}</span>

              {isPredicted && (
                <span
                  className="plan-rate-predicted plan-tooltip-trigger"
                  data-tooltip="Predicted rate: Predbat is using Nord Pool data until the tariff publishes this period’s rate."
                  aria-label="Predicted rate"
                >
                  <NordPoolIcon />
                </span>
              )}
            </span>

            {debugEnabled && Number.isFinite(adjustedRate) && (
              <small title="Rate adjusted for conversion losses and battery cycling">
                {adjustedRate.toFixed(2)}{currencyMinor} with loss
              </small>
            )}
          </span>

          {override && (
            <span
              className="plan-rate-manual-dot"
              title="Manual rate override"
              aria-hidden="true"
            />
          )}
        </button>

        {open && !isPast && (
          <form className="plan-cell-popover" onSubmit={setRate}>
            <label>
              <span>{type === 'import' ? 'Import rate' : 'Export rate'}</span>

              <div className="plan-rate-input">
                <input
                  type="number"
                  step="0.01"
                  inputMode="decimal"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  autoFocus
                  disabled={saving}
                />

                <span>{currencyMinor}/kWh</span>
              </div>
            </label>

            <button type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Set override'}
            </button>

            {override && (
              <button
                type="button"
                className="plan-rate-clear"
                disabled={saving}
                onClick={() => {
                  void clearRate()
                }}
              >
                Use tariff rate
              </button>
            )}

            {error && (
              <div className="plan-rate-error" role="alert">
                {error}
              </div>
            )}
          </form>
        )}
      </div>
    </td>
  )
}

type TargetCellProps = {
  row: PlanRow
  overrides: PlanOverrides
  debugEnabled: boolean
  isPast: boolean
  open: boolean
  onToggle: () => void
  onClose: () => void
  onOverrideSubmitted: () => void
}

function TargetCell({
  row,
  overrides,
  debugEnabled,
  isPast,
  open,
  onToggle,
  onClose,
  onOverrideSubmitted
}: TargetCellProps) {
  const controlRef = useRef<HTMLDivElement | null>(null)

  const [value, setValue] = useState('')

  const [saving, setSaving] = useState(false)

  const [error, setError] = useState<string | null>(null)

  const override = overrides.manual_soc.find((item) => item.minutes === row.slot_minute) ?? null

  const plannedTarget = row.state_target?.trim() ? Number(row.state_target) : null

  /*
   * If there is a manual target, show that as the
   * target in the cell. Otherwise show Predbat's
   * calculated target.
   */
  const displayedTarget = override?.target ?? plannedTarget
  const debugLimit = row.show_limit?.match(/\(([^)]+)\)$/)?.[1]

  function openEditor() {
    if (isPast) {
      return
    }

    if (!open) {
      /*
       * Prefer the existing manual target,
       * then Predbat's target.
       *
       * If this slot doesn't currently have a
       * target, use its predicted SOC as a useful
       * starting point.
       */
      const initialValue = override?.target ?? plannedTarget ?? row.soc_percent

      setValue(String(initialValue))

      setError(null)
    }

    onToggle()
  }

  async function sendOverride(action: 'Set SOC' | 'Clear SOC', target: number) {
    setSaving(true)
    setError(null)

    const formData = new FormData()

    formData.append('time', formatOverrideTime(row.time))

    formData.append('action', action)

    /*
     * Predbat calls this field "rate" even for
     * SOC overrides.
     */
    formData.append('rate', String(target))

    try {
      const response = await fetch('./rate_override', {
        method: 'POST',
        body: formData
      })

      const result = (await response.json()) as {
        success?: boolean
        message?: string
      }

      if (!response.ok || result.success === false) {
        throw new Error(result.message || `HTTP ${response.status}`)
      }

      onClose()

      onOverrideSubmitted()
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'

      setError(`Unable to update target SOC: ${message}`)
    } finally {
      setSaving(false)
    }
  }

  async function setTarget(event: React.FormEvent) {
    event.preventDefault()

    const target = Number(value)

    if (!Number.isFinite(target) || target < 0 || target > 100) {
      setError('Enter a target between 0 and 100%.')

      return
    }

    await sendOverride('Set SOC', target)
  }

  async function clearTarget() {
    if (!override) {
      return
    }

    await sendOverride('Clear SOC', override.target)
  }

  useEffect(() => {
    if (!open) {
      return
    }

    function handlePointerDown(event: PointerEvent) {
      if (controlRef.current && !controlRef.current.contains(event.target as Node)) {
        onClose()
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        onClose()
      }
    }

    document.addEventListener('pointerdown', handlePointerDown)

    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)

      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, onClose])

  const action = getActionType(row.state)

  return (
    <td
      className={[
        'plan-number',
        'plan-target',
        displayedTarget !== null ? getTargetClass(action) : '',
        override ? 'has-manual-target' : ''
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div ref={controlRef} className="plan-target-control">
        <button
          type="button"
          className={['plan-target-button', override ? 'is-manual' : ''].filter(Boolean).join(' ')}
          onClick={openEditor}
          disabled={isPast}
        >
          <span>{displayedTarget !== null ? `${displayedTarget}%` : '—'}</span>

          {debugEnabled && debugLimit && (
            <small className="plan-debug-value" title="Internal optimiser limit">
              ({debugLimit})
            </small>
          )}

          {override && (
            <span className="plan-target-manual-dot" title="Manual SOC target" aria-hidden="true" />
          )}
        </button>

        {open && !isPast && (
          <form className="plan-cell-popover" onSubmit={setTarget}>
            <label>
              <span>Target SOC</span>

              <div className="plan-target-input">
                <input
                  type="number"
                  min="0"
                  max="100"
                  step="1"
                  inputMode="numeric"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  autoFocus
                  disabled={saving}
                />

                <span>%</span>
              </div>
            </label>

            <button type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Set target'}
            </button>

            {override && (
              <button
                type="button"
                className="plan-target-clear"
                disabled={saving}
                onClick={() => {
                  void clearTarget()
                }}
              >
                Use Predbat target
              </button>
            )}

            {error && (
              <div className="plan-target-error" role="alert">
                {error}
              </div>
            )}
          </form>
        )}
      </div>
    </td>
  )
}

/*
 * Convert Predbat's raw state into a UI action type.
 *
 * Keep the detailed states separate here. Unlike the dashboard
 * summary, this view should distinguish Charge from Hold Charge,
 * Freeze Charge, etc.
 */
function getActionType(state: string): ActionType {
  switch (state) {
    case 'Chrg':
      return 'charge'

    case 'HoldChrg':
      return 'hold-charge'

    case 'FrzChrg':
      return 'freeze-charge'

    case 'Exp':
      return 'export'

    case 'HoldExp':
      return 'hold-export'

    case 'FrzExp':
      return 'freeze-export'

    case 'Hold for car':
    case 'Demand, Hold for car':
      return 'hold-car'

    case 'Hold for iBoost':
    case 'Demand, Hold for iBoost':
      return 'hold-iboost'

    case 'No Charge':
      return 'no-charge'

    case 'Demand':
      return 'demand'

    default:
      return 'unknown'
  }
}

/*
 * Friendly action label.
 */
function getActionLabel(action: ActionType) {
  switch (action) {
    case 'charge':
      return 'Charge'

    case 'hold-charge':
      return 'Hold Charge'

    case 'freeze-charge':
      return 'Freeze Charge'

    case 'export':
      return 'Export'

    case 'hold-export':
      return 'Hold Export'

    case 'freeze-export':
      return 'Freeze Export'

    case 'hold-car':
      return 'Hold for Car'

    case 'hold-iboost':
      return 'Hold for iBoost'

    case 'no-charge':
      return 'No Charge'

    case 'demand':
      return 'Demand'

    default:
      return 'Unknown'
  }
}

/**
 * Return the Font Awesome icon associated with an action.
 *
 * Icons are deliberately semantic:
 * - bolt: battery charging
 * - bolt + export arrow: battery exporting
 * - snowflake: freeze behaviour
 * - pause: hold behaviour
 * - car: EV hold
 * - water: iBoost hold
 * - ban: charging disabled
 * - house: normal household demand
 */
function getActionIcon(action?: ActionType) {
  switch (action) {
    case 'charge':
    case 'hold-charge':
      return <FontAwesomeIcon icon={faBolt} />

    case 'freeze-charge':
      return <FontAwesomeIcon icon={faSnowflake} />

    case 'export':
      return (
        <FontAwesomeLayers>
          <FontAwesomeIcon icon={faBolt} />
          <FontAwesomeIcon
            icon={faArrowTrendUp}
            transform="shrink-4 right-6 up-6"
          />
        </FontAwesomeLayers>
      )

    case 'hold-export':
      return <FontAwesomeIcon icon={faPause} />

    case 'freeze-export':
      return <FontAwesomeIcon icon={faSnowflake} />

    case 'hold-car':
      return <FontAwesomeIcon icon={faCar} />

    case 'hold-iboost':
      return <FontAwesomeIcon icon={faWater} />

    case 'no-charge':
      return <FontAwesomeIcon icon={faBan} />

    case 'demand':
      return <FontAwesomeIcon icon={faHouse} />

    default:
      return <FontAwesomeIcon icon={faArrowRight} />
  }
}

/*
 * CSS class for the action badge.
 *
 * Charge and export actions deliberately use the same semantic
 * colours as the rest of the modern UI.
 */
function getActionClass(action: ActionType) {
  switch (action) {
    case 'charge':
      return 'plan-action-charge'

    case 'hold-charge':
    case 'freeze-charge':
      return 'plan-action-charge-secondary'

    case 'export':
      return 'plan-action-export'

    case 'hold-export':
    case 'freeze-export':
      return 'plan-action-export-secondary'

    default:
      return 'plan-action-neutral'
  }
}

function getTargetClass(action: ActionType) {
  switch (action) {
    case 'charge':
    case 'hold-charge':
    case 'freeze-charge':
      return 'plan-target-charge'

    case 'export':
    case 'hold-export':
    case 'freeze-export':
      return 'plan-target-export'

    default:
      return ''
  }
}

/*
 * Format a plan timestamp as local time.
 */
function formatTime(value: string) {
  return new Date(value).toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit'
  })
}

/*
 * A stable local date key used to determine when a new day begins.
 */
function getDateKey(value: string) {
  const date = new Date(value)

  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0')
  ].join('-')
}

/*
 * Friendly heading displayed between days.
 */
function formatDateHeading(value: string) {
  const date = new Date(value)

  const today = new Date()

  const todayKey = getDateKey(today.toISOString())

  const rowKey = getDateKey(value)

  const prefix = rowKey === todayKey ? 'Today · ' : ''

  return (
    prefix +
    date.toLocaleDateString('en-GB', {
      weekday: 'long',
      day: 'numeric',
      month: 'short'
    })
  )
}

/*
 * Human-readable version of a reason code.
 *
 * Used as a fallback if Predbat did not supply a template.
 */
function getReasonLabel(code: string) {
  return code.replaceAll('_', ' ').replace(/\b\w/g, (character) => character.toUpperCase())
}

/*
 * Turn Predbat's structured reason into the explanation supplied
 * by the backend.
 */
function getReasonText(plan: Plan, row: PlanRow) {
  const reason = row.reasons?.[0]

  if (!reason) {
    return ''
  }

  const template = plan.reason_templates?.[reason.code]
  const { minor: currencyMinor } = resolveCurrencySymbols(plan.currency_symbols)

  if (!template) {
    return getReasonLabel(reason.code)
  }

  /*
   * Most values are supplied directly in reason.params.
   *
   * The additional values here provide sensible fallbacks for
   * templates which reference values already available on the row.
   */
  const params: Record<string, unknown> = {
    ...(reason.params ?? {}),

    target_percent: reason.params?.target_percent ?? row.state_target,

    target_percentage: reason.params?.target_percentage ?? row.state_target,

    import_rate: row.import_rate.toFixed(2),

    export_rate: row.export_rate.toFixed(2),

    currency_unit: currencyMinor
  }

  return template.replace(/\{(\w+)\}/g, (_match, key: string) => {
    const value = params[key]

    return value !== undefined && value !== '' ? String(value) : `{${key}}`
  })
}

/*
 * Find the currently active row.
 *
 * Predbat's rows represent the start of each slot, so the latest
 * row whose start time is before "now" is the active one.
 */
function findCurrentRowIndex(rows: Plan['rows']) {
  const now = new Date()

  return rows.findLastIndex((row) => new Date(row.time) <= now)
}

type ColumnHeadingProps = {
  label: string
  mobileLabel?: string
  help: string
}

function ColumnHeading({ label, mobileLabel, help }: ColumnHeadingProps) {
  return (
    <span className="plan-column-heading">
      <span>{label}</span>

      <span className="plan-column-label-mobile">{mobileLabel ?? label}</span>

      <span className="plan-tooltip-trigger" data-tooltip={help} tabIndex={0} aria-label={help}>
        <FontAwesomeIcon icon={faCircleInfo} />
      </span>
    </span>
  )
}

export default function PlanTable({
  plan,
  overrides,
  debugEnabled,
  readOnly = false,
  onOverrideSubmitted
}: PlanTableProps) {
  const rows = plan.rows ?? []
  const { major: currencyMajor, minor: currencyMinor } = resolveCurrencySymbols(plan.currency_symbols)

  const showCar = plan.num_cars > 0

  const showIBoost = plan.iboost_enable === true

  const showExtraLoad = debugEnabled && rows.some((row) => row.extra_load !== undefined)

  const showCarbon = plan.carbon_enable === true

  const columnCount = 10 + (showCar ? 1 : 0) + (showIBoost ? 1 : 0) + (debugEnabled ? 1 : 0) + (showExtraLoad ? 1 : 0) + (showCarbon ? 2 : 0)

  const [openOverrideTime, setOpenOverrideTime] = useState<string | null>(null)

  const [savingOverrideTime, setSavingOverrideTime] = useState<string | null>(null)

  const [overrideError, setOverrideError] = useState<string | null>(null)

  const currentRowIndex = readOnly ? -1 : findCurrentRowIndex(rows)

  const [openCellEditor, setOpenCellEditor] = useState<string | null>(null)

  const [colourStyle, setColourStyle] = useState<'dots' | 'cells'>(() => {
    try {
      return localStorage.getItem('predbat-plan-colour-style') === 'cells' ? 'cells' : 'dots'
    } catch {
      return 'dots'
    }
  })

  const colourCells = colourStyle === 'cells'

  useEffect(() => {
    try {
      localStorage.setItem('predbat-plan-colour-style', colourStyle)
    } catch {
      // Browsers may disable storage; the in-memory preference still works.
    }
  }, [colourStyle])

  useEffect(() => {
    function closeMenu() {
      setOpenOverrideTime(null)
      setOverrideError(null)
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        closeMenu()
      }
    }

    document.addEventListener('pointerdown', closeMenu)

    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('pointerdown', closeMenu)

      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [])

  // Keep hooks above this return: a later poll can populate an initially empty plan.
  if (rows.length === 0) {
    return (
      <section className="plan-table-card">
        <div className="plan-table-empty">No detailed plan is currently available.</div>
      </section>
    )
  }

  async function setPlanOverride(row: PlanRow, action: OverrideAction | 'Clear') {
    setSavingOverrideTime(row.time)

    setOverrideError(null)

    const formData = new FormData()

    formData.append('time', formatOverrideTime(row.time))

    formData.append('action', action)

    try {
      const response = await fetch('./plan_override', {
        method: 'POST',
        body: formData
      })

      const result = (await response.json()) as {
        success?: boolean
        message?: string
      }

      if (!response.ok || result.success === false) {
        throw new Error(result.message || `HTTP ${response.status}`)
      }

      setOpenOverrideTime(null)

      /*
       * The override handler has now marked Predbat's plan as
       * needing recalculation. Ask App to update its status
       * immediately instead of waiting for the next 5-second poll.
       */
      onOverrideSubmitted()
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'

      setOverrideError(`Unable to update this slot: ${message}`)
    } finally {
      setSavingOverrideTime(null)
    }
  }

  return (
    <section className="plan-table-card">
      <header className="plan-table-header">
        <div>
          <h2>Detailed Plan</h2>

          <p>Forecast actions and energy use for each plan slot.</p>
        </div>

        <div className="plan-colour-style" role="group" aria-label="Plan colour display">
          <button type="button" className={colourStyle === 'dots' ? 'is-active' : ''} aria-pressed={colourStyle === 'dots'} onClick={() => setColourStyle('dots')}>Dots</button>
          <button type="button" className={colourStyle === 'cells' ? 'is-active' : ''} aria-pressed={colourStyle === 'cells'} onClick={() => setColourStyle('cells')}>Cells</button>
        </div>
      </header>

      {debugEnabled && (
        <div className="plan-debug-notice" role="status">
          <span>
            Debug details are enabled: bracketed forecasts show the 10% confidence case, and adjusted
            rates include conversion losses and battery cycling.
          </span>

        </div>
      )}

      <div className="plan-table-scroll">
        <table className={['plan-table', debugEnabled ? 'is-debug' : '', colourCells ? 'is-cell-colours' : ''].filter(Boolean).join(' ')}>
          <thead>
            <tr>
              <th>
                <ColumnHeading label="Time" help="The start time of this Predbat planning slot." />
              </th>

              <th>
                <ColumnHeading
                  label="Action"
                  help="What Predbat plans to do during this slot. Hover over the action icon for an explanation of why this action was chosen."
                />
              </th>

              <th>
                <ColumnHeading
                  label="Import"
                  mobileLabel="IMP"
                  help={
                    debugEnabled
                      ? 'The tariff import rate, followed by the effective rate after conversion losses and battery cycling.'
                      : `The electricity import price for this slot, in ${currencyMinor} per kWh.`
                  }
                />
              </th>

              <th>
                <ColumnHeading
                  label="Export"
                  mobileLabel="EXP"
                  help={
                    debugEnabled
                      ? 'The tariff export rate, followed by the effective rate after conversion losses and battery cycling.'
                      : `The electricity export price for this slot, in ${currencyMinor} per kWh.`
                  }
                />
              </th>

              <th>
                <ColumnHeading
                  label={debugEnabled ? 'PV (10%)' : 'PV'}
                  help={
                    debugEnabled
                      ? 'The main solar forecast, followed by the lower 10% confidence forecast in brackets.'
                      : 'The forecast solar generation during this slot, in kWh.'
                  }
                />
              </th>

              <th>
                <ColumnHeading
                  label={debugEnabled ? 'Load (10%)' : 'Load'}
                  help={
                    debugEnabled
                      ? 'The main load forecast, followed by the lower 10% confidence forecast in brackets.'
                      : 'The forecast household electricity consumption during this slot, in kWh.'
                  }
                />
              </th>

              {debugEnabled && (
                <th>
                  <ColumnHeading
                    label="Clip"
                    help="Solar energy predicted to be lost because of inverter or export limits, in kWh."
                  />
                </th>
              )}

              {showExtraLoad && (
                <th>
                  <ColumnHeading
                    label="XLoad"
                    help="Extra forecast load added by external sources such as PredAI or PredHeat, in kWh."
                  />
                </th>
              )}

              {showCar && (
                <th>
                  <ColumnHeading
                    label="Car"
                    help="Predicted energy used to charge the car during this plan slot."
                  />
                </th>
              )}

              {showIBoost && (
                <th>
                  <ColumnHeading
                    label="iBoost"
                    help="Cumulative energy diverted to iBoost, with the energy added during this slot shown in brackets, in kWh."
                  />
                </th>
              )}

              <th>
                <ColumnHeading
                  label="Target"
                  mobileLabel="TRGT"
                  help="The battery target Predbat is aiming for during a charge or export action. Charge actions target an upper SOC; export actions target a lower SOC."
                />
              </th>

              <th>
                <ColumnHeading
                  label="SOC"
                  help="The predicted battery state of charge at this point in the plan."
                />
              </th>

              <th>
                <ColumnHeading
                  label="Cost"
                  help="The estimated change in energy cost during this plan slot."
                />
              </th>

              <th>
                <ColumnHeading
                  label="Total"
                  mobileLabel="TOTL"
                  help="Predbat's running estimated energy cost at the start of this slot."
                />
              </th>

              {showCarbon && (
                <>
                  <th>
                    <ColumnHeading
                      label="CO₂ intensity"
                      mobileLabel="CO2"
                      help="The forecast carbon intensity of grid electricity during this slot, in grams of CO₂ per kWh."
                    />
                  </th>

                  <th>
                    <ColumnHeading
                      label="CO₂ total"
                      mobileLabel="CO2 TOTL"
                      help="Predbat's running forecast of net carbon emissions at the start of this slot, in kilograms of CO₂."
                    />
                  </th>
                </>
              )}
            </tr>
          </thead>

          <tbody>
            {rows.map((row, index) => {
              const previousRow = index > 0 ? rows[index - 1] : undefined

              const newDay = !previousRow || getDateKey(previousRow.time) !== getDateKey(row.time)

              const action = getActionType(row.state)

              const reason = getReasonText(plan, row)

              const isCurrent = index === currentRowIndex

              const isPast = readOnly || (currentRowIndex >= 0 && index < currentRowIndex)

              const manualOverride = getManualOverride(row.slot_minute, overrides)

              const menuOpen = openOverrideTime === row.time

              const saving = savingOverrideTime === row.time

              return (
                <Fragment key={row.time}>
                  {newDay && (
                    <tr className="plan-day-row">
                      <td colSpan={columnCount}>{formatDateHeading(row.time)}</td>
                    </tr>
                  )}

                  <tr
                    className={['plan-row', isPast ? 'is-past' : '', isCurrent ? 'is-current' : '']
                      .filter(Boolean)
                      .join(' ')}
                  >
                    <td className="plan-time">
                      {formatTime(row.time)}

                      {isCurrent && <span className="plan-now-label">Now</span>}
                    </td>

                    <td>
                      <div
                        className={['plan-action-control', menuOpen ? 'is-open' : '']
                          .filter(Boolean)
                          .join(' ')}
                        onPointerDown={(event) => {
                          event.stopPropagation()
                        }}
                      >
                        <button
                          type="button"
                          className={[
                            'plan-action',
                            'plan-tooltip-trigger',
                            getActionClass(action),
                            manualOverride ? 'is-manual' : ''
                          ]
                            .filter(Boolean)
                            .join(' ')}
                          data-tooltip={
                            reason || 'No additional explanation is available for this slot.'
                          }
                          aria-expanded={menuOpen}
                          aria-haspopup={!isPast ? 'menu' : undefined}
                          aria-disabled={isPast}
                          onClick={() => {
                            /*
                             * Historical slots remain available for their
                             * explanation tooltip but cannot be modified.
                             */
                            if (isPast) {
                              return
                            }

                            setOverrideError(null)

                            setOpenOverrideTime(menuOpen ? null : row.time)
                          }}
                        >
                          {getActionIcon(action)}

                          <span>{getActionLabel(action)}</span>

                          {manualOverride && (
                            <span
                              className="plan-action-manual-dot"
                              title="Manual override"
                              aria-hidden="true"
                            />
                          )}

                          {!isPast && (
                            <FontAwesomeIcon
                              icon={faChevronDown}
                              className="plan-action-chevron"
                              aria-hidden="true"
                            />
                          )}
                        </button>

                        {menuOpen && (
                          <div className="plan-action-menu" role="menu">
                            {reason && <div className="plan-action-menu-reason">{reason}</div>}

                            <div className="plan-action-menu-divider" />

                            {OVERRIDE_OPTIONS.map((option) => {
                              const selected = manualOverride === option.action

                              return (
                                <button
                                  key={option.action}
                                  type="button"
                                  role="menuitem"
                                  className={selected ? 'is-selected' : ''}
                                  disabled={saving}
                                  onClick={() => {
                                    void setPlanOverride(row, option.action)
                                  }}
                                >
                                  <FontAwesomeIcon icon={option.icon} />

                                  <span>{option.label}</span>

                                  {selected && (
                                    <FontAwesomeIcon
                                      icon={faCheck}
                                      className="plan-action-menu-check"
                                    />
                                  )}
                                </button>
                              )
                            })}

                            {manualOverride && (
                              <>
                                <div className="plan-action-menu-divider" />

                                <button
                                  type="button"
                                  role="menuitem"
                                  className="plan-action-menu-clear"
                                  disabled={saving}
                                  onClick={() => {
                                    void setPlanOverride(row, 'Clear')
                                  }}
                                >
                                  <FontAwesomeIcon icon={faRotateLeft} />

                                  <span>Automatic</span>
                                </button>
                              </>
                            )}

                            {saving && (
                              <div className="plan-action-menu-status">Updating plan…</div>
                            )}

                            {overrideError && (
                              <div className="plan-action-menu-error" role="alert">
                                {overrideError}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </td>

                    <RateCell
                      row={row}
                      type="import"
                      overrides={overrides}
                      isPast={isPast}
                      debugEnabled={debugEnabled}
                      currencyMinor={currencyMinor}
                      onOverrideSubmitted={onOverrideSubmitted}
                      open={openCellEditor === `rate:${row.time}:import`}
                      onToggle={() => {
                        const key = `rate:${row.time}:import`

                        setOpenCellEditor((current) => (current === key ? null : key))
                      }}
                      onClose={() => {
                        setOpenCellEditor(null)
                      }}
                    />

                    <RateCell
                      row={row}
                      type="export"
                      overrides={overrides}
                      isPast={isPast}
                      debugEnabled={debugEnabled}
                      currencyMinor={currencyMinor}
                      onOverrideSubmitted={onOverrideSubmitted}
                      open={openCellEditor === `rate:${row.time}:export`}
                      onToggle={() => {
                        const key = `rate:${row.time}:export`

                        setOpenCellEditor((current) => (current === key ? null : key))
                      }}
                      onClose={() => {
                        setOpenCellEditor(null)
                      }}
                    />

                    <td
                      className={[
                        'plan-number',
                        'plan-pv-value',
                        row.pv_forecast > 0 ? 'is-generating' : ''
                      ].filter(Boolean).join(' ')}
                    >
                      {row.pv_forecast.toFixed(2)}
                      {debugEnabled && (row.pv_forecast10 ?? 0) > 0 && (
                        <small className="plan-debug-value">
                          ({row.pv_forecast10?.toFixed(2)})
                        </small>
                      )}
                    </td>

                    <td
                      className={['plan-number', row.load_color ? 'has-plan-cell-colour' : ''].filter(Boolean).join(' ')}
                      style={cellColour(row.load_color)}
                    >
                      <span className="plan-value-with-indicator">
                        {row.load_forecast > 0 && row.load_color && (
                          <span
                            className="plan-value-indicator"
                            style={{ backgroundColor: row.load_color }}
                            aria-hidden="true"
                          />
                        )}
                        <span>{row.load_forecast.toFixed(2)}</span>
                      </span>
                      {debugEnabled && (row.load_forecast10 ?? 0) > 0 && (
                        <small className="plan-debug-value">
                          ({row.load_forecast10?.toFixed(2)})
                        </small>
                      )}
                    </td>

                    {debugEnabled && (
                      <td className="plan-number plan-debug-column">
                        {(row.clipped ?? 0) === 0 ? '—' : row.clipped?.toFixed(2)}
                      </td>
                    )}

                    {showExtraLoad && (
                      <td className="plan-number plan-debug-column">{row.extra_load || '—'}</td>
                    )}

                    {showCar && (
                      <td
                        className={[
                          'plan-number',
                          'plan-car',
                          (row.car_charging ?? 0) > 0 ? 'is-charging' : ''
                        ]
                          .filter(Boolean)
                          .join(' ')}
                      >
                        {(row.car_charging ?? 0) > 0 ? (
                          <span className="plan-car-value">
                            <FontAwesomeIcon icon={faCar} />

                            <span>{row.car_charging?.toFixed(2)}</span>
                          </span>
                        ) : (
                          '—'
                        )}
                      </td>
                    )}

                    {showIBoost && (
                      <td
                        className={[
                          'plan-number',
                          (row.iboost_change ?? 0) > 0 && row.iboost_color ? 'has-plan-cell-colour' : ''
                        ].filter(Boolean).join(' ')}
                        style={cellColour((row.iboost_change ?? 0) > 0 ? row.iboost_color : undefined)}
                      >
                        {(row.iboost_change ?? 0) > 0 ? (
                          <span className="plan-value-with-indicator">
                            {row.iboost_color && (
                              <span
                                className="plan-value-indicator"
                                style={{ backgroundColor: row.iboost_color }}
                                aria-hidden="true"
                              />
                            )}
                            <span>
                              {(row.iboost ?? 0).toFixed(2)} (+{(row.iboost_change ?? 0).toFixed(2)})
                            </span>
                          </span>
                        ) : (row.iboost ?? 0) > 0 ? (
                          (row.iboost ?? 0).toFixed(2)
                        ) : (
                          '—'
                        )}
                      </td>
                    )}

                    <TargetCell
                      row={row}
                      overrides={overrides}
                      debugEnabled={debugEnabled}
                      isPast={isPast}
                      open={openCellEditor === `target:${row.time}`}
                      onToggle={() => {
                        const key = `target:${row.time}`

                        setOpenCellEditor((current) => (current === key ? null : key))
                      }}
                      onClose={() => {
                        setOpenCellEditor(null)
                      }}
                      onOverrideSubmitted={onOverrideSubmitted}
                    />

                    <td className="plan-number plan-soc">{row.soc_percent}%</td>

                    <td
                      className={['plan-number', Math.abs(row.cost_change) >= 0.005 && row.cost_color ? 'has-plan-cell-colour' : ''].filter(Boolean).join(' ')}
                      style={cellColour(Math.abs(row.cost_change) >= 0.005 ? row.cost_color : undefined)}
                    >
                      <span className="plan-value-with-indicator">
                        {Math.abs(row.cost_change) >= 0.005 && row.cost_color && (
                          <span
                            className="plan-value-indicator"
                            style={{ backgroundColor: row.cost_color }}
                            aria-hidden="true"
                          />
                        )}
                        <span>
                          {row.cost_change > 0 ? '+' : ''}
                          {formatMajorCurrency(row.cost_change, currencyMajor)}
                        </span>
                      </span>
                    </td>

                    <td className="plan-number">{formatMajorCurrency(row.total_cost, currencyMajor)}</td>

                    {showCarbon && (
                      <>
                        <td
                          className={['plan-number', row.carbon_intensity_color ? 'has-plan-cell-colour' : ''].filter(Boolean).join(' ')}
                          style={cellColour(row.carbon_intensity_color)}
                        >
                          <span className="plan-value-with-indicator">
                            {row.carbon_intensity_color && (
                              <span
                                className="plan-value-indicator"
                                style={{ backgroundColor: row.carbon_intensity_color }}
                                aria-hidden="true"
                              />
                            )}
                            <span>{row.carbon_intensity?.toFixed(0) ?? '—'}</span>
                          </span>
                        </td>

                        <td
                          className={['plan-number', Math.abs(row.carbon_change ?? 0) >= 10 && row.carbon_color ? 'has-plan-cell-colour' : ''].filter(Boolean).join(' ')}
                          style={cellColour(Math.abs(row.carbon_change ?? 0) >= 10 ? row.carbon_color : undefined)}
                        >
                          <span className="plan-value-with-indicator">
                            {Math.abs(row.carbon_change ?? 0) >= 10 && row.carbon_color && (
                              <span
                                className="plan-value-indicator"
                                style={{ backgroundColor: row.carbon_color }}
                                aria-hidden="true"
                              />
                            )}
                            <span>{row.total_carbon?.toFixed(2) ?? '—'}</span>
                          </span>
                        </td>
                      </>
                    )}
                  </tr>
                </Fragment>
              )
            })}
          </tbody>

          {plan.totals && (
            <tfoot>
              <tr className="plan-totals-row">
                <td colSpan={4}>Plan total</td>
                <td className={plan.totals.pv_forecast > 0 ? 'plan-pv-value is-generating' : ''}>
                  {plan.totals.pv_forecast.toFixed(2)}
                </td>
                <td>{plan.totals.load_forecast.toFixed(2)}</td>
                {debugEnabled && <td>{(plan.totals.clipped ?? 0).toFixed(2)}</td>}
                {showExtraLoad && <td>{(plan.totals.extra_load ?? 0).toFixed(2)}</td>}
                {showCar && <td>{(plan.totals.car_charging ?? 0).toFixed(2)}</td>}
                {showIBoost && <td>{(plan.totals.iboost ?? 0) > 0 ? (plan.totals.iboost ?? 0).toFixed(2) : '—'}</td>}
                <td>—</td>
                <td>{plan.totals.soc_percent}%</td>
                <td>—</td>
                <td>{formatMajorCurrency(plan.totals.total_cost, currencyMajor)}</td>
                {showCarbon && (
                  <>
                    <td>—</td>
                    <td>{plan.totals.total_carbon?.toFixed(2) ?? '—'}</td>
                  </>
                )}
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </section>
  )
}
