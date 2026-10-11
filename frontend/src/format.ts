export function pct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

export function times(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value.toFixed(2)}x`
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toFixed(digits)
}

export function stamp(iso: string): string {
  return iso.replace('.000000', '').replace('+00:00', 'Z')
}

export const FLAG_TEXT: Record<string, string> = {
  STALE_MARKETS: 'A quote feeding the run was older than five minutes and was left out.',
  FUEL_NO_MARKET_INPUT: 'No trusted WTI ladder, so the breach probability is not available.',
  RATES_NO_MARKET_INPUT: 'No trusted Fed market, so rates stay at hold.',
  LADDER_INCONSISTENT: 'A touch market disagrees with its close-above twin. The numbers are unchanged.',
  VENUE_DISAGREE: 'Kalshi and Polymarket disagree on the Fed decision.',
  PERIOD_MISMATCH: 'A peer reports a different quarter from the company in the question.',
  VARIABLE_SHARE_STALE: 'The variable-rate share is from an earlier period than total debt.',
  FACT_CONFLICT: 'Two sources disagree. The filing figure is the one used.',
  REF_WTI_OVERRIDE: 'Baseline WTI comes from configuration, not FRED.',
  BACKFILL_INPUTS: 'This replay uses backfilled quotes, so trust is capped.',
  BACKFILL_GAP: 'Some markets had no history for this time and were left out.',
  MARGIN_BASIS_MISMATCH: 'Margins mix GAAP and adjusted revenue.',
  CRUDE_ONLY: 'Fuel cost moves with WTI. The jet crack spread is not modeled.',
  RESOLVE_DATE_PROXY: 'The WTI contract’s resolve date stands in for the quarter.',
}
