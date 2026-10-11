import { useState, type FormEvent } from 'react'
import { ApiError, createRun } from '../api'
import { ResultView } from '../Result'
import type { RunResult } from '../types'

export function Replay({ onRun }: { onRun: (run: RunResult) => void }) {
  const [day, setDay] = useState('2026-07-10')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [run, setRun] = useState<RunResult | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const next = await createRun({
        ticker: 'DAL',
        as_of: `${day}T20:00:00Z`,
        note_size_usd_m: 1000,
        tenor_years: 7,
        coupon_spread_bp: 150,
        coverage_threshold: 6,
        fuel_passthrough: 1,
        include_peers: true,
      })
      setRun(next)
      onRun(next)
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'The replay did not run.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <p className="kicker">Replay</p>
          <h1>What was visible on July 10</h1>
          <p className="lede">
            A run at 20:00 UTC uses only data known then. The 10-year yield in that vintage is the July 9 print, 4.54%, so a $1,000M note at a 150 bp spread carries a 6.04% coupon. There is no July 10 Treasury observation.
          </p>
        </div>
      </header>
      <form className="card form" onSubmit={submit}>
        <label>
          As-of date, 20:00 UTC
          <input type="date" value={day} max="2026-10-11" onChange={(event) => setDay(event.target.value)} />
        </label>
        <button className="primary" type="submit" disabled={busy}>{busy ? 'Running' : 'Replay'}</button>
      </form>
      {error && <p className="banner">{error}</p>}
      {run && <ResultView run={run} />}
    </>
  )
}
