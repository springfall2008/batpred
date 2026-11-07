export type CurrencySymbols = string | readonly string[] | null | undefined

export type ResolvedCurrencySymbols = {
  major: string
  minor: string
}

/** Normalise Predbat's configured major/minor symbols from YAML or legacy string data. */
export function resolveCurrencySymbols(symbols: CurrencySymbols): ResolvedCurrencySymbols {
  const values = Array.isArray(symbols)
    ? symbols
    : typeof symbols === 'string'
      ? Array.from(symbols)
      : []

  return {
    major: values[0] || '£',
    minor: values[1] || 'p'
  }
}

/** Format a value already expressed in the configured major currency unit. */
export function formatMajorCurrency(value: number, symbol: string): string {
  const sign = value < 0 ? '-' : ''
  return `${sign}${symbol}${Math.abs(value).toFixed(2)}`
}

/** Format an electricity rate using Predbat's configured minor currency unit. */
export function formatRate(value: number, minorSymbol: string): string {
  return `${value.toFixed(2)}${minorSymbol}/kWh`
}
