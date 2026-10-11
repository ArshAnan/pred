import { useEffect, useState } from 'react'
import { ApiError, getAlerts, sendTestAlert } from '../api'
import { stamp } from '../format'
import type { Alert } from '../types'

export function Alerts() {
  const [rows, setRows] = useState<Alert[]>([])
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  function load() {
    getAlerts().then(setRows).catch((err) => setError(err instanceof ApiError ? err.message : 'Alerts are unavailable.'))
  }

  useEffect(load, [])

  async function test() {
    setBusy(true)
    setError('')
    setNote('')
    try {
      await sendTestAlert()
      setNote('Slack accepted the test message.')
      load()
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'The test alert was not sent.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <p className="kicker">Alerts</p>
          <h1>When the answer would flip</h1>
          <p className="lede">Breach probability crossing 20%, a trusted driver moving 10 points in an hour, or a source staying down. Delivery is a Slack message for the demo channel.</p>
        </div>
        <button className="primary" type="button" onClick={test} disabled={busy}>{busy ? 'Sending' : 'Send test alert'}</button>
      </header>
      {error && <p className="banner">{error}</p>}
      {note && <p className="banner ok">{note}</p>}
      <article className="card">
        {rows.length === 0 && <p className="muted">No alerts in the recent window.</p>}
        <ul className="market-list">
          {rows.map((alert) => (
            <li key={alert.alert_id}>
              <div>
                <strong>{alert.company} · {alert.rule}</strong>
                <p className="muted">{stamp(alert.ts)} · {alert.severity} · {alert.status}</p>
              </div>
              <span className={`pill ${alert.status === 'sent' ? 'ok' : ''}`}>{alert.status}</span>
            </li>
          ))}
        </ul>
      </article>
    </>
  )
}
