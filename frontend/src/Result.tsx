import { decisionCopy, holdLadder, ladderIsLive, ratesAreFlat } from './decision'
import { pct, times, num, stamp, FLAG_TEXT } from './format'
import type { RunResult, ScenarioRow } from './types'
import { UNSCORED } from './unscored'

export function ResultView({ run }: { run: RunResult }) {
  const brief = run.brief.llm_status === 'ready' && run.brief.llm_md ? run.brief.llm_md : run.brief.template_md
  const briefNote =
    run.brief.llm_status === 'ready'
      ? 'Written brief, every number checked against this run.'
      : run.brief.llm_status === 'pending'
        ? 'Checking the written brief. The template is shown until it passes.'
        : 'Template brief. A written brief replaces it only after every number checks out.'
  const ladder = holdLadder(run)
  const live = ladderIsLive(ladder)
  const decision = decisionCopy(run)
  const threshold = num(run.inputs.coverage_threshold, 1)

  return (
    <>
      <article className="card decision">
        <p className="kicker">{run.company} · {stamp(run.as_of)}</p>
        <h2>{decision.title}</h2>
        <p>{decision.detail}</p>
        <p className="muted">
          {ratesAreFlat(run)
            ? 'The Fed market had no quote fresh enough to use, so the rate is held flat. Oil is what moves this answer.'
            : 'Hold and a 25 bp hike are both priced from the Fed market.'}
          {' '}Conditional on these odds and the stated assumptions. Not a forecast.
        </p>
      </article>

      {live && (
        <article className="card">
          <header className="card-head">
            <h2>What the oil markets do to the note</h2>
            <p>P10, P50 and P90 are the curve’s presentation states. Operating income falls as WTI rises; the new note is already in net interest.</p>
          </header>
          <table>
            <thead>
              <tr>
                <th>State</th>
                <th>WTI</th>
                <th>Operating income</th>
                <th>Coverage</th>
                <th>Against {threshold}x</th>
              </tr>
            </thead>
            <tbody>
              {ladder.map((row) => (
                <tr key={row.fuel_label}>
                  <td>{row.fuel_label}</td>
                  <td>${num(row.wti)}</td>
                  <td>${num(row.operating_income_usd_m, 0)}M</td>
                  <td>{times(row.coverage)}</td>
                  <td>{row.breach ? 'Below' : 'Clears'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <CoverageBars rows={ladder} />
        </article>
      )}

      <section className="stats">
        <article className="card stat">
          <Ring value={run.summary.p_breach} />
          <div>
            <p className="kicker">Breach probability</p>
            <p className="stat-value">{pct(run.summary.p_breach)}</p>
            <p className="muted">Coverage below {num(run.inputs.coverage_threshold, 1)}x</p>
          </div>
        </article>
        <article className="card stat">
          <p className="kicker">Coverage, hold</p>
          <p className="stat-value">{times(run.summary.coverage_p50)}</p>
          <p className="muted">P50 · P90 {times(run.summary.coverage_p90)}</p>
        </article>
        <article className="card stat">
          <p className="kicker">Breakeven WTI</p>
          <p className="stat-value">{num(run.summary.breakeven_wti_hold)}</p>
          <p className="muted">Hold · hike {num(run.summary.breakeven_wti_hike)}</p>
        </article>
        <article className="card stat">
          <p className="kicker">Liquidity cover</p>
          <p className="stat-value">{times(run.summary.liquidity_cover)}</p>
          <p className="muted">
            {run.summary.liquidity_ok == null
              ? 'Liquidity or current debt is missing'
              : run.summary.liquidity_ok
                ? 'Covers debt due within a year'
                : 'Short of debt due within a year'}
          </p>
        </article>
      </section>

      <article className="card">
        <header className="card-head">
          <h2>Brief</h2>
          <p>{briefNote}</p>
        </header>
        <pre className="brief">{brief}</pre>
      </article>

      <PeerNote run={run} />

      <details className="card flags">
        <summary>Limits recorded on this run</summary>
        <ul className="flag-list">
          {run.flags.length === 0 && <li>No flags.</li>}
          {run.flags.map((flag) => (
            <li key={flag}>
              <span className="pill">{flag}</span>
              <span>{FLAG_TEXT[flag] || flag}</span>
            </li>
          ))}
        </ul>
      </details>
    </>
  )
}

function Ring({ value }: { value: number | null }) {
  const ratio = value == null ? 0 : Math.max(0, Math.min(1, value))
  const radius = 28
  const circ = 2 * Math.PI * radius
  return (
    <svg className="ring" viewBox="0 0 72 72" aria-hidden="true">
      <circle cx="36" cy="36" r={radius} />
      <circle
        cx="36"
        cy="36"
        r={radius}
        className="ring-value"
        strokeDasharray={`${circ * ratio} ${circ}`}
      />
    </svg>
  )
}

function CoverageBars({ rows }: { rows: ScenarioRow[] }) {
  const max = Math.max(1, ...rows.map((row) => row.coverage))
  return (
    <div className="bars">
      {rows.map((row) => (
        <div className="bar-row" key={row.fuel_label}>
          <span>{row.fuel_label} ${num(row.wti, 0)}</span>
          <div className="track">
            <div className={row.breach ? 'fill breach' : 'fill'} style={{ width: `${(row.coverage / max) * 100}%` }} />
          </div>
          <strong>{times(row.coverage)}</strong>
        </div>
      ))}
    </div>
  )
}

function PeerNote({ run }: { run: RunResult }) {
  if (!run.inputs.include_peers) return null
  const omitted = (['UAL', 'AAL'] as const).filter(
    (ticker) => ticker !== run.company && !run.peers.some((peer) => peer.ticker === ticker),
  )
  if (run.peers.length === 0 && omitted.length === 0) return null
  return (
    <article className="card">
      <header className="card-head">
        <h2>Same odds, other books</h2>
        <p>United and American use this fuel curve only when their own filings state a variable-rate share.</p>
      </header>
      {run.peers.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Airline</th>
              <th>Period</th>
              <th>Breach</th>
              <th>P50</th>
              <th>Liquidity</th>
            </tr>
          </thead>
          <tbody>
            {run.peers.map((peer) => (
              <tr key={peer.ticker}>
                <td>{peer.ticker}</td>
                <td>{peer.period_end}</td>
                <td>{pct(peer.summary.p_breach)}</td>
                <td>{times(peer.summary.coverage_p50)}</td>
                <td>{times(peer.summary.liquidity_cover)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {omitted.length > 0 && (
        <p className="muted omitted">
          {omitted.map((ticker) => UNSCORED[ticker].name).join(' and ')} {omitted.length === 1 ? 'is' : 'are'} left out.
          {' '}The rate shock is the variable share of debt times total debt, and their June 30, 2026 filings do not state that share.
        </p>
      )}
    </article>
  )
}

