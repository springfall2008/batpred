import { useLayoutEffect, useRef, useState } from 'react'

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

import type { PowerFlowData } from '../types/powerFlow'

import './SimplePowerFlow.css'

/**
 * Data supplied by the parent PowerFlow component.
 */
type SimplePowerFlowProps = {
  data: PowerFlowData
}

/**
 * A point within the power-flow container.
 *
 * Coordinates are measured in actual CSS pixels, so the SVG overlay
 * follows the rendered layout rather than relying on hard-coded positions.
 */
type Point = {
  x: number
  y: number
}

/**
 * Start/end coordinates for one power-flow connection.
 */
type LineCoordinates = {
  start: Point
  end: Point
}

/**
 * All possible connections in the simple power-flow diagram.
 *
 * Car is optional because the EV node is only rendered when a
 * car charging sensor is configured in Predbat.
 */
type FlowLines = {
  solar?: LineCoordinates
  battery?: LineCoordinates
  grid?: LineCoordinates
  car?: LineCoordinates
}

/**
 * Props for an individual SVG flow line.
 */
type FlowLineProps = {
  x1: number
  y1: number
  x2: number
  y2: number

  active: boolean
  direction: 'forward' | 'reverse'

  className: string
  power: number
}

/**
 * Format watts into a compact value for display.
 *
 * Values below 1kW remain in watts.
 * Larger values are shown in kW.
 */
function formatPower(power: number) {
  const value = Math.abs(power)

  if (value >= 1000) {
    return `${(value / 1000).toFixed(2)} kW`
  }

  return `${value.toFixed(0)} W`
}

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

/**
 * Calculate a line connecting the EDGES of two circular DOM elements.
 *
 * getBoundingClientRect() gives us the real on-screen position of each
 * circle. We convert those positions into coordinates relative to the
 * SimplePowerFlow container.
 *
 * The line starts and ends one radius away from each centre, so it meets
 * the circle edge rather than passing underneath the icons.
 */
function getConnection(
  from: HTMLElement,
  to: HTMLElement,
  container: HTMLElement
): LineCoordinates {
  const fromRect = from.getBoundingClientRect()
  const toRect = to.getBoundingClientRect()
  const containerRect = container.getBoundingClientRect()

  const fromCenter: Point = {
    x: fromRect.left - containerRect.left + fromRect.width / 2,

    y: fromRect.top - containerRect.top + fromRect.height / 2
  }

  const toCenter: Point = {
    x: toRect.left - containerRect.left + toRect.width / 2,

    y: toRect.top - containerRect.top + toRect.height / 2
  }

  const dx = toCenter.x - fromCenter.x
  const dy = toCenter.y - fromCenter.y

  const distance = Math.sqrt(dx * dx + dy * dy)

  // Defensive fallback if two nodes somehow occupy the same point.
  if (distance === 0) {
    return {
      start: fromCenter,
      end: toCenter
    }
  }

  // Unit vector pointing from the first circle to the second.
  const ux = dx / distance
  const uy = dy / distance

  const fromRadius = Math.min(fromRect.width, fromRect.height) / 2

  const toRadius = Math.min(toRect.width, toRect.height) / 2

  return {
    start: {
      x: fromCenter.x + ux * fromRadius,
      y: fromCenter.y + uy * fromRadius
    },

    end: {
      x: toCenter.x - ux * toRadius,
      y: toCenter.y - uy * toRadius
    }
  }
}

function getFlowDuration(power: number) {
  const value = Math.abs(power)

  // Clamp between roughly 0.8s and 2.8s
  const duration = 2.8 - Math.min(value, 3000) / 1500

  return `${Math.max(0.8, duration).toFixed(2)}s`
}

/**
 * Draw one power-flow connection.
 *
 * Direction does not change the physical line coordinates.
 * SVG animateMotion keyPoints reverse particle travel along the same path.
 */
function FlowLine({ x1, y1, x2, y2, active, direction, className, power }: FlowLineProps) {
  const path = `M ${x1} ${y1} L ${x2} ${y2}`
  const duration = getFlowDuration(power)

  return (
    <g className={className}>
      <line
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        className={`simple-flow-base ${active ? 'is-active' : 'is-idle'}`}
      />

      {active && (
        <>
          <circle r="4" className="simple-flow-dot simple-flow-dot-primary">
            <animateMotion
              dur={duration}
              repeatCount="indefinite"
              path={path}
              keyPoints={direction === 'forward' ? '0;1' : '1;0'}
              keyTimes="0;1"
            />
          </circle>

          <circle r="3" className="simple-flow-dot simple-flow-dot-secondary">
            <animateMotion
              dur={duration}
              begin="-0.6s"
              repeatCount="indefinite"
              path={path}
              keyPoints={direction === 'forward' ? '0;1' : '1;0'}
              keyTimes="0;1"
            />
          </circle>

          <circle r="2" className="simple-flow-dot simple-flow-dot-tertiary">
            <animateMotion
              dur={duration}
              begin="-1.2s"
              repeatCount="indefinite"
              path={path}
              keyPoints={direction === 'forward' ? '0;1' : '1;0'}
              keyTimes="0;1"
            />
          </circle>
        </>
      )}
    </g>
  )
}

/**
 * Simple live power-flow view.
 *
 * The node positions are controlled by CSS Grid.
 * An absolutely positioned SVG sits underneath those nodes and its
 * connection coordinates are calculated from the actual DOM layout.
 *
 * This keeps the lines attached to the circles when the card resizes.
 */
function SimplePowerFlow({ data }: SimplePowerFlowProps) {
  /**
   * Reference to the whole diagram.
   *
   * All SVG coordinates are calculated relative to this element.
   */
  const containerRef = useRef<HTMLDivElement>(null)

  /**
   * References to each circular node.
   *
   * We deliberately measure the circle itself rather than the whole
   * node because the surrounding labels have different heights.
   */
  const solarRef = useRef<HTMLDivElement>(null)

  const batteryRef = useRef<HTMLDivElement>(null)

  const homeRef = useRef<HTMLDivElement>(null)

  const gridRef = useRef<HTMLDivElement>(null)

  const carRef = useRef<HTMLDivElement>(null)

  /**
   * Calculated SVG coordinates.
   */
  const [flowLines, setFlowLines] = useState<FlowLines>({})

  const batteryIcon = getBatteryIcon(data.soc_percent)

  /**
   * Determine whether each connection is currently carrying power.
   *
   * Predbat already gives us battery and grid direction information,
   * so the frontend doesn't need to reinterpret their signs.
   */
  const batteryActive = data.battery_charging || data.battery_discharging

  const gridActive = Math.abs(data.grid_power) >= 10

  /**
   * Measure the rendered circles and update every SVG connection.
   *
   * ResizeObserver means this also runs whenever responsive CSS changes
   * the diagram dimensions, not just when the browser window fires a
   * traditional resize event.
   */
  useLayoutEffect(() => {
    const container = containerRef.current

    if (!container) {
      return
    }

    function updateFlowLines() {
      const container = containerRef.current

      const solar = solarRef.current

      const battery = batteryRef.current

      const home = homeRef.current

      const grid = gridRef.current

      if (!container || !solar || !battery || !home || !grid) {
        return
      }

      const lines: FlowLines = {
        // Solar always connects towards Home.
        solar: getConnection(solar, home, container),

        // Battery direction is controlled by charge/discharge state.
        battery: getConnection(battery, home, container),

        // Grid connection is defined Grid -> Home.
        // FlowLine reverses particle travel during export.
        grid: getConnection(grid, home, container)
      }

      /**
       * Car connection only exists when Predbat has a configured
       * car charging power sensor.
       */
      if (data.car.configured && carRef.current) {
        lines.car = getConnection(home, carRef.current, container)
      }

      setFlowLines(lines)
    }

    // Calculate immediately after the DOM layout is available.
    updateFlowLines()

    /**
     * Running once more on the next animation frame catches small layout
     * adjustments such as font rendering that can occur immediately
     * after the first measurement.
     */
    const frame = window.requestAnimationFrame(updateFlowLines)

    const resizeObserver = new ResizeObserver(updateFlowLines)

    resizeObserver.observe(container)

    if (solarRef.current) {
      resizeObserver.observe(solarRef.current)
    }

    if (batteryRef.current) {
      resizeObserver.observe(batteryRef.current)
    }

    if (homeRef.current) {
      resizeObserver.observe(homeRef.current)
    }

    if (gridRef.current) {
      resizeObserver.observe(gridRef.current)
    }

    if (carRef.current) {
      resizeObserver.observe(carRef.current)
    }

    return () => {
      window.cancelAnimationFrame(frame)
      resizeObserver.disconnect()
    }
  }, [data.car.configured])

  return (
    <div className="simple-power-flow" ref={containerRef}>
      {/*
       * SVG CONNECTION LAYER
       *
       * This fills the entire diagram and sits underneath the
       * HTML nodes.
       *
       * No viewBox is used: coordinates correspond directly to
       * CSS pixels inside simple-power-flow.
       */}
      <svg className="simple-power-flow-lines" aria-hidden="true">
        {/* Solar -> Home */}
        {flowLines.solar && (
          <FlowLine
            x1={flowLines.solar.start.x}
            y1={flowLines.solar.start.y}

            x2={flowLines.solar.end.x}
            y2={flowLines.solar.end.y}

            active={data.pv_generating}

            power={data.pv_power}

            direction="forward"

            className="flow-solar"
          />
        )}

        {/* Battery <-> Home */}
        {flowLines.battery && (
          <FlowLine
            x1={flowLines.battery.start.x}
            y1={flowLines.battery.start.y}

            x2={flowLines.battery.end.x}
            y2={flowLines.battery.end.y}

            power={data.battery_power}

            active={batteryActive}

            direction={data.battery_discharging ? 'forward' : 'reverse'}

            className="flow-battery"
          />
        )}

        {/* Grid <-> Home */}
        {flowLines.grid && (
          <FlowLine
            x1={flowLines.grid.start.x}
            y1={flowLines.grid.start.y}

            x2={flowLines.grid.end.x}
            y2={flowLines.grid.end.y}

            power={data.grid_power}

            active={gridActive}

            direction={data.grid_importing ? 'forward' : 'reverse'}

            className="flow-grid"
          />
        )}

        {/* Home -> Car */}
        {flowLines.car && (
          <FlowLine
            x1={flowLines.car.start.x}
            y1={flowLines.car.start.y}

            x2={flowLines.car.end.x}
            y2={flowLines.car.end.y}

            active={data.car.charging}

            power={data.car.power}

            direction="forward"

            className="flow-car"
          />
        )}
      </svg>

      {/*
       * SOLAR NODE
       */}
      <div className="simple-node simple-solar">
        <strong>Solar</strong>

        <span className="simple-node-power">{formatPower(data.pv_power)}</span>

        <div className="simple-node-circle" ref={solarRef}>
          <FontAwesomeIcon icon={faSolarPanel} />
        </div>
      </div>

      {/*
       * BATTERY NODE
       *
       * The icon comes FIRST so its circle lines up horizontally
       * with Home and Grid regardless of how much descriptive text
       * is displayed underneath.
       */}
      <div className="simple-node simple-battery">
        <div className="simple-node-circle" ref={batteryRef}>
          <FontAwesomeIcon icon={batteryIcon} className="detailed-flow-icon-large" />
        </div>

        <strong>Battery</strong>

        <span className="simple-node-power">{formatPower(data.battery_power)}</span>

        <span>{data.soc_percent}% SoC</span>

        <small>
          {data.battery_charging ? 'Charging' : data.battery_discharging ? 'Discharging' : 'Idle'}
        </small>
      </div>

      {/*
       * HOME NODE
       */}
      <div className="simple-node simple-home">
        <div className="simple-node-circle" ref={homeRef}>
          <FontAwesomeIcon icon={faHouse} />
        </div>

        <div className="right-100">
          <strong>Home</strong>
          <span className="simple-node-power">{formatPower(data.house_power)}</span>
        </div>

      </div>

      {/*
       * GRID NODE
       *
       * Uses our custom electricity pylon SVG rather than the
       * Font Awesome broadcast tower.
       */}
      <div className="simple-node simple-grid">
        <div className="simple-node-circle" ref={gridRef}>
          <GridIcon className="detailed-flow-icon" />
        </div>

        <strong>Grid</strong>

        <span className="simple-node-power">{formatPower(data.grid_power)}</span>

        <small>{!gridActive ? 'Idle' : data.grid_importing ? 'Importing' : 'Exporting'}</small>
      </div>

      {/*
       * CAR NODE
       *
       * Hide the entire node when no car charging sensor has been
       * configured, matching Predbat's existing power-flow behaviour.
       */}
      {data.car.configured && (
        <div className={`simple-node simple-car ${data.car.charging ? 'is-charging' : ''}`}>
          <div className="simple-node-circle" ref={carRef}>
            <FontAwesomeIcon icon={faCar} />
          </div>

          <strong>Car</strong>

          <span className="simple-node-power">{formatPower(data.car.power)}</span>

          <small>{data.car.charging ? 'Charging' : 'Idle'}</small>
        </div>
      )}
    </div>
  )
}

export default SimplePowerFlow
