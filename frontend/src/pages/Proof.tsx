import { useCallback, useEffect, useState } from 'react'
import { ApiError, getProof } from '../api'
import { stamp } from '../format'
import type { DecisionProof, RunResult } from '../types'

export function Proof({ run, onOpen }: { run: RunResult | null; onOpen: () => void }) {
  const [proof, setProof] = useState<DecisionProof | null>(null)
  const [error, setError] = useState('')
  const [generation, setGeneration] = useState(0)
  const runId = run?.run_id ?? ''

  const reload = useCallback(() => {
    setProof(null)
    setError('')
    setGeneration((current) => current + 1)
  }, [])

  useEffect(() => {
    if (!runId) return
    let cancel = false
    getProof(runId)
      .then((next) => {
        if (!cancel) setProof(next)
      })
      .catch((err) => {
        if (cancel) return
        setProof(null)
        setError(err instanceof ApiError ? err.message : 'The proof did not load.')
      })
    return () => {
      cancel = true
    }
  }, [runId, generation])

  const busy = Boolean(runId) && proof === null && error === ''
  const passed = proof ? proof.checks.filter((check) => check.passed).length : 0

  return (
    <>
      <header className="page-head">
        <div>
          <p className="kicker">Proof</p>
          <h1>Does this answer follow from its inputs?</h1>
          <p className="lede">
            Filings, the Treasury yield, the baseline oil price and trusted quotes are read again at the
            run’s as-of time. The quarterly formulas are applied a second time. A language model may label
            a market or write the brief. The formulas read the quotes and the filing figures.
          </p>
        </div>
        {run && (
          <button className="primary" type="button" onClick={reload} disabled={busy}>
            {busy ? 'Checking' : 'Check again'}
          </button>
        )}
      </header>

      {!run && (
        <article className="card empty">
          <p>Run a scenario first. The proof checks that answer.</p>
          <button className="primary" type="button" onClick={onOpen}>Go to the question</button>
        </article>
      )}

      {error && <p className="banner">{error}</p>}

      {proof && (
        <>
          <p className={proof.verdict === 'proved' ? 'banner ok' : 'banner'}>
            <strong className="verdict">{proof.verdict === 'proved' ? 'Proved' : 'Not proved'}</strong>
            {' '}
            {proof.verdict === 'proved'
              ? 'The stored answer is the one the formulas produce from these inputs.'
              : 'At least one check failed, so this answer does not follow from the inputs visible now.'}
            {' '}
            {proof.company} · {stamp(proof.as_of)}
          </p>
          <article className="card">
            <p>{proof.statement}</p>
            <div className="fact-grid">
              <div>
                <span>Checks passed</span>
                <strong>{passed} / {proof.checks.length}</strong>
              </div>
              <div>
                <span>Language model wrote a decision number</span>
                <strong>{proof.decision_numbers_from_llm ? 'Yes' : 'No'}</strong>
              </div>
              <div>
                <span>Written brief</span>
                <strong>{proof.llm_status}</strong>
              </div>
            </div>
          </article>
          <article className="card">
            <header className="card-head">
              <h2>Checks</h2>
              <p>Each line is one property the stored answer has to satisfy.</p>
            </header>
            <div className="checks">
              {proof.checks.map((check) => (
                <div className="check-row" key={check.code}>
                  <span className={check.passed ? 'pass' : 'fail'}>{check.passed ? 'Pass' : 'Fail'}</span>
                  <div>
                    <strong>{check.title}</strong>
                    <p className="muted">{check.detail}</p>
                  </div>
                </div>
              ))}
            </div>
          </article>
          <article className="card">
            <header className="card-head">
              <h2>Inputs</h2>
              <p>What was visible at the as-of time, and which of it enters the formulas.</p>
            </header>
            {proof.inputs.length === 0 ? (
              <p>No inputs could be read for this as-of time.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Kind</th>
                    <th>Name</th>
                    <th>Value</th>
                    <th>Source</th>
                    <th>Visible</th>
                    <th>In the formulas</th>
                  </tr>
                </thead>
                <tbody>
                  {proof.inputs.map((trace) => (
                    <tr key={`${trace.kind}-${trace.name}-${trace.source}`}>
                      <td>{trace.kind}</td>
                      <td>{trace.name}</td>
                      <td>{trace.value}</td>
                      <td>
                        {trace.source.startsWith('http')
                          ? <a href={trace.source}>{trace.source}</a>
                          : trace.source}
                      </td>
                      <td>{trace.as_of_visible ? 'yes' : 'no'}</td>
                      <td>{trace.used_in_decision ? 'yes' : 'no'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </article>
        </>
      )}
    </>
  )
}
