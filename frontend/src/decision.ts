import { num, pct, times } from './format'
import type { RunResult, ScenarioRow } from './types'

const LABELS = ['P10', 'P50', 'P90'] as const

export function holdLadder(run: RunResult): ScenarioRow[] {
  return LABELS.flatMap((label) => {
    const row = run.rows.find((item) => item.fuel_label === label && item.rate_label === 'hold')
    return row ? [row] : []
  })
}

export function ladderIsLive(rows: ScenarioRow[]): boolean {
  return rows.length === 3 && new Set(rows.map((row) => row.wti.toFixed(2))).size > 1
}

export function ratesAreFlat(run: RunResult): boolean {
  return run.flags.includes('RATES_NO_MARKET_INPUT')
}

export function decisionCopy(run: RunResult): { title: string; detail: string } {
  const rows = holdLadder(run)
  const threshold = num(run.inputs.coverage_threshold, 1)
  if (!ladderIsLive(rows)) {
    return {
      title: 'This time has no oil ladder',
      detail:
        'The WTI quotes visible then are missing or older than five minutes, so all three fuel states use one price. Coverage here is the company’s own baseline, not a market scenario.',
    }
  }
  const [low, mid, high] = rows
  const oil = `$${num(low.wti)} / $${num(mid.wti)} / $${num(high.wti)}`
  const cover = `${times(low.coverage)} / ${times(mid.coverage)} / ${times(high.coverage)}`
  const line = `The WTI markets put P10 / P50 / P90 at ${oil}. Coverage on the new note is ${cover}.`
  const be = run.summary.breakeven_wti_hold
  const breach = run.summary.p_breach
  if (breach == null) {
    return { title: 'The oil ladder is in, and the breach probability is not', detail: line }
  }
  const through = be == null
    ? ''
    : ` Coverage falls below ${threshold}x only if WTI settles above $${num(be)}.`
  if (breach === 0) {
    return {
      title: `Coverage stays above ${threshold}x across the market’s oil range`,
      detail: `${line}${through} The survival curve puts ${pct(breach)} of the weight above that price.`,
    }
  }
  return {
    title: `${pct(breach)} of the market-implied oil distribution puts coverage under ${threshold}x`,
    detail: `${line}${through}`,
  }
}

/** Three minutes after a quote, still inside the five-minute window the engine trusts. */
export function asOfForQuote(ts: string): { iso: string; field: string } {
  const when = new Date(Date.parse(ts) + 180_000)
  const iso = when.toISOString().replace(/\.\d{3}Z$/, 'Z')
  return { iso, field: iso.slice(0, 16) }
}

/** The minute when the most fuel markets last printed. A lone later tick is not the ladder. */
export function quoteCluster(times: (string | null | undefined)[]): string | null {
  const buckets = new Map<string, number>()
  for (const ts of times) {
    if (!ts) continue
    const minute = ts.slice(0, 16)
    buckets.set(minute, (buckets.get(minute) ?? 0) + 1)
  }
  let best: string | null = null
  let count = 0
  for (const [minute, n] of buckets) {
    if (n > count || (n === count && (best === null || minute > best))) {
      best = minute
      count = n
    }
  }
  return best ? `${best}:00Z` : null
}
