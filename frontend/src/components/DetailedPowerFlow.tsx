import { useLayoutEffect, useRef, useState } from 'react'

import type { PowerFlowData } from '../types/powerFlow'

import dayNoCar from '../assets/house_day_no_car.png'
import dayWithCar from '../assets/house_day_car.png'
import nightNoCar from '../assets/house_night_no_car.png'
import nightWithCar from '../assets/house_night_car.png'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'

import {
  faSolarPanel,
  faHouse,
  faBatteryEmpty,
  faBatteryQuarter,
  faBatteryHalf,
  faBatteryThreeQuarters,
  faBatteryFull,
  faCar
} from '@fortawesome/free-solid-svg-icons'

import GridIcon from './GridIcon'

import './DetailedPowerFlow.css'

/*
 * Development controls simulate grid charging or battery export without changing
 * the supplied inverter data. The view starts in live mode; controls are hidden
 * in production builds.
 */
type TestMode = 'live' | 'charge' | 'export'

/*
 * Vite exposes import.meta.env.DEV as true only when running
 * the development server.
 *
 * The debug controls therefore disappear completely from
 * production builds.
 */
const SHOW_DEBUG_CONTROLS = import.meta.env.DEV

/*
 * ============================================================
 * COMPONENT TYPES
 * ============================================================
 */

type DetailedPowerFlowProps = {
  data: PowerFlowData
  showCar: boolean
}

/*
 * Simple x/y coordinate used after measuring an anchor inside
 * the Detailed Power Flow container.
 */
type Point = {
  x: number
  y: number
}

/*
 * SVG path strings generated from the measured DOM anchors.
 *
 * The EV path is optional because Predbat installations do not
 * necessarily have an EV configured.
 */
type DetailedPaths = {
  solar?: string
  grid?: string
  battery?: string
  home?: string
  car?: string
}

/*
 * Properties required by the reusable animated flow component.
 */
type FlowPathProps = {
  path: string

  active: boolean

  direction: 'forward' | 'reverse'

  className: string
  power: number
}

/*
 * ============================================================
 * DEVELOPMENT DATA OVERRIDE
 * ============================================================
 */

/**
 * Return the data that should actually be rendered by the
 * Detailed Power Flow view.
 *
 * In normal operation this simply returns the original Predbat
 * data object.
 *
 * In a development test mode it returns a NEW object containing
 * the real data plus a small set of overridden values.
 *
 * The original `data` object is never mutated.
 *
 * This is useful because the rest of the component can always
 * work with `displayData` without knowing whether the values
 * came from Predbat or from the development test harness.
 */
function getDisplayData(data: PowerFlowData, mode: TestMode): PowerFlowData {
  /*
   * --------------------------------------------------------
   * CHARGE TEST
   * --------------------------------------------------------
   *
   * Simulated power balance:
   *
   * Grid import        3.9 kW
   *
   *                  ┌── 0.9 kW -> Home
   * Grid -> Inverter ┤
   *                  └── 3.0 kW -> Battery
   *
   * Solar is deliberately disabled so the charge state is
   * visually unambiguous.
   */
  if (mode === 'charge') {
    return {
      ...data,

      pv_power: 0,
      pv_generating: false,

      battery_power: 3000,
      battery_charging: true,
      battery_discharging: false,

      grid_power: 3900,
      grid_importing: true,

      house_power: 900,

      /*
       * A mid-range SOC makes the test state look
       * reasonably realistic.
       */
      soc_percent: 45
    }
  }

  /*
   * --------------------------------------------------------
   * EXPORT TEST
   * --------------------------------------------------------
   *
   * Simulated power balance:
   *
   * Battery            3.0 kW
   *                      |
   *                      v
   *                  Inverter
   *                  /       \
   *             0.9 kW       2.1 kW
   *               |             |
   *               v             v
   *             Home           Grid
   *
   * Again solar is disabled so it is obvious that the export
   * is coming from battery discharge.
   */
  if (mode === 'export') {
    return {
      ...data,

      pv_power: 0,
      pv_generating: false,

      battery_power: 3000,
      battery_charging: false,
      battery_discharging: true,

      grid_power: 2100,
      grid_importing: false,

      house_power: 900,

      soc_percent: 80
    }
  }

  /*
   * --------------------------------------------------------
   * LIVE MODE
   * --------------------------------------------------------
   *
   * No test overrides at all.
   *
   * Returning the original object also avoids creating an
   * unnecessary copy during normal operation.
   */
  return data
}

/*
 * ============================================================
 * DISPLAY HELPERS
 * ============================================================
 */

/**
 * Format power in the same human-readable form used by the
 * Simple Power Flow view.
 *
 * Power is displayed as an absolute value because direction is
 * communicated separately through the status text and animated
 * flow direction.
 *
 * Examples:
 *
 *     560  -> "560 W"
 *     1250 -> "1.25 kW"
 */
function formatPower(power: number) {
  const value = Math.abs(power)

  if (value >= 1000) {
    return `${(value / 1000).toFixed(2)} kW`
  }

  return `${value.toFixed(0)} W`
}

/**
 * Convert power into an animation duration.
 *
 * Higher power means faster moving particles.
 *
 * The speed is deliberately capped:
 *
 * - very low power = approximately 2.8 seconds
 * - >= 3 kW       = approximately 0.8 seconds
 *
 * This prevents very large loads from producing excessively
 * fast or distracting animation.
 */
function getFlowDuration(power: number) {
  const value = Math.abs(power)

  const duration = 2.8 - Math.min(value, 3000) / 1500

  return `${Math.max(0.8, duration).toFixed(2)}s`
}

/**
 * Determine whether the night-time background should be used.
 *
 * Home Assistant's sun.sun entity is preferred because it
 * reflects the actual local sunrise and sunset.
 *
 * Browser time is used only as a fallback if sun_state is not
 * available.
 */
function isNightTime(sunState: string | null) {
  if (sunState === 'below_horizon') {
    return true
  }

  if (sunState === 'above_horizon') {
    return false
  }

  const hour = new Date().getHours()

  return hour < 7 || hour >= 19
}

/**
 * Select one of the four detailed scene backgrounds.
 *
 * There are separate images for:
 *
 * - day without car
 * - day with car
 * - night without car
 * - night with car
 */
function getBackgroundImage(night: boolean, carConfigured: boolean) {
  if (night && carConfigured) {
    return nightWithCar
  }

  if (night) {
    return nightNoCar
  }

  if (carConfigured) {
    return dayWithCar
  }

  return dayNoCar
}

/*
 * ============================================================
 * SVG POSITIONING HELPERS
 * ============================================================
 */

/**
 * Measure the centre of an HTML anchor relative to the
 * DetailedPowerFlow container.
 *
 * Anchor locations themselves are positioned using percentage
 * values in DetailedPowerFlow.css.
 *
 * The SVG paths, however, use actual CSS pixel coordinates.
 *
 * Measuring the DOM means:
 *
 * - the background can resize responsively;
 * - anchor positions remain percentage based;
 * - the SVG paths always terminate at the correct rendered
 *   location;
 * - no fixed SVG viewBox calculations are required.
 */
function getAnchorPoint(element: HTMLElement, container: HTMLElement): Point {
  const rect = element.getBoundingClientRect()

  const containerRect = container.getBoundingClientRect()

  return {
    x: rect.left - containerRect.left + rect.width / 2,

    y: rect.top - containerRect.top + rect.height / 2
  }
}

/**
 * Create an SVG path containing a rounded 90-degree bend.
 *
 * Two routing styles are supported.
 *
 *
 * horizontal-first:
 *
 * from ─────────╮
 *               │
 *               │
 *               to
 *
 *
 * vertical-first:
 *
 * from
 *   │
 *   │
 *   ╰────────── to
 *
 *
 * The radius is automatically reduced if there is insufficient
 * distance between the two points. This prevents malformed
 * paths when the dashboard becomes very small.
 */
function createRoundedPath(
  from: Point,
  to: Point,
  route: 'horizontal-first' | 'vertical-first',
  radius = 18
) {
  const dx = to.x - from.x

  const dy = to.y - from.y

  /*
   * Never allow the corner radius to exceed half of either
   * axis distance.
   */
  const safeRadius = Math.min(radius, Math.abs(dx) / 2, Math.abs(dy) / 2)

  /*
   * --------------------------------------------------------
   * HORIZONTAL-FIRST ROUTE
   * --------------------------------------------------------
   */
  if (route === 'horizontal-first') {
    const cornerX = to.x

    const cornerY = from.y

    const horizontalDirection = Math.sign(dx) || 1

    const verticalDirection = Math.sign(dy) || 1

    return `
            M ${from.x} ${from.y}

            L ${cornerX - safeRadius * horizontalDirection} ${cornerY}

            Q ${cornerX} ${cornerY}
              ${cornerX}
              ${cornerY + safeRadius * verticalDirection}

            L ${to.x} ${to.y}
        `
  }

  /*
   * --------------------------------------------------------
   * VERTICAL-FIRST ROUTE
   * --------------------------------------------------------
   */

  const cornerX = from.x

  const cornerY = to.y

  const verticalDirection = Math.sign(dy) || 1

  const horizontalDirection = Math.sign(dx) || 1

  return `
        M ${from.x} ${from.y}

        L ${cornerX}
          ${cornerY - safeRadius * verticalDirection}

        Q ${cornerX} ${cornerY}
          ${cornerX + safeRadius * horizontalDirection}
          ${cornerY}

        L ${to.x} ${to.y}
    `
}

/*
 * ============================================================
 * ANIMATED FLOW PATH
 * ============================================================
 */

/**
 * Draw one power-flow route.
 *
 * Each route contains:
 *
 * 1. A static underlying cable/path.
 * 2. Three moving particles when the flow is active.
 *
 * Particle speed is determined from the current power.
 *
 * Direction is controlled using SVG animateMotion keyPoints:
 *
 *     forward -> 0 to 1
 *     reverse -> 1 to 0
 *
 * This allows the same SVG path to represent both:
 *
 * - grid import and export;
 * - battery charge and discharge.
 */
function FlowPath({ path, active, direction, className, power }: FlowPathProps) {
  /*
   * Calculate the speed of the particles from the current
   * power level.
   */
  const duration = getFlowDuration(power)

  /*
   * SVG animateMotion normally travels from the beginning
   * of a path to its end.
   *
   * Reversing keyPoints makes the particles travel backwards
   * without needing to regenerate the SVG path.
   */
  const keyPoints = direction === 'forward' ? '0;1' : '1;0'

  return (
    <g className={className}>
      {/*
       * Static route beneath the moving particles.
       *
       * CSS changes its opacity depending on whether the
       * path is active or idle.
       */}
      <path d={path} className={`detailed-flow-base ${active ? 'is-active' : 'is-idle'}`} />

      {/*
       * Moving particles are only rendered while power
       * is actually flowing.
       */}
      {active && (
        <>
          {/*
           * Main particle.
           */}
          <circle
            r="4"
            className="
                            detailed-flow-dot
                            detailed-flow-dot-primary
                        "
          >
            <animateMotion
              dur={duration}
              repeatCount="indefinite"
              path={path}
              keyPoints={keyPoints}
              keyTimes="0;1"
            />
          </circle>

          {/*
           * Secondary particle.
           *
           * Negative begin values stagger the particle
           * positions immediately rather than waiting
           * for the first animation cycle.
           */}
          <circle
            r="3"
            className="
                            detailed-flow-dot
                            detailed-flow-dot-secondary
                        "
          >
            <animateMotion
              dur={duration}
              begin="-0.65s"
              repeatCount="indefinite"
              path={path}
              keyPoints={keyPoints}
              keyTimes="0;1"
            />
          </circle>

          {/*
           * Small trailing particle.
           */}
          <circle
            r="2"
            className="
                            detailed-flow-dot
                            detailed-flow-dot-tertiary
                        "
          >
            <animateMotion
              dur={duration}
              begin="-1.3s"
              repeatCount="indefinite"
              path={path}
              keyPoints={keyPoints}
              keyTimes="0;1"
            />
          </circle>
        </>
      )}
    </g>
  )
}

/*
 * ============================================================
 * DETAILED POWER FLOW COMPONENT
 * ============================================================
 */

function DetailedPowerFlow({ data, showCar }: DetailedPowerFlowProps) {
  /*
   * --------------------------------------------------------
   * CONTAINER
   * --------------------------------------------------------
   *
   * All anchor measurements are calculated relative to this
   * element.
   */
  const containerRef = useRef<HTMLDivElement>(null)

  /*
   * --------------------------------------------------------
   * FLOW ANCHORS
   * --------------------------------------------------------
   *
   * Each physical object in the background artwork has a DOM
   * anchor.
   *
   * CSS determines where those anchors sit over the artwork.
   *
   * Their measured centres become the start/end coordinates
   * of the SVG paths.
   */

  const solarAnchorRef = useRef<HTMLDivElement>(null)

  const gridAnchorRef = useRef<HTMLDivElement>(null)

  const inverterAnchorRef = useRef<HTMLDivElement>(null)

  const batteryAnchorRef = useRef<HTMLDivElement>(null)

  const homeAnchorRef = useRef<HTMLDivElement>(null)

  const carAnchorRef = useRef<HTMLDivElement>(null)

  /*
   * Generated SVG path strings.
   *
   * They are rebuilt whenever the scene changes size.
   */
  const [paths, setPaths] = useState<DetailedPaths>({})

  /*
   * ============================================================
   * DEVELOPMENT DEBUG STATE
   * ============================================================
   *
   * These controls only affect this Detailed Power Flow view.
   *
   * They do not write anything back to Predbat or Home Assistant.
   *
   * Every fresh page load starts in:
   *
   *     live data
   *     real EV state
   */

  const [testMode, setTestMode] = useState<TestMode>('live')

  const [testCar, setTestCar] = useState(false)

  /*
   * --------------------------------------------------------
   * DISPLAY DATA
   * --------------------------------------------------------
   *
   * EVERYTHING in the visualisation should use displayData
   * rather than the original `data` prop.
   *
   * In live mode:
   *
   *     displayData === data
   *
   * In a test mode:
   *
   *     displayData contains the simulated values.
   *
   * Keeping this decision in one place prevents test logic
   * becoming scattered throughout the JSX.
   */
  const displayData = getDisplayData(data, testMode)

  /*
   * The EV can be forced on independently of the main test mode.
   *
   * This lets us test combinations such as:
   *
   *     charge + EV
   *     export + EV
   *     live + forced EV
   */
  const car = testCar
    ? {
      configured: true,
      power: 3200,
      inside_clamp: true,
      charging: true
    }
    : displayData.car

  /*
   * Planning a car and monitoring its live charger power are separate
   * settings. Keep a planned car visible even when no live power sensor is
   * available; the label below makes the missing live reading explicit.
   */
  const carVisible = testCar || showCar

  /*
   * Determine which artwork should be displayed.
   */
  const night = isNightTime(displayData.sun_state)

  const backgroundImage = getBackgroundImage(night, carVisible)

  /*
   * --------------------------------------------------------
   * DERIVED FLOW STATES
   * --------------------------------------------------------
   */

  /*
   * Battery power only flows when Predbat reports either
   * charging or discharging.
   */
  const batteryActive = displayData.battery_charging || displayData.battery_discharging

  /*
   * Ignore tiny grid readings.
   *
   * Inverters commonly report a few watts of noise around
   * zero, which should not result in an animated grid flow.
   */
  const gridActive = Math.abs(displayData.grid_power) >= 10

  /*
   * Battery label state.
   *
   * This is mainly used for CSS styling.
   *
   * Charging gets the Predbat charge action colour.
   * Discharging and idle remain neutral.
   */
  const batteryStateClass = displayData.battery_charging
    ? 'is-charging'
    : displayData.battery_discharging
      ? 'is-discharging'
      : 'is-idle'

  /*
   * Grid label state.
   *
   * Export receives the Predbat export action colour.
   * Import and idle remain neutral.
   */
  const gridStateClass = !gridActive
    ? 'is-idle'
    : displayData.grid_importing
      ? 'is-importing'
      : 'is-exporting'

  /*
   * ========================================================
   * SVG PATH MEASUREMENT
   * ========================================================
   *
   * Measure the HTML anchors and rebuild all SVG routes
   * whenever the detailed scene changes size.
   *
   * ResizeObserver handles:
   *
   * - browser resizing;
   * - responsive layout changes;
   * - container width changes caused by other dashboard UI.
   */
  useLayoutEffect(() => {
    const container = containerRef.current

    if (!container) {
      return
    }

    /**
     * Measure every anchor and construct the SVG paths.
     */
    function updatePaths() {
      /*
       * Re-read refs every time rather than relying on
       * values captured when the effect first ran.
       */
      const container = containerRef.current

      const solar = solarAnchorRef.current

      const grid = gridAnchorRef.current

      const inverter = inverterAnchorRef.current

      const battery = batteryAnchorRef.current

      const home = homeAnchorRef.current

      /*
       * Solar, grid, inverter, battery and home are
       * required for every Detailed Power Flow scene.
       */
      if (!container || !solar || !grid || !inverter || !battery || !home) {
        return
      }

      /*
       * Convert DOM anchor positions into coordinates
       * relative to the scene container.
       */
      const solarPoint = getAnchorPoint(solar, container)

      const gridPoint = getAnchorPoint(grid, container)

      const inverterPoint = getAnchorPoint(inverter, container)

      const batteryPoint = getAnchorPoint(battery, container)

      const homePoint = getAnchorPoint(home, container)

      /*
       * Construct the four paths that always exist.
       */
      const newPaths: DetailedPaths = {
        /*
         * ------------------------------------------------
         * SOLAR -> INVERTER
         * ------------------------------------------------
         *
         * The path starts by moving vertically down from
         * the panels, then bends towards the inverter.
         *
         * Solar only has one meaningful direction.
         */
        solar: createRoundedPath(solarPoint, inverterPoint, 'vertical-first'),

        /*
         * ------------------------------------------------
         * GRID <-> INVERTER
         * ------------------------------------------------
         *
         * One route represents both:
         *
         *     Grid -> inverter   (import)
         *     Inverter -> grid   (export)
         *
         * The animation direction is reversed later
         * depending on grid_importing.
         */
        grid: createRoundedPath(gridPoint, inverterPoint, 'horizontal-first'),

        /*
         * ------------------------------------------------
         * BATTERY <-> INVERTER
         * ------------------------------------------------
         *
         * One route represents both:
         *
         *     Battery -> inverter  (discharge)
         *     Inverter -> battery  (charge)
         */
        battery: createRoundedPath(batteryPoint, inverterPoint, 'vertical-first'),

        /*
         * ------------------------------------------------
         * INVERTER -> HOME
         * ------------------------------------------------
         *
         * House consumption always travels away from
         * the inverter towards the house.
         */
        home: createRoundedPath(inverterPoint, homePoint, 'horizontal-first')
      }

      /*
       * ----------------------------------------------------
       * OPTIONAL EV ROUTE
       * ----------------------------------------------------
       *
       * The EV anchor only exists when a car is configured.
       *
       * While TEST_CAR is true this path will always be
       * created.
       */
      if (carVisible && carAnchorRef.current) {
        const carPoint = getAnchorPoint(carAnchorRef.current, container)

        newPaths.car = createRoundedPath(inverterPoint, carPoint, 'horizontal-first')
      }

      /*
       * Replace all paths as one state update.
       */
      setPaths(newPaths)
    }

    /*
     * Initial measurement.
     */
    updatePaths()

    /*
     * Measure once more on the next animation frame.
     *
     * This catches layout changes that complete after the
     * first synchronous React layout effect.
     */
    const frame = window.requestAnimationFrame(updatePaths)

    /*
     * Continue rebuilding paths whenever the outer scene
     * changes size.
     */
    const resizeObserver = new ResizeObserver(updatePaths)

    resizeObserver.observe(container)

    /*
     * Remove browser resources when the component unmounts
     * or when the selected artwork changes.
     */
    return () => {
      window.cancelAnimationFrame(frame)

      resizeObserver.disconnect()
    }
  }, [carVisible, backgroundImage])

  /*
   * Human-readable states for the detailed Power Flow cards.
   */

  const gridStatus = !gridActive ? 'Idle' : displayData.grid_importing ? 'Importing' : 'Exporting'

  const solarStatus = displayData.pv_generating ? 'Generating' : 'Idle'

  const batteryStatus = displayData.battery_charging
    ? 'Charging'
    : displayData.battery_discharging
      ? 'Discharging'
      : 'Idle'

  const homeStatus = displayData.house_power >= 10 ? 'Load' : 'Idle'

  const carStatus = !testCar && !car.configured
    ? 'Power unavailable'
    : car.charging && car.power >= 10
      ? 'Charging'
      : 'Idle'

  function getBatteryIcon(socPercent: number) {
    if (socPercent >= 88) {
      return faBatteryFull
    }

    if (socPercent >= 63) {
      return faBatteryThreeQuarters
    }

    if (socPercent >= 38) {
      return faBatteryHalf
    }

    if (socPercent >= 13) {
      return faBatteryQuarter
    }

    return faBatteryEmpty
  }

  const batteryIcon = getBatteryIcon(displayData.soc_percent)

  /*
   * ========================================================
   * RENDER
   * ========================================================
   */

  return (
    <div className="detailed-power-flow" ref={containerRef}>
      {/*
       * ============================================================
       * DEVELOPMENT CONTROLS
       * ============================================================
       *
       * import.meta.env.DEV ensures this UI is not included during
       * normal production use.
       *
       * These buttons only change the data rendered by this component.
       * Nothing is sent to Predbat, the inverter or Home Assistant.
       */}
      {SHOW_DEBUG_CONTROLS && (
        <div
          className="detailed-flow-debug"
          role="group"
          aria-label="Power flow development controls"
        >
          <span className="detailed-flow-debug-title">Debug</span>

          <div className="detailed-flow-debug-modes">
            <button
              type="button"
              className={`detailed-flow-debug-button ${testMode === 'live' ? 'is-active' : ''}`}
              aria-pressed={testMode === 'live'}
              onClick={() => setTestMode('live')}
            >
              Live
            </button>

            <button
              type="button"
              className={`detailed-flow-debug-button detailed-flow-debug-charge ${testMode === 'charge' ? 'is-active' : ''}`}
              aria-pressed={testMode === 'charge'}
              onClick={() => setTestMode('charge')}
            >
              Charge
            </button>

            <button
              type="button"
              className={`detailed-flow-debug-button detailed-flow-debug-export ${testMode === 'export' ? 'is-active' : ''}`}
              aria-pressed={testMode === 'export'}
              onClick={() => setTestMode('export')}
            >
              Export
            </button>
          </div>

          <button
            type="button"
            className={`detailed-flow-debug-button detailed-flow-debug-car ${testCar ? 'is-active' : ''}`}
            aria-pressed={testCar}
            onClick={() => setTestCar((current) => !current)}
          >
            EV
          </button>
        </div>
      )}

      {/*
       * ----------------------------------------------------
       * BACKGROUND ARTWORK
       * ----------------------------------------------------
       */}
      <img
        src={backgroundImage}
        alt=""
        className="
                    detailed-power-flow-background
                "
      />

      {/*
       * ----------------------------------------------------
       * POSITIONING ANCHORS
       * ----------------------------------------------------
       *
       * Grid, solar, battery, home and car anchors are
       * normally invisible.
       *
       * The inverter anchor is different: it also acts as
       * the visible inverter hub.
       *
       * Its centre is therefore both:
       *
       * - the visual junction shown to the user;
       * - the exact point all SVG routes connect to.
       *
       * Anchor locations are controlled entirely by
       * DetailedPowerFlow.css.
       */}

      {/* Grid anchor */}
      <div
        ref={gridAnchorRef}
        className="
                    detailed-anchor
                    detailed-anchor-grid
                "
      />

      {/* Solar anchor */}
      <div
        ref={solarAnchorRef}
        className="
                    detailed-anchor
                    detailed-anchor-solar
                "
      />

      {/*
       * Inverter anchor / visible hub.
       *
       * The pulse animation belongs to this same element,
       * so there is no risk of a separate visual hub being
       * slightly misaligned with the actual SVG endpoint.
       */}
      <div
        ref={inverterAnchorRef}
        className="
                    detailed-anchor
                    detailed-anchor-inverter
                    detailed-inverter-hub
                "
        role="img"
        aria-label="Inverter"
      >
        <span
          className="
                        detailed-inverter-hub-core
                    "
        />
      </div>

      {/* Battery anchor */}
      <div
        ref={batteryAnchorRef}
        className="
                    detailed-anchor
                    detailed-anchor-battery
                "
      />

      {/* Home anchor */}
      <div
        ref={homeAnchorRef}
        className="
                    detailed-anchor
                    detailed-anchor-home
                "
      />

      {/*
       * EV anchor is only rendered when an EV is
       * configured.
       */}
      {carVisible && (
        <div
          ref={carAnchorRef}
          className="
                        detailed-anchor
                        detailed-anchor-car
                    "
        />
      )}

      {/*
       * ----------------------------------------------------
       * LIVE FLOW SVG
       * ----------------------------------------------------
       *
       * The SVG sits over the background artwork.
       *
       * Path coordinates are real rendered CSS pixels,
       * calculated from the anchors above.
       */}
      <svg
        className="
                    detailed-power-flow-lines
                "
        aria-hidden="true"
      >
        {/*
         * SOLAR -> INVERTER
         *
         * Solar flow always travels forwards along the
         * generated path.
         */}
        {paths.solar && (
          <FlowPath
            path={paths.solar}
            active={displayData.pv_generating}
            direction="forward"
            className="flow-solar"
            power={displayData.pv_power}
          />
        )}

        {/*
         * GRID <-> INVERTER
         *
         * Path geometry:
         *
         *     Grid -> inverter
         *
         * Import:
         *     forward
         *
         * Export:
         *     reverse
         */}
        {paths.grid && (
          <FlowPath
            path={paths.grid}
            active={gridActive}
            direction={displayData.grid_importing ? 'forward' : 'reverse'}
            className="flow-grid"
            power={displayData.grid_power}
          />
        )}

        {/*
         * BATTERY <-> INVERTER
         *
         * Path geometry:
         *
         *     Battery -> inverter
         *
         * Discharging:
         *     forward
         *
         * Charging:
         *     reverse
         */}
        {paths.battery && (
          <FlowPath
            path={paths.battery}
            active={batteryActive}
            direction={displayData.battery_discharging ? 'forward' : 'reverse'}
            className="flow-battery"
            power={displayData.battery_power}
          />
        )}

        {/*
         * INVERTER -> HOME
         */}
        {paths.home && (
          <FlowPath
            path={paths.home}
            active={displayData.house_power >= 10}
            direction="forward"
            className="flow-home"
            power={displayData.house_power}
          />
        )}

        {/*
         * INVERTER -> EV
         *
         * The path can exist while the car is idle.
         * Particles only move while charging is true.
         */}
        {paths.car && (
          <FlowPath
            path={paths.car}
            active={car.charging}
            direction="forward"
            className="flow-car"
            power={car.power}
          />
        )}
      </svg>

      {/*
       * ----------------------------------------------------
       * LIVE DATA LABELS
       * ----------------------------------------------------
       *
       * Label positions remain controlled by
       * DetailedPowerFlow.css.
       */}

      {/*
       * GRID
       *
       * Only exporting receives an action colour.
       *
       * Importing and idle remain neutral.
       */}
      <div className={`detailed-flow-label detailed-flow-grid ${gridStateClass}`}>
        <div className="detailed-flow-icon-panel">
          <GridIcon className="detailed-flow-icon-large" />
        </div>

        <div className="detailed-flow-content">
          <span className="detailed-flow-name">Grid</span>

          <strong>{formatPower(displayData.grid_power)}</strong>

          <small>{gridStatus}</small>
        </div>
      </div>

      {/*
       * SOLAR
       */}
      <div
        className={`detailed-flow-label detailed-flow-solar ${displayData.pv_generating ? 'is-generating' : ''}`}
      >
        <div className="detailed-flow-icon-panel">
          <FontAwesomeIcon icon={faSolarPanel} className="detailed-flow-icon-large" />
        </div>

        <div className="detailed-flow-content">
          <span className="detailed-flow-name">Solar</span>

          <strong>{formatPower(displayData.pv_power)}</strong>

          <small>{solarStatus}</small>
        </div>
      </div>

      {/*
       * BATTERY
       *
       * Only charging receives an action colour.
       *
       * Discharging and idle deliberately remain neutral
       * because charge/export are the Predbat actions we
       * want to emphasise visually.
       */}
      <div className={`detailed-flow-label detailed-flow-battery ${batteryStateClass}`}>
        <div className="detailed-flow-icon-panel">
          <FontAwesomeIcon icon={batteryIcon} className="detailed-flow-icon-large" />
        </div>

        <div className="detailed-flow-content">
          <span className="detailed-flow-name">Battery</span>

          <strong>{formatPower(displayData.battery_power)}</strong>

          <small>
            {displayData.soc_percent}% · {batteryStatus}
          </small>
        </div>
      </div>

      {/*
       * HOME
       */}
      <div className="detailed-flow-label detailed-flow-home">
        <div className="detailed-flow-icon-panel">
          <FontAwesomeIcon icon={faHouse} className="detailed-flow-icon-large" />
        </div>

        <div className="detailed-flow-content">
          <span className="detailed-flow-name">Home</span>

          <strong>{formatPower(displayData.house_power)}</strong>

          <small>{homeStatus}</small>
        </div>
      </div>

      {/*
       * CAR
       *
       * The entire label disappears when no EV has been
       * configured.
       */}
      {carVisible && (
        <div
          className={`detailed-flow-label detailed-flow-car ${car.charging ? 'is-charging' : ''}`}
        >
          <div className="detailed-flow-icon-panel">
            <FontAwesomeIcon icon={faCar} className="detailed-flow-icon-large" />
          </div>

          <div className="detailed-flow-content">
            <span className="detailed-flow-name">Car</span>

            <strong>{!testCar && !car.configured ? '—' : formatPower(car.power)}</strong>

            <small>{carStatus}</small>
          </div>
        </div>
      )}
    </div>
  )
}

export default DetailedPowerFlow
