export type AxisBounds = {
  min: number
  max: number
}

const AXIS_PADDING = 0.08

function paddedBounds(values: number[]): AxisBounds {
  const finiteValues = values.filter(Number.isFinite)
  const min = Math.min(0, ...finiteValues)
  const max = Math.max(0, ...finiteValues)

  if (min === 0 && max === 0) {
    return { min: 0, max: 1 }
  }

  const padding = (max - min) * AXIS_PADDING
  return {
    min: min < 0 ? min - padding : 0,
    max: max > 0 ? max + padding : 0
  }
}

function crossesZero(bounds: AxisBounds): boolean {
  return bounds.min < 0 && bounds.max > 0
}

function alignBounds(bounds: AxisBounds, zeroPosition: number): AxisBounds {
  if (zeroPosition === 1) {
    return { min: 0, max: Math.max(bounds.max, 1) }
  }
  if (zeroPosition === 0) {
    return { min: Math.min(bounds.min, -1), max: 0 }
  }

  const span = Math.max(bounds.max / zeroPosition, -bounds.min / (1 - zeroPosition), 1)
  return {
    min: -(1 - zeroPosition) * span,
    max: zeroPosition * span
  }
}

/** Return independent daily and running-total scales with zero at the same height. */
export function alignedSavingsAxisBounds(dailyValues: number[], totalValues: number[]) {
  const daily = paddedBounds(dailyValues)
  const total = paddedBounds(totalValues)
  const reference = crossesZero(daily) ? daily : crossesZero(total) ? total : null
  const zeroPosition = reference
    ? reference.max / (reference.max - reference.min)
    : daily.min === 0 && total.min === 0
      ? 1
      : daily.max === 0 && total.max === 0
        ? 0
        : 0.5

  return {
    daily: alignBounds(daily, zeroPosition),
    total: alignBounds(total, zeroPosition)
  }
}
