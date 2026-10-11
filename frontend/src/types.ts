export type Health = {
  status: string
  db: string
  llm: { mode: string }
  last_snapshot_age_s: number | null
}

export type Company = {
  ticker: string
  name: string
  peers: string[]
}

export type Fact = {
  metric: string
  value: number
  unit: string
  period_end: string
  basis: string
}

export type ScenarioRow = {
  fuel_label: 'P10' | 'P50' | 'P90'
  rate_label: 'hold' | 'hike'
  wti: number
  probability: number | null
  operating_income_usd_m: number
  net_interest_usd_m: number
  coverage: number
  breach: boolean
}

export type ScenarioSummary = {
  p_breach: number | null
  breakeven_wti_hold: number | null
  breakeven_wti_hike: number | null
  coverage_p50: number
  coverage_p90: number
  liquidity_cover: number | null
  liquidity_ok: boolean | null
}

export type PeerResult = {
  ticker: string
  period_end: string
  summary: ScenarioSummary
  flags: string[]
  operating_margin: number | null
}

export type Brief = {
  template_md: string
  llm_md: string | null
  llm_status: 'pending' | 'ready' | 'failed' | 'rejected' | 'skipped'
}

export type RunResult = {
  run_id: string
  company: string
  as_of: string
  created_at: string
  inputs: {
    ticker: string
    note_size_usd_m: number
    tenor_years: number
    coupon_spread_bp: number
    coverage_threshold: number
    fuel_passthrough: number
    include_peers: boolean
  }
  rows: ScenarioRow[]
  summary: ScenarioSummary
  peers: PeerResult[]
  flags: string[]
  brief: Brief
  operating_margin: number | null
}

export type RunSummary = {
  run_id: string
  company: string
  as_of: string
  p_breach: number | null
  flags: string[]
  llm_status: string
}

export type MarketView = {
  meta: {
    venue: string
    market_id: string
    title: string
    close_time: string | null
    threshold: number | null
    threshold_unit: string | null
    outcome: string | null
  }
  label: {
    relevant: boolean
    factor: string | null
    yes_moves_factor: string | null
    mechanism: string
    label_source: 'hand' | 'llm'
  } | null
  trust: { score: number; mid: number | null } | null
  latest: { ts: string } | null
  stale: boolean
}

export type Alert = {
  alert_id: string
  ts: string
  company: string
  rule: string
  severity: string
  status: string
  payload: Record<string, unknown>
}

export type RunBody = {
  ticker: string
  as_of: string | null
  note_size_usd_m: number
  tenor_years: number
  coupon_spread_bp: number
  coverage_threshold: number
  fuel_passthrough: number
  include_peers: boolean
}
