import { Chart, type ChartEvent, type Plugin } from 'chart.js'
import zoomPlugin from 'chartjs-plugin-zoom'

const chartZoomHint = 'Scroll or pinch to zoom. Drag to pan. Double-click to reset.'
type ScaleRange = { min?: number; max?: number }

const savedZoomRanges = new WeakMap<HTMLCanvasElement, Record<string, ScaleRange>>()
const resettingCharts = new WeakSet<Chart>()

/** Zoom may produce intermediate ticks outside the original timestamp range. */
export function formatChartTimestamp(value: unknown, options: Intl.DateTimeFormatOptions): string {
  const timestamp = Number(value)
  const isValidDate = Number.isFinite(timestamp) && Math.abs(timestamp) <= 8.64e15
  return isValidDate ? new Intl.DateTimeFormat(undefined, options).format(timestamp) : ''
}

const resetZoomOnDoubleClick: Plugin = {
  id: 'resetZoomOnDoubleClick',
  afterInit(chart) {
    chart.canvas.title = chartZoomHint
  },
  beforeUpdate(chart) {
    const ranges = savedZoomRanges.get(chart.canvas)
    if (!ranges) return

    Object.entries(ranges).forEach(([scaleId, range]) => {
      const options = chart.options.scales?.[scaleId] as ScaleRange | undefined
      if (!options) return
      options.min = range.min
      options.max = range.max
    })
    savedZoomRanges.delete(chart.canvas)
  },
  beforeDestroy(chart) {
    if (!chart.isZoomedOrPanned()) return

    const ranges = Object.fromEntries(
      Object.entries(chart.scales)
        .filter(([, scale]) => scale.isHorizontal())
        .map(([scaleId, scale]) => {
          const options = scale.options as ScaleRange
          return [scaleId, { min: options.min, max: options.max }]
        })
    )
    savedZoomRanges.set(chart.canvas, ranges)
  },
  afterEvent(chart, args) {
    const event = args.event as ChartEvent & { native?: Event }
    if (event.native?.type !== 'dblclick' || resettingCharts.has(chart)) return

    resettingCharts.add(chart)
    chart.resetZoom()
    resettingCharts.delete(chart)
  }
}

Chart.register(zoomPlugin, resetZoomOnDoubleClick)

if (!Chart.defaults.events.includes('dblclick')) Chart.defaults.events.push('dblclick')

Chart.defaults.plugins.zoom = {
  limits: {
    x: {
      min: 'original',
      max: 'original'
    }
  },
  pan: {
    enabled: true,
    mode: 'x',
    threshold: 10
  },
  zoom: {
    wheel: {
      enabled: true,
      speed: 0.1
    },
    drag: {
      enabled: false
    },
    pinch: {
      enabled: true
    },
    mode: 'x'
  }
}
