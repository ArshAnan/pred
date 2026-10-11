import { pct, times } from '../format'
import type { RunResult } from '../types'

export function Peers({ run, onOpen }: { run: RunResult | null; onOpen: () => void }) {
  return (
    <>
      <header className="page-head">
        <div>
          <p className="kicker">Peers</p>
          <h1>Same question, three books</h1>
          <p className="lede">United and American use the same fuel curve and Fed odds as the company you asked about, and their own financials. A peer is omitted when a required fact is missing.</p>
        </div>
      </header>
      {!run && (
        <article className="card empty">
          <p>Run a scenario first. The peer table is part of that run.</p>
          <button className="primary" type="button" onClick={onOpen}>Go to the question</button>
        </article>
      )}
      {run && (
        <article className="card">
          <header className="card-head">
            <h2>{run.company} and peers</h2>
            <p>Operating margin uses adjusted revenue when every company in the comparison has it.</p>
          </header>
          {!run.inputs.include_peers ? (
            <p>Peers were left out of this run.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Airline</th>
                  <th>Period</th>
                  <th>Breach</th>
                  <th>Breakeven</th>
                  <th>P50</th>
                  <th>P90</th>
                  <th>Liquidity</th>
                  <th>Margin</th>
                  <th>Flags</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{run.company}</td>
                  <td>focal</td>
                  <td>{pct(run.summary.p_breach)}</td>
                  <td>{run.summary.breakeven_wti_hold == null ? '—' : run.summary.breakeven_wti_hold.toFixed(2)}</td>
                  <td>{times(run.summary.coverage_p50)}</td>
                  <td>{times(run.summary.coverage_p90)}</td>
                  <td>{times(run.summary.liquidity_cover)}</td>
                  <td>{pct(run.operating_margin)}</td>
                  <td>{run.flags.join(', ') || '—'}</td>
                </tr>
                {run.peers.map((peer) => (
                  <tr key={peer.ticker}>
                    <td>{peer.ticker}</td>
                    <td>{peer.period_end}</td>
                    <td>{pct(peer.summary.p_breach)}</td>
                    <td>{peer.summary.breakeven_wti_hold == null ? '—' : peer.summary.breakeven_wti_hold.toFixed(2)}</td>
                    <td>{times(peer.summary.coverage_p50)}</td>
                    <td>{times(peer.summary.coverage_p90)}</td>
                    <td>{times(peer.summary.liquidity_cover)}</td>
                    <td>{pct(peer.operating_margin)}</td>
                    <td>{peer.flags.join(', ') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {run.inputs.include_peers && run.peers.length === 0 && (
            <p className="muted omitted">
              United and American are left out. The rate shock needs the variable share of debt, and their June 30, 2026 filings do not state it.
            </p>
          )}
        </article>
      )}
    </>
  )
}
