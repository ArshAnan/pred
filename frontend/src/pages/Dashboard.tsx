import { useEffect, useState, type FormEvent } from 'react'
import { ApiError, createRun, getCompanies, getFacts, getMarkets, getRun, getRuns } from '../api'
import { asOfForQuote, quoteCluster } from '../decision'
import { pct, stamp } from '../format'
import { ResultView } from '../Result'
import type { Company, Fact, RunBody, RunResult, RunSummary } from '../types'
import { FILED_METRICS, UNSCORED, formatFact, unscoredTicker } from '../unscored'

const QUOTE_WINDOW_MS = 300_000

type Opening = { result: RunResult; field: string; note: string }
let opening: Promise<Opening> | null = null

function openOnLastQuotes(): Promise<Opening> {
  if (!opening) {
    opening = (async () => {
      const markets = await getMarkets('fuel')
        const latest = quoteCluster(markets.map((market) => market.latest?.ts))
      let asOfIso: string | null = null
      let field = ''
      let note = ''
      if (latest && Date.now() - Date.parse(latest) > QUOTE_WINDOW_MS) {
        const timed = asOfForQuote(latest)
        asOfIso = timed.iso
        field = timed.field
        note = `The last WTI quotes are ${stamp(latest)}. A run at the clock would drop them, so this answer uses ${stamp(timed.iso)}.`
      }
      return { result: await createRun({ ...EMPTY, as_of: asOfIso }), field, note }
    })().catch((err) => {
      opening = null
      throw err
    })
  }
  return opening
}

const EMPTY: RunBody = {
  ticker: 'DAL',
  as_of: null,
  note_size_usd_m: 1000,
  tenor_years: 7,
  coupon_spread_bp: 150,
  coverage_threshold: 6,
  fuel_passthrough: 1,
  include_peers: true,
}

export function Dashboard({
  run,
  onRun,
}: {
  run: RunResult | null
  onRun: (run: RunResult) => void
}) {
  const [companies, setCompanies] = useState<Company[]>([])
  const [recent, setRecent] = useState<RunSummary[]>([])
  const [form, setForm] = useState<RunBody>(EMPTY)
  const [asOf, setAsOf] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [facts, setFacts] = useState<Fact[]>([])
  const [quoteNote, setQuoteNote] = useState('')
  const gap = unscoredTicker(form.ticker)

  useEffect(() => {
    if (run) return
    let cancel = false
    setBusy(true)
    openOnLastQuotes()
      .then((opened) => {
        if (cancel) return
        if (opened.field) setAsOf(opened.field)
        if (opened.note) setQuoteNote(opened.note)
        onRun(opened.result)
      })
      .catch((err) => {
        if (!cancel) setError(err instanceof ApiError ? err.message : 'The opening run did not start.')
      })
      .finally(() => {
        if (!cancel) setBusy(false)
      })
    return () => {
      cancel = true
    }
  }, [onRun, run])

  useEffect(() => {
    getCompanies().then(setCompanies).catch(() => setCompanies([]))
    getRuns().then(setRecent).catch(() => setRecent([]))
  }, [run?.run_id])

  useEffect(() => {
    if (!gap) {
      setFacts([])
      return
    }
    const asOfValue = asOf ? `${asOf}:00Z` : null
    getFacts(gap, asOfValue).then(setFacts).catch(() => setFacts([]))
  }, [gap, asOf])

  useEffect(() => {
    if (!run || run.brief.llm_status !== 'pending') return
    let stopped = false
    const timer = window.setInterval(async () => {
      try {
        const next = await getRun(run.run_id)
        if (!stopped) onRun(next)
        if (next.brief.llm_status !== 'pending') window.clearInterval(timer)
      } catch {
        window.clearInterval(timer)
      }
    }, 2000)
    const stop = window.setTimeout(() => window.clearInterval(timer), 60000)
    return () => {
      stopped = true
      window.clearInterval(timer)
      window.clearTimeout(stop)
    }
  }, [run, onRun])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const ticker = gap ? 'DAL' : form.ticker
    const body: RunBody = { ...form, ticker, as_of: asOf ? `${asOf}:00Z` : null }
    try {
      if (gap) setForm({ ...form, ticker: 'DAL' })
      onRun(await createRun(body))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The run did not start.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <p className="kicker">Debt service</p>
          <h1>Can the note be serviced?</h1>
          <p className="lede">
            Kalshi’s WTI ladder becomes three oil states. Each one is run through the company’s fuel burn, interest, and the new note. You get the chance coverage falls below your line, and the oil price where it would.
          </p>
        </div>
      </header>

      <form className="card form" onSubmit={submit}>
        <label>
          Company
          <select value={form.ticker} onChange={(event) => setForm({ ...form, ticker: event.target.value })}>
            {(companies.length ? companies : [{ ticker: 'DAL', name: 'Delta Air Lines', peers: [] }]).map((company) => (
              <option key={company.ticker} value={company.ticker}>{company.ticker} · {company.name}</option>
            ))}
          </select>
        </label>
        <label>
          Note, USD millions
          <input type="number" min={1} value={form.note_size_usd_m} onChange={(event) => setForm({ ...form, note_size_usd_m: Number(event.target.value) })} />
        </label>
        <label>
          Tenor, years
          <input type="number" min={1} max={40} value={form.tenor_years} onChange={(event) => setForm({ ...form, tenor_years: Number(event.target.value) })} />
        </label>
        <label>
          Spread, bp
          <input type="number" min={0} value={form.coupon_spread_bp} onChange={(event) => setForm({ ...form, coupon_spread_bp: Number(event.target.value) })} />
        </label>
        <label>
          Coverage threshold
          <input type="number" min={0.1} step={0.1} value={form.coverage_threshold} onChange={(event) => setForm({ ...form, coverage_threshold: Number(event.target.value) })} />
        </label>
        <label>
          Fuel pass-through
          <input type="number" min={0} max={1} step={0.1} value={form.fuel_passthrough} onChange={(event) => setForm({ ...form, fuel_passthrough: Number(event.target.value) })} />
        </label>
        <label>
          As of, UTC
          <input type="datetime-local" value={asOf} onChange={(event) => setAsOf(event.target.value)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={form.include_peers} onChange={(event) => setForm({ ...form, include_peers: event.target.checked })} />
          Include peers
        </label>
        <button className="primary" type="submit" disabled={busy}>{busy ? 'Running' : gap ? 'Score Delta instead' : 'Run scenario'}</button>
      </form>
      {gap && (
        <article className="card notice">
          <header className="card-head">
            <h2>{UNSCORED[gap].name} is not scored</h2>
            <p>The rate shock is the variable share of debt times total debt. Without that share, a coverage number would be a guess.</p>
          </header>
          <p>{UNSCORED[gap].reason} Delta is scored because its June 30, 2026 release states 22% variable-rate debt.</p>
          <div className="fact-grid">
            {FILED_METRICS.map(({ metric, label }) => {
              const fact = facts.find((item) => item.metric === metric)
              if (!fact) return null
              return (
                <div key={metric}>
                  <span>{label}</span>
                  <strong>{formatFact(fact.unit, fact.value)}</strong>
                  <small>Filed, {fact.period_end}</small>
                </div>
              )
            })}
          </div>
        </article>
      )}
      {error && <p className="banner">{error}</p>}
      {quoteNote && <p className="form-note">{quoteNote}</p>}
      <p className="muted form-note">A blank time uses the clock. Quotes older than five minutes stay out, which is why an idle market day opens on the last quote instead.</p>

      {run && !gap && <ResultView run={run} />}

      <article className="card">
        <header className="card-head">
          <h2>Recent runs</h2>
          <p>Newest first. Open one to see its brief and grid.</p>
        </header>
        {recent.length === 0 && <p className="muted">No saved runs yet.</p>}
        <ul className="run-list">
          {recent.map((item) => (
            <li key={item.run_id}>
              <button type="button" onClick={() => getRun(item.run_id).then(onRun).catch((err) => setError(err.message))}>
                <strong>{item.company}</strong>
                <span>{stamp(item.as_of)}</span>
                <span>{pct(item.p_breach)} breach</span>
                <span className="pill">{item.llm_status}</span>
              </button>
            </li>
          ))}
        </ul>
      </article>
    </>
  )
}
