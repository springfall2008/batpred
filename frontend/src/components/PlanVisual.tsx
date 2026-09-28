import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'

import {
  FontAwesomeIcon,
  FontAwesomeLayers
} from '@fortawesome/react-fontawesome'

import {
  faArrowTrendUp,
  faArrowRight,
  faBan,
  faBolt,
  faCar,
  faHouse,
  faPause,
  faSnowflake,
  faWater
} from '@fortawesome/free-solid-svg-icons'

import type { Plan } from '../types/plan'
import { resolveCurrencySymbols } from '../utils/currency'

import './PlanVisual.css'

type PlanVisualProps = {
  plan: Plan
}

type PlanRow = Plan['rows'][number]

type TimelineAction =
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

type TimelineEvent = {
  id: string

  action: TimelineAction

  startIndex: number
  endIndex: number

  start: Date
  end: Date

  startSoc: number
  endSoc: number

  carCharging: number

  target: number | null
  targetSocStart: number | null
  targetSocEnd: number | null

  importRateMin: number
  importRateMax: number

  exportRateMin: number
  exportRateMax: number

  carbonIntensityMin: number | null
  carbonIntensityMax: number | null
  carbonChange: number

  explanation?: string
}

type TimelineTick = {
  time: number
  label: string
  dateLabel?: string
  edge: 'start' | 'middle' | 'end'
}

type SocPoint = {
  time: number
  soc: number
}

type SocBoundary = {
  time: number
  soc: number
}

type SocHover = SocPoint & {
  x: number
}

/*
 * Space reserved on the left and right of the graph.
 *
 * The action track and SOC graph use exactly the same
 * horizontal plotting area, so events and SOC remain
 * visually aligned.
 */
const PLOT_LEFT = 44
const PLOT_RIGHT = 14

function getTimelineBlockLabel(action: TimelineAction, width: number): string | null {
  // Very small blocks: icon only.
  if (width < 44) {
    return null
  }

  // Wide enough for the full action name.
  if (width >= 105) {
    return getActionLabel(action)
  }

  // Medium-width blocks get a compact label.
  switch (action) {
    case 'charge':
      return 'Charge'

    case 'hold-charge':
      return 'Hold'

    case 'freeze-charge':
      return 'Freeze'

    case 'export':
      return 'Export'

    case 'hold-export':
      return 'Hold'

    case 'freeze-export':
      return 'Freeze'

    case 'hold-car':
      return 'Car'

    case 'hold-iboost':
      return 'iBoost'

    case 'no-charge':
      return 'No Chg'

    case 'demand':
      return 'Demand'

    default:
      return null
  }
}

/*
 * Predbat's detailed states are converted into the
 * user-facing action windows we want on the timeline.
 *
 * Hold states stay distinct from active charging/exporting. Event grouping
 * combines consecutive rows only while this action remains unchanged.
 */
function getTimelineAction(state: string): TimelineAction {
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

function getActionLabel(action: TimelineAction) {
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
      return 'Other'
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
function getActionIcon(action?: TimelineAction) {
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

function getSocColour(soc: number) {
  if (soc <= 20) {
    return 'var(--color-battery-low)'
  }

  if (soc <= 50) {
    return 'var(--color-battery-medium)'
  }

  return 'var(--color-battery-high)'
}

/*
 * Parse state_target safely.
 *
 * A target of 0 is perfectly valid, so this must not use
 * simple truthiness checks.
 */
function parseTarget(value: unknown): number | null {
  if (value === '' || value === null || value === undefined) {
    return null
  }

  const parsed = Number(value)

  return Number.isFinite(parsed) ? parsed : null
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value))
}

// oxlint-disable-next-line react/only-export-components -- exported for the timeline hover test
export function interpolateSoc(points: SocPoint[], time: number) {
  const nextIndex = points.findIndex((point) => point.time >= time)

  if (nextIndex === -1) {
    return points[points.length - 1]?.soc ?? 0
  }

  if (nextIndex === 0) {
    return points[0]?.soc ?? 0
  }

  const previous = points[nextIndex - 1]
  const next = points[nextIndex]
  const progress = (time - previous.time) / Math.max(1, next.time - previous.time)

  return previous.soc + (next.soc - previous.soc) * progress
}

/*
 * Predbat can start a plan with a partial current slot.
 *
 * For example, the first point may be 19:40 followed by
 * normal half-hour slots at 20:00, 20:30, etc.
 *
 * Taking the median interval gives us the normal plan slot
 * length rather than accidentally treating that first
 * partial interval as the standard slot duration.
 */
function inferSlotDuration(rows: PlanRow[]) {
  const differences = rows
    .slice(1)
    .map((row, index) => {
      return new Date(row.time).getTime() - new Date(rows[index].time).getTime()
    })
    .filter((value) => value > 0)
    .sort((first, second) => first - second)

  if (differences.length === 0) {
    return 30 * 60 * 1000
  }

  return differences[Math.floor(differences.length / 2)]
}

function formatReason(template: string, params: Record<string, unknown>) {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => {
    const value = params[key]

    return value !== undefined && value !== null ? String(value) : `{${key}}`
  })
}

function parseTargetSoc(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }

  const parsed = Number(value)

  return Number.isFinite(parsed) ? parsed : null
}

/*
 * Convert the detailed half-hour rows into meaningful
 * user-facing events.
 */
function buildTimelineEvents(
  plan: Plan,
  slotDuration: number,
  initialSoc: number
): TimelineEvent[] {
  const rows = plan.rows
  const { minor: currencyMinor } = resolveCurrencySymbols(plan.currency_symbols)

  if (rows.length === 0) {
    return []
  }

  const events: TimelineEvent[] = []

  let startIndex = 0

  let currentAction = getTimelineAction(rows[0].state)

  function addEvent(endIndex: number) {
    const eventRows = rows.slice(startIndex, endIndex + 1)

    const targetSocValues = eventRows
      .map((row) => parseTargetSoc(row.state_target))
      .filter((value): value is number => value !== null)

    const targetSocStart = targetSocValues.length > 0 ? targetSocValues[0] : null

    const targetSocEnd =
      targetSocValues.length > 0 ? targetSocValues[targetSocValues.length - 1] : null

    const first = eventRows[0]

    const last = eventRows[eventRows.length - 1]

    const start = new Date(first.time)

    const end =
      endIndex < rows.length - 1
        ? new Date(rows[endIndex + 1].time)
        : new Date(new Date(last.time).getTime() + slotDuration)

    // Row SOC is the end-of-slot forecast; an event starts at the previous boundary.
    const startSoc = startIndex === 0 ? initialSoc : rows[startIndex - 1].soc_percent

    const targets = eventRows
      .map((row) => parseTarget(row.state_target))
      .filter((target): target is number => target !== null)

    /*
     * Charge/export windows normally carry the same
     * target throughout. If Predbat does change it,
     * the final target is the most meaningful summary
     * of where the window is trying to finish.
     */
    const target = targets.length > 0 ? targets[targets.length - 1] : null

    const importRates = eventRows.map((row) => row.import_rate)

    const exportRates = eventRows.map((row) => row.export_rate)

    const carbonIntensities = eventRows
      .map((row) => row.carbon_intensity)
      .filter((value): value is number => value !== undefined)

    const carbonChange = eventRows.reduce((total, row) => total + (row.carbon_change ?? 0), 0)

    const carCharging = eventRows.reduce((total, row) => total + (row.car_charging ?? 0), 0)

    /*
     * Use the first reason as the event explanation.
     *
     * This normally describes why Predbat entered the
     * action window. Internal state changes later in the
     * same window remain intentionally hidden here.
     */
    const reason = first.reasons?.[0]

    let explanation: string | undefined

    if (reason && plan.reason_templates[reason.code]) {
      const relevantRate =
        currentAction === 'export' || currentAction === 'freeze-export'
          ? first.export_rate
          : first.import_rate

      explanation = formatReason(plan.reason_templates[reason.code], {
        ...(reason.params ?? {}),

        target_percent: target ?? first.state_target,

        target_percentage: target ?? first.state_target,

        rate: relevantRate.toFixed(2),

        import_rate: first.import_rate.toFixed(2),

        export_rate: first.export_rate.toFixed(2),

        currency_unit: currencyMinor
      })
    }

    events.push({
      id: `${first.time}-${currentAction}`,

      action: currentAction,

      startIndex,

      endIndex,

      start,

      end,

      startSoc,

      endSoc: last.soc_percent,

      targetSocStart,
      targetSocEnd,

      target,

      importRateMin: Math.min(...importRates),

      importRateMax: Math.max(...importRates),

      exportRateMin: Math.min(...exportRates),

      exportRateMax: Math.max(...exportRates),

      carbonIntensityMin:
        carbonIntensities.length > 0 ? Math.min(...carbonIntensities) : null,

      carbonIntensityMax:
        carbonIntensities.length > 0 ? Math.max(...carbonIntensities) : null,

      carbonChange,

      carCharging,

      explanation
    })
  }

  for (let index = 1; index < rows.length; index += 1) {
    const action = getTimelineAction(rows[index].state)

    if (action !== currentAction) {
      addEvent(index - 1)

      startIndex = index

      currentAction = action
    }
  }

  addEvent(rows.length - 1)

  return events
}

function getEventTitle(event: TimelineEvent) {
  switch (event.action) {
    case 'charge':
      return event.target !== null ? `Charge to ${event.target}%` : 'Charge'

    case 'hold-charge':
      return event.target !== null ? `Hold Charge at ${event.target}%` : 'Hold Charge'

    case 'export':
      return event.target !== null ? `Export to ${event.target}%` : 'Export'

    case 'hold-export':
      return event.target !== null ? `Hold Export at ${event.target}%` : 'Hold Export'

    case 'freeze-charge':
      return event.target !== null ? `Freeze Charge ${event.target}%` : 'Freeze Charge'

    case 'freeze-export':
      return event.target !== null ? `Freeze Export ${event.target}%` : 'Freeze Export'

    default:
      return getActionLabel(event.action)
  }
}

function formatTargetSoc(start: number | null, end: number | null): string | null {
  if (start === null && end === null) {
    return null
  }

  if (start !== null && end !== null) {
    if (start === end) {
      return `${Math.round(start)}%`
    }

    return `${Math.round(start)}% → ${Math.round(end)}%`
  }

  const value = start ?? end

  return value !== null ? `${Math.round(value)}%` : null
}

function formatRateRange(minimum: number, maximum: number, currencyMinor: string) {
  if (Math.abs(maximum - minimum) < 0.005) {
    return `${minimum.toFixed(2)}${currencyMinor}/kWh`
  }

  return `${minimum.toFixed(2)}–` + `${maximum.toFixed(2)}${currencyMinor}/kWh`
}

function getRelevantRate(event: TimelineEvent, currencyMinor: string) {
  switch (event.action) {
    case 'charge':
    case 'hold-charge':
    case 'freeze-charge':
      return formatRateRange(event.importRateMin, event.importRateMax, currencyMinor)

    case 'export':
    case 'hold-export':
    case 'freeze-export':
      return formatRateRange(event.exportRateMin, event.exportRateMax, currencyMinor)

    default:
      return null
  }
}

function formatDuration(start: Date, end: Date) {
  const minutes = Math.max(0, Math.round((end.getTime() - start.getTime()) / 60000))

  const hours = Math.floor(minutes / 60)

  const remainingMinutes = minutes % 60

  if (hours === 0) {
    return `${remainingMinutes}m`
  }

  if (remainingMinutes === 0) {
    return `${hours}h`
  }

  return `${hours}h ${remainingMinutes}m`
}

function formatEventRange(start: Date, end: Date) {
  const sameDay =
    start.getFullYear() === end.getFullYear() &&
    start.getMonth() === end.getMonth() &&
    start.getDate() === end.getDate()

  const startText = start.toLocaleString('en-GB', {
    weekday: 'short',

    day: 'numeric',

    month: 'short',

    hour: '2-digit',

    minute: '2-digit'
  })

  const endText = end.toLocaleString(
    'en-GB',
    sameDay
      ? {
        hour: '2-digit',

        minute: '2-digit'
      }
      : {
        weekday: 'short',

        day: 'numeric',

        month: 'short',

        hour: '2-digit',

        minute: '2-digit'
      }
  )

  return `${startText} → ${endText}`
}

/*
 * Build an uncomplicated three-hour time scale.
 *
 * Start and end are always labelled, with regular three-hour
 * landmarks in between.
 */
// oxlint-disable-next-line react/only-export-components -- exported for the timeline spacing test
export function buildTimeTicks(startMs: number, endMs: number): TimelineTick[] {
  const ticks: TimelineTick[] = []

  const start = new Date(startMs)

  ticks.push({
    time: startMs,

    label: start.toLocaleTimeString('en-GB', {
      hour: '2-digit',

      minute: '2-digit'
    }),

    dateLabel: start.toLocaleDateString('en-GB', {
      weekday: 'short',

      day: 'numeric',

      month: 'short'
    }),

    edge: 'start'
  })

  const cursor = new Date(startMs)

  cursor.setMinutes(0, 0, 0)

  const nextHour = (Math.floor(cursor.getHours() / 3) + 1) * 3

  cursor.setHours(nextHour)

  while (cursor.getTime() < endMs) {
    const midnight = cursor.getHours() === 0

    ticks.push({
      time: cursor.getTime(),

      label: cursor.toLocaleTimeString('en-GB', {
        hour: '2-digit',

        minute: '2-digit'
      }),

      dateLabel: midnight
        ? cursor.toLocaleDateString('en-GB', {
          weekday: 'short',

          day: 'numeric',

          month: 'short'
        })
        : undefined,

      edge: 'middle'
    })

    cursor.setHours(cursor.getHours() + 3)
  }

  const end = new Date(endMs)

  /*
   * Avoid two almost-identical labels at the far right.
   */
  const lastTick = ticks[ticks.length - 1]

  if (!lastTick || endMs - lastTick.time > 60 * 60 * 1000) {
    ticks.push({
      time: endMs,

      label: end.toLocaleTimeString('en-GB', {
        hour: '2-digit',

        minute: '2-digit'
      }),

      dateLabel: end.getHours() === 0
        ? end.toLocaleDateString('en-GB', {
          weekday: 'short',

          day: 'numeric',

          month: 'short'
        })
        : undefined,

      edge: 'end'
    })
  } else {
    lastTick.edge = 'end'
  }

  return ticks
}

export default function PlanVisual({ plan }: PlanVisualProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const { minor: currencyMinor } = resolveCurrencySymbols(plan.currency_symbols)

  const [availableWidth, setAvailableWidth] = useState(0)

  /*
   * Keep the timeline at least as wide as the available
   * card space.
   *
   * Longer plans can still exceed this width and scroll
   * horizontally.
   */
  useEffect(() => {
    const element = scrollRef.current

    if (!element) {
      return
    }

    function updateWidth() {
      setAvailableWidth(element?.clientWidth ?? 0)
    }

    updateWidth()

    const observer = new ResizeObserver(updateWidth)

    observer.observe(element)

    return () => {
      observer.disconnect()
    }
  }, [])

  const [hoveredEventId, setHoveredEventId] = useState<string | null>(null)

  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)

  const [socHover, setSocHover] = useState<SocHover | null>(null)

  const rows = plan.rows

  if (rows.length === 0) {
    return null
  }

  const slotDuration = inferSlotDuration(rows)

  const timelineStart = new Date(rows[0].time).getTime()

  const timelineEnd = new Date(rows[rows.length - 1].time).getTime() + slotDuration

  const timelineDuration = Math.max(1, timelineEnd - timelineStart)

  /*
   * Use actual current battery SOC as the first point
   * where possible rather than the end-of-first-slot SOC.
   */
  const initialSoc =
    plan.soc_max > 0 ? clamp((plan.soc / plan.soc_max) * 100, 0, 100) : rows[0].soc_percent

  const events = buildTimelineEvents(plan, slotDuration, initialSoc)

  const durationHours = timelineDuration / (60 * 60 * 1000)

  /*
   * Natural width is based on the duration of the plan.
   *
   * If the card is wider than that, expand the timeline to
   * fill the card instead of leaving unused space.
   */
  const naturalTimelineWidth = Math.min(2800, Math.max(960, durationHours * 26))

  const timelineWidth = Math.max(naturalTimelineWidth, availableWidth)

  const plotWidth = timelineWidth - PLOT_LEFT - PLOT_RIGHT

  function xForTime(time: number) {
    return PLOT_LEFT + ((time - timelineStart) / timelineDuration) * plotWidth
  }

  const now = Date.now()

  const currentEvent =
    events.find((event) => now >= event.start.getTime() && now < event.end.getTime()) ?? null

  const hoveredEvent = hoveredEventId
    ? (events.find((event) => event.id === hoveredEventId) ?? null)
    : null

  const selectedEvent = selectedEventId
    ? (events.find((event) => event.id === selectedEventId) ?? null)
    : null

  /*
   * Desktop:
   * hover/focus temporarily takes priority.
   *
   * Mobile:
   * tapping an event selects it.
   *
   * With no interaction, details for the current event are
   * shown by default.
   */
  const activeEvent = hoveredEvent ?? selectedEvent ?? currentEvent ?? events[0] ?? null

  const activeTargetSoc = activeEvent
    ? formatTargetSoc(activeEvent.targetSocStart, activeEvent.targetSocEnd)
    : null

  const ticks = buildTimeTicks(timelineStart, timelineEnd)

  /*
   * SOC data is plotted at the END of each plan slot.
   *
   * The first point is the battery's current SOC at the
   * beginning of the timeline.
   */
  const socPoints: SocPoint[] = [
    {
      time: timelineStart,

      soc: initialSoc
    },

    ...rows.map((row, index) => {
      const time = index < rows.length - 1 ? new Date(rows[index + 1].time).getTime() : timelineEnd

      return {
        time,

        soc: row.soc_percent
      }
    })
  ]

  const SOC_HEIGHT = 132

  const SOC_TOP = 18

  const SOC_BOTTOM = 103

  function yForSoc(soc: number) {
    return SOC_BOTTOM - (clamp(soc, 0, 100) / 100) * (SOC_BOTTOM - SOC_TOP)
  }

  function updateSocHover(event: ReactPointerEvent<SVGSVGElement>) {
    const bounds = event.currentTarget.getBoundingClientRect()
    const x = clamp(((event.clientX - bounds.left) / bounds.width) * timelineWidth, PLOT_LEFT, timelineWidth - PLOT_RIGHT)
    const time = timelineStart + ((x - PLOT_LEFT) / plotWidth) * timelineDuration

    setSocHover({ x, time, soc: interpolateSoc(socPoints, time) })
  }

  const socPath = socPoints
    .map((point, index) => {
      const x = xForTime(point.time)

      const y = yForSoc(point.soc)

      return `${index === 0 ? 'M' : 'L'} ${x} ${y}`
    })
    .join(' ')

  /*
   * SOC labels are only shown at action boundaries rather
   * than for every individual plan slot.
   */
  const boundaries: SocBoundary[] = [
    {
      time: timelineStart,

      soc: initialSoc
    },

    ...events.map((event) => ({
      time: event.end.getTime(),

      soc: event.endSoc
    }))
  ]

  /*
   * Suppress labels that would land almost on top of the
   * previous one. The final value is always retained.
   */
  const labelledBoundaries: SocBoundary[] = []

  let previousLabelX = Number.NEGATIVE_INFINITY

  boundaries.forEach((boundary, index) => {
    const x = xForTime(boundary.time)

    const isFirst = index === 0

    const isLast = index === boundaries.length - 1

    if (isFirst || isLast || x - previousLabelX >= 48) {
      labelledBoundaries.push(boundary)

      previousLabelX = x
    }
  })

  const showNow = now >= timelineStart && now <= timelineEnd

  const nowX = showNow ? xForTime(now) : null

  return (
    <section className="plan-timeline">
      <div className="plan-timeline-header">
        <div>
          <h3>Plan Timeline</h3>

          <p>Predbat's planned battery strategy</p>
        </div>

        <div className="plan-timeline-key">
          <span className="timeline-key-charge">Charge</span>

          <span className="timeline-key-export">Export</span>

          <span className="timeline-key-demand">Demand</span>
        </div>
      </div>

      {activeEvent && (
        <div className={`plan-timeline-detail timeline-action-${activeEvent.action}`}>
          <div className="plan-timeline-detail-heading">
            <span className="plan-timeline-detail-icon">
              {getActionIcon(activeEvent.action)}
            </span>

            <div>
              <strong>{getEventTitle(activeEvent)}</strong>

              <span>{formatEventRange(activeEvent.start, activeEvent.end)}</span>
            </div>
          </div>

          <div className="plan-timeline-detail-values">
            {activeTargetSoc && (
              <div>
                <span>Target SOC</span>
                <strong>{activeTargetSoc}</strong>
              </div>
            )}

            {activeEvent.startSoc !== null && activeEvent.endSoc !== null && (
              <div>
                <span>Predicted SOC</span>
                <strong>
                  {Math.round(activeEvent.startSoc)}%{' → '}
                  {Math.round(activeEvent.endSoc)}%
                </strong>
              </div>
            )}

            {activeEvent.target !== null && (
              <div>
                <span>Target</span>

                <strong>{activeEvent.target}%</strong>
              </div>
            )}

            <div>
              <span>Import</span>

              <strong>
                {formatRateRange(activeEvent.importRateMin, activeEvent.importRateMax, currencyMinor)}
              </strong>
            </div>

            <div>
              <span>Export</span>

              <strong>
                {formatRateRange(activeEvent.exportRateMin, activeEvent.exportRateMax, currencyMinor)}
              </strong>
            </div>

            <div>
              <span>Duration</span>

              <strong>{formatDuration(activeEvent.start, activeEvent.end)}</strong>
            </div>

            {plan.carbon_enable && activeEvent.carbonIntensityMin !== null && (
              <div>
                <span>CO₂ intensity</span>

                <strong>
                  {activeEvent.carbonIntensityMin === activeEvent.carbonIntensityMax
                    ? activeEvent.carbonIntensityMin.toFixed(0)
                    : `${activeEvent.carbonIntensityMin.toFixed(0)}–${activeEvent.carbonIntensityMax?.toFixed(0)}`}{' '}
                  g/kWh
                </strong>
              </div>
            )}

            {plan.carbon_enable && (
              <div>
                <span>Net CO₂</span>

                <strong>
                  {activeEvent.carbonChange > 0 ? '+' : ''}
                  {(activeEvent.carbonChange / 1000).toFixed(2)} kg
                </strong>
              </div>
            )}
          </div>

          {activeEvent.explanation && (
            <p className="plan-timeline-explanation">{activeEvent.explanation}</p>
          )}
        </div>
      )}

      <div ref={scrollRef} className="plan-timeline-scroll">
        <div
          className="plan-timeline-canvas"
          style={{
            width: `${timelineWidth}px`
          }}
        >
          {/*
           * Shared time scale.
           */}
          <div className="plan-timeline-axis" aria-hidden="true">
            {ticks.map((tick, index) => {
              const x = xForTime(tick.time)

              return (
                <div
                  key={`${tick.time}-${index}`}
                  className={`plan-timeline-tick is-${tick.edge}`}
                  style={{
                    left: `${x}px`
                  }}
                >
                  {tick.dateLabel && <span className="plan-timeline-tick-date">{tick.dateLabel}</span>}
                  <span className="plan-timeline-tick-time">{tick.label}</span>
                </div>
              )
            })}
          </div>

          {/*
           * Action ribbon.
           */}
          <div className="plan-timeline-track">
            {events.map((event) => {
              const startX = xForTime(event.start.getTime())

              const endX = xForTime(event.end.getTime())

              const eventWidth = Math.max(2, endX - startX)

              const blockLabel = getTimelineBlockLabel(event.action, eventWidth)

              const showRate = eventWidth >= 110

              const rate = getRelevantRate(event, currencyMinor)

              const isCurrent = currentEvent?.id === event.id

              const isActive = activeEvent?.id === event.id

              return (
                <button
                  key={event.id}
                  type="button"
                  className={`plan-timeline-event timeline-action-${event.action} ${isCurrent ? 'is-current' : ''} ${isActive ? 'is-active' : ''}`}
                  style={{
                    left: `${startX}px`,

                    width: `${eventWidth}px`
                  }}
                  aria-label={
                    `${getEventTitle(event)}, ` + `${formatEventRange(event.start, event.end)}`
                  }
                  aria-pressed={selectedEventId === event.id}
                  onMouseEnter={() => {
                    setHoveredEventId(event.id)
                  }}
                  onMouseLeave={() => {
                    setHoveredEventId(null)
                  }}
                  onFocus={() => {
                    setHoveredEventId(event.id)
                  }}
                  onBlur={() => {
                    setHoveredEventId(null)
                  }}
                  onClick={() => {
                    setSelectedEventId((current) => (current === event.id ? null : event.id))
                  }}
                >
                  <span className="plan-timeline-event-main">
                    {getActionIcon(event.action)}

                    {blockLabel && <span>{blockLabel}</span>}
                  </span>

                  {showRate && rate && <span className="plan-timeline-event-rate">{rate}</span>}
                </button>
              )
            })}
          </div>

          <div className="plan-timeline-soc-title">Predicted SOC</div>

          {/*
           * SOC graph shares exactly the same X scale
           * as the action ribbon above it.
           */}
          <svg
            className="plan-timeline-soc"
            width={timelineWidth}
            height={SOC_HEIGHT}
            viewBox={`0 0 ${timelineWidth} ${SOC_HEIGHT}`}
            role="img"
            aria-label="Predicted battery state of charge"
            onPointerMove={updateSocHover}
            onPointerLeave={() => setSocHover(null)}
          >
            <defs>
              {/*
               * SOC colour follows the vertical battery level:
               *
               * 0–20%   = low
               * 20–50%  = medium
               * 50–100% = high
               *
               * gradientUnits="userSpaceOnUse" is important because
               * it makes the colours correspond to the actual 0–100%
               * SOC scale rather than the bounding box of the path.
               */}
              <linearGradient
                id="plan-soc-gradient"
                gradientUnits="userSpaceOnUse"
                x1="0"
                x2="0"
                y1={SOC_BOTTOM}
                y2={SOC_TOP}
              >
                <stop offset="0%" stopColor="var(--color-battery-low)" />

                <stop offset="20%" stopColor="var(--color-battery-low)" />

                <stop offset="20%" stopColor="var(--color-battery-medium)" />

                <stop offset="50%" stopColor="var(--color-battery-medium)" />

                <stop offset="50%" stopColor="var(--color-battery-high)" />

                <stop offset="100%" stopColor="var(--color-battery-high)" />
              </linearGradient>
            </defs>

            {/*
             * Active event range.
             */}
            {activeEvent && (
              <rect
                x={xForTime(activeEvent.start.getTime())}
                y={SOC_TOP - 7}
                width={xForTime(activeEvent.end.getTime()) - xForTime(activeEvent.start.getTime())}
                height={SOC_BOTTOM - SOC_TOP + 14}
                className="plan-timeline-active-range"
              />
            )}

            {/*
             * SOC reference lines.
             */}
            {[0, 50, 100].map((level) => {
              const y = yForSoc(level)

              return (
                <g key={`soc-${level}`}>
                  <line
                    x1={PLOT_LEFT}
                    x2={timelineWidth - PLOT_RIGHT}
                    y1={y}
                    y2={y}
                    className="plan-timeline-soc-grid"
                  />

                  <text
                    x={PLOT_LEFT - 7}
                    y={y + 3}
                    textAnchor="end"
                    className="plan-timeline-soc-scale"
                  >
                    {level}%
                  </text>
                </g>
              )
            })}

            {/*
             * Vertical three-hour guide lines.
             */}
            {ticks.map((tick, index) => {
              const x = xForTime(tick.time)

              return (
                <line
                  key={`soc-tick-${tick.time}-${index}`}
                  x1={x}
                  x2={x}
                  y1={SOC_TOP}
                  y2={SOC_BOTTOM}
                  className="plan-timeline-time-grid"
                />
              )
            })}

            {/*
             * Target SOC lines appear only during
             * windows which actually have a target.
             */}
            {events.map((event) => {
              if (event.target === null) {
                return null
              }

              return (
                <line
                  key={`target-${event.id}`}
                  x1={xForTime(event.start.getTime())}
                  x2={xForTime(event.end.getTime())}
                  y1={yForSoc(event.target)}
                  y2={yForSoc(event.target)}
                  className={`plan-timeline-target timeline-target-${event.action}`}
                />
              )
            })}

            <path
              d={socPath}
              className="plan-timeline-soc-line"
              style={{
                stroke: 'url(#plan-soc-gradient)'
              }}
            />

            {/*
             * SOC boundary points.
             */}
            {boundaries.map((boundary, index) => {
              return (
                <circle
                  key={`boundary-${index}`}
                  cx={xForTime(boundary.time)}
                  cy={yForSoc(boundary.soc)}
                  r="3"
                  className="plan-timeline-soc-point"
                  style={{
                    stroke: getSocColour(boundary.soc)
                  }}
                />
              )
            })}

            {/*
             * Numeric SOC labels are restricted to
             * meaningful action boundaries.
             */}
            {labelledBoundaries.map((boundary, index) => {
              const x = xForTime(boundary.time)

              const y = yForSoc(boundary.soc)

              const first = index === 0

              const last = index === labelledBoundaries.length - 1

              return (
                <text
                  key={`soc-label-${index}`}
                  x={x}
                  y={boundary.soc > 88 ? y + 14 : y - 7}
                  textAnchor={first ? 'start' : last ? 'end' : 'middle'}
                  style={{
                    fill: getSocColour(boundary.soc)
                  }}
                  className="plan-timeline-soc-value"
                >
                  {Math.round(boundary.soc)}%
                </text>
              )
            })}

            {socHover && (
              <g className="plan-timeline-soc-hover" pointerEvents="none">
                <line x1={socHover.x} x2={socHover.x} y1={SOC_TOP} y2={SOC_BOTTOM} />
                <circle cx={socHover.x} cy={yForSoc(socHover.soc)} r="4" />
                <g transform={`translate(${clamp(socHover.x - 57, PLOT_LEFT, timelineWidth - PLOT_RIGHT - 114)} ${yForSoc(socHover.soc) < 48 ? yForSoc(socHover.soc) + 10 : yForSoc(socHover.soc) - 30})`}>
                  <rect width="114" height="22" rx="4" />
                  <text x="57" y="14" textAnchor="middle">
                    {new Date(socHover.time).toLocaleString('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit' })} · {socHover.soc.toFixed(1)}%
                  </text>
                </g>
              </g>
            )}

            <rect
              x={PLOT_LEFT}
              y={SOC_TOP - 8}
              width={plotWidth}
              height={SOC_BOTTOM - SOC_TOP + 16}
              className="plan-timeline-soc-hover-area"
            />
          </svg>

          {/*
           * One Now marker runs through both the action
           * ribbon and SOC graph.
           */}
          {nowX !== null && (
            <div
              className="plan-timeline-now"
              style={{
                left: `${nowX}px`
              }}
              aria-hidden="true"
            >
              <span>NOW</span>
            </div>
          )}
        </div>
      </div>

      <p className="plan-timeline-hint">
        Hover or focus an action for details. On touch devices, tap an action to keep it selected.
      </p>
    </section>
  )
}
