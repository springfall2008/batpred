import type { Plan } from '../types/plan'
import { formatRate, resolveCurrencySymbols } from '../utils/currency'
import './PlanSummary.css'

import {
  FontAwesomeIcon,
  FontAwesomeLayers
} from '@fortawesome/react-fontawesome'

import {
  faArrowTrendUp,
  faArrowTrendDown,
  faArrowRight,
  faCircleInfo,
  faBolt,
  faSnowflake,
  faHouse,
  faPause,
  faCar,
  faWater,
  faBan
} from '@fortawesome/free-solid-svg-icons'

/**
 * Props supplied to the PlanSummary component.
 *
 * The whole Plan object is passed in rather than individual values because
 * the summary uses both row-level forecast data and plan-level values such
 * as the actual battery energy and maximum capacity.
 */
type PlanSummaryProps = {
  plan: Plan
  showPlanLink?: boolean
}

/**
 * Normalised action types used by the React UI.
 *
 * Predbat uses short/raw state names such as "Chrg" and "HoldChrg".
 * Converting those to our own semantic action types means the rest of the
 * UI does not need to know about Predbat's internal abbreviations.
 */
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

/**
 * Convert a raw Predbat plan state into a semantic UI action type.
 *
 * These labels are also interpreted by PlanTable and PlanVisual; keep their
 * mappings aligned when adding a new backend state.
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

/**
 * Human-readable label for each semantic action type.
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

/**
 * Convert a raw Predbat state into a readable label.
 *
 * Known states use our semantic action mapping. Unknown states are returned
 * unchanged so that a newly-added Predbat state is still visible rather
 * than being hidden behind "Unknown".
 */
function getStateLabel(state: string) {
  const action = getActionType(state)

  return action === 'unknown' ? state : getActionLabel(action)
}

/**
 * Return the CSS class associated with an action.
 *
 * The component CSS contains no hard-coded action colours; these classes
 * reference semantic colour variables from theme.css instead.
 */
function getActionClass(action?: ActionType) {
  switch (action) {
    case 'charge':
      return 'action-charge'

    case 'hold-charge':
      return 'action-hold-charge'

    case 'freeze-charge':
      return 'action-freeze-charge'

    case 'export':
      return 'action-export'

    case 'hold-export':
      return 'action-hold-export'

    case 'freeze-export':
      return 'action-freeze-export'

    case 'hold-car':
      return 'action-hold-car'

    case 'hold-iboost':
      return 'action-hold-iboost'

    case 'no-charge':
      return 'action-no-charge'

    case 'demand':
      return 'action-demand'

    default:
      return ''
  }
}

/**
 * Replace placeholders in Predbat's reason templates.
 *
 * Example:
 *
 *     "Charge to {target_percentage}% at {import_rate}p"
 *
 * becomes:
 *
 *     "Charge to 69% at 8.00c"
 *
 * Unknown placeholders are deliberately left intact. That makes missing
 * data obvious rather than silently removing useful diagnostic information.
 */
function formatReason(template: string, params: Record<string, unknown>) {
  return template.replace(/\{(\w+)\}/g, (_match, key) => {
    const value = params[key]

    return value !== undefined ? String(value) : `{${key}}`
  })
}

/**
 * Summary card shown above the detailed Predbat plan.
 *
 * The card answers four immediate questions:
 *
 * 1. How full is the battery right now?
 * 2. What is Predbat doing now?
 * 3. What is the current import rate and when does it change?
 * 4. What will Predbat do next?
 */
function PlanSummary({ plan, showPlanLink = true }: PlanSummaryProps) {
  const rows = plan.rows
  const reasonTemplates = plan.reason_templates
  const { minor: currencyMinor } = resolveCurrencySymbols(plan.currency_symbols)

  // A plan with no rows cannot provide any useful summary information.
  if (rows.length === 0) {
    return <p>No plan data available</p>
  }

  /**
   * -------------------------------------------------------------
   * CURRENT PLAN ROW
   * -------------------------------------------------------------
   *
   * Predbat includes some rows from before the current time, so rows[0]
   * must not be assumed to represent "now".
   *
   * Find the latest plan row whose timestamp is not in the future.
   */
  const now = new Date()

  const currentIndex = rows.findLastIndex((row) => new Date(row.time) <= now)

  // Fall back to the first row if the plan unexpectedly begins in the future.
  const current = currentIndex >= 0 ? rows[currentIndex] : rows[0]

  /**
   * -------------------------------------------------------------
   * BATTERY
   * -------------------------------------------------------------
   *
   * plan.soc is the actual current stored energy.
   * current.soc_percent is the modelled/planned SoC for a plan row, so it
   * should not be used as the live battery percentage.
   */
  const batteryPercent = plan.soc_max === 0 ? 0 : (plan.soc / plan.soc_max) * 100

  // Predicted battery movement during the current plan slot.
  const socDirection =
    current.soc_change > 0 ? 'rising' : current.soc_change < 0 ? 'falling' : 'steady'

  const socDirectionIcon =
    socDirection === 'rising'
      ? faArrowTrendUp
      : socDirection === 'falling'
        ? faArrowTrendDown
        : faArrowRight

  // Colour the battery bar according to its current SoC.
  const batteryLevelClass =
    batteryPercent <= 20 ? 'battery-low' : batteryPercent <= 50 ? 'battery-medium' : 'battery-high'

  /**
   * -------------------------------------------------------------
   * CURRENT STATE / EXPLANATION
   * -------------------------------------------------------------
   *
   * Each Predbat row can provide a reason code plus parameters.
   * The reason code selects a human-readable template supplied by Predbat.
   */
  const currentReason = current.reasons[0]

  // Add row-derived values to the parameters supplied by Predbat.
  const currentReasonParams = {
    ...currentReason?.params,
    target_percentage: current.state_target,
    import_rate: current.import_rate.toFixed(2),
    export_rate: current.export_rate.toFixed(2),
    currency_unit: currencyMinor
  }

  // Full reason is shown in the information tooltip.
  const currentExplanation =
    currentReason && reasonTemplates[currentReason.code]
      ? formatReason(reasonTemplates[currentReason.code], currentReasonParams)
      : undefined

  /**
   * -------------------------------------------------------------
   * NEXT IMPORT RATE CHANGE
   * -------------------------------------------------------------
   *
   * Search forward from the current plan row for the first slot whose
   * import rate differs from the current rate.
   */
  const nextRateChange = rows
    .slice(currentIndex + 1)
    .find((row) => row.import_rate !== current.import_rate)

  let nextRate = current.import_rate
  let nextRateTime = ''
  let nextRateDirection: 'rising' | 'falling' | 'steady' = 'steady'

  if (nextRateChange) {
    nextRate = nextRateChange.import_rate

    nextRateDirection = nextRate < current.import_rate ? 'falling' : 'rising'

    const time = new Date(nextRateChange.time)

    nextRateTime = time.toLocaleTimeString('en-GB', {
      hour: '2-digit',
      minute: '2-digit'
    })
  }

  // Falling import prices are positive; rising prices are negative.
  const nextRateClass =
    nextRateDirection === 'falling' ? 'rate-good' : nextRateDirection === 'rising' ? 'rate-bad' : ''

  const nextRateIcon =
    nextRateDirection === 'rising'
      ? faArrowTrendUp
      : nextRateDirection === 'falling'
        ? faArrowTrendDown
        : faArrowRight

  /*
   * Current and Next Action
   *
   * Keep Predbat's operational states distinct here.
   *
   * In particular:
   *
   * Charge -> Hold Charge -> Charge
   *
   * must be treated as three separate phases. Combining
   * Hold Charge into Charge makes a period of flat SOC look
   * as though charging has failed.
   */
  const currentActionType = getActionType(current.state)

  /*
   * Battery animation is only shown while energy is
   * actually being charged into or exported from the
   * battery.
   */
  const isCharging = currentActionType === 'charge'

  const isExporting = currentActionType === 'export'

  /*
   * Find the first future row whose actual action differs
   * from what Predbat is doing now.
   *
   * Hold Charge and Charge deliberately count as different
   * actions here.
   */
  const nextActionOffset = rows
    .slice(currentIndex + 1)
    .findIndex((row) => getActionType(row.state) !== currentActionType)

  const nextActionRowIndex = nextActionOffset >= 0 ? currentIndex + 1 + nextActionOffset : -1

  const nextActionRow = nextActionRowIndex >= 0 ? rows[nextActionRowIndex] : undefined

  const nextActionType = nextActionRow ? getActionType(nextActionRow.state) : undefined

  const nextActionLabel = nextActionType ? getActionLabel(nextActionType) : 'No scheduled action'

  let nextActionStart = ''
  let nextActionEnd = ''

  let nextActionLimit: number | undefined

  if (nextActionRow && nextActionType) {
    const startTime = new Date(nextActionRow.time)

    nextActionStart = startTime.toLocaleTimeString('en-GB', {
      hour: '2-digit',

      minute: '2-digit'
    })

    /*
     * Collect only consecutive rows belonging to this
     * exact action.
     *
     * Charge stops when Hold Charge starts, and vice
     * versa.
     */
    const remainingRows = rows.slice(nextActionRowIndex)

    const endOffset = remainingRows.findIndex((row) => getActionType(row.state) !== nextActionType)

    const actionRows = endOffset >= 0 ? remainingRows.slice(0, endOffset) : remainingRows

    /*
     * Use the target at the end of this particular
     * operational phase.
     */
    const finalActionRow = actionRows[actionRows.length - 1]

    const finalTarget = finalActionRow?.state_target?.trim()

    if (finalTarget) {
      const parsedTarget = Number(finalTarget)

      nextActionLimit = Number.isNaN(parsedTarget) ? undefined : parsedTarget
    }

    /*
     * The first row belonging to a different action marks
     * the end of this phase.
     */
    const actionEndRow = endOffset >= 0 ? remainingRows[endOffset] : undefined

    if (actionEndRow) {
      const endTime = new Date(actionEndRow.time)

      nextActionEnd = endTime.toLocaleTimeString('en-GB', {
        hour: '2-digit',

        minute: '2-digit'
      })
    }
  }

  return (
    <section className="plan-summary">
      <div className="plan-summary-header">
        <h2>Plan Summary</h2>

        {showPlanLink && (
          <a href="./plan" className="plan-summary-full-link">
            View full plan
            <FontAwesomeIcon icon={faArrowRight} />
          </a>
        )}
      </div>

      <div className="plan-summary-context">
        <span className="plan-summary-context-now">Now</span>

        <span className="plan-summary-context-next">Next</span>
      </div>

      <div className="plan-summary-metrics">
        {/* --------------------------------------------------------
                    BATTERY SOC
                    Live battery level plus active charge/export indicator.
                   -------------------------------------------------------- */}
        <div className="plan-summary-metric">
          <span className="metric-label">Battery SOC</span>

          <strong className={`metric-value battery-value ${getActionClass(currentActionType)}`}>
            {isCharging && <FontAwesomeIcon icon={faBolt} className="battery-charging-icon" />}

            {isExporting && (
              <FontAwesomeLayers>
                <FontAwesomeIcon icon={faBolt} />
                <FontAwesomeIcon
                  icon={faArrowTrendUp}
                  transform="shrink-8 right-6 up-6"
                />
              </FontAwesomeLayers>
            )}

            <span>{batteryPercent.toFixed(0)}%</span>
          </strong>

          <div className="battery-bar">
            <div
              className={`battery-bar-fill ${batteryLevelClass} ${isCharging ? 'is-charging' : ''} ${isExporting ? 'is-exporting' : ''}`}
              style={{
                width: `${batteryPercent}%`
              }}
            />
          </div>

          <span className="metric-detail">
            {plan.soc.toFixed(1)}
            {' / '}
            {plan.soc_max.toFixed(1)} kWh
          </span>
        </div>

        {/* --------------------------------------------------------
                    NOW
                    Current Predbat state and explanation.
                   -------------------------------------------------------- */}
        <div className="plan-summary-metric">
          <span className="metric-label">Action</span>

          <strong className={`metric-value action-value ${getActionClass(currentActionType)}`}>
            {getActionIcon(currentActionType)}

            <span>{getStateLabel(current.state)}</span>
          </strong>

          <span className="state-reason">
            <FontAwesomeIcon icon={socDirectionIcon} />

            <span>SOC {socDirection}</span>

            {currentExplanation && (
              <span className="reason-tooltip">
                <FontAwesomeIcon icon={faCircleInfo} />

                <span className="reason-tooltip-content">{currentExplanation}</span>
              </span>
            )}
          </span>
        </div>

        {/* --------------------------------------------------------
                    IMPORT RATE
                    Current import price and next price movement.
                   -------------------------------------------------------- */}
        <div className="plan-summary-metric">
          <span className="metric-label">Import Rate</span>

          <strong className="metric-value">{formatRate(current.import_rate, currencyMinor)}</strong>

          {nextRateChange && (
            <span className={`metric-detail ${nextRateClass}`}>
              <FontAwesomeIcon icon={nextRateIcon} />

              <span>
                {formatRate(nextRate, currencyMinor)} at {nextRateTime}
              </span>
            </span>
          )}
        </div>

        {/* --------------------------------------------------------
                    NEXT ACTION
                    Next action family, its window and final planned limit.
                   -------------------------------------------------------- */}
        <div className="plan-summary-metric">
          <span className="metric-label plan-summary-next-label-desktop">Action</span>

          <span className="metric-label plan-summary-next-label-compact">Next action</span>

          <strong className={`metric-value action-value ${getActionClass(nextActionType)}`}>
            {nextActionType && getActionIcon(nextActionType)}

            <span>{nextActionLabel}</span>
          </strong>

          {nextActionRow && (
            <span className="metric-detail">
              {nextActionStart}

              {nextActionEnd && ` → ${nextActionEnd}`}
            </span>
          )}

          {nextActionLimit !== undefined && (
            <span className="metric-detail">Limit {nextActionLimit}%</span>
          )}
        </div>
      </div>
    </section>
  )
}

export default PlanSummary
