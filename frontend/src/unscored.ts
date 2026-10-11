export const UNSCORED = {
  UAL: {
    name: 'United',
    reason:
      'The June 30, 2026 10-Q states $11.2 billion of floating-rate principal subject to increased-cost provisions. It does not state what share of debt is variable, and that share is not derived.',
  },
  AAL: {
    name: 'American',
    reason:
      'The June 30, 2026 10-Q lists variable-rate facilities one by one. It does not state what share of debt is variable, and those facilities are not summed into one.',
  },
} as const

export type UnscoredTicker = keyof typeof UNSCORED

export function unscoredTicker(ticker: string): UnscoredTicker | null {
  return ticker === 'UAL' || ticker === 'AAL' ? ticker : null
}

export const FILED_METRICS: { metric: string; label: string }[] = [
  { metric: 'operating_income', label: 'Operating income' },
  { metric: 'net_interest_expense', label: 'Net interest' },
  { metric: 'fuel_gallons_m', label: 'Fuel burned' },
  { metric: 'debt_total', label: 'Debt' },
  { metric: 'liquidity', label: 'Liquidity' },
]

export function formatFact(unit: string, value: number): string {
  const rounded = Math.round(value).toLocaleString('en-US')
  if (unit === 'usd_m') return `$${rounded}M`
  if (unit === 'fraction') return value.toLocaleString('en-US')
  return `${rounded}M gal`
}
